"""Deciding what actually happened, by reading rather than by sending.

Reconciliation exists because a non-idempotent order endpoint plus an unreliable
network produces a state no amount of care can avoid: *the order may or may not
exist*.  There are exactly two honest ways out of that state -- find the order,
or prove its absence -- and this module implements both, plus a third answer
that is just as important:

    INCONCLUSIVE

Guessing is not an option.  Concluding "not placed" when an order exists means
the reservation is released and the next proposal is sized against money that is
already committed.  Concluding "placed" when none exists strands an authorized
trade forever.  So absence is only evidence when *both* read paths answered, and
only after the broker has had time to make a new order visible.

**Matching is deliberately narrow.**  Trading 212 supports no client-supplied
idempotency key or reference, so an attempt is matched to an order by:

* ``initiatedFrom == "API"`` -- the strongest signal available, and the reason a
  buy the operator placed on their phone can never be attributed to StockBrain;
* the exact broker ticker;
* the exact **signed** quantity, as a ``Decimal`` -- a buy for 3 and a sell for
  3 are different orders;
* ``type == "MARKET"``;
* ``createdAt`` inside a bounded window around ``sent_at``;
* and an order id not already mirrored against another attempt.

If more than one order still fits, the answer is ``MULTIPLE_CANDIDATES`` and the
attempt stays ambiguous.  Two identical market orders on the same listing in the
same window are genuinely indistinguishable through this API, and the limitation
is documented rather than papered over.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.config import Settings
from stockbrain.db.base import utcnow
from stockbrain.db.models.portfolio import BrokerOrder
from stockbrain.db.models.proposals import ExecutionAttempt, TradeProposal
from stockbrain.db.models.system import AuditLog
from stockbrain.db.session import Database
from stockbrain.enums import (
    ActorType,
    Broker,
    ExecutionOutcome,
    NotificationEvent,
    OrderType,
    ProposalStatus,
    ReconciliationResult,
)
from stockbrain.errors import ProviderError
from stockbrain.execution.base import BrokerExecutionProvider
from stockbrain.execution.models import BrokerOrderView
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS
from stockbrain.proposals.service import ProposalService
from stockbrain.proposals.state_machine import assert_transition, can_transition

__all__ = ["ReconciliationOutcome", "ReconciliationService"]

log = get_logger(__name__)

#: Broker statuses that end an order's life.
_FILLED = "FILLED"
_DEAD: frozenset[str] = frozenset({"CANCELLED", "REJECTED"})


@dataclass(slots=True)
class ReconciliationOutcome:
    attempt_id: uuid.UUID
    result: ReconciliationResult
    broker_order_id: str | None = None
    proposal_status: ProposalStatus | None = None
    candidates: int = 0
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.result in {
            ReconciliationResult.ORDER_FOUND,
            ReconciliationResult.ORDER_NOT_PLACED,
        }


class ReconciliationService:
    """Reads the broker to resolve an attempt.  Never transmits anything."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        provider: BrokerExecutionProvider,
        proposals: ProposalService,
        broker: Broker = Broker.TRADING212,
    ) -> None:
        self.database = database
        self.settings = settings
        self.provider = provider
        self.proposals = proposals
        self.broker = broker

    # ------------------------------------------------------------------
    async def reconcile(
        self, attempt_id: uuid.UUID, *, now: dt.datetime | None = None
    ) -> ReconciliationOutcome:
        moment = now or utcnow()
        async with self.database.session() as session:
            attempt = await session.get(ExecutionAttempt, attempt_id)
            if attempt is None:
                return ReconciliationOutcome(
                    attempt_id=attempt_id,
                    result=ReconciliationResult.INCONCLUSIVE,
                    detail="the attempt no longer exists",
                )
            proposal = await session.get(TradeProposal, attempt.proposal_id)
            if proposal is None:  # pragma: no cover - FK guarantees this
                return ReconciliationOutcome(
                    attempt_id=attempt_id,
                    result=ReconciliationResult.INCONCLUSIVE,
                    detail="the proposal no longer exists",
                )
            snapshot = _AttemptFacts.of(attempt, proposal)

        if not snapshot.needs_reconciliation:
            return ReconciliationOutcome(
                attempt_id=attempt_id,
                result=ReconciliationResult.ORDER_FOUND
                if snapshot.broker_order_id
                else ReconciliationResult.INCONCLUSIVE,
                broker_order_id=snapshot.broker_order_id,
                detail=f"the attempt is already {snapshot.outcome.value}",
            )
        if snapshot.broker_environment != self.provider.environment:
            # A worker configured for the other environment must not read -- or
            # write -- another environment's order state.
            return ReconciliationOutcome(
                attempt_id=attempt_id,
                result=ReconciliationResult.INCONCLUSIVE,
                detail=(
                    f"the attempt belongs to the {snapshot.broker_environment!r} environment "
                    f"and this process is configured for {self.provider.environment!r}"
                ),
            )

        # A known order id is the easy case: ask about it directly.
        if snapshot.broker_order_id:
            return await self._reconcile_known_order(snapshot, moment)
        return await self._search_for_order(snapshot, moment)

    # ------------------------------------------------------------------
    async def _reconcile_known_order(
        self, facts: _AttemptFacts, now: dt.datetime
    ) -> ReconciliationOutcome:
        """We have an id.  Look it up, in pending first and then in history."""
        assert facts.broker_order_id is not None
        try:
            order = await self.provider.fetch_order(facts.broker_order_id)
            if order is None:
                # Not pending any more: it filled, cancelled or was rejected.
                history = await self.provider.find_candidates(
                    broker_ticker=facts.broker_ticker,
                    since=facts.window_start(self.settings),
                    until=facts.window_end(self.settings, now),
                )
                order = next(
                    (
                        candidate
                        for candidate in history.candidates
                        if candidate.broker_order_id == facts.broker_order_id
                    ),
                    None,
                )
        except ProviderError as exc:
            return await self._record(
                facts,
                ReconciliationResult.BROKER_UNAVAILABLE,
                now,
                detail=f"the broker could not be read ({type(exc).__name__})",
            )
        if order is None:
            return await self._record(
                facts,
                ReconciliationResult.INCONCLUSIVE,
                now,
                detail=(
                    "the broker order id is known but the broker returned no such order; "
                    "not concluding absence from a single read"
                ),
            )
        return await self._record(
            facts, ReconciliationResult.ORDER_FOUND, now, order=order, candidates=1
        )

    async def _search_for_order(
        self, facts: _AttemptFacts, now: dt.datetime
    ) -> ReconciliationOutcome:
        """No id.  Search conservatively, and accept an inconclusive answer."""
        try:
            search = await self.provider.find_candidates(
                broker_ticker=facts.broker_ticker,
                since=facts.window_start(self.settings),
                until=facts.window_end(self.settings, now),
            )
        except ProviderError as exc:
            return await self._record(
                facts,
                ReconciliationResult.BROKER_UNAVAILABLE,
                now,
                detail=f"the broker could not be read ({type(exc).__name__})",
            )

        async with self.database.session() as session:
            claimed = await _claimed_order_ids(session, self.broker, facts.attempt_id)

        matches = [
            order
            for order in search.candidates
            if _matches(order, facts) and order.broker_order_id not in claimed
        ]

        if len(matches) == 1:
            return await self._record(
                facts,
                ReconciliationResult.ORDER_FOUND,
                now,
                order=matches[0],
                candidates=1,
                detail="one API-initiated order matched the attempt exactly",
            )
        if len(matches) > 1:
            # Genuinely indistinguishable through this API. Documented, not guessed.
            return await self._record(
                facts,
                ReconciliationResult.MULTIPLE_CANDIDATES,
                now,
                candidates=len(matches),
                detail=(
                    f"{len(matches)} API-initiated orders match this attempt; the broker "
                    "supports no client reference that could tell them apart"
                ),
            )

        if not search.complete:
            return await self._record(
                facts,
                ReconciliationResult.BROKER_UNAVAILABLE,
                now,
                detail=(
                    "absence is not evidence unless both the pending list and the history "
                    f"answered (pending={search.pending_ok}, history={search.history_ok})"
                ),
            )
        settle = self.settings.execution_reconcile_min_age_seconds
        if facts.sent_at is not None and (now - facts.sent_at).total_seconds() < settle:
            return await self._record(
                facts,
                ReconciliationResult.INCONCLUSIVE,
                now,
                detail=(
                    f"no matching order yet, and the attempt is younger than the {settle:g}s "
                    "settle window; a new order may not be visible yet"
                ),
            )
        return await self._record(
            facts,
            ReconciliationResult.ORDER_NOT_PLACED,
            now,
            detail=(
                f"both broker read paths answered and no API-initiated order matches this "
                f"attempt ({search.scanned} order(s) scanned)"
            ),
        )

    # ------------------------------------------------------------------
    async def _record(
        self,
        facts: _AttemptFacts,
        result: ReconciliationResult,
        now: dt.datetime,
        *,
        order: BrokerOrderView | None = None,
        candidates: int = 0,
        detail: str = "",
    ) -> ReconciliationOutcome:
        """Persist the conclusion, and move the proposal only when entitled to.

        An inconclusive pass still writes: the attempt count, the timestamp and
        what was seen.  An operator looking at a stuck order needs to know it is
        being looked at.
        """
        final_status: ProposalStatus | None = None
        async with self.database.transaction() as session:
            attempt = (
                await session.execute(
                    sa.select(ExecutionAttempt)
                    .where(ExecutionAttempt.id == facts.attempt_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if attempt is None:  # pragma: no cover - defensive
                return ReconciliationOutcome(
                    attempt_id=facts.attempt_id,
                    result=ReconciliationResult.INCONCLUSIVE,
                    detail="the attempt vanished",
                )
            proposal = (
                await session.execute(
                    sa.select(TradeProposal)
                    .where(TradeProposal.id == facts.proposal_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if proposal is None:  # pragma: no cover - FK guarantees this
                return ReconciliationOutcome(
                    attempt_id=facts.attempt_id,
                    result=ReconciliationResult.INCONCLUSIVE,
                    detail="the proposal vanished",
                )

            attempt.reconciled_at = now
            attempt.reconciliation_result = result.value
            attempt.reconciliation_attempts = attempt.reconciliation_attempts + 1
            attempt.reconciliation_detail = {
                "result": result.value,
                "detail": detail,
                "candidates": candidates,
                "checked_at": now.isoformat(),
                "broker_environment": facts.broker_environment,
            }

            if result is ReconciliationResult.ORDER_FOUND and order is not None:
                attempt.broker_order_id = order.broker_order_id
                attempt.outcome = (
                    ExecutionOutcome.RECONCILED_FILLED
                    if (order.status or "").upper() == _FILLED
                    else ExecutionOutcome.SUBMITTED
                )
                attempt.ambiguous = False
                await _mirror(session, proposal, attempt, order, now)
                final_status = _advance_for_order(proposal, order, now)
            elif result is ReconciliationResult.ORDER_NOT_PLACED:
                # Proven absence. This is the only path that releases the
                # reservation for an attempt that was recorded as sent.
                attempt.outcome = ExecutionOutcome.RECONCILED_NOT_PLACED
                attempt.ambiguous = False
                if can_transition(proposal.status, ProposalStatus.FAILED):
                    assert_transition(proposal.status, ProposalStatus.FAILED)
                    proposal.status = ProposalStatus.FAILED
                    proposal.status_reason = (
                        "reconciliation proved the order never reached the broker"
                    )
                    proposal.updated_at = now
                final_status = proposal.status
            else:
                # Still unknown. The attempt stays ambiguous and the proposal
                # keeps reserving its exposure.
                final_status = proposal.status

            session.add(
                AuditLog(
                    actor_type=ActorType.SYSTEM,
                    actor_id="system:reconciliation",
                    action="execution.reconciled",
                    entity_type="execution_attempt",
                    entity_id=facts.attempt_id,
                    details={
                        "result": result.value,
                        "candidates": candidates,
                        "broker_order_id": attempt.broker_order_id,
                        "detail": detail[:500],
                        "order_resent": False,
                    },
                )
            )
            if result in {
                ReconciliationResult.ORDER_FOUND,
                ReconciliationResult.ORDER_NOT_PLACED,
            }:
                await self.proposals.enqueue_notification(
                    session,
                    facts.proposal_id,
                    NotificationEvent.EXECUTION_RECONCILED,
                    detail=f"{result.value}: {detail}"[:400],
                )
            if final_status is ProposalStatus.EXECUTED:
                await self.proposals.enqueue_notification(
                    session, facts.proposal_id, NotificationEvent.EXECUTION_CONFIRMED
                )

        METRICS.inc("stockbrain_reconciliations_total", labels={"result": result.value})
        log.info(
            "execution_reconciled",
            attempt_id=str(facts.attempt_id),
            proposal_id=str(facts.proposal_id),
            result=result.value,
            candidates=candidates,
            order_resent=False,
        )
        return ReconciliationOutcome(
            attempt_id=facts.attempt_id,
            result=result,
            broker_order_id=order.broker_order_id if order else facts.broker_order_id,
            proposal_status=final_status,
            candidates=candidates,
            detail=detail,
        )

    # ------------------------------------------------------------------
    async def sweep(self, *, now: dt.datetime | None = None, limit: int = 5) -> dict[str, int]:
        """Reconcile a bounded batch of unresolved attempts.

        Bounded because every pass spends rate-limited broker reads, and capped
        by ``EXECUTION_RECONCILE_MAX_ATTEMPTS`` because an order the broker
        cannot account for is a thing for a human to look at, not a thing to
        poll until the end of time.
        """
        moment = now or utcnow()
        if self.settings.broker_credentials_present is False:
            return {"checked": 0, "resolved": 0}
        async with self.database.session() as session:
            pending = list(
                (
                    await session.execute(
                        sa.select(ExecutionAttempt.id)
                        .where(
                            ExecutionAttempt.sent_to_broker.is_(True),
                            ExecutionAttempt.outcome.in_(
                                (ExecutionOutcome.AMBIGUOUS, ExecutionOutcome.SUBMITTED)
                            ),
                            ExecutionAttempt.broker_environment == self.provider.environment,
                            ExecutionAttempt.reconciliation_attempts
                            < self.settings.execution_reconcile_max_attempts,
                        )
                        .order_by(ExecutionAttempt.sent_at)
                        .limit(limit)
                    )
                ).scalars()
            )
        resolved = 0
        for attempt_id in pending:
            outcome = await self.reconcile(attempt_id, now=moment)
            if outcome.resolved:
                resolved += 1
        if pending:
            log.info("reconciliation_sweep", checked=len(pending), resolved=resolved)
        return {"checked": len(pending), "resolved": resolved}


# ---------------------------------------------------------------------------
# Facts and matching
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _AttemptFacts:
    """Everything matching needs, read once so no query runs inside a loop."""

    attempt_id: uuid.UUID
    proposal_id: uuid.UUID
    outcome: ExecutionOutcome
    broker_order_id: str | None
    broker_environment: str
    broker_ticker: str
    signed_quantity: Decimal
    sent_at: dt.datetime | None

    @classmethod
    def of(cls, attempt: ExecutionAttempt, proposal: TradeProposal) -> _AttemptFacts:
        payload = attempt.request_payload or {}
        raw = payload.get("signed_quantity")
        signed = Decimal(str(raw)) if raw is not None else Decimal(0)
        return cls(
            attempt_id=attempt.id,
            proposal_id=attempt.proposal_id,
            outcome=attempt.outcome,
            broker_order_id=attempt.broker_order_id,
            broker_environment=attempt.broker_environment,
            broker_ticker=str(payload.get("broker_ticker") or proposal.broker_ticker),
            signed_quantity=signed,
            sent_at=attempt.sent_at,
        )

    @property
    def needs_reconciliation(self) -> bool:
        return self.outcome in {
            ExecutionOutcome.PENDING,
            ExecutionOutcome.AMBIGUOUS,
            ExecutionOutcome.SUBMITTED,
        }

    def window_start(self, settings: Settings) -> dt.datetime:
        anchor = self.sent_at or utcnow()
        return anchor - dt.timedelta(seconds=settings.execution_reconcile_window_seconds)

    def window_end(self, settings: Settings, now: dt.datetime) -> dt.datetime:
        anchor = self.sent_at or now
        return anchor + dt.timedelta(seconds=settings.execution_reconcile_window_seconds)


def _matches(order: BrokerOrderView, facts: _AttemptFacts) -> bool:
    """Conservative candidate matching.

    Every clause exists to *exclude*.  The API-initiated check is the one that
    stops a manual order in the Trading 212 app being attributed to StockBrain;
    the signed-quantity check is the one that stops a buy matching a sell.
    """
    if not order.placed_by_api:
        return False
    if order.broker_ticker != facts.broker_ticker:
        return False
    if (order.order_type or "").upper() != OrderType.MARKET.value:
        return False
    if order.signed_quantity is None:
        return False
    return order.signed_quantity == facts.signed_quantity


async def _claimed_order_ids(
    session: AsyncSession, broker: Broker, exclude_attempt: uuid.UUID
) -> set[str]:
    """Broker order ids already mirrored against a *different* attempt.

    Without this, two attempts on the same listing could both claim the same
    order and each report success.
    """
    rows = (
        await session.execute(
            sa.select(BrokerOrder.broker_order_id).where(
                BrokerOrder.broker == broker,
                sa.or_(
                    BrokerOrder.execution_attempt_id.is_(None),
                    BrokerOrder.execution_attempt_id != exclude_attempt,
                ),
            )
        )
    ).scalars()
    return {str(value) for value in rows}


def _advance_for_order(
    proposal: TradeProposal, order: BrokerOrderView, now: dt.datetime
) -> ProposalStatus:
    """Move the proposal to match what the broker says about the order."""
    status = (order.status or "").upper()
    if status == _FILLED and can_transition(proposal.status, ProposalStatus.EXECUTED):
        assert_transition(proposal.status, ProposalStatus.EXECUTED)
        proposal.status = ProposalStatus.EXECUTED
        proposal.status_reason = "the broker reports the order filled"
        proposal.executed_at = order.created_at or now
        proposal.updated_at = now
    elif status in _DEAD and can_transition(proposal.status, ProposalStatus.FAILED):
        assert_transition(proposal.status, ProposalStatus.FAILED)
        proposal.status = ProposalStatus.FAILED
        proposal.status_reason = f"the broker ended the order as {status}"
        proposal.updated_at = now
    else:
        # The order exists and is still working. The proposal stays where it is,
        # which keeps its exposure reserved.
        proposal.status_reason = f"a broker order exists (status {status or 'unknown'})"
        proposal.updated_at = now
    return proposal.status


async def _mirror(
    session: AsyncSession,
    proposal: TradeProposal,
    attempt: ExecutionAttempt,
    order: BrokerOrderView,
    now: dt.datetime,
) -> None:
    """Upsert the local mirror of a broker order discovered by reconciliation."""
    existing = (
        await session.execute(
            sa.select(BrokerOrder).where(
                BrokerOrder.broker == proposal.broker,
                BrokerOrder.broker_order_id == order.broker_order_id,
            )
        )
    ).scalar_one_or_none()
    signed = order.signed_quantity
    row = existing or BrokerOrder(
        broker=proposal.broker,
        broker_order_id=order.broker_order_id,
        broker_ticker=order.broker_ticker or proposal.broker_ticker,
        side=proposal.side,
        order_type=OrderType.MARKET,
        quantity=abs(signed) if signed is not None else proposal.proposed_quantity,
        discovered_by_reconciliation=existing is None,
    )
    row.proposal_id = proposal.id
    row.execution_attempt_id = attempt.id
    row.filled_quantity = order.filled_quantity
    row.filled_value = order.filled_value
    row.currency = order.currency
    row.broker_status = order.status
    row.broker_environment = proposal.broker_environment
    row.initiated_from = order.initiated_from
    row.is_terminal = order.is_terminal
    row.submitted_at = order.created_at or now
    row.last_synced_at = now
    row.raw = _strip(order.raw)
    if existing is None:
        session.add(row)


def _strip(payload: dict[str, Any]) -> dict[str, Any]:
    forbidden = ("authorization", "token", "secret", "password", "api_key", "apikey", "cookie")
    return {
        key: value
        for key, value in payload.items()
        if not any(fragment in key.lower() for fragment in forbidden)
    }
