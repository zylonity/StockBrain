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
from dataclasses import dataclass
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.broker.account_state import DEFAULT_ACCOUNT_ID, AccountStateService
from stockbrain.broker.automation import BrokerAutomationCapability, automation_capability
from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument, EventCompanyImpact
from stockbrain.db.models.proposals import ApprovalAction, RiskEvaluation, TradeProposal
from stockbrain.db.models.research import ResearchRun, Thesis
from stockbrain.db.models.system import AuditLog
from stockbrain.db.session import Database
from stockbrain.enums import (
    ActorType,
    AuthorizationSource,
    Broker,
    ExecutionPolicy,
    JobType,
    NotificationEvent,
    OrderSide,
    OrderType,
    ProposalStatus,
    ResearchStatus,
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
from stockbrain.fx.base import normalize_currency
from stockbrain.fx.service import FxService
from stockbrain.jobs.queue import JobQueue
from stockbrain.logging import get_logger
from stockbrain.market_data.base import MarketDataProvider
from stockbrain.observability.metrics import METRICS
from stockbrain.proposals.quotes import QuoteFetcher
from stockbrain.proposals.state_machine import (
    ACTIVE_STATUSES,
    AUTHORIZABLE_STATUSES,
    EXPOSURE_RESERVING_STATUSES,
    assert_transition,
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
    action_is_executable,
    fx_rate_drift,
    proposal_ttl,
    reference_price_drift,
)

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
    thesis_id: uuid.UUID
    created: bool
    proposal_id: uuid.UUID | None = None
    evaluation_id: uuid.UUID | None = None
    outcome: RiskOutcome = RiskOutcome.BLOCK
    reason: str = ""
    authorized: bool = False
    blocks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Revalidation:
    """A fresh, read-only verdict on a proposal.  Changes nothing.

    Produced by :meth:`ProposalService.revalidate`, which Phase 8's pre-send
    preflight uses so that the checks guarding a broker POST are *the same
    checks*, from the same engine and the same rule functions, that guarded the
    authorization.  A second implementation of "is this still safe" is how two
    answers start disagreeing.
    """

    proposal_id: uuid.UUID
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
    ) -> None:
        self.database = database
        self.settings = settings
        self.config = risk_config
        self.account_state = account_state
        self.quotes = QuoteFetcher(market_data)
        # Optional so a same-currency deployment (and a unit test) needs no FX
        # source at all. Absent, `_fx_snapshot` reports "no provider" for a
        # cross-currency pair, which blocks -- it never falls back to parity.
        self.fx = fx or FxService(settings, provider=None)
        self.broker = broker
        self.engine = engine or RiskEngine()
        self.queue = JobQueue()
        # Durable pause / kill switch. Optional so a unit test can build the
        # service without one; absent, nothing is halted, which is the same
        # answer an empty table gives.
        self.control = control or ControlStateService(database)

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

    async def _fx_snapshot(
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

        account, account_reason = await self.account_state.load(
            max_age_seconds=self.config.max_account_state_age_seconds, now=moment
        )
        quote: QuoteSnapshot | None = None
        quote_reason: str | None = "the listing could not be identified for pricing"
        if candidate.instrument is not None:
            async with self.database.session() as session:
                quote, quote_reason = await self.quotes.fetch(
                    session, candidate.instrument, self.config, now=moment
                )

        fx = await self._fx_snapshot(
            account=account,
            instrument_currency=candidate.identity.currency,
            now=moment,
        )
        account_id = account.account_id if account else DEFAULT_ACCOUNT_ID

        async with self.database.transaction() as session:
            await self._lock_account(session, account_id)

            # Identity is re-read under the lock: a sync that retired the
            # listing between the load and here must not slip through.
            fresh_identity = await self._reload_identity(session, candidate)
            reserved = await self._reserved_exposure(
                session, account_id, fresh_identity.broker_ticker
            )
            decision = self.engine.evaluate(
                RiskInputs(
                    config=self.config,
                    action=candidate.action,
                    confidence=candidate.confidence,
                    identity=fresh_identity,
                    account=account,
                    quote=quote,
                    reserved=reserved,
                    now=moment,
                    fx=fx,
                    account_state_missing_reason=account_reason,
                    quote_missing_reason=quote_reason,
                ),
                now=moment,
            )

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
                detail="; ".join(decision.blocks) or None,
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
        account, account_reason = await self.account_state.load(
            max_age_seconds=self.config.max_account_state_age_seconds, now=moment
        )
        quote: QuoteSnapshot | None = None
        quote_reason: str | None = "the listing could not be identified for pricing"
        if instrument is not None:
            async with self.database.session() as session:
                quote, quote_reason = await self.quotes.fetch(
                    session, instrument, self.config, now=moment
                )

        # Re-resolved here, not carried from generation. An authorization that
        # trusted the rate the proposal was drafted with would be authorizing a
        # size derived from a number that may be hours old.
        fx = await self._fx_snapshot(
            account=account,
            instrument_currency=instrument.currency if instrument else None,
            now=moment,
        )

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
            reserved = await self._reserved_exposure(
                session,
                locked.account_id,
                identity.broker_ticker,
                exclude_proposal_id=locked.id,
            )
            decision = self.engine.evaluate(
                RiskInputs(
                    config=self.config,
                    action=action,
                    confidence=confidence,
                    identity=identity,
                    account=account,
                    quote=quote,
                    reserved=reserved,
                    now=moment,
                    fx=fx,
                    authorized_fx_rate=locked.fx_rate,
                    account_state_missing_reason=account_reason,
                    quote_missing_reason=quote_reason,
                ),
                now=moment,
            )
            rules = (
                *decision.rules,
                *self._authorization_rules(locked, decision, quote, fx, moment),
            )
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

        account, account_reason = await self.account_state.load(
            max_age_seconds=self.config.max_account_state_age_seconds, now=moment
        )
        quote: QuoteSnapshot | None = None
        quote_reason: str | None = "the listing could not be identified for pricing"
        if instrument is not None:
            async with self.database.session() as session:
                quote, quote_reason = await self.quotes.fetch(
                    session, instrument, self.config, now=moment
                )

        fx = await self._fx_snapshot(
            account=account,
            instrument_currency=instrument.currency if instrument else None,
            now=moment,
        )

        async with self.database.session() as session:
            fresh = await session.get(TradeProposal, proposal_id)
            if fresh is None:  # pragma: no cover - selected a moment ago
                return None
            identity = await self._identity_from_proposal(session, fresh)
            reserved = await self._reserved_exposure(
                session, account_id, identity.broker_ticker, exclude_proposal_id=proposal_id
            )
            decision = self.engine.evaluate(
                RiskInputs(
                    config=self.config,
                    action=action,
                    confidence=confidence,
                    identity=identity,
                    account=account,
                    quote=quote,
                    reserved=reserved,
                    now=moment,
                    fx=fx,
                    authorized_fx_rate=fresh.fx_rate,
                    account_state_missing_reason=account_reason,
                    quote_missing_reason=quote_reason,
                ),
                now=moment,
            )
            rules = (
                *decision.rules,
                *self._authorization_rules(fresh, decision, quote, fx, moment),
            )
        return Revalidation(
            proposal_id=proposal_id,
            decision=decision,
            rules=rules,
            quote=quote,
            account=account,
            fx=fx,
            quote_reason=quote_reason,
            account_reason=account_reason,
        )

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
        async with self.database.transaction() as session:
            proposal = (
                await session.execute(
                    sa.select(TradeProposal)
                    .where(TradeProposal.id == proposal_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if proposal is None:
                raise ProposalAlreadyConsumed("proposal not found")
            if not _can_reach(proposal.status, target):
                raise ProposalAlreadyConsumed(
                    f"proposal is {proposal.status.value} and can no longer be "
                    f"{target.value.lower()}"
                )
            assert_transition(proposal.status, target)
            proposal.status = target
            proposal.status_reason = reason[:1000]
            proposal.updated_at = now
            if target is ProposalStatus.REJECTED:
                proposal.rejected_at = now
                proposal.rejected_by = actor
            await self._retire_approval_actions(session, [proposal.id], now)
            if target is ProposalStatus.REJECTED:
                await self._notify(session, proposal.id, NotificationEvent.PROPOSAL_REJECTED)
            session.add(
                AuditLog(
                    actor_type=ActorType.USER,
                    actor_id=actor,
                    action=action,
                    entity_type="trade_proposal",
                    entity_id=proposal.id,
                    details={"reason": reason[:1000], "previous_status": proposal.status.value},
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
        async with self.database.transaction() as session:
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
            await self._retire_approval_actions(session, expired, now)
            for proposal_id in expired:
                await self._notify(session, proposal_id, NotificationEvent.PROPOSAL_EXPIRED)
            return len(expired)

    async def _invalidate_stale_preconditions(self, now: dt.datetime) -> int:
        """Invalidate proposals whose durable preconditions stopped holding.

        Covers the conditions a sweep can see without a market-data call:
        the listing changed, the risk policy changed, a newer thesis superseded
        this one, or a position a reduction depends on is gone.
        """
        count = 0
        async with self.database.transaction() as session:
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
                if not _can_reach(proposal.status, ProposalStatus.INVALIDATED):
                    continue
                self._invalidate(proposal, reason, now)
                await self._retire_approval_actions(session, [proposal.id], now)
                await self._notify(
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
            and proposal.risk_policy_version != self.config.version
        ):
            return (
                "the risk configuration changed after this proposal was generated "
                f"(policy {proposal.risk_policy_version} -> {self.config.version})"
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

        if proposal.thesis_id is not None:
            newer = await session.scalar(
                sa.select(sa.func.count())
                .select_from(Thesis)
                .join(ResearchRun, ResearchRun.id == Thesis.research_run_id)
                .where(
                    Thesis.supersedes_thesis_id == proposal.thesis_id,
                    ResearchRun.status == ResearchStatus.SUCCEEDED,
                )
            )
            if newer:
                return "a newer thesis supersedes the one this proposal was built on"

        if proposal.side is OrderSide.SELL:
            account, _reason = await self.account_state.load(
                max_age_seconds=self.config.max_account_state_age_seconds, now=now
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
        limit = max(0, self.settings.proposal_revalidation_batch)
        if limit == 0:
            return 0
        async with self.database.session() as session:
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

        account, _ = await self.account_state.load(
            max_age_seconds=self.config.max_account_state_age_seconds, now=now
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
            async with self.database.session() as session:
                quote, quote_reason = await self.quotes.fetch(
                    session, instrument, self.config, now=now
                )
            fx: FxSnapshot | None = None
            if proposal.fx_required:
                pair_key = (
                    normalize_currency(account.currency if account else None),
                    normalize_currency(instrument.currency),
                )
                if pair_key not in fx_by_pair:
                    fx_by_pair[pair_key] = await self._fx_snapshot(
                        account=account,
                        instrument_currency=instrument.currency,
                        now=now,
                    )
                fx = fx_by_pair[pair_key]
            failure = self._market_failure(proposal, quote, quote_reason, fx, now)
            if failure is None:
                continue
            async with self.database.transaction() as session:
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
                self._invalidate(locked, failure, now)
                await self._retire_approval_actions(session, [locked.id], now)
                await self._notify(
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

    def _market_failure(
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
            drift_fx = fx_rate_drift(
                proposal.fx_rate, fx, max_drift_pct=self.settings.fx_max_rate_drift_pct
            )
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
        drift = reference_price_drift(
            proposal.reference_price,
            quote.mid,
            max_drift_pct=self.config.max_reference_price_drift_pct,
        )
        if drift.outcome is RuleOutcome.BLOCK:
            return drift.reason
        return None

    # ------------------------------------------------------------------
    # Enqueueing
    # ------------------------------------------------------------------
    async def enqueue_pending(self, limit: int = 25) -> int:
        """Queue proposal generation for every published thesis without one.

        Restart-safe by construction: the backlog is derived from the database,
        not from an in-memory list, and the dedupe key means re-enqueuing a
        thesis that already has a pending job is a no-op.
        """
        if blockers := await self.control_blockers():
            log.debug("proposal_backlog_skipped", blockers=blockers)
            return 0
        enqueued = 0
        async with self.database.transaction() as session:
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
                                    RiskEvaluation.policy_version == self.config.version,
                                )
                            ),
                        )
                        .order_by(Thesis.created_at.desc())
                        .limit(limit)
                    )
                ).scalars()
            )
            for thesis_id in thesis_ids:
                job_id = await self.queue.enqueue(
                    session,
                    JobType.GENERATE_PROPOSAL,
                    payload={"thesis_id": str(thesis_id)},
                    dedupe_key=f"proposal:{thesis_id}",
                    priority=25,
                )
                if job_id is not None:
                    enqueued += 1
        if enqueued:
            log.info("proposal_generation_enqueued", count=enqueued)
        return enqueued

    async def enqueue_for_run(self, run_id: uuid.UUID) -> int:
        """Queue generation for the thesis a completed research run published."""
        async with self.database.transaction() as session:
            thesis_id = await session.scalar(
                sa.select(Thesis.id).where(Thesis.research_run_id == run_id)
            )
            if thesis_id is None:
                return 0
            job_id = await self.queue.enqueue(
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
        await self._notify(session, proposal_id, event, detail=detail)

    async def _notify(
        self,
        session: AsyncSession,
        proposal_id: uuid.UUID,
        event: NotificationEvent,
        *,
        detail: str | None = None,
    ) -> None:
        """Queue one outbound notification for a proposal transition.

        Queued, never sent inline: a Telegram outage must not be able to fail an
        authorization that has already happened, and a notification is derived
        from database state rather than being the record of it.

        Once-only by construction at two layers -- ``uq_jobs_dedupe_key_active``
        stops a second *pending* job for the same transition, and the
        notification row's own unique dedupe key stops a redelivered job or a
        restarted process producing a second message.
        """
        await self.queue.enqueue(
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
    async def _retire_approval_actions(
        session: AsyncSession, proposal_ids: Sequence[uuid.UUID], now: dt.datetime
    ) -> None:
        """Consume every outstanding approval token for these proposals.

        Called on every transition that ends a proposal's authorizable life.
        This -- not blanking a keyboard -- is what makes an old button inert: a
        message can be forwarded, screenshotted, or edited by a client that
        refuses the edit, and none of that reaches the row the token names.
        """
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

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
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

    async def _reserved_exposure(
        self,
        session: AsyncSession,
        account_id: str,
        broker_ticker: str,
        *,
        exclude_proposal_id: uuid.UUID | None = None,
    ) -> ReservedExposure:
        """What live proposals have already claimed.

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
                    ).where(*conditions)
                )
            ).all()
        )
        notional = sum((row.estimated_notional for row in rows if row.side is OrderSide.BUY), ZERO)
        same = [row for row in rows if row.broker_ticker == broker_ticker]
        return ReservedExposure(
            count=len(rows),
            notional=notional,
            same_instrument_count=len(same),
            same_instrument_notional=sum(
                (row.estimated_notional for row in same if row.side is OrderSide.BUY), ZERO
            ),
            same_instrument_sides=tuple(sorted({row.side.value for row in same})),
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
            estimated_notional_account_currency=sizing.notional_account_currency,
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
            reference_price_drift(
                proposal.reference_price,
                quote.mid if quote else None,
                max_drift_pct=self.config.max_reference_price_drift_pct,
            ),
            # The FX analogue, and load-bearing on a GBP account holding USD
            # listings: a one percent move in the pair moves the trade's
            # account-currency notional by one percent, straight through the
            # per-trade cap, the cash reserve and the concentration limit.
            fx_rate_drift(
                proposal.fx_rate,
                fx,
                max_drift_pct=self.settings.fx_max_rate_drift_pct,
            ),
            _policy_version_unchanged(proposal, self.config.version),
            _envelope_still_covers(proposal, decision),
        ]
        return rules

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


def _can_reach(current: ProposalStatus, target: ProposalStatus) -> bool:
    from stockbrain.proposals.state_machine import can_transition

    return can_transition(current, target)


def _actor_type(source: AuthorizationSource) -> ActorType:
    return ActorType.USER if source.is_human else ActorType.SYSTEM


def _constraint_name(exc: IntegrityError) -> str | None:
    original = getattr(exc, "orig", None)
    return getattr(original, "constraint_name", None) or None
