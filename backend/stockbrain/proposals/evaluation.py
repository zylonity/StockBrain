"""Shared proposal safety evaluation and the sweep's market-only projection."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.broker.account_state import AccountStateService
from stockbrain.config import Settings
from stockbrain.db.models.companies import BrokerInstrument
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.session import Database
from stockbrain.enums import (
    Broker,
    OrderSide,
    RiskOutcome,
    RuleOutcome,
    ThesisAction,
)
from stockbrain.fx.base import normalize_currency
from stockbrain.fx.service import FxService
from stockbrain.logging import get_logger
from stockbrain.proposals.quotes import QuoteFetcher
from stockbrain.proposals.state_machine import (
    EXPOSURE_RESERVING_STATUSES,
)
from stockbrain.risk.config import RiskConfig
from stockbrain.risk.engine import RiskEngine
from stockbrain.risk.models import (
    ZERO,
    AccountState,
    FxSnapshot,
    InstrumentIdentity,
    QuoteSnapshot,
    ReservedExposure,
    RiskDecision,
    RiskInputs,
    RuleResult,
)
from stockbrain.risk.rules import (
    fx_rate_drift,
    proposal_ttl,
    reference_price_drift,
)

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Revalidation:
    """A fresh, read-only verdict on a candidate or existing proposal.  Changes nothing.

    Produced by :meth:`ProposalService.revalidate`, which Phase 8's pre-send
    preflight uses so that the checks guarding a broker POST are *the same
    checks*, from the same engine and the same rule functions, that guarded the
    authorization.  A second implementation of "is this still safe" is how two
    answers start disagreeing.
    """

    proposal_id: uuid.UUID | None
    decision: RiskDecision
    rules: tuple[RuleResult, ...]
    quote: QuoteSnapshot | None
    account: AccountState | None
    fx: FxSnapshot | None = None
    quote_reason: str | None = None
    account_reason: str | None = None

    @property
    def blocked(self) -> tuple[RuleResult, ...]:
        return tuple(rule for rule in self.rules if rule.outcome is RuleOutcome.BLOCK)

    @property
    def allowed(self) -> bool:
        return not self.blocked

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(rule.reason for rule in self.blocked)

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(rule.rule_id for rule in self.blocked)

    def as_dict(self) -> dict[str, object]:
        return {
            "outcome": (RiskOutcome.BLOCK if self.blocked else self.decision.outcome).value,
            "policy_version": self.decision.policy_version,
            "rules": [rule.as_dict() for rule in self.rules],
            "snapshot": self.decision.as_dict(),
            "snapshot_hash": self.decision.snapshot_hash(),
            "quote_missing_reason": self.quote_reason,
            "account_missing_reason": self.account_reason,
            "fx": self.fx.as_dict() if self.fx else None,
        }


@dataclass(frozen=True, slots=True)
class EvaluationFacts:
    """External observations, loaded before the caller acquires mutation locks."""

    account: AccountState | None
    quote: QuoteSnapshot | None
    fx: FxSnapshot | None
    account_reason: str | None
    quote_reason: str | None


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    """Durable facts re-read at the caller's original transaction boundary.

    Generation has no proposal; existing proposals exclude their own reservation
    and add the recorded FX reference and proposal constraints. Research scalars
    remain those read before the network calls, as on the original paths.
    """

    action: ThesisAction
    confidence: Decimal
    identity: InstrumentIdentity
    account_id: str
    proposal: TradeProposal | None = None


class ProposalEvaluator:
    """One safety path, split only where PostgreSQL locking requires it.

    ``load`` reads account, quote/session/spread and FX in that order, without
    mutation locks. ``evaluate`` reads reservations in the caller's session,
    constructs the only RiskInputs in the proposal package and runs the engine.
    It then appends existing-proposal constraints in their original order.
    Neither method authorizes, persists a verdict or changes proposal state.
    """

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        config: RiskConfig,
        account_state: AccountStateService,
        quotes: QuoteFetcher,
        fx: FxService,
        broker: Broker,
        engine: RiskEngine,
    ) -> None:
        self.database = database
        self.settings = settings
        self.config = config
        self.account_state = account_state
        self.quotes = quotes
        self.fx = fx
        self.broker = broker
        self.engine = engine

    async def load(
        self,
        instrument: BrokerInstrument | None,
        *,
        instrument_currency: str | None,
        now: dt.datetime,
    ) -> EvaluationFacts:
        account, account_reason = await self.account_state.load(
            max_age_seconds=self.config.max_account_state_age_seconds, now=now
        )
        quote: QuoteSnapshot | None = None
        quote_reason: str | None = "the listing could not be identified for pricing"
        if instrument is not None:
            async with self.database.session() as session:
                quote, quote_reason = await self.quotes.fetch(
                    session, instrument, self.config, now=now
                )
        fx = await self.fx_snapshot(
            account=account, instrument_currency=instrument_currency, now=now
        )
        return EvaluationFacts(account, quote, fx, account_reason, quote_reason)

    async def evaluate(
        self,
        session: AsyncSession,
        context: EvaluationContext,
        facts: EvaluationFacts,
        *,
        now: dt.datetime,
    ) -> Revalidation:
        proposal = context.proposal
        # walletImpact is already in account currency and is never converted.
        # Only an absent valuation may use quantity * broker price, converted
        # with the very same FX measurement that the engine will judge below.
        account = facts.account
        position = account.position(context.identity.broker_ticker) if account else None
        fx = facts.fx
        if (
            account is not None
            and position is not None
            and position.market_value is None
            and position.current_price is not None
            and normalize_currency(position.currency)
            == normalize_currency(context.identity.currency)
            and fx is not None
            and fx.usable
        ):
            value = fx.to_account_currency(position.quantity * position.current_price)
            account = replace(
                account,
                positions={
                    **account.positions,
                    position.broker_ticker: replace(position, market_value=value),
                },
            )
            facts = replace(facts, account=account)
        reserved = await self._reserved_exposure(
            session,
            context.account_id,
            context.identity.broker_ticker,
            account_currency=facts.account.currency if facts.account else None,
            exclude_proposal_id=proposal.id if proposal is not None else None,
        )
        decision = self.engine.evaluate(
            RiskInputs(
                config=self.config,
                action=context.action,
                confidence=context.confidence,
                identity=context.identity,
                account=facts.account,
                quote=facts.quote,
                reserved=reserved,
                now=now,
                fx=facts.fx,
                authorized_fx_rate=proposal.fx_rate if proposal is not None else None,
                account_state_missing_reason=facts.account_reason,
                quote_missing_reason=facts.quote_reason,
            ),
            now=now,
        )
        rules = decision.rules
        if proposal is not None:
            rules = (
                *rules,
                *self._authorization_rules(proposal, decision, facts.quote, facts.fx, now),
            )
        return Revalidation(
            proposal_id=proposal.id if proposal is not None else None,
            decision=decision,
            rules=rules,
            quote=facts.quote,
            account=facts.account,
            fx=facts.fx,
            quote_reason=facts.quote_reason,
            account_reason=facts.account_reason,
        )

    async def fx_snapshot(
        self,
        *,
        account: AccountState | None,
        instrument_currency: str | None,
        now: dt.datetime,
    ) -> FxSnapshot | None:
        """Assemble the FX facts for one account/instrument pair.

        ``None`` only when the currencies themselves are unknown -- at which
        point ``currency_alignment`` blocks on that, and reporting a snapshot
        would imply more was known than is.

        No caching and no fallback, exactly as with quotes: a rate is fetched
        when it is needed, and there is no second source to quietly substitute.
        """
        account_currency = normalize_currency(account.currency if account else None)
        listing_currency = normalize_currency(instrument_currency)
        if not account_currency or not listing_currency:
            return None
        if account_currency == listing_currency:
            return FxSnapshot.same_currency_snapshot(account_currency)

        resolution = await self.fx.resolve(
            from_currency=account_currency, to_currency=listing_currency, now=now
        )
        rate = resolution.rate
        return FxSnapshot(
            account_currency=account_currency,
            instrument_currency=listing_currency,
            same_currency=False,
            blockers=resolution.blockers,
            rate=rate.rate if rate else None,
            base_currency=rate.base_currency if rate else None,
            quote_currency=rate.quote_currency if rate else None,
            provider=rate.provider if rate else resolution.provider,
            grade=rate.grade if rate else None,
            rate_type=rate.rate_type if rate else None,
            provider_timestamp=rate.provider_timestamp if rate else None,
            received_at=rate.received_at if rate else None,
            age_seconds=rate.age_seconds(now) if rate else None,
            provider_timestamp_precision=(rate.provider_timestamp_precision if rate else None),
        )

    async def _reserved_exposure(
        self,
        session: AsyncSession,
        account_id: str,
        broker_ticker: str,
        *,
        account_currency: str | None = None,
        exclude_proposal_id: uuid.UUID | None = None,
    ) -> ReservedExposure:
        """What live proposals have already claimed.

        Cross-currency amounts were converted with the evaluation's validated
        FX snapshot when written. Read them directly: another conversion or FX
        request here would give the same obligation two incompatible prices.
        An unknown amount is a risk blocker, never an invented zero or parity.

        Only exposure-*increasing* sides reserve: a pending sell frees cash
        rather than committing it, so counting it as reserved would shrink the
        budget for the very trade that is about to enlarge it.
        """
        conditions = [
            TradeProposal.status.in_(EXPOSURE_RESERVING_STATUSES),
            TradeProposal.broker == self.broker,
            TradeProposal.account_id == account_id,
        ]
        if exclude_proposal_id is not None:
            conditions.append(TradeProposal.id != exclude_proposal_id)

        rows = list(
            (
                await session.execute(
                    sa.select(
                        TradeProposal.id,
                        TradeProposal.broker_ticker,
                        TradeProposal.side,
                        TradeProposal.estimated_notional,
                        TradeProposal.estimated_notional_account_currency,
                        TradeProposal.account_currency,
                        TradeProposal.reference_currency,
                        TradeProposal.fx_required,
                    ).where(*conditions)
                )
            ).all()
        )
        amounts: dict[uuid.UUID, Decimal] = {}
        blockers: list[str] = []
        for row in rows:
            if row.side is not OrderSide.BUY:
                continue
            currency = normalize_currency(row.account_currency)
            reference_currency = normalize_currency(row.reference_currency)
            if not currency or (
                account_currency and currency != normalize_currency(account_currency)
            ):
                blockers.append(
                    f"proposal {row.id} reserves a different or unknown account currency"
                )
                continue
            # Old same-currency rows may predate the converted-notional column.
            # Parity is valid only when both currency identities establish it.
            same_currency = bool(reference_currency) and currency == reference_currency
            amount = row.estimated_notional_account_currency
            if same_currency and not row.fx_required:
                if amount is None:
                    amount = row.estimated_notional
                elif amount.is_finite():
                    # Keep the upward-rounded reservation. The instrument
                    # amount can have rounded down at database precision.
                    # max also covers same-currency rows authorized before
                    # authorization refreshed the account-currency column.
                    amount = max(amount, row.estimated_notional)
            if (
                not reference_currency
                or row.fx_required == same_currency
                or amount is None
                or not amount.is_finite()
                or amount <= ZERO
            ):
                blockers.append(f"proposal {row.id} has no validated account-currency reservation")
                continue
            amounts[row.id] = amount
        same = [row for row in rows if row.broker_ticker == broker_ticker]
        return ReservedExposure(
            blockers=tuple(blockers),
            count=len(rows),
            notional=sum(amounts.values(), ZERO),
            same_instrument_count=len(same),
            same_instrument_notional=sum((amounts.get(row.id, ZERO) for row in same), ZERO),
            same_instrument_sides=tuple(sorted({row.side.value for row in same})),
        )

    def _authorization_rules(
        self,
        proposal: TradeProposal,
        decision: RiskDecision,
        quote: QuoteSnapshot | None,
        fx: FxSnapshot | None,
        now: dt.datetime,
    ) -> list[RuleResult]:
        """The checks that only exist because a proposal already exists."""
        rules = [
            proposal_ttl(proposal.expires_at, now),
            self._price_drift(proposal, quote),
            # The FX analogue, and load-bearing on a GBP account holding USD
            # listings: a one percent move in the pair moves the trade's
            # account-currency notional by one percent, straight through the
            # per-trade cap, the cash reserve and the concentration limit.
            self._fx_drift(proposal, fx),
            _policy_version_unchanged(proposal, self.config.version),
            _envelope_still_covers(proposal, decision),
        ]
        return rules

    def _price_drift(self, proposal: TradeProposal, quote: QuoteSnapshot | None) -> RuleResult:
        return reference_price_drift(
            proposal.reference_price,
            quote.mid if quote else None,
            max_drift_pct=self.config.max_reference_price_drift_pct,
        )

    def _fx_drift(self, proposal: TradeProposal, fx: FxSnapshot | None) -> RuleResult:
        return fx_rate_drift(
            proposal.fx_rate, fx, max_drift_pct=self.settings.fx_max_rate_drift_pct
        )

    def market_failure(
        self,
        proposal: TradeProposal,
        quote: QuoteSnapshot | None,
        quote_reason: str | None,
        fx: FxSnapshot | None,
        now: dt.datetime,
    ) -> str | None:
        """Whether the market has moved out from under a live proposal.

        A missing quote is *not* treated as invalidation on its own: providers
        go down, and retiring every open proposal because one request failed
        would make an outage destructive rather than merely degrading.  A quote
        that arrives and is bad -- too wide, or too far from the reference
        price -- is a real change and does invalidate.

        The exchange rate is treated exactly the same way, and for the same
        reason: on a GBP account holding a USD listing, the account-currency
        size the operator is looking at is the product of the price *and* the
        rate.  A missing rate degrades; a rate that arrived and has moved past
        the envelope invalidates.
        """
        if proposal.fx_required and fx is not None and fx.usable:
            drift_fx = self._fx_drift(proposal, fx)
            if drift_fx.outcome is RuleOutcome.BLOCK:
                return drift_fx.reason
        if quote is None:
            log.debug(
                "proposal_revalidation_skipped",
                proposal_id=str(proposal.id),
                reason=quote_reason,
            )
            return None
        if not quote.spread.is_ok:
            return f"the market widened after this proposal was generated: {quote.spread.detail}"
        drift = self._price_drift(proposal, quote)
        if drift.outcome is RuleOutcome.BLOCK:
            return drift.reason
        return None


def _policy_version_unchanged(proposal: TradeProposal, current: str) -> RuleResult:
    unchanged = proposal.risk_policy_version in (None, current)
    return RuleResult(
        rule_id="risk_policy_version",
        rule_version=1,
        outcome=RuleOutcome.PASS if unchanged else RuleOutcome.BLOCK,
        reason=(
            "the risk configuration is unchanged since this proposal was generated"
            if unchanged
            else (
                "the risk configuration changed after this proposal was generated; "
                "a new analysis is required rather than authorizing against limits "
                "nobody chose for it"
            )
        ),
        observed=proposal.risk_policy_version,
        threshold=current,
    )


def _envelope_still_covers(proposal: TradeProposal, decision: RiskDecision) -> RuleResult:
    """The fresh risk envelope must still permit the quantity on the row.

    Not "re-size to whatever fits now": a quantity that changes between the
    screen and the click is a quantity nobody approved.  If the envelope
    shrank, the proposal is invalidated and a new analysis is required.

    **A blocked decision carries no envelope to compare against**, and saying
    so is the whole subtlety of this function.  A blocked decision deliberately
    produces no size at all -- ``RiskEngine.evaluate`` does not even call
    ``size_trade`` -- so ``sizing.side`` is ``None`` and a naive comparison
    reports "the side changed from BUY to none".  That reads as a statement
    about the trade, which retires the proposal; and a *stale quote* is enough
    to produce it.  A market-data provider running a minute behind would
    therefore destroy every authorized proposal it touched.

    So a blocked decision records ``WARN`` here: a rule that could not run.
    The rules that actually blocked are already in the list, and it is their
    own transient/permanent classification (see
    :mod:`stockbrain.execution.preflight`) that decides whether the proposal
    survives.  Found in Phase 9; it is the same class of mistake as Phase 8's
    bug 21, one layer up.
    """
    sizing = decision.sizing
    if decision.outcome is RiskOutcome.BLOCK:
        return RuleResult(
            rule_id="authorization_envelope",
            rule_version=2,
            outcome=RuleOutcome.WARN,
            reason=(
                "the fresh risk envelope could not be recomputed because deterministic "
                "risk blocked before sizing; the blocking rules are the verdict"
            ),
            observed=None,
            threshold=str(proposal.proposed_quantity),
        )
    if sizing.side is not proposal.side:
        return RuleResult(
            rule_id="authorization_envelope",
            rule_version=2,
            outcome=RuleOutcome.BLOCK,
            reason=(
                "the deterministic side changed since generation "
                f"({proposal.side.value} -> {sizing.side.value if sizing.side else 'none'})"
            ),
            observed=sizing.side.value if sizing.side else None,
            threshold=proposal.side.value,
        )
    covered = sizing.max_quantity >= proposal.proposed_quantity
    return RuleResult(
        rule_id="authorization_envelope",
        rule_version=2,
        outcome=RuleOutcome.PASS if covered else RuleOutcome.BLOCK,
        reason=(
            f"the fresh risk envelope still permits {proposal.proposed_quantity} share(s) "
            f"(up to {sizing.max_quantity})"
            if covered
            else (
                f"the fresh risk envelope permits only {sizing.max_quantity} share(s), "
                f"below the proposed {proposal.proposed_quantity}; the proposal is not "
                "silently re-sized"
            )
        ),
        observed=str(sizing.max_quantity),
        threshold=str(proposal.proposed_quantity),
    )
