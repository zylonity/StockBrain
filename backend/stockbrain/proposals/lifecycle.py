"""Proposal lifecycle workflows and their durable side effects.

Generation and authorization remain separate workflows. This module owns the
state transitions around an existing proposal: operator termination, expiry,
invalidation, market review, backlog queueing, and notifications.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.base import JSONDict, utcnow
from stockbrain.db.models.companies import BrokerInstrument
from stockbrain.db.models.proposals import ApprovalAction, RiskEvaluation, TradeProposal
from stockbrain.db.models.research import ResearchRun, Thesis
from stockbrain.db.models.system import AuditLog
from stockbrain.enums import (
    ActorType,
    AuthorizationSource,
    JobType,
    MarketSession,
    NotificationEvent,
    OrderSide,
    ProposalStatus,
    ResearchStatus,
)
from stockbrain.errors import ProposalAlreadyConsumed
from stockbrain.fx.base import normalize_currency
from stockbrain.jobs.queue import JobQueue
from stockbrain.logging import get_logger
from stockbrain.market_data.sessions import session_from_us_clock
from stockbrain.proposals.state_machine import ACTIVE_STATUSES, assert_transition, can_transition
from stockbrain.risk.models import ZERO, FxSnapshot

if TYPE_CHECKING:
    from stockbrain.proposals.service import ProposalService

log = get_logger(__name__)

STAGE_GENERATION = "GENERATION"
_SWEEPABLE_STATUSES: tuple[ProposalStatus, ...] = (
    ProposalStatus.DRAFT,
    ProposalStatus.READY,
    ProposalStatus.NOTIFIED,
    ProposalStatus.APPROVAL_PENDING,
)


async def thesis_is_superseded(session: AsyncSession, thesis_id: uuid.UUID) -> bool:
    """Whether a newer SUCCEEDED thesis names this one in ``supersedes_thesis_id``.

    Shared by the proposal sweep (a pending proposal built on a superseded
    thesis is invalidated) and the exit sweep (a held position opened on a
    superseded thesis is an exit signal).
    """
    count = await session.scalar(
        sa.select(sa.func.count())
        .select_from(Thesis)
        .join(ResearchRun, ResearchRun.id == Thesis.research_run_id)
        .where(
            Thesis.supersedes_thesis_id == thesis_id,
            ResearchRun.status == ResearchStatus.SUCCEEDED,
        )
    )
    return bool(count)


def _blocked_by_market_session(rules: Sequence[JSONDict]) -> bool:
    """Whether the market session was one of the rules that refused a run.

    A deferral caused by the session is special: the answer cannot change until
    the market reopens, so re-asking on every TTL tick is a free quote and a
    risk_evaluations row for the same refusal, over and over.  The rules JSON is
    the durable record of *why* -- no extra column is needed to find it.
    """
    return any(
        rule.get("rule_id") == "market_session" and rule.get("outcome") == "BLOCK" for rule in rules
    )


class ProposalLifecycle:
    """Lifecycle workflow collaborator for one proposal service."""

    def __init__(self, service: ProposalService, queue: JobQueue) -> None:
        self._service = service
        self._queue = queue

    async def reject(
        self, proposal_id: uuid.UUID, *, actor: str, reason: str | None = None
    ) -> TradeProposal:
        """Durably refuse a proposal.  Terminal; it is never revived."""
        return await self._terminate(
            proposal_id,
            target=ProposalStatus.REJECTED,
            actor=actor,
            reason=reason or "rejected by operator",
            action="proposal.rejected",
        )

    async def cancel(
        self, proposal_id: uuid.UUID, *, actor: str, reason: str | None = None
    ) -> TradeProposal:
        """Withdraw a proposal.  Legal from every pre-execution state."""
        return await self._terminate(
            proposal_id,
            target=ProposalStatus.CANCELLED,
            actor=actor,
            reason=reason or "cancelled by operator",
            action="proposal.cancelled",
        )

    async def _terminate(
        self,
        proposal_id: uuid.UUID,
        *,
        target: ProposalStatus,
        actor: str,
        reason: str,
        action: str,
    ) -> TradeProposal:
        now = utcnow()
        async with self._service.database.transaction() as session:
            proposal = (
                await session.execute(
                    sa.select(TradeProposal)
                    .where(TradeProposal.id == proposal_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if proposal is None:
                raise ProposalAlreadyConsumed("proposal not found")
            if not can_transition(proposal.status, target):
                raise ProposalAlreadyConsumed(
                    f"proposal is {proposal.status.value} and can no longer be "
                    f"{target.value.lower()}"
                )
            assert_transition(proposal.status, target)
            previous_status = proposal.status
            proposal.status = target
            proposal.status_reason = reason[:1000]
            proposal.updated_at = now
            if target is ProposalStatus.REJECTED:
                proposal.rejected_at = now
                proposal.rejected_by = actor
            await self.retire_approval_actions(session, [proposal.id], now)
            if target is ProposalStatus.REJECTED:
                await self.notify(session, proposal.id, NotificationEvent.PROPOSAL_REJECTED)
            session.add(
                AuditLog(
                    actor_type=ActorType.USER,
                    actor_id=actor,
                    action=action,
                    entity_type="trade_proposal",
                    entity_id=proposal.id,
                    details={
                        "reason": reason[:1000],
                        "previous_status": previous_status.value,
                        "new_status": proposal.status.value,
                    },
                )
            )
            await session.flush()
            return proposal

    # ------------------------------------------------------------------
    # Expiry and invalidation
    # ------------------------------------------------------------------
    async def sweep(self, *, now: dt.datetime | None = None) -> dict[str, int]:
        """Expire, invalidate and re-price live proposals.

        Called by the scheduler once a minute.  Every branch is idempotent, so a
        restart mid-sweep repeats work rather than losing it.
        """
        moment = now or utcnow()
        expired = await self._expire(moment)
        invalidated = await self._invalidate_stale_preconditions(moment)
        repriced = await self._revalidate_market(moment)
        if expired or invalidated or repriced:
            log.info(
                "proposal_sweep_complete",
                expired=expired,
                invalidated=invalidated,
                market_invalidated=repriced,
            )
        return {"expired": expired, "invalidated": invalidated, "market_invalidated": repriced}

    async def _expire(self, now: dt.datetime) -> int:
        async with self._service.database.transaction() as session:
            result = await session.execute(
                sa.update(TradeProposal)
                .where(
                    TradeProposal.status.in_(_SWEEPABLE_STATUSES),
                    TradeProposal.expires_at <= now,
                )
                .values(
                    status=ProposalStatus.EXPIRED,
                    status_reason="the proposal's time to live elapsed before authorization",
                    updated_at=now,
                    version=TradeProposal.version + 1,
                )
                .returning(TradeProposal.id)
            )
            expired = list(result.scalars())
            await self.retire_approval_actions(session, expired, now)
            for proposal_id in expired:
                await self.notify(session, proposal_id, NotificationEvent.PROPOSAL_EXPIRED)
            return len(expired)

    async def _invalidate_stale_preconditions(self, now: dt.datetime) -> int:
        """Invalidate proposals whose durable preconditions stopped holding.

        Covers the conditions a sweep can see without a market-data call:
        the listing changed, the risk policy changed, a newer thesis superseded
        this one, or a position a reduction depends on is gone.
        """
        count = 0
        async with self._service.database.transaction() as session:
            proposals = list(
                (
                    await session.execute(
                        sa.select(TradeProposal)
                        .where(TradeProposal.status.in_(ACTIVE_STATUSES))
                        .order_by(TradeProposal.created_at)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for proposal in proposals:
                reason = await self._precondition_failure(session, proposal, now)
                if reason is None:
                    continue
                if not can_transition(proposal.status, ProposalStatus.INVALIDATED):
                    continue
                self._service._invalidate(proposal, reason, now)
                await self.retire_approval_actions(session, [proposal.id], now)
                await self.notify(
                    session,
                    proposal.id,
                    NotificationEvent.PROPOSAL_INVALIDATED,
                    detail=reason,
                )
                session.add(
                    AuditLog(
                        actor_type=ActorType.SYSTEM,
                        actor_id="system:sweep",
                        action="proposal.invalidated",
                        entity_type="trade_proposal",
                        entity_id=proposal.id,
                        details={"reason": reason[:1000]},
                    )
                )
                count += 1
        return count

    async def _precondition_failure(
        self, session: AsyncSession, proposal: TradeProposal, now: dt.datetime
    ) -> str | None:
        if (
            proposal.risk_policy_version is not None
            and proposal.risk_policy_version != self._service.config.version
        ):
            return (
                "the risk configuration changed after this proposal was generated "
                f"(policy {proposal.risk_policy_version} -> {self._service.config.version})"
            )

        instrument = (
            await session.get(BrokerInstrument, proposal.broker_instrument_id)
            if proposal.broker_instrument_id
            else None
        )
        if instrument is None:
            return "the broker listing this proposal references no longer exists"
        if not instrument.is_active:
            return f"{instrument.broker_ticker} is no longer an active broker listing"
        if instrument.broker_ticker != proposal.broker_ticker:
            return "the broker listing's identity changed after this proposal was generated"
        if proposal.company_id is not None and instrument.company_id != proposal.company_id:
            return "the listing is no longer attached to the company this proposal was about"

        # A superseded thesis says "do not enter on this"; for a reduction,
        # supersession is the reason to act, so this invalidation is entry-only.
        if (
            proposal.thesis_id is not None
            and proposal.side is OrderSide.BUY
            and await thesis_is_superseded(session, proposal.thesis_id)
        ):
            return "a newer thesis supersedes the one this proposal was built on"

        if proposal.side is OrderSide.SELL:
            account, _reason = await self._service.account_state.load(
                max_age_seconds=self._service.config.max_account_state_age_seconds, now=now
            )
            if account is not None:
                position = account.position(proposal.broker_ticker)
                available = position.quantity_available if position else ZERO
                if available < proposal.proposed_quantity:
                    return (
                        f"the position backing this reduction fell to {available} share(s), "
                        f"below the proposed {proposal.proposed_quantity}"
                    )
        return None

    async def _revalidate_market(self, now: dt.datetime) -> int:
        """Re-price a bounded batch of live proposals.

        Bounded on purpose: an unbounded sweep would turn every scheduler tick
        into one market-data request per open proposal, which is how a polite
        integration becomes a rate-limit incident.
        """
        limit = max(0, self._service.settings.proposal_revalidation_batch)
        if limit == 0:
            return 0
        async with self._service.database.session() as session:
            proposals = list(
                (
                    await session.execute(
                        sa.select(TradeProposal)
                        .where(TradeProposal.status.in_(_SWEEPABLE_STATUSES))
                        .order_by(TradeProposal.created_at)
                        .limit(limit)
                    )
                ).scalars()
            )
            instruments = {
                proposal.id: await session.get(BrokerInstrument, proposal.broker_instrument_id)
                if proposal.broker_instrument_id
                else None
                for proposal in proposals
            }

        account, _ = await self._service.account_state.load(
            max_age_seconds=self._service.config.max_account_state_age_seconds, now=now
        )
        # One rate per currency pair per sweep pass, not one per proposal. Every
        # proposal in this pass is judged against the same `now`, so they would
        # all receive the same rate; asking a free public API five times a minute
        # for the same number is impolite rather than safer. The memo is local
        # to this call and dies with it -- it is not a cache across sweeps,
        # which is what would let a stale rate be reused.
        fx_by_pair: dict[tuple[str, str], FxSnapshot | None] = {}

        count = 0
        for proposal in proposals:
            instrument = instruments.get(proposal.id)
            if instrument is None:
                continue
            async with self._service.database.session() as session:
                quote, quote_reason = await self._service.quotes.fetch(
                    session, instrument, self._service.config, now=now
                )
            fx: FxSnapshot | None = None
            if proposal.fx_required:
                pair_key = (
                    normalize_currency(account.currency if account else None),
                    normalize_currency(instrument.currency),
                )
                if pair_key not in fx_by_pair:
                    fx_by_pair[pair_key] = await self._service._evaluator().fx_snapshot(
                        account=account,
                        instrument_currency=instrument.currency,
                        now=now,
                    )
                fx = fx_by_pair[pair_key]
            failure = self._service._evaluator().market_failure(
                proposal, quote, quote_reason, fx, now
            )
            if failure is None:
                continue
            async with self._service.database.transaction() as session:
                locked = (
                    await session.execute(
                        sa.select(TradeProposal)
                        .where(
                            TradeProposal.id == proposal.id,
                            TradeProposal.status.in_(_SWEEPABLE_STATUSES),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if locked is None:
                    continue
                self._service._invalidate(locked, failure, now)
                await self.retire_approval_actions(session, [locked.id], now)
                await self.notify(
                    session,
                    locked.id,
                    NotificationEvent.PROPOSAL_INVALIDATED,
                    detail=failure,
                )
                session.add(
                    AuditLog(
                        actor_type=ActorType.SYSTEM,
                        actor_id="system:sweep",
                        action="proposal.invalidated",
                        entity_type="trade_proposal",
                        entity_id=locked.id,
                        details={"reason": failure[:1000], "cause": "market"},
                    )
                )
                count += 1
        return count

    # ------------------------------------------------------------------
    # Enqueueing
    # ------------------------------------------------------------------
    async def enqueue_pending(self, limit: int = 25, *, now: dt.datetime | None = None) -> int:
        """Queue proposal generation for every published thesis without one.

        Restart-safe by construction: the backlog is derived from the database,
        not from an in-memory list, and the dedupe key means re-enqueuing a
        thesis that already has a pending job is a no-op.

        A *deferred* generation refusal is not a terminal answer, so it is
        retried -- but at most once per ``proposal_ttl_minutes``, so a shut market
        produces one attempt per tick-window rather than one per scheduler tick.
        The exception is a deferral caused by the market session itself: the
        answer cannot change while the market is closed, so it waits for the next
        regular open instead of re-asking every TTL.  A *terminal* refusal is
        never retried; asking the same rules the same question forever is how a
        backlog becomes a loop.
        """
        if blockers := await self._service.control_blockers():
            log.debug("proposal_backlog_skipped", blockers=blockers)
            return 0
        moment = now or utcnow()
        regular_session = session_from_us_clock(moment).session is MarketSession.REGULAR
        enqueued = 0
        waiting = 0
        async with self._service.database.transaction() as session:
            thesis_ids = list(
                (
                    await session.execute(
                        sa.select(Thesis.id)
                        .join(ResearchRun, ResearchRun.id == Thesis.research_run_id)
                        .where(
                            ResearchRun.status == ResearchStatus.SUCCEEDED,
                            ~sa.exists(
                                sa.select(TradeProposal.id).where(
                                    TradeProposal.thesis_id == Thesis.id
                                )
                            ),
                            ~sa.exists(
                                sa.select(RiskEvaluation.id).where(
                                    RiskEvaluation.thesis_id == Thesis.id,
                                    RiskEvaluation.stage == STAGE_GENERATION,
                                    RiskEvaluation.policy_version == self._service.config.version,
                                    RiskEvaluation.deferred.is_(False),
                                )
                            ),
                            ~sa.exists(
                                sa.select(RiskEvaluation.id).where(
                                    RiskEvaluation.thesis_id == Thesis.id,
                                    RiskEvaluation.stage == STAGE_GENERATION,
                                    RiskEvaluation.policy_version == self._service.config.version,
                                    RiskEvaluation.deferred.is_(True),
                                    RiskEvaluation.created_at
                                    > moment
                                    - dt.timedelta(
                                        minutes=self._service.config.proposal_ttl_minutes
                                    ),
                                )
                            ),
                        )
                        .order_by(Thesis.created_at.desc())
                        .limit(limit)
                    )
                ).scalars()
            )
            for thesis_id in thesis_ids:
                latest_rules = (
                    await session.execute(
                        sa.select(RiskEvaluation.rules)
                        .where(
                            RiskEvaluation.thesis_id == thesis_id,
                            RiskEvaluation.stage == STAGE_GENERATION,
                            RiskEvaluation.policy_version == self._service.config.version,
                            RiskEvaluation.deferred.is_(True),
                        )
                        .order_by(RiskEvaluation.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if (
                    latest_rules is not None
                    and _blocked_by_market_session(latest_rules)
                    and not regular_session
                ):
                    waiting += 1
                    continue
                job_id = await self._queue.enqueue(
                    session,
                    JobType.GENERATE_PROPOSAL,
                    payload={"thesis_id": str(thesis_id)},
                    dedupe_key=f"proposal:{thesis_id}",
                    priority=25,
                )
                if job_id is not None:
                    enqueued += 1
        if waiting:
            log.info("proposal_deferrals_waiting_for_open", count=waiting)
        if enqueued:
            log.info("proposal_generation_enqueued", count=enqueued)
        return enqueued

    async def enqueue_for_run(self, run_id: uuid.UUID) -> int:
        """Queue generation for the thesis a completed research run published."""
        async with self._service.database.transaction() as session:
            thesis_id = await session.scalar(
                sa.select(Thesis.id).where(Thesis.research_run_id == run_id)
            )
            if thesis_id is None:
                return 0
            job_id = await self._queue.enqueue(
                session,
                JobType.GENERATE_PROPOSAL,
                payload={"thesis_id": str(thesis_id)},
                dedupe_key=f"proposal:{thesis_id}",
                priority=25,
            )
        return 1 if job_id is not None else 0

    async def enqueue_notification(
        self,
        session: AsyncSession,
        proposal_id: uuid.UUID,
        event: NotificationEvent,
        *,
        detail: str | None = None,
    ) -> None:
        """Public entry point to the same queueing the lifecycle uses.

        Phase 8's execution service announces its own transitions, and it must
        do so through *this* function rather than its own: the dedupe key shape
        is what makes "one notification per transition" a unique-index
        guarantee, and two spellings of it would be two guarantees.
        """
        await self.notify(session, proposal_id, event, detail=detail)

    async def notify(
        self,
        session: AsyncSession,
        proposal_id: uuid.UUID,
        event: NotificationEvent,
        *,
        detail: str | None = None,
    ) -> None:
        """Queue one notification, idempotently, inside the caller's transaction."""
        await self._queue.enqueue(
            session,
            JobType.SEND_NOTIFICATION,
            payload={
                "proposal_id": str(proposal_id),
                "event": event.value,
                "detail": (detail or "")[:500] or None,
            },
            dedupe_key=f"notify:{proposal_id}:{event.value}",
            priority=15,
        )

    @staticmethod
    async def retire_approval_actions(
        session: AsyncSession, proposal_ids: Sequence[uuid.UUID], now: dt.datetime
    ) -> None:
        """Consume every outstanding approval token for the supplied proposals."""
        if not proposal_ids:
            return
        await session.execute(
            sa.update(ApprovalAction)
            .where(
                ApprovalAction.proposal_id.in_(list(proposal_ids)),
                ApprovalAction.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )


def _actor_type(source: AuthorizationSource) -> ActorType:
    return ActorType.USER if source.is_human else ActorType.SYSTEM
