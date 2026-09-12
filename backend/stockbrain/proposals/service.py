"""Trade-proposal generation, authorization, expiry and invalidation.

The pipeline this closes is::

    succeeded research -> published thesis
        -> revalidated instrument identity
        -> fresh broker account state
        -> fresh execution-grade quote
        -> deterministic risk
        -> deterministic sizing
        -> durable proposal
        -> authorization, per the recorded execution policy

**Authorization is not execution.**  Nothing in this module, or anywhere it can
reach, sends an order to a broker.  The only broker clients constructed by this
phase are the read-only metadata and account clients, and neither has a mutation
method.  Transmission arrives in Phase 8 behind its own four-gate live check.

Concurrency is handled in PostgreSQL, never in memory:

* a **transaction-scoped advisory lock** on ``(broker, account)`` serialises
  every exposure calculation, so two proposals generated a millisecond apart
  cannot each believe the whole cash buffer is theirs;
* ``SELECT ... FOR UPDATE`` plus a check of the optimistic ``version`` column
  serialises authorization, so two browser tabs, or an approve racing a reject,
  produce exactly one winner and one ``409``;
* ``uq_trade_proposals_active_instrument`` makes "one live proposal per listing"
  a database guarantee;
* ``trade_proposals.dedupe_key`` makes a redelivered generation job a no-op at
  the database rather than at a hopeful check.

The engine's answer is *never* softened here.  A ``BLOCK`` produces a durable
``risk_evaluations`` row and no proposal; there is no branch that creates one
anyway.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, Decimal
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.broker.account_state import DEFAULT_ACCOUNT_ID, AccountStateService
from stockbrain.broker.automation import BrokerAutomationCapability, automation_capability
from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument, EventCompanyImpact
from stockbrain.db.models.proposals import RiskEvaluation, TradeProposal
from stockbrain.db.models.research import ResearchRun, Thesis
from stockbrain.db.models.system import AuditLog
from stockbrain.db.session import Database
from stockbrain.enums import (
    ActorType,
    AuthorizationSource,
    Broker,
    ExecutionPolicy,
    NotificationEvent,
    OrderSide,
    OrderType,
    ProposalStatus,
    ResolutionStatus,
    RiskOutcome,
    RuleOutcome,
    ThesisAction,
)
from stockbrain.errors import (
    AuthorizationNotPermitted,
    ProposalAlreadyConsumed,
    ProposalExpired,
    ProposalInvalidated,
    RiskBlocked,
)
from stockbrain.fx.service import FxService
from stockbrain.jobs.queue import JobQueue
from stockbrain.logging import get_logger
from stockbrain.market_data.base import MarketDataProvider
from stockbrain.observability.metrics import METRICS
from stockbrain.proposals.evaluation import EvaluationContext, ProposalEvaluator, Revalidation
from stockbrain.proposals.lifecycle import ProposalLifecycle
from stockbrain.proposals.quotes import QuoteFetcher
from stockbrain.proposals.state_machine import (
    AUTHORIZABLE_STATUSES,
    assert_transition,
)
from stockbrain.risk.config import RiskConfig
from stockbrain.risk.engine import RiskEngine
from stockbrain.risk.exits import ExitSignal
from stockbrain.risk.models import (
    AccountState,
    FxSnapshot,
    InstrumentIdentity,
    QuoteSnapshot,
    RiskDecision,
    RuleResult,
)
from stockbrain.risk.rules import (
    action_is_executable,
)

if TYPE_CHECKING:
    # Imported only for the annotation: a module-level import of the Telegram
    # package from here would close an import cycle (telegram.runtime imports
    # this module, and this module would import telegram.preferences).
    from stockbrain.telegram.preferences import NotificationPreferences

__all__ = ["AuthorizationResult", "GenerationResult", "ProposalService", "Revalidation"]

log = get_logger(__name__)

STAGE_GENERATION = "GENERATION"
STAGE_AUTHORIZATION = "AUTHORIZATION"

#: Statuses the expiry sweep may move to EXPIRED. ``APPROVED`` is deliberately
#: excluded: an authorized proposal belongs to the execution phase, and having a
#: background sweep quietly retract an authorization would make "approve then
#: expire" a race whose winner depends on scheduler timing. Authorization itself
#: still refuses an expired proposal under lock, which is the property that
#: matters.
_SWEEPABLE_STATUSES: tuple[ProposalStatus, ...] = (
    ProposalStatus.DRAFT,
    ProposalStatus.READY,
    ProposalStatus.NOTIFIED,
    ProposalStatus.APPROVAL_PENDING,
)


@dataclass(slots=True)
class GenerationResult:
    thesis_id: uuid.UUID | None
    created: bool
    proposal_id: uuid.UUID | None = None
    evaluation_id: uuid.UUID | None = None
    outcome: RiskOutcome = RiskOutcome.BLOCK
    reason: str = ""
    authorized: bool = False
    blocks: tuple[str, ...] = ()


@dataclass(slots=True)
class AuthorizationResult:
    proposal_id: uuid.UUID
    source: AuthorizationSource
    actor: str
    authorized_at: dt.datetime
    evaluation_id: uuid.UUID | None = None
    decision: RiskDecision | None = None


@dataclass(slots=True)
class _Candidate:
    """Everything loaded for one thesis before any network call."""

    thesis: Thesis
    run: ResearchRun
    impact: EventCompanyImpact | None
    instrument: BrokerInstrument | None
    identity: InstrumentIdentity
    action: ThesisAction
    confidence: Decimal
    event_id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None


class ProposalService:
    """Generates, authorizes and retires trade proposals."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        risk_config: RiskConfig,
        account_state: AccountStateService,
        market_data: MarketDataProvider | None,
        fx: FxService | None = None,
        broker: Broker = Broker.TRADING212,
        engine: RiskEngine | None = None,
        control: ControlStateService | None = None,
        preferences: NotificationPreferences | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.config = risk_config
        self.account_state = account_state
        self.quotes = QuoteFetcher(market_data)
        # Optional so a same-currency deployment (and a unit test) needs no FX
        # source at all. Absent, the evaluator reports "no provider" for a
        # cross-currency pair, which blocks -- it never falls back to parity.
        self.fx = fx or FxService(settings, provider=None)
        self.broker = broker
        self.engine = engine or RiskEngine()
        self.queue = JobQueue()
        self.lifecycle = ProposalLifecycle(self, self.queue)
        # Durable pause / kill switch. Optional so a unit test can build the
        # service without one; absent, nothing is halted, which is the same
        # answer an empty table gives.
        self.control = control or ControlStateService(database)
        # Optional so a unit test that only exercises proposal state needs no
        # preference store; ``enqueue_pipeline_notification`` is a no-op without
        # one, which is exactly the shipped quiet behaviour.
        self._preferences = preferences

    def _evaluator(self) -> ProposalEvaluator:
        return ProposalEvaluator(
            self.database,
            self.settings,
            config=self.config,
            account_state=self.account_state,
            quotes=self.quotes,
            fx=self.fx,
            broker=self.broker,
            engine=self.engine,
        )

    # ------------------------------------------------------------------
    # Policy
    # ------------------------------------------------------------------
    @property
    def execution_policy(self) -> ExecutionPolicy:
        return self.settings.execution_policy

    def automation(self) -> BrokerAutomationCapability:
        return automation_capability(self.settings, self.broker)

    def policy_snapshot(self, source: AuthorizationSource) -> dict[str, object]:
        """What was permitted at the instant of authorization.

        Persisted onto the proposal so a later configuration change cannot
        rewrite the record of the permission that was actually relied on.
        """
        return {
            "authorization_source": source.value,
            "execution_policy": self.execution_policy.value,
            "risk_policy_version": self.config.version,
            "broker_environment": self.settings.t212_env.value,
            "automation": self.automation().as_dict(),
            "automatic_authorization_permitted": (self.settings.automatic_authorization_permitted),
            "automation_blockers": self.settings.automation_blockers,
            "broker_order_transmitted": False,
            "notice": (
                "Authorization records that deterministic risk allowed this trade and who "
                "signed it off. No broker order has been sent; order transmission is Phase 8."
            ),
        }

    async def control_blockers(self) -> list[str]:
        """Every durable reason proposal work is currently halted.

        Read from PostgreSQL on every call rather than cached: a pause set from
        Telegram must take effect for the job worker in the same process and
        for any other process reading the same database, and a cache is exactly
        what would let those disagree.
        """
        return (await self.control.snapshot()).blockers

    async def _assert_not_halted(self) -> None:
        """Refuse authorization while paused or killed.

        Enforced *here* rather than in each client so the web, Telegram and the
        automatic path cannot diverge: there is one authorization function and
        it is the one that checks. ``AuthorizationNotPermitted`` rather than
        ``ExecutionNotPermitted`` because what is being refused is the
        authorization -- transmission is Phase 8's separate permission and its
        own gate.
        """
        if blockers := await self.control_blockers():
            raise AuthorizationNotPermitted("authorization is halted: " + "; ".join(blockers))

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------
    async def generate(
        self, thesis_id: uuid.UUID, *, now: dt.datetime | None = None
    ) -> GenerationResult:
        """Evaluate one published thesis and, if risk allows, persist a proposal."""
        moment = now or utcnow()

        # A pause stops *new* proposals. Ingestion, classification, research and
        # broker reconciliation deliberately keep running (spec section 20), so
        # the work already in flight is not lost -- it simply waits.
        if blockers := await self.control_blockers():
            reason = "proposal generation is halted: " + "; ".join(blockers)
            log.info("proposal_generation_halted", thesis_id=str(thesis_id), blockers=blockers)
            return GenerationResult(thesis_id=thesis_id, created=False, reason=reason)

        async with self.database.session() as session:
            candidate = await self._load_candidate(session, thesis_id)
        if candidate is None:
            return GenerationResult(
                thesis_id=thesis_id, created=False, reason="thesis or research run not found"
            )

        # A HOLD is a conclusion, not an order. Refuse it before spending a
        # market-data request to reach the same answer, but refuse it durably.
        action_rule = action_is_executable(candidate.action)
        if action_rule.outcome is RuleOutcome.BLOCK:
            evaluation_id = await self._record_evaluation(
                stage=STAGE_GENERATION,
                thesis_id=thesis_id,
                proposal_id=None,
                identity=candidate.identity,
                outcome=RiskOutcome.BLOCK,
                rules=(action_rule,),
                snapshot={"action": candidate.action.value, "reason": action_rule.reason},
                snapshot_hash=None,
                actor="system:generator",
                detail=action_rule.reason,
            )
            return GenerationResult(
                thesis_id=thesis_id,
                created=False,
                evaluation_id=evaluation_id,
                reason=action_rule.reason,
                blocks=(action_rule.reason,),
            )

        evaluator = self._evaluator()
        facts = await evaluator.load(
            candidate.instrument, instrument_currency=candidate.identity.currency, now=moment
        )
        account, quote, fx = facts.account, facts.quote, facts.fx

        account_id = account.account_id if account else DEFAULT_ACCOUNT_ID

        async with self.database.transaction() as session:
            await self._lock_account(session, account_id)

            # Identity is re-read under the lock: a sync that retired the
            # listing between the load and here must not slip through.
            fresh_identity = await self._reload_identity(session, candidate)
            verdict = await evaluator.evaluate(
                session,
                EvaluationContext(
                    candidate.action, candidate.confidence, fresh_identity, account_id
                ),
                facts,
                now=moment,
            )
            decision = verdict.decision

            evaluation = RiskEvaluation(
                stage=STAGE_GENERATION,
                thesis_id=thesis_id,
                broker=self.broker,
                broker_ticker=fresh_identity.broker_ticker,
                broker_instrument_id=fresh_identity.broker_instrument_id,
                outcome=decision.outcome,
                policy_version=decision.policy_version,
                rules=[rule.as_dict() for rule in decision.rules],
                snapshot=decision.as_dict(),
                snapshot_hash=decision.snapshot_hash(),
                actor="system:generator",
                detail="; ".join(decision.blocks) or "; ".join(decision.sizing.reasons) or None,
            )
            session.add(evaluation)
            await session.flush()

            METRICS.inc(
                "stockbrain_risk_evaluations_total",
                labels={"stage": STAGE_GENERATION, "outcome": decision.outcome.value},
            )

            if not decision.allowed:
                reason = "; ".join(decision.blocks) or "; ".join(decision.sizing.reasons)
                log.info(
                    "proposal_blocked",
                    thesis_id=str(thesis_id),
                    broker_ticker=fresh_identity.broker_ticker,
                    outcome=decision.outcome.value,
                    blocks=list(decision.block_rule_ids),
                )
                # --- PROPOSAL_BLOCKED notification (task 1) ------------------
                # Without this the operator sees "Research completed: BUY" and
                # then silence. Announce the refusal from the same transaction
                # that recorded it, so an announced refusal is a real one.
                if candidate.action in {
                    ThesisAction.BUY,
                    ThesisAction.SELL,
                    ThesisAction.REDUCE,
                }:
                    # Local imports: see the TYPE_CHECKING note above. Both
                    # packages are fully loaded by the time a proposal is
                    # generated.
                    from stockbrain.jobs.notifications import enqueue_pipeline_notification
                    from stockbrain.telegram.preferences import PipelineEvent

                    await enqueue_pipeline_notification(
                        session,
                        self.queue,
                        self._preferences,
                        entity_id=candidate.run.id,
                        event=PipelineEvent.PROPOSAL_BLOCKED,
                    )
                # --- end PROPOSAL_BLOCKED notification -----------------------
                return GenerationResult(
                    thesis_id=thesis_id,
                    created=False,
                    evaluation_id=evaluation.id,
                    outcome=decision.outcome,
                    reason=reason,
                    blocks=decision.blocks,
                )

            assert quote is not None and account is not None  # implied by an allowed decision
            proposal = self._build_proposal(
                candidate=candidate,
                identity=fresh_identity,
                decision=decision,
                quote=quote,
                account=account,
                fx=fx,
                now=moment,
            )
            session.add(proposal)
            try:
                await session.flush()
            except IntegrityError as exc:
                # Either the dedupe key (a redelivered job) or the one-active-
                # proposal-per-instrument index (a concurrent generator). Both
                # mean "somebody already did this", which is success, not error.
                await session.rollback()
                log.info(
                    "proposal_generation_deduped",
                    thesis_id=str(thesis_id),
                    broker_ticker=fresh_identity.broker_ticker,
                    constraint=_constraint_name(exc),
                )
                return GenerationResult(
                    thesis_id=thesis_id,
                    created=False,
                    outcome=decision.outcome,
                    reason="a live proposal already exists for this thesis or listing",
                )

            evaluation.proposal_id = proposal.id
            session.add(
                AuditLog(
                    actor_type=ActorType.SYSTEM,
                    actor_id="system:generator",
                    action="proposal.generated",
                    entity_type="trade_proposal",
                    entity_id=proposal.id,
                    details={
                        "thesis_id": str(thesis_id),
                        "broker_ticker": proposal.broker_ticker,
                        "side": proposal.side.value,
                        "quantity": str(proposal.proposed_quantity),
                        "notional": str(proposal.estimated_notional),
                        "currency": proposal.account_currency,
                        "risk_outcome": decision.outcome.value,
                        "risk_policy_version": decision.policy_version,
                        "execution_policy": proposal.execution_policy.value,
                    },
                )
            )
            if proposal.execution_policy is ExecutionPolicy.MANUAL:
                # An AUTOMATIC proposal is announced only once its authorization
                # has been attempted, so the message can state what actually
                # happened rather than what was about to be tried.
                await self._notify(session, proposal.id, NotificationEvent.PROPOSAL_MANUAL)
            proposal_id = proposal.id
            evaluation_id = evaluation.id

        METRICS.inc("stockbrain_trade_proposals_total", labels={"outcome": decision.outcome.value})
        log.info(
            "proposal_generated",
            proposal_id=str(proposal_id),
            thesis_id=str(thesis_id),
            broker_ticker=fresh_identity.broker_ticker,
            side=decision.sizing.side.value if decision.sizing.side else None,
            quantity=str(decision.sizing.quantity),
            execution_policy=self.execution_policy.value,
        )

        result = GenerationResult(
            thesis_id=thesis_id,
            created=True,
            proposal_id=proposal_id,
            evaluation_id=evaluation_id,
            outcome=decision.outcome,
            reason="proposal generated",
        )

        if (
            self.execution_policy is ExecutionPolicy.AUTOMATIC
            and self.settings.automatic_authorization_permitted
        ):
            try:
                await self.authorize(
                    proposal_id,
                    source=AuthorizationSource.SYSTEM_AUTOMATIC,
                    actor="system:automatic",
                )
                result.authorized = True
                async with self.database.transaction() as session:
                    await self._notify(
                        session, proposal_id, NotificationEvent.PROPOSAL_AUTO_AUTHORIZED
                    )
            except (
                RiskBlocked,
                ProposalExpired,
                ProposalInvalidated,
                ProposalAlreadyConsumed,
                AuthorizationNotPermitted,
            ) as exc:
                # Fail closed and visibly: the proposal stays unauthorized (or
                # invalidated) and an operator can see exactly why.
                log.warning(
                    "automatic_authorization_refused",
                    proposal_id=str(proposal_id),
                    error_type=type(exc).__name__,
                    reason=str(exc)[:300],
                )
                async with self.database.transaction() as session:
                    await self._notify(
                        session,
                        proposal_id,
                        NotificationEvent.AUTHORIZATION_REFUSED,
                        detail=str(exc),
                    )
        return result

    async def generate_exit(
        self,
        broker_ticker: str,
        signal: ExitSignal,
        *,
        now: dt.datetime | None = None,
    ) -> GenerationResult:
        """Turn one exit signal into a proposal, on the ordinary risk path.

        The signal decides *that* the position should be reduced and *why*.
        Everything else -- the quantity, the reference price, the spread, the
        session, the FX -- is the existing evaluator's answer, unchanged.  An
        exit is not a privileged order; it is an ordinary proposal whose action
        came from a rule instead of from research.
        """
        moment = now or utcnow()

        if blockers := await self.control_blockers():
            return GenerationResult(
                thesis_id=None,
                created=False,
                reason="proposal generation is halted: " + "; ".join(blockers),
            )

        async with self.database.session() as session:
            origin = await self.origin_proposal(session, broker_ticker)
            if origin is None or origin.thesis_id is None:
                return GenerationResult(
                    thesis_id=None,
                    created=False,
                    reason=(
                        f"{broker_ticker} has no executed StockBrain buy to exit against, "
                        "so there is no thesis this exit could supersede"
                    ),
                )
            thesis_id = origin.thesis_id
            candidate = await self._load_candidate(session, thesis_id)

        if candidate is None:
            return GenerationResult(
                thesis_id=thesis_id,
                created=False,
                reason="the opening thesis or its research run is no longer present",
            )

        # The action is the rule's, not the thesis's.  Confidence is carried over
        # from the opening thesis for the audit trail only: task 4 made the
        # confidence floor skip risk-reducing actions, so it gates nothing here.
        candidate = replace(candidate, action=signal.action)

        evaluator = self._evaluator()
        facts = await evaluator.load(
            candidate.instrument, instrument_currency=candidate.identity.currency, now=moment
        )
        account, quote, fx = facts.account, facts.quote, facts.fx
        account_id = account.account_id if account else DEFAULT_ACCOUNT_ID

        async with self.database.transaction() as session:
            await self._lock_account(session, account_id)
            fresh_identity = await self._reload_identity(session, candidate)
            verdict = await evaluator.evaluate(
                session,
                EvaluationContext(
                    candidate.action, candidate.confidence, fresh_identity, account_id
                ),
                facts,
                now=moment,
            )
            decision = verdict.decision

            evaluation = RiskEvaluation(
                stage=STAGE_GENERATION,
                thesis_id=thesis_id,
                broker=self.broker,
                broker_ticker=fresh_identity.broker_ticker,
                broker_instrument_id=fresh_identity.broker_instrument_id,
                outcome=decision.outcome,
                policy_version=decision.policy_version,
                rules=[rule.as_dict() for rule in (*decision.rules, signal.rule)],
                snapshot=decision.as_dict(),
                snapshot_hash=decision.snapshot_hash(),
                actor=f"system:exit:{signal.rule_id}",
                detail="; ".join(decision.blocks) or signal.reason,
            )
            session.add(evaluation)
            await session.flush()

            METRICS.inc(
                "stockbrain_risk_evaluations_total",
                labels={"stage": STAGE_GENERATION, "outcome": decision.outcome.value},
            )

            if not decision.allowed:
                # A blocked exit is recorded, not dropped: "we wanted out and
                # the envelope refused" is the single most important thing an
                # operator can be told about a holding.
                reason = "; ".join(decision.blocks) or "; ".join(decision.sizing.reasons)
                log.warning(
                    "exit_proposal_blocked",
                    broker_ticker=broker_ticker,
                    exit_rule=signal.rule_id,
                    outcome=decision.outcome.value,
                    blocks=list(decision.block_rule_ids),
                )
                return GenerationResult(
                    thesis_id=thesis_id,
                    created=False,
                    evaluation_id=evaluation.id,
                    outcome=decision.outcome,
                    reason=reason,
                    blocks=decision.blocks,
                )

            assert quote is not None and account is not None
            proposal = self._build_proposal(
                candidate=candidate,
                identity=fresh_identity,
                decision=decision,
                quote=quote,
                account=account,
                fx=fx,
                now=moment,
            )
            # An exit reuses the origin thesis, so ``_build_proposal`` derives
            # the same ``dedupe_key`` the opening BUY already holds -- a plain
            # UNIQUE column -- and the insert would be swallowed as a duplicate.
            # The key guards a *redelivered generation job*, and an exit is not
            # one; the active-proposal partial index is what keeps a live exit
            # from being proposed twice, and a lapsed exit must be
            # re-proposable.
            proposal.dedupe_key = None
            # Why this proposal exists, in the fields the GUI and Telegram
            # already render.
            proposal.sizing_reasons = [
                {"reason": signal.reason},
                *proposal.sizing_reasons,
            ]
            proposal.risk_rules = [*proposal.risk_rules, signal.rule.as_dict()]
            proposal.research_action = signal.action.value
            session.add(proposal)
            try:
                await session.flush()
            except IntegrityError as exc:
                await session.rollback()
                log.info(
                    "exit_proposal_deduped",
                    broker_ticker=broker_ticker,
                    exit_rule=signal.rule_id,
                    constraint=_constraint_name(exc),
                )
                return GenerationResult(
                    thesis_id=thesis_id,
                    created=False,
                    outcome=decision.outcome,
                    reason="a live proposal already exists for this listing",
                )

            evaluation.proposal_id = proposal.id
            session.add(
                AuditLog(
                    actor_type=ActorType.SYSTEM,
                    actor_id=f"system:exit:{signal.rule_id}",
                    action="proposal.exit_generated",
                    entity_type="trade_proposal",
                    entity_id=proposal.id,
                    details={
                        "thesis_id": str(thesis_id),
                        "broker_ticker": proposal.broker_ticker,
                        "exit_rule": signal.rule_id,
                        "exit_reason": signal.reason,
                        "side": proposal.side.value,
                        "quantity": str(proposal.proposed_quantity),
                        "risk_outcome": decision.outcome.value,
                        "risk_policy_version": decision.policy_version,
                        "execution_policy": proposal.execution_policy.value,
                    },
                )
            )
            # An exit is never auto-authorized until a per-kind policy exists, so
            # under either execution policy a human must see it.
            await self._notify(session, proposal.id, NotificationEvent.PROPOSAL_MANUAL)
            proposal_id = proposal.id
            evaluation_id = evaluation.id

        METRICS.inc("stockbrain_trade_proposals_total", labels={"outcome": decision.outcome.value})
        METRICS.inc("stockbrain_position_exits_total", labels={"rule": signal.rule_id})
        log.info(
            "exit_proposal_generated",
            proposal_id=str(proposal_id),
            broker_ticker=broker_ticker,
            exit_rule=signal.rule_id,
            side=proposal.side.value,
            quantity=str(proposal.proposed_quantity),
        )
        return GenerationResult(
            thesis_id=thesis_id,
            created=True,
            proposal_id=proposal_id,
            evaluation_id=evaluation_id,
            outcome=decision.outcome,
            reason=signal.reason,
        )

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------
    async def authorize(
        self,
        proposal_id: uuid.UUID,
        *,
        source: AuthorizationSource,
        actor: str,
        now: dt.datetime | None = None,
    ) -> AuthorizationResult:
        """Re-validate everything, then record the authorization.

        Nothing is trusted from generation time.  The listing, the account, the
        quote, the spread, the session, the exposure already reserved and every
        deterministic limit are all re-read and re-evaluated here, because the
        gap between drafting a trade and signing it off is exactly where the
        world changes.

        The quantity the operator saw is **not** silently re-sized.  If the
        fresh envelope no longer permits it, the proposal is invalidated and a
        new analysis is required: re-pricing under someone's finger is how a
        person approves a trade they did not read.

        A refusal is *committed* and then raised.  Raising from inside the
        transaction would roll back the invalidation and the evaluation row, and
        a refusal nobody can read afterwards is not a durable refusal.
        """
        moment = now or utcnow()
        await self._assert_not_halted()
        if source is AuthorizationSource.SYSTEM_AUTOMATIC:
            self._assert_automation_permitted()

        async with self.database.session() as session:
            proposal = await session.get(TradeProposal, proposal_id)
            if proposal is None:
                raise ProposalAlreadyConsumed("proposal not found")
            observed_version = proposal.version
            instrument_id = proposal.broker_instrument_id
            thesis_id = proposal.thesis_id
            account_id = proposal.account_id
            self._assert_authorizable(proposal, source, moment)
            instrument = (
                await session.get(BrokerInstrument, instrument_id) if instrument_id else None
            )
            action = ThesisAction(proposal.research_action or ThesisAction.BUY.value)
            confidence = Decimal(str(proposal.research_confidence or 0))

        # Network reads happen outside the transaction; the row is re-checked
        # under lock afterwards and a changed `version` aborts.
        evaluator = self._evaluator()
        facts = await evaluator.load(
            instrument, instrument_currency=instrument.currency if instrument else None, now=moment
        )
        account, quote = facts.account, facts.quote

        refusal: RiskBlocked | None = None
        async with self.database.transaction() as session:
            await self._lock_account(session, account_id)
            locked = (
                await session.execute(
                    sa.select(TradeProposal)
                    .where(TradeProposal.id == proposal_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked is None:  # pragma: no cover - selected a statement ago
                raise ProposalAlreadyConsumed("proposal not found")
            if locked.version != observed_version:
                # Something changed while the quote was being fetched. The two
                # browser tabs case, and the approve-versus-reject case.
                raise ProposalAlreadyConsumed(
                    "another actor changed this proposal while it was being authorized"
                )
            self._assert_authorizable(locked, source, moment)

            identity = await self._identity_from_proposal(session, locked)
            verdict = await evaluator.evaluate(
                session,
                EvaluationContext(action, confidence, identity, locked.account_id, locked),
                facts,
                now=moment,
            )
            decision, rules = verdict.decision, verdict.rules
            blocked = [rule for rule in rules if rule.outcome is RuleOutcome.BLOCK]
            outcome = RiskOutcome.BLOCK if blocked else decision.outcome

            evaluation = RiskEvaluation(
                stage=STAGE_AUTHORIZATION,
                thesis_id=thesis_id,
                proposal_id=locked.id,
                broker=self.broker,
                broker_ticker=identity.broker_ticker,
                broker_instrument_id=identity.broker_instrument_id,
                outcome=outcome,
                policy_version=decision.policy_version,
                rules=[rule.as_dict() for rule in rules],
                snapshot=decision.as_dict(),
                snapshot_hash=decision.snapshot_hash(),
                actor=actor,
                detail="; ".join(rule.reason for rule in blocked) or None,
            )
            session.add(evaluation)
            METRICS.inc(
                "stockbrain_risk_evaluations_total",
                labels={"stage": STAGE_AUTHORIZATION, "outcome": outcome.value},
            )

            if blocked:
                reason = "; ".join(rule.reason for rule in blocked)
                self._invalidate(locked, reason, moment)
                session.add(
                    AuditLog(
                        actor_type=_actor_type(source),
                        actor_id=actor,
                        action="proposal.authorization_refused",
                        entity_type="trade_proposal",
                        entity_id=locked.id,
                        details={
                            "authorization_source": source.value,
                            "blocked_rules": [rule.rule_id for rule in blocked],
                            "reason": reason[:1000],
                        },
                    )
                )
                refusal = RiskBlocked(reason, rule_ids=tuple(rule.rule_id for rule in blocked))
                # The proposal is now INVALIDATED, so every button that still
                # points at it must stop working, whichever client drew it.
                await self._retire_approval_actions(session, [locked.id], moment)
                await self._notify(
                    session,
                    locked.id,
                    NotificationEvent.AUTHORIZATION_REFUSED,
                    detail=reason,
                )
            else:
                # The row is refreshed to the values authorization actually
                # judged; the drift rule has already bounded how far they moved.
                assert quote is not None and account is not None
                self._apply_quote(locked, quote)
                locked.reference_price = decision.sizing.reference_price or locked.reference_price
                locked.estimated_notional = locked.proposed_quantity * locked.reference_price
                # Convert the fixed quantity being authorized, not the newly
                # computed maximum/recommended quantity. Use this evaluation's
                # FX facts once and persist their provenance with the amount.
                assert facts.fx is not None and facts.fx.usable
                locked.estimated_notional_account_currency = facts.fx.to_account_currency(
                    locked.estimated_notional
                ).quantize(Decimal("0.0001"), rounding=ROUND_CEILING)
                self._apply_fx(locked, facts.fx)
                locked.max_quantity = decision.sizing.max_quantity
                locked.max_notional = decision.sizing.max_notional
                locked.account_currency = account.currency
                locked.account_id = account.account_id
                locked.risk_outcome = outcome
                locked.risk_policy_version = decision.policy_version
                locked.risk_rules = [rule.as_dict() for rule in rules]
                locked.risk_snapshot = decision.as_dict()
                locked.risk_snapshot_hash = decision.snapshot_hash()
                locked.sizing_reasons = [{"reason": item} for item in decision.sizing.reasons]

                assert_transition(locked.status, ProposalStatus.APPROVED)
                locked.status = ProposalStatus.APPROVED
                locked.status_reason = "deterministic risk revalidated at authorization"
                locked.approved_at = moment
                locked.approved_by = actor
                locked.approved_channel = source.channel
                locked.authorization_source = source
                locked.authorization_policy_snapshot = self.policy_snapshot(source)
                locked.updated_at = moment
                session.add(
                    AuditLog(
                        actor_type=_actor_type(source),
                        actor_id=actor,
                        action="proposal.authorized",
                        entity_type="trade_proposal",
                        entity_id=locked.id,
                        details={
                            "authorization_source": source.value,
                            "execution_policy": locked.execution_policy.value,
                            "risk_policy_version": decision.policy_version,
                            "quantity": str(locked.proposed_quantity),
                            "reference_price": str(locked.reference_price),
                            "broker_order_transmitted": False,
                        },
                    )
                )
            if refusal is None:
                # APPROVED is not authorizable, so any outstanding approve or
                # reject button is now meaningless. Consuming the rows is what
                # makes a stale tap a refusal rather than a second decision.
                await self._retire_approval_actions(session, [locked.id], moment)
            await session.flush()
            evaluation_id = evaluation.id

        if refusal is not None:
            log.info(
                "proposal_authorization_refused",
                proposal_id=str(proposal_id),
                authorization_source=source.value,
                blocked_rules=list(refusal.rule_ids),
            )
            raise refusal

        METRICS.inc("stockbrain_proposal_authorizations_total", labels={"source": source.value})
        log.info(
            "proposal_authorized",
            proposal_id=str(proposal_id),
            authorization_source=source.value,
            actor=actor,
            broker_order_transmitted=False,
        )
        return AuthorizationResult(
            proposal_id=proposal_id,
            source=source,
            actor=actor,
            authorized_at=moment,
            evaluation_id=evaluation_id,
            decision=decision,
        )

    async def revalidate(
        self, proposal_id: uuid.UUID, *, now: dt.datetime | None = None
    ) -> Revalidation | None:
        """Re-run every deterministic check against a fresh world, mutating nothing.

        Same inputs, same engine and same rule set as :meth:`authorize`; the
        difference is that this records no evaluation, takes no lock and moves
        no status.  Phase 8 calls it immediately before a broker POST, because
        an authorization is a statement about the moment it was given and an
        order is sent into a later one.

        Returns ``None`` when the proposal no longer exists.
        """
        moment = now or utcnow()
        async with self.database.session() as session:
            proposal = await session.get(TradeProposal, proposal_id)
            if proposal is None:
                return None
            instrument = (
                await session.get(BrokerInstrument, proposal.broker_instrument_id)
                if proposal.broker_instrument_id
                else None
            )
            action = ThesisAction(proposal.research_action or ThesisAction.BUY.value)
            confidence = Decimal(str(proposal.research_confidence or 0))
            account_id = proposal.account_id

        evaluator = self._evaluator()
        facts = await evaluator.load(
            instrument, instrument_currency=instrument.currency if instrument else None, now=moment
        )

        async with self.database.session() as session:
            fresh = await session.get(TradeProposal, proposal_id)
            if fresh is None:  # pragma: no cover - selected a moment ago
                return None
            identity = await self._identity_from_proposal(session, fresh)
            return await evaluator.evaluate(
                session,
                EvaluationContext(action, confidence, identity, account_id, fresh),
                facts,
                now=moment,
            )

    # ------------------------------------------------------------------
    # Lifecycle facade
    # ------------------------------------------------------------------
    async def reject(
        self, proposal_id: uuid.UUID, *, actor: str, reason: str | None = None
    ) -> TradeProposal:
        return await self.lifecycle.reject(proposal_id, actor=actor, reason=reason)

    async def cancel(
        self, proposal_id: uuid.UUID, *, actor: str, reason: str | None = None
    ) -> TradeProposal:
        return await self.lifecycle.cancel(proposal_id, actor=actor, reason=reason)

    async def sweep(self, *, now: dt.datetime | None = None) -> dict[str, int]:
        return await self.lifecycle.sweep(now=now)

    async def enqueue_pending(self, limit: int = 25) -> int:
        return await self.lifecycle.enqueue_pending(limit)

    async def enqueue_for_run(self, run_id: uuid.UUID) -> int:
        return await self.lifecycle.enqueue_for_run(run_id)

    async def enqueue_notification(
        self,
        session: AsyncSession,
        proposal_id: uuid.UUID,
        event: NotificationEvent,
        *,
        detail: str | None = None,
    ) -> None:
        await self.lifecycle.notify(session, proposal_id, event, detail=detail)

    async def _notify(
        self,
        session: AsyncSession,
        proposal_id: uuid.UUID,
        event: NotificationEvent,
        *,
        detail: str | None = None,
    ) -> None:
        await self.lifecycle.notify(session, proposal_id, event, detail=detail)

    async def _retire_approval_actions(
        self, session: AsyncSession, proposal_ids: Sequence[uuid.UUID], now: dt.datetime
    ) -> None:
        await self.lifecycle.retire_approval_actions(session, proposal_ids, now)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def origin_proposal(
        self, session: AsyncSession, broker_ticker: str
    ) -> TradeProposal | None:
        """The proposal the current position was opened on, if StockBrain opened it.

        Defined as the most recently executed BUY proposal for this listing in
        this environment.  A position StockBrain did not open has no thesis to
        exit against, and inventing one would put a research conclusion's name
        on a decision it never made.
        """
        return (
            await session.execute(
                sa.select(TradeProposal)
                .where(
                    TradeProposal.broker == self.broker,
                    TradeProposal.broker_ticker == broker_ticker,
                    TradeProposal.broker_environment == self.settings.t212_env.value,
                    TradeProposal.side == OrderSide.BUY,
                    TradeProposal.status == ProposalStatus.EXECUTED,
                    TradeProposal.thesis_id.is_not(None),
                    TradeProposal.executed_at.is_not(None),
                )
                .order_by(TradeProposal.executed_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _load_candidate(
        self, session: AsyncSession, thesis_id: uuid.UUID
    ) -> _Candidate | None:
        thesis = await session.get(Thesis, thesis_id)
        if thesis is None:
            return None
        run = await session.get(ResearchRun, thesis.research_run_id)
        if run is None:
            return None
        impact = await session.get(EventCompanyImpact, run.impact_id) if run.impact_id else None
        instrument_id = (
            impact.broker_instrument_id
            if impact is not None and impact.resolution_status is ResolutionStatus.RESOLVED
            else None
        ) or run.broker_instrument_id
        instrument = await session.get(BrokerInstrument, instrument_id) if instrument_id else None
        identity = _identity(instrument, impact, self.broker)
        return _Candidate(
            thesis=thesis,
            run=run,
            impact=impact,
            instrument=instrument,
            identity=identity,
            action=thesis.action,
            confidence=Decimal(str(thesis.confidence)),
            event_id=run.event_id,
            company_id=run.company_id,
        )

    async def _reload_identity(
        self, session: AsyncSession, candidate: _Candidate
    ) -> InstrumentIdentity:
        """Re-read the listing and its resolution verdict under the lock.

        The executable symbol always comes from the ``broker_instruments`` row
        the resolver selected.  It is never taken from the thesis, the research
        packet or a model hint, at generation or at any later point.
        """
        impact = (
            await session.get(EventCompanyImpact, candidate.impact.id)
            if candidate.impact is not None
            else None
        )
        instrument_id = (
            impact.broker_instrument_id
            if impact is not None and impact.resolution_status is ResolutionStatus.RESOLVED
            else None
        ) or (candidate.instrument.id if candidate.instrument else None)
        instrument = await session.get(BrokerInstrument, instrument_id) if instrument_id else None
        return _identity(instrument, impact, self.broker)

    async def _identity_from_proposal(
        self, session: AsyncSession, proposal: TradeProposal
    ) -> InstrumentIdentity:
        instrument = (
            await session.get(BrokerInstrument, proposal.broker_instrument_id)
            if proposal.broker_instrument_id
            else None
        )
        impact: EventCompanyImpact | None = None
        if proposal.thesis_id is not None:
            impact = (
                await session.execute(
                    sa.select(EventCompanyImpact)
                    .join(ResearchRun, ResearchRun.impact_id == EventCompanyImpact.id)
                    .join(Thesis, Thesis.research_run_id == ResearchRun.id)
                    .where(Thesis.id == proposal.thesis_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
        return _identity(instrument, impact, self.broker)

    async def _lock_account(self, session: AsyncSession, account_id: str) -> None:
        """Serialise exposure accounting for one broker account.

        A transaction-scoped PostgreSQL advisory lock, not an in-memory one: the
        guarantee has to hold across worker tasks, across processes and across a
        restart, and an ``asyncio.Lock`` holds across none of them.
        """
        key = f"stockbrain:proposals:{self.broker.value}:{account_id}"
        await session.execute(
            sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(key, 0)))
        )

    def _build_proposal(
        self,
        *,
        candidate: _Candidate,
        identity: InstrumentIdentity,
        decision: RiskDecision,
        quote: QuoteSnapshot,
        account: AccountState,
        fx: FxSnapshot | None,
        now: dt.datetime,
    ) -> TradeProposal:
        sizing = decision.sizing
        assert sizing.side is not None and sizing.reference_price is not None
        proposal = TradeProposal(
            thesis_id=candidate.thesis.id,
            event_id=candidate.event_id,
            company_id=candidate.company_id,
            broker_instrument_id=identity.broker_instrument_id,
            research_run_id=candidate.run.id,
            broker=self.broker,
            broker_ticker=identity.broker_ticker,
            account_id=account.account_id,
            broker_environment=self.settings.t212_env.value,
            side=sizing.side,
            order_type=OrderType.MARKET,
            proposed_quantity=sizing.quantity,
            reference_price=sizing.reference_price,
            reference_currency=quote.currency,
            price_source=quote.price_source,
            quote_timestamp=quote.provider_timestamp,
            quote_age_ms=quote.age_ms,
            estimated_notional=sizing.target_notional,
            # The database stores four decimal places. Round the reservation
            # upward so persistence cannot create a fraction of spare cash.
            estimated_notional_account_currency=sizing.notional_account_currency.quantize(
                Decimal("0.0001"), rounding=ROUND_CEILING
            ),
            account_currency=account.currency,
            max_quantity=sizing.max_quantity,
            max_notional=sizing.max_notional,
            sizing_reasons=[{"reason": item} for item in sizing.reasons],
            risk_snapshot=decision.as_dict(),
            risk_snapshot_hash=decision.snapshot_hash(),
            risk_outcome=decision.outcome,
            risk_policy_version=decision.policy_version,
            risk_rules=[rule.as_dict() for rule in decision.rules],
            research_action=candidate.action.value,
            research_confidence=float(candidate.confidence),
            execution_policy=self.execution_policy,
            status=ProposalStatus.READY,
            status_reason="deterministic risk allowed this trade; awaiting authorization",
            expires_at=now + dt.timedelta(minutes=self.config.proposal_ttl_minutes),
            dedupe_key=self._dedupe_key(candidate.thesis.id, identity.broker_ticker),
        )
        self._apply_quote(proposal, quote)
        self._apply_fx(proposal, fx)
        return proposal

    @staticmethod
    def _apply_fx(proposal: TradeProposal, fx: FxSnapshot | None) -> None:
        """Record the exchange rate the size was derived from.

        A same-currency proposal records ``fx_required=False`` and no rate,
        which is a different fact from a rate of 1.0 and is stored as such.
        """
        if fx is None or fx.same_currency:
            proposal.fx_required = False
            return
        proposal.fx_required = True
        proposal.fx_rate = fx.rate
        proposal.fx_base_currency = fx.base_currency
        proposal.fx_quote_currency = fx.quote_currency
        proposal.fx_direction = fx.direction().value
        proposal.fx_provider = fx.provider
        proposal.fx_rate_grade = fx.grade.value if fx.grade else None
        proposal.fx_rate_type = fx.rate_type
        proposal.fx_provider_timestamp = fx.provider_timestamp
        proposal.fx_received_at = fx.received_at
        proposal.fx_age_seconds = fx.age_seconds

    def _dedupe_key(self, thesis_id: uuid.UUID, broker_ticker: str) -> str:
        return hashlib.sha256(
            f"{thesis_id}:{self.broker.value}:{broker_ticker}:"
            f"{self.execution_policy.value}:{self.config.version}".encode()
        ).hexdigest()

    @staticmethod
    def _apply_quote(proposal: TradeProposal, quote: QuoteSnapshot) -> None:
        proposal.quote_provider = quote.provider
        proposal.quote_feed = quote.feed
        proposal.quote_bid = quote.bid
        proposal.quote_ask = quote.ask
        proposal.quote_mid = quote.mid
        proposal.quote_spread = quote.spread.spread
        proposal.quote_spread_bps = quote.spread.spread_bps
        proposal.quote_spread_status = quote.spread.status.value
        proposal.quote_timestamp = quote.provider_timestamp
        proposal.quote_age_ms = quote.age_ms
        proposal.price_source = quote.price_source
        proposal.reference_currency = quote.currency
        proposal.market_session = quote.session.value
        proposal.market_session_source = quote.session_source

    def _assert_automation_permitted(self) -> None:
        if not self.settings.automatic_authorization_permitted:
            raise AuthorizationNotPermitted(
                "automatic authorization is not permitted: "
                + "; ".join(self.settings.automation_blockers)
            )

    def _assert_authorizable(
        self, proposal: TradeProposal, source: AuthorizationSource, now: dt.datetime
    ) -> None:
        if proposal.status is ProposalStatus.INVALIDATED:
            raise ProposalInvalidated(
                proposal.invalidation_reason or "the proposal was invalidated"
            )
        if proposal.status not in AUTHORIZABLE_STATUSES:
            raise ProposalAlreadyConsumed(
                f"proposal is {proposal.status.value} and can no longer be authorized"
            )
        if proposal.expires_at <= now:
            raise ProposalExpired("the proposal expired before it was authorized")
        if (
            source is AuthorizationSource.SYSTEM_AUTOMATIC
            and proposal.execution_policy is not ExecutionPolicy.AUTOMATIC
        ):
            # Changing the deployment policy must never retroactively authorize
            # work generated under the other one.
            raise AuthorizationNotPermitted(
                "this proposal was generated under the MANUAL execution policy and "
                "cannot be authorized by the system"
            )

    @staticmethod
    def _invalidate(proposal: TradeProposal, reason: str, now: dt.datetime) -> None:
        assert_transition(proposal.status, ProposalStatus.INVALIDATED)
        proposal.status = ProposalStatus.INVALIDATED
        proposal.status_reason = reason[:1000]
        proposal.invalidated_at = now
        proposal.invalidation_reason = reason[:1000]
        proposal.updated_at = now

    async def _record_evaluation(
        self,
        *,
        stage: str,
        thesis_id: uuid.UUID | None,
        proposal_id: uuid.UUID | None,
        identity: InstrumentIdentity,
        outcome: RiskOutcome,
        rules: tuple[RuleResult, ...],
        snapshot: dict[str, object],
        snapshot_hash: str | None,
        actor: str,
        detail: str | None,
    ) -> uuid.UUID:
        async with self.database.transaction() as session:
            row = RiskEvaluation(
                stage=stage,
                thesis_id=thesis_id,
                proposal_id=proposal_id,
                broker=self.broker,
                broker_ticker=identity.broker_ticker or None,
                broker_instrument_id=identity.broker_instrument_id,
                outcome=outcome,
                policy_version=self.config.version,
                rules=[rule.as_dict() for rule in rules],
                snapshot=snapshot,
                snapshot_hash=snapshot_hash,
                actor=actor,
                detail=detail,
            )
            session.add(row)
            await session.flush()
            return row.id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _identity(
    instrument: BrokerInstrument | None,
    impact: EventCompanyImpact | None,
    broker: Broker,
) -> InstrumentIdentity:
    """Build the identity the risk engine judges.

    A missing instrument produces a well-formed identity with
    ``NOT_FOUND`` rather than ``None``, so the ``instrument_identity`` rule
    reports the refusal in the same shape as every other rule instead of the
    caller having to special-case it.
    """
    if instrument is None:
        return InstrumentIdentity(
            broker_instrument_id=None,
            broker=broker,
            broker_ticker="",
            market_symbol=None,
            resolution_status=(
                impact.resolution_status if impact is not None else ResolutionStatus.NOT_FOUND
            ),
            is_active=False,
        )
    status = impact.resolution_status if impact is not None else ResolutionStatus.RESOLVED
    return InstrumentIdentity(
        broker_instrument_id=instrument.id,
        broker=instrument.broker,
        broker_ticker=instrument.broker_ticker,
        market_symbol=instrument.market_symbol,
        resolution_status=status,
        is_active=instrument.is_active,
        instrument_type=instrument.instrument_type,
        currency=instrument.currency,
        exchange=instrument.exchange,
        isin=instrument.isin,
        company_id=instrument.company_id,
        max_open_quantity=instrument.max_open_quantity,
        extended_hours=instrument.extended_hours,
    )


def _can_reach(current: ProposalStatus, target: ProposalStatus) -> bool:
    from stockbrain.proposals.state_machine import can_transition

    return can_transition(current, target)


def _actor_type(source: AuthorizationSource) -> ActorType:
    return ActorType.USER if source.is_human else ActorType.SYSTEM


def _constraint_name(exc: IntegrityError) -> str | None:
    original = getattr(exc, "orig", None)
    return getattr(original, "constraint_name", None) or None
