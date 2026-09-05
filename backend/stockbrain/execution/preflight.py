"""The last set of questions asked before a broker order is transmitted.

An authorization is a statement about the moment it was given.  An order is sent
into a later one, and everything the authorization relied on can have moved:
the market, the account, the position, the listing, the operator's intent.  So
every check runs again here, and a refusal is *the default* -- the function
returns a :class:`~stockbrain.execution.models.PreflightRefusal` unless every
gate passes.

Two kinds of gate, deliberately separated:

* **Deterministic risk** comes from
  :meth:`~stockbrain.proposals.service.ProposalService.revalidate`, which is the
  same engine and the same rule functions that guarded the authorization.  There
  is no second risk implementation here, and a test asserts this module imports
  none.
* **Permission and state** are checks that only exist because transmission
  exists: the environment binding, the transmission gates, the kill switch, the
  send-time automation consent, and the absence of an earlier transmitted
  attempt.

A refusal also says whether the proposal *survives* it.  "The market moved past
the drift limit" retires the proposal; "the quote provider is down" does not,
because failing every authorized trade whenever a provider blinks would make an
outage destructive rather than degrading.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from stockbrain.config import BrokerEnvironment, Settings
from stockbrain.control.state import ControlSnapshot, ControlStateService
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.enums import (
    AuthorizationSource,
    ExecutionFailure,
    ExecutionPolicy,
    OrderSide,
)
from stockbrain.execution.models import PreflightRefusal
from stockbrain.proposals.service import ProposalService, Revalidation
from stockbrain.proposals.state_machine import EXECUTABLE_STATUSES
from stockbrain.risk.models import ZERO

__all__ = ["ExecutionPreflight", "PreflightOutcome"]

#: Rules whose failure means an *input was missing*, not that the trade is
#: wrong.  When one of these blocks, every downstream rule blocks mechanically
#: -- there is no price to judge a spread against and no balance to judge a cap
#: against -- so the verdict says nothing about the trade and the proposal must
#: survive it.  Failing every authorized proposal whenever a market-data
#: provider blinks would make an outage destructive rather than degrading.
_MISSING_INPUT_RULE_IDS: frozenset[str] = frozenset({"quote_available", "account_state_available"})

#: Rules whose failure means a *provider is behind*.  A stale quote or a stale
#: account snapshot is a StockBrain problem that the next sweep may not have, so
#: it defers -- but only when nothing else blocked, because a stale quote must
#: never mask a genuine drift or spread refusal sitting beside it.
_STALE_INPUT_RULE_IDS: frozenset[str] = frozenset({"quote_freshness", "account_state_freshness"})


def _is_transient(blocked_rule_ids: frozenset[str]) -> bool:
    """Whether a risk refusal should leave the proposal alone.

    Everything not covered here -- drift, spread, two-sidedness, session, every
    cap, the position, the listing's identity, the TTL -- is a statement *about
    the trade* and retires the proposal, because the trade that was authorized
    is no longer available and re-deriving it needs the full pipeline.
    """
    if not blocked_rule_ids:
        return False
    if blocked_rule_ids & _MISSING_INPUT_RULE_IDS:
        return True
    return blocked_rule_ids <= _STALE_INPUT_RULE_IDS


@dataclass(frozen=True, slots=True)
class PreflightOutcome:
    """Either a refusal, or a green light plus everything it was based on."""

    refusal: PreflightRefusal | None
    revalidation: Revalidation | None
    control: ControlSnapshot | None
    proposal_version: int | None = None

    @property
    def permitted(self) -> bool:
        return self.refusal is None


class ExecutionPreflight:
    """Runs every pre-send check.  Reads only; decides nothing else."""

    def __init__(
        self,
        settings: Settings,
        *,
        proposals: ProposalService,
        control: ControlStateService,
        provider_environment: str,
    ) -> None:
        self._settings = settings
        self._proposals = proposals
        self._control = control
        self._provider_environment = provider_environment

    async def run(
        self, proposal: TradeProposal, *, now: dt.datetime, sent_attempt_exists: bool
    ) -> PreflightOutcome:
        control = await self._control.snapshot()

        # Cheap, local, certain checks first: there is no point spending a
        # market-data request to price a trade the kill switch has already
        # stopped.
        blocking = [
            *self._permission_reasons(proposal, control),
            *self._state_reasons(proposal, now, sent_attempt_exists),
        ]
        if blocking:
            return PreflightOutcome(
                refusal=PreflightRefusal(
                    category=ExecutionFailure.PREFLIGHT_REFUSED,
                    reasons=tuple(blocking),
                    # A permission or state refusal is about *this deployment* or
                    # *this moment*, not about the trade being wrong. Engaging a
                    # kill switch must not silently destroy every authorized
                    # proposal it stops.
                    invalidates=False,
                ),
                revalidation=None,
                control=control,
            )

        revalidation = await self._proposals.revalidate(proposal.id, now=now)
        if revalidation is None:
            return PreflightOutcome(
                refusal=PreflightRefusal(
                    category=ExecutionFailure.PREFLIGHT_REFUSED,
                    reasons=("the proposal no longer exists",),
                    invalidates=False,
                ),
                revalidation=None,
                control=control,
            )

        risk_reasons = list(revalidation.reasons)
        position_reason = self._position_reason(proposal, revalidation)
        if position_reason is not None:
            risk_reasons.append(position_reason)

        if risk_reasons:
            transient = _is_transient(frozenset(rule.rule_id for rule in revalidation.blocked))
            return PreflightOutcome(
                refusal=PreflightRefusal(
                    category=ExecutionFailure.PREFLIGHT_REFUSED,
                    reasons=tuple(risk_reasons),
                    invalidates=not transient,
                    rule_ids=revalidation.rule_ids,
                ),
                revalidation=revalidation,
                control=control,
            )

        return PreflightOutcome(
            refusal=None,
            revalidation=revalidation,
            control=control,
            proposal_version=proposal.version,
        )

    # ------------------------------------------------------------------
    def _permission_reasons(self, proposal: TradeProposal, control: ControlSnapshot) -> list[str]:
        """Is this deployment allowed to send *this* order, right now?"""
        reasons: list[str] = []

        # Environment isolation, checked against three independent facts: what
        # the proposal was created under, what this process is configured for,
        # and what the constructed provider actually points at.
        configured = self._settings.t212_env.value
        if proposal.broker_environment != configured:
            reasons.append(
                f"the proposal was created against the {proposal.broker_environment!r} "
                f"broker environment and this process is configured for {configured!r}"
            )
        if proposal.broker_environment != self._provider_environment:
            reasons.append(
                f"the proposal was created against the {proposal.broker_environment!r} "
                f"broker environment and the execution provider points at "
                f"{self._provider_environment!r}"
            )

        reasons.extend(self._settings.order_transmission_blockers)

        if control.trading_halted:
            reasons.extend(control.blockers)

        # Re-checked at send time, not merely at authorization. The operator's
        # relationship with their broker can change between the two, and clause
        # 4.2(a) is about the moment an order is determined and sent.
        if (
            proposal.authorization_source is AuthorizationSource.SYSTEM_AUTOMATIC
            and self._settings.t212_env is BrokerEnvironment.LIVE
            and not self._settings.t212_automated_trading_consent_confirmed
        ):
            reasons.append(
                "a SYSTEM_AUTOMATIC authorization cannot be transmitted to the live "
                "environment without T212_AUTOMATED_TRADING_CONSENT_CONFIRMED=true"
            )
        if (
            proposal.execution_policy is ExecutionPolicy.AUTOMATIC
            and proposal.authorization_source is not AuthorizationSource.SYSTEM_AUTOMATIC
            and proposal.authorization_source is not None
            and self._settings.t212_env is BrokerEnvironment.LIVE
            and not self._settings.t212_automated_trading_consent_confirmed
        ):  # pragma: no cover - defence in depth against a future provenance mix
            reasons.append(
                "an AUTOMATIC-policy proposal cannot be transmitted live without "
                "T212_AUTOMATED_TRADING_CONSENT_CONFIRMED=true"
            )
        return reasons

    def _state_reasons(
        self, proposal: TradeProposal, now: dt.datetime, sent_attempt_exists: bool
    ) -> list[str]:
        """Is this proposal in a shape that may be transmitted at all?"""
        reasons: list[str] = []
        if proposal.status not in EXECUTABLE_STATUSES:
            reasons.append(
                f"the proposal is {proposal.status.value} and only "
                f"{'/'.join(sorted(s.value for s in EXECUTABLE_STATUSES))} may be executed"
            )
        if proposal.authorization_source is None or proposal.approved_at is None:
            reasons.append("the proposal carries no authorization provenance")
        if proposal.expires_at <= now:
            # The TTL is what bounds how stale the approved price may be. An
            # authorization does not become a licence to send at any later time.
            reasons.append("the proposal expired before it could be transmitted")
        if sent_attempt_exists:
            reasons.append(
                "an execution attempt for this proposal has already been recorded as sent; "
                "reconciliation decides what happened to it"
            )
        if proposal.proposed_quantity <= 0:
            reasons.append("the proposal carries no positive quantity")
        return reasons

    def _position_reason(self, proposal: TradeProposal, revalidation: Revalidation) -> str | None:
        """Named separately because selling what a pie holds is its own hazard.

        Trading 212 reports ``quantityAvailableForTrading`` apart from
        ``quantity``, and Phase 6 measured them differing on 13 of 14 real
        positions: shares inside a pie are owned but not individually tradable.
        Sizing a reduction against the total produces an order the broker
        refuses -- and this check runs against the *current* snapshot, because
        the pie can have moved since authorization.
        """
        if proposal.side is not OrderSide.SELL:
            return None
        account = revalidation.account
        if account is None:  # already reported by `account_state_available`
            return None
        position = account.position(proposal.broker_ticker)
        available: Decimal = position.quantity_available if position is not None else ZERO
        if available < proposal.proposed_quantity:
            return (
                f"the position backing this sale has {available} share(s) available to "
                f"trade, below the proposed {proposal.proposed_quantity}"
            )
        return None
