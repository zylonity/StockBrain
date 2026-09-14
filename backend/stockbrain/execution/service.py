"""One authorized proposal, at most one broker order.

The ordering in this module is the whole safety argument, so it is written out
once here and then implemented literally:

1. **Look for an already-transmitted attempt.**  If one exists, this proposal's
   outcome is a question for reconciliation, never for a second POST.
2. **Preflight**, with no locks held, because it makes network calls.
3. **Take a local rate-limit token.**  Before the send transaction, so a denial
   is provably pre-send and can simply defer.
4. **The send transaction**: lock the proposal row, re-check its version, its
   status, its expiry and the kill switch *from the database*, insert the
   attempt with ``sent_to_broker = True``, move the proposal to ``EXECUTING``,
   commit.
5. **POST exactly once.**  No retry, at any layer, for any reason.
6. **Classify and persist.**

Step 4 commits "bytes may have left" *before* step 5 makes it possible.  That is
deliberate and it is the point: if the process dies anywhere between the commit
and the response, the next worker finds a transmitted attempt and reconciles
instead of resending.  The cost is that a genuine pre-send failure has to be
*retracted* -- which happens in exactly one place, on the strength of an httpx
exception that cannot occur after the request line is written.

What this module never does: retry a mutation, choose a quantity, resolve an
ambiguity by sending again, cancel an order, or read an order parameter from
anything other than the persisted proposal row.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.config import Settings
from stockbrain.control.state import ControlSnapshot, ControlStateService
from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument
from stockbrain.db.models.portfolio import BrokerOrder
from stockbrain.db.models.proposals import ExecutionAttempt, TradeProposal
from stockbrain.db.models.system import AuditLog
from stockbrain.db.session import Database
from stockbrain.enums import (
    ActorType,
    Broker,
    ExecutionFailure,
    ExecutionOutcome,
    JobType,
    NotificationEvent,
    OrderType,
    ProposalStatus,
)
from stockbrain.errors import (
    AmbiguousTransportFailure,
    BrokerRejection,
    DefinitePreSendFailure,
)
from stockbrain.execution.base import BrokerExecutionProvider
from stockbrain.execution.fingerprint import fingerprint_for
from stockbrain.execution.models import (
    BrokerAcknowledgement,
    ExecutionCommand,
    PreflightRefusal,
)
from stockbrain.execution.preflight import ExecutionPreflight
from stockbrain.jobs.queue import JobQueue
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS
from stockbrain.proposals.service import ProposalService, Revalidation
from stockbrain.proposals.state_machine import (
    EXECUTABLE_STATUSES,
    PRESEND_RECOVERY_TARGET,
    assert_transition,
)

__all__ = ["ExecutionResult", "ExecutionService"]

log = get_logger(__name__)

#: Outcomes that mean "an order we transmitted might still be queued at the
#: broker".  ``PENDING`` is an attempt whose result was never recorded, and
#: ``AMBIGUOUS`` is one whose result is unknown; both have to be counted against
#: the per-ticker pending limit, because both may correspond to a real order.
_UNRESOLVED_OUTCOMES = (ExecutionOutcome.PENDING, ExecutionOutcome.AMBIGUOUS)

#: Broker order statuses that mean the order is finished and the position (or
#: the lack of one) is now the account's truth.
_FILLED_STATUSES: frozenset[str] = frozenset({"FILLED"})
_DEAD_STATUSES: frozenset[str] = frozenset({"CANCELLED", "REJECTED"})


@dataclass(slots=True)
class ExecutionResult:
    """What one execution job did.  Every field is also persisted."""

    proposal_id: uuid.UUID
    transmitted: bool = False
    outcome: ExecutionOutcome | None = None
    attempt_id: uuid.UUID | None = None
    broker_order_id: str | None = None
    status: ProposalStatus | None = None
    reason: str = ""
    failure: ExecutionFailure | None = None
    reconcile_required: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


class ExecutionService:
    """Drives an authorized proposal to exactly one broker transmission."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        proposals: ProposalService,
        provider: BrokerExecutionProvider,
        control: ControlStateService,
        broker: Broker = Broker.TRADING212,
    ) -> None:
        self.database = database
        self.settings = settings
        self.proposals = proposals
        self.provider = provider
        self.control = control
        self.broker = broker
        self.queue = JobQueue()
        self.preflight = ExecutionPreflight(
            settings,
            proposals=proposals,
            control=control,
            provider_environment=provider.environment,
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    async def execute(
        self, proposal_id: uuid.UUID, *, now: dt.datetime | None = None
    ) -> ExecutionResult:
        moment = now or utcnow()

        async with self.database.session() as session:
            proposal = await session.get(TradeProposal, proposal_id)
            if proposal is None:
                return ExecutionResult(proposal_id=proposal_id, reason="proposal not found")
            sent = await self._sent_attempt(session, proposal_id)
            status = proposal.status

        if sent is not None:
            # Somebody -- possibly this process, before it died -- already
            # recorded a transmission. There is exactly one safe response.
            return await self._recover_sent_attempt(sent.id, proposal_id, moment)

        if status not in EXECUTABLE_STATUSES:
            return ExecutionResult(
                proposal_id=proposal_id,
                status=status,
                reason=f"the proposal is {status.value} and is not executable",
            )

        async with self.database.session() as session:
            proposal = await session.get(TradeProposal, proposal_id)
            assert proposal is not None
            outcome = await self.preflight.run(proposal, now=moment, sent_attempt_exists=False)
            command = _command_from(proposal, self.settings)
            observed_version = proposal.version

        if outcome.refusal is not None:
            return await self._record_refusal(
                proposal_id, command, outcome.refusal, outcome, moment
            )

        # The broker's per-ticker pending-order limit, checked before anything
        # is reserved. A read, and a refusal by default: see
        # `_pending_order_refusal`.
        pending_refusal = await self._pending_order_refusal(command, moment)
        if pending_refusal is not None:
            return await self._record_refusal(
                proposal_id, command, pending_refusal, outcome, moment
            )

        # Before the send transaction, so a denial is provably pre-send.
        if not await self.provider.reserve_slot():
            return await self._record_refusal(
                proposal_id,
                command,
                PreflightRefusal(
                    category=ExecutionFailure.RATE_LIMITED_LOCALLY,
                    reasons=(
                        "the local Trading 212 order rate limiter had no token available; "
                        "the order was not transmitted and will be retried",
                    ),
                    invalidates=False,
                ),
                outcome,
                moment,
            )

        assert outcome.revalidation is not None and outcome.control is not None
        claim = await self._claim_and_mark_sent(
            proposal_id=proposal_id,
            command=command,
            revalidation=outcome.revalidation,
            control=outcome.control,
            observed_version=observed_version,
            now=moment,
        )
        if claim is None:
            return ExecutionResult(
                proposal_id=proposal_id,
                reason="another actor changed or claimed this proposal before transmission",
            )
        attempt_id, snapshot = claim

        # ------------------------------------------------------------------
        # THE transmission. One call. No retry wraps this, and none may be added.
        # ------------------------------------------------------------------
        try:
            acknowledgement = await self.provider.submit(command)
        except DefinitePreSendFailure as exc:
            return await self._retract_unsent(attempt_id, proposal_id, exc, moment)
        except BrokerRejection as exc:
            return await self._record_rejection(attempt_id, proposal_id, exc, moment)
        except AmbiguousTransportFailure as exc:
            return await self._record_ambiguous(
                attempt_id,
                proposal_id,
                category=_ambiguous_category(exc),
                detail=str(exc),
                now=moment,
            )
        except Exception as exc:
            # An unexpected exception after the request may or may not have left.
            # Ambiguity is the only honest answer, and it is also the safe one.
            log.exception(
                "execution_unexpected_error",
                proposal_id=str(proposal_id),
                error_type=type(exc).__name__,
            )
            return await self._record_ambiguous(
                attempt_id,
                proposal_id,
                category=ExecutionFailure.TRANSPORT_AMBIGUOUS,
                detail=f"unexpected {type(exc).__name__} during transmission",
                now=moment,
            )

        assert snapshot is not None
        return await self._record_success(attempt_id, proposal_id, acknowledgement, moment)

    # ------------------------------------------------------------------
    # The send transaction
    # ------------------------------------------------------------------
    async def _claim_and_mark_sent(
        self,
        *,
        proposal_id: uuid.UUID,
        command: ExecutionCommand,
        revalidation: Revalidation,
        control: ControlSnapshot,
        observed_version: int,
        now: dt.datetime,
    ) -> tuple[uuid.UUID, dict[str, Any]] | None:
        """Record "bytes may have left" before they can.

        Returns ``None`` when the claim was lost -- the proposal moved, the kill
        switch came on between the preflight and here, or another worker got
        there first.  Losing a claim is never an error; it is the mechanism.
        """
        async with self.database.transaction() as session:
            # One transmission per proposal, serialised across processes.
            await self._lock_proposal(session, proposal_id)

            locked = (
                await session.execute(
                    sa.select(TradeProposal)
                    .where(TradeProposal.id == proposal_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked is None:  # pragma: no cover - selected a moment ago
                return None
            if locked.version != observed_version:
                log.info(
                    "execution_claim_lost",
                    proposal_id=str(proposal_id),
                    reason="the proposal changed during preflight",
                )
                return None
            if locked.status not in EXECUTABLE_STATUSES or locked.expires_at <= now:
                return None
            if await self._sent_attempt(session, proposal_id) is not None:
                return None

            # The last kill/pause check, from the database, inside the same
            # transaction that is about to authorise transmission. An operator
            # who hits the switch during the preflight still stops this order.
            fresh_control = await self.control.snapshot()
            if fresh_control.trading_halted:
                log.warning(
                    "execution_halted_before_send",
                    proposal_id=str(proposal_id),
                    blockers=fresh_control.blockers,
                )
                return None

            snapshot = _execution_snapshot(
                proposal=locked,
                command=command,
                revalidation=revalidation,
                control=fresh_control,
                settings=self.settings,
                provider_environment=self.provider.environment,
                now=now,
            )
            attempt_number = await self._next_attempt_number(session, proposal_id)
            attempt = ExecutionAttempt(
                proposal_id=proposal_id,
                attempt_number=attempt_number,
                started_at=now,
                preflight_at=now,
                broker_environment=locked.broker_environment,
                request_payload=_request_payload(command),
                request_fingerprint=fingerprint_for(command),
                execution_snapshot=snapshot,
                # Set before the request, never after the response.
                sent_to_broker=True,
                sent_at=now,
                outcome=ExecutionOutcome.PENDING,
                ambiguous=False,
                actor_identifier=locked.approved_by,
            )
            session.add(attempt)

            assert_transition(locked.status, ProposalStatus.EXECUTING)
            locked.status = ProposalStatus.EXECUTING
            locked.status_reason = "an order is being transmitted to the broker"
            locked.updated_at = now

            session.add(
                AuditLog(
                    actor_type=ActorType.SYSTEM,
                    actor_id="system:execution",
                    action="execution.transmitting",
                    entity_type="trade_proposal",
                    entity_id=proposal_id,
                    details={
                        "broker_environment": locked.broker_environment,
                        "broker_ticker": command.broker_ticker,
                        "side": command.side.value,
                        "quantity": str(command.quantity),
                        "request_fingerprint": attempt.request_fingerprint,
                        "authorization_source": (
                            locked.authorization_source.value
                            if locked.authorization_source
                            else None
                        ),
                    },
                )
            )
            try:
                await session.flush()
            except IntegrityError:
                # `uq_execution_attempts_sent_once`. Another worker committed a
                # transmitted attempt between the lock and here -- which the lock
                # should prevent, so this is the database having the last word.
                await session.rollback()
                log.warning("execution_claim_lost_at_index", proposal_id=str(proposal_id))
                return None
            return attempt.id, snapshot

    # ------------------------------------------------------------------
    # Outcome recording
    # ------------------------------------------------------------------
    async def _record_success(
        self,
        attempt_id: uuid.UUID,
        proposal_id: uuid.UUID,
        acknowledgement: BrokerAcknowledgement,
        now: dt.datetime,
    ) -> ExecutionResult:
        order = acknowledgement.order
        broker_status = (order.status or "").upper()
        filled = broker_status in _FILLED_STATUSES
        dead = broker_status in _DEAD_STATUSES

        async with self.database.transaction() as session:
            attempt = await self._lock_attempt(session, attempt_id)
            proposal = await self._lock_proposal_row(session, proposal_id)
            if attempt is None or proposal is None:  # pragma: no cover - defensive
                return ExecutionResult(proposal_id=proposal_id, reason="attempt vanished")

            attempt.outcome = ExecutionOutcome.SUBMITTED
            attempt.ambiguous = False
            attempt.http_status = acknowledgement.http_status
            attempt.broker_order_id = order.broker_order_id
            attempt.response_payload = _safe_payload(acknowledgement.payload)
            attempt.rate_limit_headers = acknowledgement.rate_limit
            attempt.completed_at = now
            attempt.error = None
            attempt.error_category = None

            await self._mirror_order(session, proposal, attempt, order, now)

            if filled:
                assert_transition(proposal.status, ProposalStatus.EXECUTED)
                proposal.status = ProposalStatus.EXECUTED
                proposal.status_reason = "the broker reported the order filled"
                proposal.executed_at = now
            elif dead:
                assert_transition(proposal.status, ProposalStatus.FAILED)
                proposal.status = ProposalStatus.FAILED
                proposal.status_reason = f"the broker ended the order as {broker_status}"
            else:
                # Acknowledged and working. The proposal stays EXECUTING, which
                # keeps its exposure reserved -- an unfilled order is committed
                # cash the account snapshot does not show yet.
                proposal.status_reason = (
                    f"the broker accepted the order (status {broker_status or 'unknown'})"
                )
            proposal.updated_at = now

            session.add(
                AuditLog(
                    actor_type=ActorType.BROKER,
                    actor_id=order.broker_order_id,
                    action="execution.submitted",
                    entity_type="trade_proposal",
                    entity_id=proposal_id,
                    details={
                        "broker_order_id": order.broker_order_id,
                        "broker_status": order.status,
                        "http_status": acknowledgement.http_status,
                        "filled_quantity": str(order.filled_quantity or Decimal(0)),
                        "initiated_from": order.initiated_from,
                    },
                )
            )
            await self.proposals.enqueue_notification(
                session, proposal_id, NotificationEvent.EXECUTION_SUBMITTED
            )
            if not filled and not dead:
                # A working order still has an outcome to learn.
                await self._enqueue_reconcile(session, attempt_id)
            final_status = proposal.status

        METRICS.inc("stockbrain_execution_attempts_total", labels={"outcome": "SUBMITTED"})
        log.info(
            "execution_submitted",
            proposal_id=str(proposal_id),
            broker_order_id=order.broker_order_id,
            broker_status=order.status,
            broker_environment=self.provider.environment,
        )
        return ExecutionResult(
            proposal_id=proposal_id,
            transmitted=True,
            outcome=ExecutionOutcome.SUBMITTED,
            attempt_id=attempt_id,
            broker_order_id=order.broker_order_id,
            status=final_status,
            reason="the broker accepted the order",
            reconcile_required=not filled and not dead,
        )

    async def _record_rejection(
        self,
        attempt_id: uuid.UUID,
        proposal_id: uuid.UUID,
        exc: BrokerRejection,
        now: dt.datetime,
    ) -> ExecutionResult:
        """The broker answered and refused.  Definitive: no order exists.

        The reservation is released, because there is nothing at the broker to
        reserve against -- which is the one failure shape where that is provably
        true.
        """
        async with self.database.transaction() as session:
            attempt = await self._lock_attempt(session, attempt_id)
            proposal = await self._lock_proposal_row(session, proposal_id)
            if attempt is None or proposal is None:  # pragma: no cover - defensive
                return ExecutionResult(proposal_id=proposal_id, reason="attempt vanished")
            attempt.outcome = ExecutionOutcome.REJECTED_BY_BROKER
            attempt.ambiguous = False
            attempt.http_status = exc.status
            error = str(exc)
            if exc.detail:
                error = f"{error} — {exc.detail}"
            attempt.error = error[:1000]
            attempt.error_category = exc.category
            if exc.payload is not None:
                attempt.response_payload = _safe_payload(exc.payload)
            attempt.completed_at = now

            assert_transition(proposal.status, ProposalStatus.FAILED)
            proposal.status = ProposalStatus.FAILED
            proposal.status_reason = f"the broker refused the order (HTTP {exc.status})"
            proposal.updated_at = now
            audit_details: dict[str, Any] = {
                "http_status": exc.status,
                "category": exc.category,
            }
            if exc.detail:
                audit_details["detail"] = exc.detail
            session.add(
                AuditLog(
                    actor_type=ActorType.BROKER,
                    actor_id="trading212",
                    action="execution.rejected",
                    entity_type="trade_proposal",
                    entity_id=proposal_id,
                    details=audit_details,
                )
            )
            notification_detail = f"HTTP {exc.status} ({exc.category})"
            if exc.detail:
                notification_detail = f"{notification_detail} — {exc.detail}"
            await self.proposals.enqueue_notification(
                session,
                proposal_id,
                NotificationEvent.EXECUTION_REJECTED,
                detail=notification_detail,
            )

            # A quantity-precision refusal is the broker teaching us the one
            # number it publishes nowhere.  Learn it on the instrument, release
            # the failed proposal's dedupe key and let the thesis generate a
            # *new* proposal -- never a re-send of the rejected attempt.
            precision = _quantity_precision_from(exc)
            retry_run_id: uuid.UUID | None = None
            if precision is not None and proposal.broker_instrument_id is not None:
                stored = await session.scalar(
                    sa.select(BrokerInstrument.quantity_precision).where(
                        BrokerInstrument.id == proposal.broker_instrument_id
                    )
                )
                if stored != precision:
                    await session.execute(
                        sa.update(BrokerInstrument)
                        .where(BrokerInstrument.id == proposal.broker_instrument_id)
                        .values(quantity_precision=precision)
                    )
                    proposal.dedupe_key = None
                    proposal.status_reason = (
                        f"the broker requires {precision} decimal places on quantity; re-sizing"
                    )
                    retry_run_id = proposal.research_run_id
                    log.info(
                        "quantity_precision_learned",
                        ticker=proposal.broker_ticker,
                        precision=precision,
                    )

        if retry_run_id is not None:
            # Outside the transaction: the learned precision and the freed
            # dedupe key are durable before any worker can act on the retry.
            await self.proposals.enqueue_for_run(retry_run_id)

        METRICS.inc("stockbrain_execution_attempts_total", labels={"outcome": "REJECTED_BY_BROKER"})
        log.warning(
            "execution_rejected",
            proposal_id=str(proposal_id),
            http_status=exc.status,
            category=exc.category,
        )
        return ExecutionResult(
            proposal_id=proposal_id,
            transmitted=True,
            outcome=ExecutionOutcome.REJECTED_BY_BROKER,
            attempt_id=attempt_id,
            status=ProposalStatus.FAILED,
            reason=f"the broker refused the order (HTTP {exc.status})",
            failure=ExecutionFailure(exc.category),
        )

    async def _retract_unsent(
        self,
        attempt_id: uuid.UUID,
        proposal_id: uuid.UUID,
        exc: DefinitePreSendFailure,
        now: dt.datetime,
    ) -> ExecutionResult:
        """The one place ``sent_to_broker`` is ever set back to false.

        Reached only from :class:`~stockbrain.errors.DefinitePreSendFailure`,
        which the Trading 212 client raises only for httpx exceptions that
        cannot occur after a request line is written: a DNS failure, a refused
        connection, a TLS handshake failure, a connection-pool timeout.

        Retracting is not a weakening of ``uq_execution_attempts_sent_once``.
        The flag records "bytes may have left"; here we have proof they did not,
        so leaving it set would permanently strand an authorized proposal the
        broker has never heard of.  The proposal returns to ``APPROVED`` -- the
        only route back, and the only caller of ``PRESEND_RECOVERY_TARGET``.
        """
        async with self.database.transaction() as session:
            attempt = await self._lock_attempt(session, attempt_id)
            proposal = await self._lock_proposal_row(session, proposal_id)
            if attempt is None or proposal is None:  # pragma: no cover - defensive
                return ExecutionResult(proposal_id=proposal_id, reason="attempt vanished")
            attempt.sent_to_broker = False
            attempt.sent_at = None
            attempt.outcome = ExecutionOutcome.FAILED_BEFORE_SEND
            attempt.ambiguous = False
            attempt.error = str(exc)[:1000]
            attempt.error_category = ExecutionFailure.CONNECT_FAILED.value
            attempt.completed_at = now

            assert_transition(proposal.status, PRESEND_RECOVERY_TARGET)
            proposal.status = PRESEND_RECOVERY_TARGET
            proposal.status_reason = (
                "the order was provably not transmitted; the authorization still stands"
            )
            proposal.updated_at = now
            session.add(
                AuditLog(
                    actor_type=ActorType.SYSTEM,
                    actor_id="system:execution",
                    action="execution.retracted_unsent",
                    entity_type="trade_proposal",
                    entity_id=proposal_id,
                    details={
                        "category": ExecutionFailure.CONNECT_FAILED.value,
                        "sent_to_broker_retracted": True,
                        "proof": "the transport failed before a request line was written",
                    },
                )
            )
            await self.proposals.enqueue_notification(
                session,
                proposal_id,
                NotificationEvent.EXECUTION_FAILED,
                detail="the order was not transmitted; the proposal is still authorized",
            )

        METRICS.inc("stockbrain_execution_attempts_total", labels={"outcome": "FAILED_BEFORE_SEND"})
        log.warning("execution_failed_before_send", proposal_id=str(proposal_id))
        return ExecutionResult(
            proposal_id=proposal_id,
            transmitted=False,
            outcome=ExecutionOutcome.FAILED_BEFORE_SEND,
            attempt_id=attempt_id,
            status=PRESEND_RECOVERY_TARGET,
            reason="the order was provably not transmitted",
            failure=ExecutionFailure.CONNECT_FAILED,
        )

    async def _record_ambiguous(
        self,
        attempt_id: uuid.UUID,
        proposal_id: uuid.UUID,
        *,
        category: ExecutionFailure,
        detail: str,
        now: dt.datetime,
    ) -> ExecutionResult:
        """The order may or may not exist.  Nothing is resent, ever.

        The proposal keeps reserving its exposure: until reconciliation says
        otherwise, the cash may be committed at the broker, and releasing it
        would let the next proposal be sized against money that is already
        spent.
        """
        async with self.database.transaction() as session:
            attempt = await self._lock_attempt(session, attempt_id)
            proposal = await self._lock_proposal_row(session, proposal_id)
            if attempt is None or proposal is None:  # pragma: no cover - defensive
                return ExecutionResult(proposal_id=proposal_id, reason="attempt vanished")
            attempt.outcome = ExecutionOutcome.AMBIGUOUS
            attempt.ambiguous = True
            attempt.error = detail[:1000]
            attempt.error_category = category.value
            attempt.completed_at = now

            if proposal.status is not ProposalStatus.EXECUTION_AMBIGUOUS:
                assert_transition(proposal.status, ProposalStatus.EXECUTION_AMBIGUOUS)
                proposal.status = ProposalStatus.EXECUTION_AMBIGUOUS
            proposal.status_reason = (
                "the order may or may not have reached the broker; reconciliation required"
            )
            proposal.updated_at = now
            session.add(
                AuditLog(
                    actor_type=ActorType.SYSTEM,
                    actor_id="system:execution",
                    action="execution.ambiguous",
                    entity_type="trade_proposal",
                    entity_id=proposal_id,
                    details={
                        "category": category.value,
                        "resend_permitted": False,
                        "reconciliation_required": True,
                    },
                )
            )
            await self._enqueue_reconcile(session, attempt_id)
            await self.proposals.enqueue_notification(
                session,
                proposal_id,
                NotificationEvent.EXECUTION_AMBIGUOUS,
                detail=f"{category.value}: do not resend, reconciliation required",
            )

        METRICS.inc("stockbrain_execution_attempts_total", labels={"outcome": "AMBIGUOUS"})
        log.error(
            "execution_ambiguous",
            proposal_id=str(proposal_id),
            category=category.value,
            resend_permitted=False,
        )
        return ExecutionResult(
            proposal_id=proposal_id,
            transmitted=True,
            outcome=ExecutionOutcome.AMBIGUOUS,
            attempt_id=attempt_id,
            status=ProposalStatus.EXECUTION_AMBIGUOUS,
            reason="the order may or may not have reached the broker",
            failure=category,
            reconcile_required=True,
        )

    async def _record_refusal(
        self,
        proposal_id: uuid.UUID,
        command: ExecutionCommand,
        refusal: PreflightRefusal,
        outcome: Any,
        now: dt.datetime,
    ) -> ExecutionResult:
        """Persist a refusal that never touched the network.

        The attempt row exists even though nothing was sent, because "we
        declined, here is why, at this exact time" is the record an operator
        needs when a proposal quietly does not execute.
        """
        async with self.database.transaction() as session:
            proposal = await self._lock_proposal_row(session, proposal_id)
            if proposal is None:  # pragma: no cover - defensive
                return ExecutionResult(proposal_id=proposal_id, reason="proposal vanished")
            attempt = ExecutionAttempt(
                proposal_id=proposal_id,
                attempt_number=await self._next_attempt_number(session, proposal_id),
                started_at=now,
                preflight_at=now,
                broker_environment=proposal.broker_environment,
                request_payload=_request_payload(command),
                request_fingerprint=fingerprint_for(command),
                execution_snapshot={
                    "refused": True,
                    "category": refusal.category.value,
                    "reasons": list(refusal.reasons),
                    "rule_ids": list(refusal.rule_ids),
                    "preflight_at": now.isoformat(),
                },
                sent_to_broker=False,
                outcome=ExecutionOutcome.FAILED_BEFORE_SEND,
                ambiguous=False,
                error=refusal.detail[:1000],
                error_category=refusal.category.value,
                completed_at=now,
            )
            session.add(attempt)

            if refusal.invalidates and proposal.status in EXECUTABLE_STATUSES:
                assert_transition(proposal.status, ProposalStatus.INVALIDATED)
                proposal.status = ProposalStatus.INVALIDATED
                proposal.status_reason = refusal.detail[:1000]
                proposal.invalidated_at = now
                proposal.invalidation_reason = refusal.detail[:1000]
                proposal.updated_at = now
            session.add(
                AuditLog(
                    actor_type=ActorType.SYSTEM,
                    actor_id="system:execution",
                    action="execution.refused",
                    entity_type="trade_proposal",
                    entity_id=proposal_id,
                    details={
                        "category": refusal.category.value,
                        "reasons": [reason[:300] for reason in refusal.reasons][:10],
                        "invalidated": refusal.invalidates,
                        "transmitted": False,
                    },
                )
            )
            if refusal.invalidates:
                await self.proposals.enqueue_notification(
                    session,
                    proposal_id,
                    NotificationEvent.PROPOSAL_INVALIDATED,
                    detail=refusal.detail,
                )
            attempt_id = attempt.id
            final_status = proposal.status

        METRICS.inc(
            "stockbrain_execution_attempts_total",
            labels={"outcome": "FAILED_BEFORE_SEND"},
        )
        log.info(
            "execution_refused",
            proposal_id=str(proposal_id),
            category=refusal.category.value,
            invalidated=refusal.invalidates,
            transmitted=False,
        )
        return ExecutionResult(
            proposal_id=proposal_id,
            transmitted=False,
            outcome=ExecutionOutcome.FAILED_BEFORE_SEND,
            attempt_id=attempt_id,
            status=final_status,
            reason=refusal.detail,
            failure=refusal.category,
        )

    # ------------------------------------------------------------------
    # Crash recovery
    # ------------------------------------------------------------------
    async def _recover_sent_attempt(
        self, attempt_id: uuid.UUID, proposal_id: uuid.UUID, now: dt.datetime
    ) -> ExecutionResult:
        """A transmitted attempt was found before this job could send anything.

        Either the process died mid-flight or another worker is ahead of us.
        Both answers are the same: do not send, and let reconciliation decide.
        """
        async with self.database.session() as session:
            attempt = await session.get(ExecutionAttempt, attempt_id)
        if attempt is None:  # pragma: no cover - defensive
            return ExecutionResult(proposal_id=proposal_id, reason="attempt vanished")
        if attempt.outcome is not ExecutionOutcome.PENDING:
            return ExecutionResult(
                proposal_id=proposal_id,
                transmitted=True,
                outcome=attempt.outcome,
                attempt_id=attempt_id,
                broker_order_id=attempt.broker_order_id,
                reason=("an execution attempt has already been transmitted for this proposal"),
                reconcile_required=not attempt.outcome.is_terminal,
            )
        return await self._record_ambiguous(
            attempt_id,
            proposal_id,
            category=ExecutionFailure.CRASH_RECOVERY,
            detail=(
                "a transmitted attempt was found with no recorded result; the process "
                "stopped between recording the send and recording the outcome"
            ),
            now=now,
        )

    async def recover_incomplete(self, *, now: dt.datetime | None = None) -> int:
        """Sweep attempts stranded mid-flight by a crash.

        Runs on the scheduler, not only at startup, because a worker can die
        without the process doing so.  Marking them ambiguous is the whole
        action: the order is never resent.
        """
        moment = now or utcnow()
        cutoff = moment - dt.timedelta(seconds=self.settings.t212_order_timeout_seconds * 2)
        async with self.database.session() as session:
            stranded = list(
                (
                    await session.execute(
                        sa.select(ExecutionAttempt.id, ExecutionAttempt.proposal_id).where(
                            ExecutionAttempt.sent_to_broker.is_(True),
                            ExecutionAttempt.outcome == ExecutionOutcome.PENDING,
                            ExecutionAttempt.sent_at < cutoff,
                        )
                    )
                ).all()
            )
        for attempt_id, proposal_id in stranded:
            await self._record_ambiguous(
                attempt_id,
                proposal_id,
                category=ExecutionFailure.CRASH_RECOVERY,
                detail="a transmitted attempt had no recorded result after a restart",
                now=moment,
            )
        if stranded:
            log.warning("execution_crash_recovery", stranded=len(stranded))
        return len(stranded)

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    async def enqueue_ready(self, limit: int = 10) -> int:
        """Queue execution for authorized proposals that have not been sent.

        Derived from the database rather than from an in-memory list, so a
        restart resumes exactly where it left off, and the job dedupe key makes
        a second enqueue a no-op.  Both MANUAL and AUTOMATIC proposals arrive
        here identically: reaching ``APPROVED`` *is* the licence to send,
        whoever granted it.
        """
        if self.settings.order_transmission_blockers:
            return 0
        if (await self.control.snapshot()).trading_halted:
            return 0

        enqueued = 0
        async with self.database.transaction() as session:
            candidates = list(
                (
                    await session.execute(
                        sa.select(TradeProposal.id)
                        .where(
                            TradeProposal.status.in_(tuple(EXECUTABLE_STATUSES)),
                            TradeProposal.broker == self.broker,
                            TradeProposal.broker_environment == self.settings.t212_env.value,
                            TradeProposal.expires_at > utcnow(),
                            ~sa.exists(
                                sa.select(ExecutionAttempt.id).where(
                                    ExecutionAttempt.proposal_id == TradeProposal.id,
                                    ExecutionAttempt.sent_to_broker.is_(True),
                                )
                            ),
                        )
                        .order_by(TradeProposal.approved_at)
                        .limit(limit)
                    )
                ).scalars()
            )
            for proposal_id in candidates:
                job_id = await self.queue.enqueue(
                    session,
                    JobType.EXECUTE_PROPOSAL,
                    payload={"proposal_id": str(proposal_id)},
                    dedupe_key=f"execute:{proposal_id}",
                    priority=5,
                )
                if job_id is not None:
                    enqueued += 1
        if enqueued:
            log.info("execution_enqueued", count=enqueued)
        return enqueued

    async def _enqueue_reconcile(self, session: AsyncSession, attempt_id: uuid.UUID) -> None:
        await self.queue.enqueue(
            session,
            JobType.RECONCILE_EXECUTION,
            payload={"attempt_id": str(attempt_id)},
            dedupe_key=f"reconcile:{attempt_id}",
            priority=1,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _pending_order_refusal(
        self, command: ExecutionCommand, now: dt.datetime
    ) -> PreflightRefusal | None:
        """Refuse the send if the broker's per-ticker order queue is full.

        Trading 212 documents a functional limit of **50 pending orders per
        ticker per account**, and Phase 8's live test proved orders really do
        queue: a market order placed while the market was closed came back HTTP
        200 with status ``NEW``.  The fifty-first submission would be rejected
        by the broker, and provoking a rejection from a non-idempotent endpoint
        is a worse outcome than declining to call it.

        The count comes from **two** independent sources and the larger wins:

        * what the broker says is pending for the ticker, which is the number
          the limit is actually measured against and the only one that includes
          orders the operator queued by hand in the app;
        * what StockBrain's own ledger says it has transmitted and not yet
          resolved for that ticker in this environment, which covers the window
          between a successful POST and the broker's list catching up, and
          covers an attempt whose outcome is still ambiguous.

        A read that failed is **not** a count of zero.  ``read_ok=False``
        refuses, because an unknown standing between us and a non-idempotent
        POST resolves against sending.

        The refusal does not invalidate the proposal: a full queue is a
        condition of the moment, and the operator's authorization is still good
        once it drains.
        """
        limit = self.settings.t212_max_pending_orders_per_ticker
        headroom = self.settings.t212_pending_order_headroom
        ceiling = max(0, limit - headroom)

        broker_count = await self.provider.count_pending(command.broker_ticker)
        local_count = await self._unresolved_sent_for_ticker(command.broker_ticker)
        observed = max(broker_count.pending, local_count)

        if not broker_count.read_ok:
            return PreflightRefusal(
                category=ExecutionFailure.PENDING_ORDER_LIMIT,
                reasons=(
                    f"the broker's pending-order list for {command.broker_ticker} could not "
                    f"be read ({broker_count.error_category or 'unknown error'}), so the "
                    f"{limit}-per-ticker limit cannot be checked; the order was not "
                    f"transmitted",
                ),
                invalidates=False,
            )

        if observed + 1 > ceiling:
            log.warning(
                "pending_order_limit_reached",
                broker_ticker=command.broker_ticker,
                broker_pending=broker_count.pending,
                api_initiated=broker_count.api_initiated,
                local_unresolved=local_count,
                ceiling=ceiling,
                documented_limit=limit,
            )
            METRICS.inc(
                "stockbrain_execution_pending_limit_refusals_total",
                labels={"broker_ticker": command.broker_ticker},
            )
            return PreflightRefusal(
                category=ExecutionFailure.PENDING_ORDER_LIMIT,
                reasons=(
                    f"{observed} order(s) are already pending for {command.broker_ticker} "
                    f"({broker_count.pending} at the broker, {local_count} unresolved here); "
                    f"one more would pass the {ceiling} ceiling StockBrain keeps below the "
                    f"broker's documented {limit}-per-ticker limit",
                ),
                invalidates=False,
            )
        return None

    async def _unresolved_sent_for_ticker(self, broker_ticker: str) -> int:
        """Attempts this deployment transmitted for a ticker and has not resolved.

        Counted from ``execution_attempts`` rather than from ``broker_orders``,
        because the question is "what might be queued because of us", and an
        attempt whose outcome is ``PENDING`` or ``AMBIGUOUS`` might be.
        Environment-scoped: a demo attempt says nothing about the live queue.
        """
        async with self.database.session() as session:
            count = await session.scalar(
                sa.select(sa.func.count())
                .select_from(ExecutionAttempt)
                .join(TradeProposal, TradeProposal.id == ExecutionAttempt.proposal_id)
                .where(
                    ExecutionAttempt.sent_to_broker.is_(True),
                    ExecutionAttempt.outcome.in_(_UNRESOLVED_OUTCOMES),
                    ExecutionAttempt.broker_environment == self.settings.t212_env.value,
                    TradeProposal.broker_ticker == broker_ticker,
                )
            )
        return int(count or 0)

    async def _lock_proposal(self, session: AsyncSession, proposal_id: uuid.UUID) -> None:
        key = f"stockbrain:execution:{self.broker.value}:{proposal_id}"
        await session.execute(
            sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(key, 0)))
        )

    @staticmethod
    async def _lock_proposal_row(
        session: AsyncSession, proposal_id: uuid.UUID
    ) -> TradeProposal | None:
        return (
            await session.execute(
                sa.select(TradeProposal).where(TradeProposal.id == proposal_id).with_for_update()
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _lock_attempt(
        session: AsyncSession, attempt_id: uuid.UUID
    ) -> ExecutionAttempt | None:
        return (
            await session.execute(
                sa.select(ExecutionAttempt)
                .where(ExecutionAttempt.id == attempt_id)
                .with_for_update()
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _sent_attempt(
        session: AsyncSession, proposal_id: uuid.UUID
    ) -> ExecutionAttempt | None:
        return (
            (
                await session.execute(
                    sa.select(ExecutionAttempt).where(
                        ExecutionAttempt.proposal_id == proposal_id,
                        ExecutionAttempt.sent_to_broker.is_(True),
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def _next_attempt_number(session: AsyncSession, proposal_id: uuid.UUID) -> int:
        highest = await session.scalar(
            sa.select(sa.func.max(ExecutionAttempt.attempt_number)).where(
                ExecutionAttempt.proposal_id == proposal_id
            )
        )
        return int(highest or 0) + 1

    @staticmethod
    async def _mirror_order(
        session: AsyncSession,
        proposal: TradeProposal,
        attempt: ExecutionAttempt,
        order: Any,
        now: dt.datetime,
    ) -> None:
        """Mirror the broker's own record of the order locally.

        The broker stays authoritative; this is a derived copy so the GUI, the
        audit trail and reconciliation can read one consistent picture without
        spending a rate-limited request each time.
        """
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
        row.raw = _safe_payload(order.raw)
        if existing is None:
            session.add(row)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _command_from(proposal: TradeProposal, settings: Settings) -> ExecutionCommand:
    """Build the command from the persisted row and nothing else.

    Every field is read off the proposal.  There is no parameter here a caller
    could supply, which is what makes "the frontend cannot substitute an
    execution field" and "Telegram cannot choose a quantity" structural.
    """
    return ExecutionCommand(
        proposal_id=proposal.id,
        broker=proposal.broker,
        broker_environment=proposal.broker_environment,
        broker_ticker=proposal.broker_ticker,
        side=proposal.side,
        order_type=proposal.order_type,
        quantity=proposal.proposed_quantity,
        extended_hours=settings.t212_order_extended_hours,
    )


def _request_payload(command: ExecutionCommand) -> dict[str, Any]:
    """The non-secret request snapshot persisted with the attempt.

    Contains no header, no credential and no base URL -- only the trade.
    """
    return command.as_dict()


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip anything that could carry a credential out of a stored payload.

    Trading 212's order responses do not contain one, but a response body is
    provider-controlled and this table is read by the API and the GUI.
    """
    forbidden = ("authorization", "token", "secret", "password", "api_key", "apikey", "cookie")
    return {
        key: value
        for key, value in payload.items()
        if not any(fragment in key.lower() for fragment in forbidden)
    }


def _quantity_precision_from(exc: BrokerRejection) -> int | None:
    """The decimal places a ``quantity-precision-mismatch`` refusal demands.

    Trading 212 publishes no precision and its metadata carries none, so the
    broker's own sentence is the only source.  ``None`` for every other refusal,
    so an unrelated 400 is never read as a precision to learn.
    """
    payload_type = exc.payload.get("type") if exc.payload else None
    if not isinstance(payload_type, str) or not payload_type.endswith(
        "quantity-precision-mismatch"
    ):
        return None
    if not exc.detail:
        return None
    match = re.search(r"precision (\d+)", exc.detail)
    return int(match.group(1)) if match else None


def _ambiguous_category(exc: AmbiguousTransportFailure) -> ExecutionFailure:
    """Read the category the client already decided, without re-deriving it."""
    text = str(exc)
    for category in (
        ExecutionFailure.UNREADABLE_SUCCESS,
        ExecutionFailure.BROKER_TIMEOUT,
        ExecutionFailure.BROKER_RATE_LIMITED,
        ExecutionFailure.UNEXPECTED_STATUS,
    ):
        if category.value in text:
            return category
    if "could not be parsed" in text:
        return ExecutionFailure.UNREADABLE_SUCCESS
    return ExecutionFailure.TRANSPORT_AMBIGUOUS


def _execution_snapshot(
    *,
    proposal: TradeProposal,
    command: ExecutionCommand,
    revalidation: Revalidation,
    control: ControlSnapshot,
    settings: Settings,
    provider_environment: str,
    now: dt.datetime,
) -> dict[str, Any]:
    """Everything the transmission was decided on, frozen at the moment of it.

    Written once and never updated.  Deliberately *not* folded into the
    proposal's own columns: a proposal records the decision that was made, and
    overwriting it with the newer world an order was sent into would destroy the
    only record of what the operator actually approved.
    """
    quote = revalidation.quote
    account = revalidation.account
    position = account.position(proposal.broker_ticker) if account else None
    return {
        "captured_at": now.isoformat(),
        "proposal_id": str(proposal.id),
        "broker": proposal.broker.value,
        "broker_environment": proposal.broker_environment,
        "provider_environment": provider_environment,
        "configured_environment": settings.t212_env.value,
        "broker_instrument_id": (
            str(proposal.broker_instrument_id) if proposal.broker_instrument_id else None
        ),
        "broker_ticker": proposal.broker_ticker,
        "side": proposal.side.value,
        "order_type": proposal.order_type.value,
        "quantity": str(proposal.proposed_quantity),
        "signed_quantity": str(command.signed_quantity),
        "extended_hours": command.extended_hours,
        "reference_price": str(proposal.reference_price),
        "reference_currency": proposal.reference_currency,
        "estimated_notional": str(proposal.estimated_notional),
        "account_currency": proposal.account_currency,
        "account_id": proposal.account_id,
        "authorization_source": (
            proposal.authorization_source.value if proposal.authorization_source else None
        ),
        "authorized_at": proposal.approved_at.isoformat() if proposal.approved_at else None,
        "authorized_by": proposal.approved_by,
        "execution_policy": proposal.execution_policy.value,
        "risk_policy_version": revalidation.decision.policy_version,
        "risk_snapshot_hash": revalidation.decision.snapshot_hash(),
        "preflight_at": now.isoformat(),
        "quote": (
            {
                "bid": str(quote.bid) if quote.bid is not None else None,
                "ask": str(quote.ask) if quote.ask is not None else None,
                "mid": str(quote.mid) if quote.mid is not None else None,
                "spread": str(quote.spread.spread) if quote.spread.spread is not None else None,
                "spread_bps": (
                    str(quote.spread.spread_bps) if quote.spread.spread_bps is not None else None
                ),
                "spread_status": quote.spread.status.value,
                "age_ms": quote.age_ms,
                "provider_timestamp": quote.provider_timestamp.isoformat(),
                "received_at": quote.received_at.isoformat(),
                "price_source": quote.price_source.value,
                "provider": quote.provider,
                "market_session": quote.session.value if quote.session else None,
            }
            if quote is not None
            else None
        ),
        "account": (
            {
                "account_id": account.account_id,
                "currency": account.currency,
                "total_value": str(account.total_value),
                "cash_available": str(account.cash_available),
                "captured_at": account.captured_at.isoformat(),
                "cash_reserved": str(account.cash_reserved),
                "invested_value": str(account.invested_value),
            }
            if account is not None
            else None
        ),
        "position": (
            {
                "quantity": str(position.quantity),
                "quantity_available": str(position.quantity_available),
            }
            if position is not None
            else None
        ),
        "control": control.as_dict(),
        "transmission_gates": {
            "order_transmission_permitted": settings.order_transmission_permitted,
            "blockers": settings.order_transmission_blockers,
            "live_execution_permitted": settings.live_execution_permitted,
            "automated_trading_consent_confirmed": (
                settings.t212_automated_trading_consent_confirmed
            ),
            "execution_mode": settings.execution_mode.value,
        },
        "request_fingerprint": fingerprint_for(command),
    }
