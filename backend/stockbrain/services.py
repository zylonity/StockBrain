"""Runtime service container and the discovery subsystem lifecycle.

One process owns the API, the job workers, the scheduler and the Alpaca news
stream. This module builds those pieces from configuration, starts them, and
stops them cleanly.

The governing rule is *degrade, never crash*: a provider without credentials is
not constructed at all and is reported DISABLED; a provider that fails at
runtime marks its own health and leaves everything else running.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from dataclasses import dataclass, field

import sqlalchemy as sa

from stockbrain.broker.account_state import AccountStateService
from stockbrain.broker.instrument_sync import InstrumentSyncService
from stockbrain.broker.trading212_account import Trading212AccountClient
from stockbrain.broker.trading212_metadata import Trading212MetadataClient
from stockbrain.broker.trading212_orders import Trading212OrderClient
from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument, EventCompanyImpact
from stockbrain.db.models.sources import Event
from stockbrain.db.models.system import (
    AppSetting,
    DiscoveryQuery,
    DiscoveryTopic,
    Notification,
)
from stockbrain.db.session import Database
from stockbrain.enums import (
    Broker,
    CapabilityState,
    EventStatus,
    JobType,
    NotificationStatus,
    ProviderStatus,
    ResolutionStatus,
)
from stockbrain.errors import ProviderAuthError, ProviderEntitlementError
from stockbrain.execution.base import Trading212ExecutionProvider
from stockbrain.execution.reconciliation import ReconciliationService
from stockbrain.execution.service import ExecutionService
from stockbrain.fx.base import FxRateProvider
from stockbrain.fx.service import FxService, build_fx_provider
from stockbrain.ingestion.alpaca_news import AlpacaNewsClient
from stockbrain.ingestion.firecrawl import FirecrawlClient
from stockbrain.ingestion.firecrawl_budget import FirecrawlBudget
from stockbrain.ingestion.sec_edgar import SecEdgarClient
from stockbrain.ingestion.service import IngestionOutcome, IngestionService
from stockbrain.ingestion.topics import seed_default_topics
from stockbrain.instruments.service import ResolutionService
from stockbrain.intelligence.classifier import EventClassifier
from stockbrain.intelligence.research_data import AlpacaResearchProvider, FredMacroProvider
from stockbrain.intelligence.research_service import ResearchService
from stockbrain.intelligence.research_transport import ResearchTransport
from stockbrain.intelligence.semantic_dedupe import SemanticDeduplicator
from stockbrain.intelligence.service import ClassificationService
from stockbrain.intelligence.tradingagents_adapter import TradingAgentsResearchEngine
from stockbrain.jobs.handlers import (
    effective_topic_interval_minutes,
    register_ingestion_handlers,
)
from stockbrain.jobs.queue import JobQueue
from stockbrain.jobs.registry import JobRegistry
from stockbrain.jobs.runner import JobRunner
from stockbrain.jobs.scheduler import ScheduledTask, Scheduler
from stockbrain.llm.budget import BudgetGuard, BudgetStatus
from stockbrain.llm.deepseek import DeepSeekClient
from stockbrain.llm.telemetry import LlmTelemetry
from stockbrain.logging import get_logger
from stockbrain.market_data.alpaca import AlpacaMarketDataClient
from stockbrain.market_data.base import ProviderCapability
from stockbrain.observability.alerts import OperationalAlerts
from stockbrain.observability.health import ProviderHealthRegistry, ProviderName
from stockbrain.observability.metrics import METRICS
from stockbrain.proposals.service import ProposalService
from stockbrain.risk.config import RiskConfig, risk_config_from_settings
from stockbrain.telegram.runtime import TelegramRuntime

__all__ = ["DISCOVERY_PAUSED_KEY", "ServiceContainer"]

log = get_logger(__name__)

#: Persisted flag so a pause survives a restart. `/pause` stops new discovery;
#: it must never stop broker reconciliation.
DISCOVERY_PAUSED_KEY = "discovery.paused"

#: Watermark for news continuity, so a restart backfills the gap rather than
#: silently losing whatever arrived while the process was down.
NEWS_WATERMARK_KEY = "ingestion.alpaca.last_seen_at"

#: Cap on how far back a restart will backfill. Beyond this the gap is reported
#: rather than closed: a week-old news flood is not worth classifying.
MAX_BACKFILL = dt.timedelta(hours=6)


@dataclass
class ServiceContainer:
    """Everything the running application shares."""

    settings: Settings
    database: Database
    health: ProviderHealthRegistry

    ingestion: IngestionService = field(init=False)
    queue: JobQueue = field(init=False)
    registry: JobRegistry = field(init=False)
    runner: JobRunner | None = field(default=None, init=False)
    scheduler: Scheduler | None = field(default=None, init=False)

    alpaca_news: AlpacaNewsClient | None = field(default=None, init=False)
    firecrawl: FirecrawlClient | None = field(default=None, init=False)
    firecrawl_budget: FirecrawlBudget | None = field(default=None, init=False)
    sec: SecEdgarClient | None = field(default=None, init=False)

    deepseek: DeepSeekClient | None = field(default=None, init=False)
    classification: ClassificationService | None = field(default=None, init=False)
    budget: BudgetGuard | None = field(default=None, init=False)

    t212_metadata: Trading212MetadataClient | None = field(default=None, init=False)
    t212_account: Trading212AccountClient | None = field(default=None, init=False)
    account_state: AccountStateService = field(init=False)
    instrument_sync: InstrumentSyncService | None = field(default=None, init=False)
    proposals: ProposalService | None = field(default=None, init=False)
    risk_config: RiskConfig = field(init=False)
    resolution: ResolutionService | None = field(default=None, init=False)
    market_data: AlpacaMarketDataClient | None = field(default=None, init=False)
    fx_provider: FxRateProvider | None = field(default=None, init=False)
    fx: FxService = field(init=False)
    research: ResearchService | None = field(default=None, init=False)
    research_transport: ResearchTransport | None = field(default=None, init=False)
    fred: FredMacroProvider | None = field(default=None, init=False)

    control: ControlStateService = field(init=False)
    alerts: OperationalAlerts | None = field(default=None, init=False)
    telegram: TelegramRuntime | None = field(default=None, init=False)

    t212_orders: Trading212OrderClient | None = field(default=None, init=False)
    execution: ExecutionService | None = field(default=None, init=False)
    reconciliation: ReconciliationService | None = field(default=None, init=False)

    _stream_task: asyncio.Task[None] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.queue = JobQueue()
        self.registry = JobRegistry()
        self.risk_config = risk_config_from_settings(self.settings)
        # The durable pause / kill switch. Constructed unconditionally: an
        # execution control that only exists when some optional provider is
        # configured is not an execution control.
        self.control = ControlStateService(self.database)
        self._build_providers()
        if self.settings.alerts_enabled:
            # Constructed after the providers so it can read their budgets.
            # It never *changes* anything -- no pause, no halt, no cancel, no
            # order -- which is why it takes the registry and the guards rather
            # than the services that own them.
            self.alerts = OperationalAlerts(
                self.database,
                self.settings,
                health=self.health,
                llm_budget=self.budget,
                firecrawl_budget=self.firecrawl_budget,
                queue=self.queue,
            )
        if self.settings.proposals_enabled:
            self.proposals = ProposalService(
                self.database,
                self.settings,
                risk_config=self.risk_config,
                account_state=self.account_state,
                market_data=self.market_data,
                fx=self.fx,
                broker=Broker.TRADING212,
                control=self.control,
            )
            log.info(
                "proposal_service_ready",
                execution_policy=self.settings.execution_policy.value,
                risk_policy_version=self.risk_config.version,
                # Named without "authorization": the log scrubber redacts any
                # key containing it (to catch `Authorization:` headers), which
                # would turn this boolean into ***REDACTED*** and hide the
                # posture the line exists to report.
                automatic_mode_permitted=self.settings.automatic_authorization_permitted,
                automation_blockers=self.settings.automation_blockers,
                broker_order_transmission="not implemented until phase 8",
            )
        self.ingestion = IngestionService(
            self.database,
            queue=self.queue,
            # Only enqueue classification when something can actually run it.
            classification_enabled=self.classification is not None,
        )

        # The execution layer is built only when there is something to transmit
        # to *and* a proposal service to transmit for. It is deliberately not
        # gated on `order_transmission_permitted`: that predicate is re-read at
        # send time and shown in the API, so a deployment with the master switch
        # off still reports honestly rather than looking unconfigured.
        if self.proposals is not None and self.settings.broker_credentials_present:
            self.t212_orders = Trading212OrderClient(self.settings)
            provider = Trading212ExecutionProvider(self.t212_orders)
            self.execution = ExecutionService(
                self.database,
                self.settings,
                proposals=self.proposals,
                provider=provider,
                control=self.control,
                broker=Broker.TRADING212,
            )
            self.reconciliation = ReconciliationService(
                self.database,
                self.settings,
                provider=provider,
                proposals=self.proposals,
                broker=Broker.TRADING212,
            )
            log.info(
                "execution_service_ready",
                broker_environment=self.settings.t212_env.value,
                order_transmission_permitted=self.settings.order_transmission_permitted,
                order_transmission_blockers=self.settings.order_transmission_blockers,
                extended_hours=self.settings.t212_order_extended_hours,
            )

        # Built last, and only when it can actually run: enabled, a token, and a
        # non-empty numeric user allowlist. Anything less and the provider stays
        # DISABLED with nothing constructed -- there is no half-configured bot
        # that might answer a stranger. It is built after the proposal service
        # because it authorizes through that exact object, never its own copy.
        if self.settings.telegram_available:
            self.telegram = TelegramRuntime(
                self.settings,
                self.database,
                health=self.health,
                proposals=self.proposals,
                control=self.control,
            )

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_providers(self) -> None:
        settings = self.settings

        alpaca_configured = bool(
            settings.alpaca_api_key.get_secret_value()
            and settings.alpaca_api_secret.get_secret_value()
        )
        if alpaca_configured and settings.alpaca_news_enabled:
            self.alpaca_news = AlpacaNewsClient(settings)

        # The budget is constructed whether or not the client is, so the GUI and
        # the health endpoint can report yesterday's usage and today's caps for a
        # provider that is currently switched off. Constructing it does not
        # permit a call; `enabled` carries the configuration blockers, and a
        # disabled budget refuses every reservation.
        self.firecrawl_budget = FirecrawlBudget(
            self.database,
            enabled=settings.firecrawl_available,
            blockers=tuple(settings.firecrawl_blockers),
            max_searches_per_day=settings.firecrawl_max_searches_per_day,
            max_scrapes_per_day=settings.firecrawl_max_scrapes_per_day,
            daily_credit_cap=settings.firecrawl_daily_credit_cap,
            monthly_credit_cap=settings.firecrawl_monthly_credit_cap,
        )
        if settings.firecrawl_available:
            self.firecrawl = FirecrawlClient(settings)

        # data.sec.gov needs no key, but it does need a contact in the
        # User-Agent or it answers 403.
        if settings.sec_enabled and settings.sec_contact_email.strip():
            self.sec = SecEdgarClient(settings)

        if alpaca_configured and settings.alpaca_market_data_enabled:
            self.market_data = AlpacaMarketDataClient(settings)

        # Foreign exchange. Constructed unconditionally so that "no FX source"
        # is a service that reports why rather than an attribute that is None --
        # a caller that has to check for None is a caller that can forget to.
        self.fx_provider = build_fx_provider(settings)
        self.fx = FxService(settings, provider=self.fx_provider)

        # Read-only metadata and account access. Neither client has an order
        # method at all -- broker mutations arrive in Phase 8, behind the
        # four-gate live check.
        if settings.t212_metadata_enabled and settings.broker_credentials_present:
            self.t212_metadata = Trading212MetadataClient(settings)
            self.instrument_sync = InstrumentSyncService(self.database, self.t212_metadata)
        if settings.broker_credentials_present:
            self.t212_account = Trading212AccountClient(settings)

        # Constructed unconditionally: without credentials it reports "never
        # synced", which is the honest answer the risk engine needs in order to
        # fail closed, rather than a missing attribute at the point a proposal
        # needs a balance.
        self.account_state = AccountStateService(
            self.database,
            self.t212_account,
            broker=Broker.TRADING212,
            broker_environment=settings.t212_env.value,
        )

        # Resolution reads metadata already in the database, so it exists even
        # with no broker credentials; it then honestly reports NOT_FOUND.
        self.resolution = ResolutionService(self.database, broker=Broker.TRADING212)

        if settings.classifier_enabled and settings.deepseek_api_key.get_secret_value():
            self.deepseek = DeepSeekClient(settings, max_attempts=settings.deepseek_max_attempts)
            telemetry = LlmTelemetry()
            self.budget = BudgetGuard(
                self.database,
                daily_soft_usd=settings.llm_daily_soft_usd,
                daily_hard_usd=settings.llm_daily_hard_usd,
                monthly_soft_usd=settings.llm_monthly_soft_usd,
                monthly_hard_usd=settings.llm_monthly_hard_usd,
                telemetry=telemetry,
            )
            classifier = EventClassifier(
                self.deepseek,
                model=settings.deepseek_flash_model,
                timeout_seconds=settings.deepseek_timeout_seconds,
            )
            deduplicator = (
                SemanticDeduplicator(
                    self.deepseek,
                    model=settings.deepseek_flash_model,
                    merge_confidence=settings.semantic_dedupe_min_confidence,
                    timeout_seconds=settings.deepseek_timeout_seconds,
                )
                if settings.semantic_dedupe_enabled
                else None
            )
            self.classification = ClassificationService(
                self.database,
                settings,
                classifier=classifier,
                deduplicator=deduplicator,
                budget=self.budget,
                telemetry=telemetry,
            )

        if settings.research_enabled and settings.deepseek_api_key.get_secret_value():
            if self.budget is None:
                self.budget = BudgetGuard(
                    self.database,
                    daily_soft_usd=settings.llm_daily_soft_usd,
                    daily_hard_usd=settings.llm_daily_hard_usd,
                    monthly_soft_usd=settings.llm_monthly_soft_usd,
                    monthly_hard_usd=settings.llm_monthly_hard_usd,
                )
            try:
                self.research_transport = ResearchTransport(
                    settings.deepseek_api_key,
                    timeout=settings.deepseek_timeout_seconds,
                    max_tokens=settings.research_max_output_tokens,
                )
                engine = TradingAgentsResearchEngine(
                    self.research_transport,
                    quick_model=settings.deepseek_flash_model,
                    deep_model=settings.deepseek_pro_model,
                )
                if settings.fred_api_key.get_secret_value():
                    self.fred = FredMacroProvider(settings.fred_api_key)
                self.research = ResearchService(
                    self.database,
                    engine,
                    budget=self.budget,
                    health=self.health,
                    models=engine.models,
                    macro=self.fred,
                    supplemental=AlpacaResearchProvider(self.market_data)
                    if self.market_data
                    else None,
                    timeout_seconds=settings.research_timeout_seconds,
                    max_tokens=settings.research_max_output_tokens,
                )
                self.health.record(
                    ProviderName.TRADINGAGENTS,
                    ProviderStatus.HEALTHY,
                    detail="Pinned research engine loaded; provider models unprobed",
                )
            except Exception as exc:
                self.health.record(
                    ProviderName.TRADINGAGENTS,
                    ProviderStatus.DOWN,
                    detail=f"Research engine unavailable: {type(exc).__name__}",
                )
        else:
            self.health.set_disabled(
                ProviderName.TRADINGAGENTS, "Research disabled or DeepSeek key missing"
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self, *, instance_id: str) -> None:
        register_ingestion_handlers(
            self.registry,
            classifier_available=self.classification is not None,
            instrument_sync_available=self.instrument_sync is not None,
            research_available=self.research is not None,
            proposals_available=self.proposals is not None,
            account_sync_available=self.t212_account is not None,
            execution_available=self.execution is not None,
        )

        # Before a worker or the scheduler can act, say out loud whether this
        # process is coming back up halted. A crash while paused or killed must
        # not silently resume risky behaviour.
        await self.control.log_restored_state()

        async with self.database.transaction() as session:
            await seed_default_topics(session)

        self.runner = JobRunner(
            self.database,
            self.registry,
            services=self,
            concurrency=self.settings.job_worker_concurrency,
            poll_interval_seconds=self.settings.job_poll_interval_seconds,
            claim_timeout_seconds=self.settings.job_claim_timeout_seconds,
            instance_id=instance_id,
            queue=self.queue,
        )
        await self.runner.start()

        # Spec section 23 steps 8 and 12: probe what Alpaca can actually reach,
        # and refresh instrument metadata if it has gone stale. Both are
        # best-effort -- a provider failure degrades its own subsystem and
        # leaves ingestion, classification and the API running.
        await self.check_market_data_capability()
        await self._enqueue_instrument_refresh_if_stale()

        self.scheduler = Scheduler(self.database)
        self._register_schedules(self.scheduler)
        await self.scheduler.start()

        if self.alpaca_news is not None:
            self._stream_task = asyncio.create_task(self._run_news_stream(), name="alpaca-news")
        else:
            log.info("alpaca_news_stream_not_started", reason="provider not configured")

        # Spec section 23 step 16. Non-blocking by construction: if Telegram is
        # unreachable the supervisor backs off in its own task while everything
        # started above keeps running.
        if self.telegram is not None:
            await self.telegram.start()

    async def stop(self) -> None:
        if self.telegram is not None:
            await self.telegram.stop()
        if self._stream_task is not None:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stream_task
            self._stream_task = None
        if self.scheduler is not None:
            await self.scheduler.stop()
        if self.runner is not None:
            await self.runner.stop()
        for client in (
            self.t212_orders,
            self.alpaca_news,
            self.firecrawl,
            self.sec,
            self.deepseek,
            self.t212_metadata,
            self.t212_account,
            self.market_data,
            self.fx,
            self.research_transport,
            self.fred,
        ):
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.aclose()

    # ------------------------------------------------------------------
    # Schedules
    # ------------------------------------------------------------------
    def _register_schedules(self, scheduler: Scheduler) -> None:
        if self.research is not None:
            scheduler.add(
                ScheduledTask(
                    name="research_backlog",
                    interval_seconds=60,
                    run=self._research_sweep,
                    initial_delay_seconds=35,
                )
            )
        if self.firecrawl is not None:
            # The sweep ticks often and decides *nothing* about cadence: the
            # durable `next_eligible_at` column does. Ticking every five minutes
            # rather than every minute is simply five times less pointless work
            # for a provider whose cadence is measured in hours.
            scheduler.add(
                ScheduledTask(
                    name="firecrawl_topic_sweep",
                    interval_seconds=300.0,
                    run=self._enqueue_due_topic_searches,
                    initial_delay_seconds=120.0,
                )
            )
            if self.settings.firecrawl_scrape_enabled:
                scheduler.add(
                    ScheduledTask(
                        name="firecrawl_enrichment_sweep",
                        interval_seconds=300.0,
                        run=self._enqueue_firecrawl_enrichment,
                        initial_delay_seconds=180.0,
                    )
                )
        if self.sec is not None:
            scheduler.add(
                ScheduledTask(
                    name="sec_watchlist_refresh",
                    interval_seconds=300.0,
                    run=self._enqueue_sec_refresh,
                    initial_delay_seconds=30.0,
                )
            )
        if self.classification is not None:
            scheduler.add(
                ScheduledTask(
                    name="classify_pending_events",
                    interval_seconds=60.0,
                    run=self._enqueue_pending_classifications,
                    initial_delay_seconds=15.0,
                )
            )
            scheduler.add(
                ScheduledTask(
                    name="release_stalled_classifications",
                    interval_seconds=300.0,
                    run=self._release_stalled_classifications,
                    jitter_ratio=0.1,
                )
            )
            scheduler.add(
                ScheduledTask(
                    name="llm_budget_check",
                    interval_seconds=120.0,
                    run=self._refresh_budget_health,
                    jitter_ratio=0.05,
                )
            )
        if self.instrument_sync is not None:
            scheduler.add(
                ScheduledTask(
                    name="instrument_refresh",
                    interval_seconds=self.settings.instrument_refresh_interval_minutes * 60.0,
                    run=self._enqueue_instrument_refresh,
                    initial_delay_seconds=60.0,
                )
            )
        if self.market_data is not None:
            scheduler.add(
                ScheduledTask(
                    name="market_data_capability_check",
                    interval_seconds=900.0,
                    run=self._refresh_market_data_capability,
                    jitter_ratio=0.1,
                )
            )
        if self.t212_account is not None:
            scheduler.add(
                ScheduledTask(
                    name="broker_account_refresh",
                    interval_seconds=float(self.settings.t212_account_refresh_interval_seconds),
                    run=self._enqueue_account_refresh,
                    initial_delay_seconds=10.0,
                    jitter_ratio=0.1,
                )
            )
        if self.proposals is not None:
            # Spec section 22's "stale proposal sweep: every minute". The sweep
            # expires, invalidates and re-prices; the backlog enqueue is what
            # turns a published thesis into a proposal job. Both only ever
            # *enqueue* or mutate StockBrain state -- neither reaches a broker.
            scheduler.add(
                ScheduledTask(
                    name="proposal_sweep",
                    interval_seconds=60.0,
                    run=self._proposal_sweep,
                    initial_delay_seconds=45.0,
                    jitter_ratio=0.05,
                )
            )
        if self.execution is not None:
            # The scheduler only ever *enqueues*; the handler transmits. That
            # separation is what keeps a slow broker from delaying the cadence,
            # and it means a restart resumes from the database rather than from
            # an in-memory list.
            scheduler.add(
                ScheduledTask(
                    name="execution_enqueue",
                    interval_seconds=self.settings.execution_enqueue_interval_seconds,
                    run=self._enqueue_execution,
                    initial_delay_seconds=50.0,
                    jitter_ratio=0.05,
                )
            )
            scheduler.add(
                ScheduledTask(
                    name="execution_crash_recovery",
                    interval_seconds=self.settings.reconcile_interval_seconds,
                    run=self._recover_execution,
                    initial_delay_seconds=15.0,
                )
            )
        if self.reconciliation is not None:
            scheduler.add(
                ScheduledTask(
                    name="execution_reconcile",
                    interval_seconds=self.settings.reconcile_interval_seconds,
                    run=self._reconcile_execution,
                    initial_delay_seconds=70.0,
                    jitter_ratio=0.1,
                )
            )
        scheduler.add(
            ScheduledTask(
                name="resolve_pending_candidates",
                interval_seconds=120.0,
                run=self._enqueue_pending_resolutions,
                initial_delay_seconds=25.0,
            )
        )
        if self.alerts is not None:
            scheduler.add(
                ScheduledTask(
                    name="operational_alerts",
                    interval_seconds=self.settings.alert_scan_interval_seconds,
                    run=self._scan_alerts,
                    initial_delay_seconds=90.0,
                    jitter_ratio=0.1,
                )
            )
        scheduler.add(
            ScheduledTask(
                name="provider_health_persist",
                interval_seconds=60.0,
                run=self._persist_health,
                jitter_ratio=0.05,
            )
        )

    async def _discovery_paused(self) -> bool:
        async with self.database.session() as session:
            row = await session.get(AppSetting, DISCOVERY_PAUSED_KEY)
        return bool(row and row.value.get("paused"))

    async def _research_sweep(self) -> None:
        if self.research is not None:
            await self.research.sweep()
            await self.research.enqueue_event()

    async def _enqueue_due_topic_searches(self) -> None:
        """Enqueue every enabled query whose durable cooldown has elapsed.

        Three properties this has that the Phase 2 version did not:

        * **Eligibility is a column, not a subtraction.**  ``next_eligible_at``
          is written by the handler after every attempt -- succeeded, failed or
          budget-refused -- so a restart cannot reset a cooldown and a failing
          query cannot be retried on the success cadence.
        * **The interval floor is applied here too.**  A topic row asking for
          twenty minutes gets the configured floor instead, so a restored
          backup or a hand-written UPDATE cannot reintroduce the cadence that
          emptied the allowance.
        * **The budget is consulted before anything is enqueued.**  Queueing
          work that will refuse itself is noise; it also churns the dedupe key
          and the queue depth for no benefit.

        The scheduler still only *enqueues*.  It makes no HTTP call and spends
        nothing, and the dedupe key is what stops a slow provider from
        accumulating a backlog of identical searches.
        """
        if not self.settings.discovery_enabled or await self._discovery_paused():
            return
        if self.firecrawl is None or self.firecrawl_budget is None:
            return

        budget = await self.firecrawl_budget.state()
        if budget.search_exhausted:
            # Not an error and not a global failure: Alpaca news and SEC EDGAR
            # are unaffected and keep filling the pipeline.
            log.info(
                "firecrawl_sweep_skipped",
                reason="budget",
                searches_today=budget.today.searches,
                credits_today=budget.today.credits,
            )
            return

        # How many searches may still be enqueued today. Bounding the enqueue by
        # the remaining budget keeps the queue from holding jobs that exist only
        # to be refused.
        allowance = budget.searches_remaining

        async with self.database.transaction() as session:
            rows = (
                await session.execute(
                    sa.select(DiscoveryQuery, DiscoveryTopic)
                    .join(DiscoveryTopic, DiscoveryTopic.id == DiscoveryQuery.topic_id)
                    .where(DiscoveryQuery.enabled.is_(True), DiscoveryTopic.enabled.is_(True))
                    # Oldest first, so a budget that only covers part of the
                    # backlog spends it on the queries that have waited longest
                    # rather than on whichever row PostgreSQL returned first.
                    .order_by(
                        DiscoveryQuery.next_eligible_at.asc().nullsfirst(),
                        DiscoveryQuery.created_at.asc(),
                    )
                    .with_for_update(of=DiscoveryQuery, skip_locked=True)
                )
            ).all()

            # The database clock, matching what the handler wrote.
            now = (await session.execute(sa.select(sa.func.now()))).scalar_one()

            enqueued = 0
            for query, topic in rows:
                if enqueued >= allowance:
                    break
                interval = dt.timedelta(
                    minutes=effective_topic_interval_minutes(topic.interval_minutes, self.settings)
                )
                eligible_at = query.next_eligible_at
                if eligible_at is None and query.last_run_at is not None:
                    # A row written before `next_eligible_at` existed. Derive it
                    # once from the last run rather than treating the NULL as
                    # "run immediately" -- which is exactly the reading that
                    # would re-run every query the moment this ships.
                    eligible_at = query.last_run_at + interval
                    query.next_eligible_at = eligible_at
                if eligible_at is not None and eligible_at > now:
                    continue
                job_id = await self.queue.enqueue(
                    session,
                    JobType.FIRECRAWL_TOPIC_SEARCH,
                    payload={"query_id": str(query.id), "topic": topic.slug},
                    # One outstanding job per query: a slow provider must not let
                    # a backlog of identical searches build up, and two workers
                    # must not pay for the same search twice.
                    dedupe_key=f"firecrawl:{query.id}",
                    priority=60,
                    # A paid call. One attempt only -- the queue's retry would be
                    # a second reservation for the same search, and the durable
                    # cooldown is the retry.
                    max_attempts=1,
                )
                if job_id is not None:
                    enqueued += 1
                    # Claim the slot immediately so a second scheduler loop, or
                    # this one on its next tick, cannot enqueue the same query
                    # again before the handler has written its own cooldown.
                    query.next_eligible_at = now + interval
            if enqueued:
                log.info(
                    "firecrawl_searches_enqueued",
                    count=enqueued,
                    searches_remaining_today=allowance - enqueued,
                )

    async def _enqueue_firecrawl_enrichment(self) -> None:
        """Offer triaged Firecrawl sources for a paid content fetch.

        Stage two of the two-stage model.  Bounded twice: by the batch size
        here, and by the durable scrape budget inside the handler.
        """
        if not self.settings.discovery_enabled or await self._discovery_paused():
            return
        if self.firecrawl is None or self.firecrawl_budget is None:
            return
        if not self.settings.firecrawl_scrape_enabled:
            return
        budget = await self.firecrawl_budget.state()
        if budget.scrape_exhausted:
            return
        await self.ingestion.enqueue_content_fetches(limit=budget.scrapes_remaining)

    async def _enqueue_sec_refresh(self) -> None:
        if not self.settings.discovery_enabled or await self._discovery_paused():
            return
        async with self.database.transaction() as session:
            await self.queue.enqueue(
                session,
                JobType.SEC_REFRESH,
                payload={"since_days": 3},
                dedupe_key="sec:watchlist",
                priority=50,
            )

    async def _persist_health(self) -> None:
        await self.health.persist(self.database)

    async def _scan_alerts(self) -> None:
        """Look for the conditions worth telling somebody about, and enqueue them.

        The scan writes notification rows; delivery is a job, exactly as it is
        for a proposal transition. An alert that already happened must not fail
        because Telegram is unreachable, and a redelivered job is harmless
        because the row's dedupe key -- not this method's memory -- is what makes
        the message once-only.
        """
        if self.alerts is None:
            return
        scan = await self.alerts.scan()
        if not (scan.fired or scan.resolved):
            return
        async with self.database.transaction() as session:
            pending = (
                await session.execute(
                    sa.select(Notification.id).where(
                        Notification.entity_type == "alert",
                        Notification.status == NotificationStatus.PENDING,
                    )
                )
            ).scalars()
            for notification_id in pending:
                await self.queue.enqueue(
                    session,
                    JobType.SEND_NOTIFICATION,
                    payload={"notification_id": str(notification_id)},
                    dedupe_key=f"alert-send:{notification_id}",
                    priority=10,
                    # One attempt: a failed alert is recorded and never
                    # auto-resent, the same rule as a proposal notification.
                    max_attempts=1,
                )

    async def _enqueue_instrument_refresh(self) -> None:
        if self.instrument_sync is None:
            return
        async with self.database.transaction() as session:
            await self.queue.enqueue(
                session,
                JobType.INSTRUMENT_REFRESH,
                payload={"broker": Broker.TRADING212.value},
                # One outstanding refresh at a time: the endpoint allows one
                # request per 50 seconds, so a backlog would only ever wait.
                dedupe_key=f"instruments:{Broker.TRADING212.value}",
                priority=70,
            )

    async def _enqueue_instrument_refresh_if_stale(self) -> None:
        """Startup step 12: refresh broker instruments if they have gone stale."""
        if self.instrument_sync is None:
            return
        async with self.database.session() as session:
            newest = (
                await session.execute(
                    sa.select(sa.func.max(BrokerInstrument.last_refreshed_at)).where(
                        BrokerInstrument.broker == Broker.TRADING212
                    )
                )
            ).scalar_one_or_none()
        cutoff = dt.timedelta(hours=self.settings.instrument_staleness_hours)
        if newest is not None and (utcnow() - newest) < cutoff:
            log.info("instrument_metadata_fresh", last_refreshed_at=newest.isoformat())
            return
        await self._enqueue_instrument_refresh()
        log.info("instrument_refresh_enqueued_at_startup", reason="stale or never synced")

    async def _enqueue_account_refresh(self) -> None:
        """Queue a read-only broker cash and position refresh.

        The scheduler only enqueues; the handler does the two GETs. One
        outstanding refresh at a time, because a backlog of identical account
        reads would only ever wait on the same 1-req/5s bucket.
        """
        if self.t212_account is None:
            return
        async with self.database.transaction() as session:
            await self.queue.enqueue(
                session,
                JobType.BROKER_RECONCILE,
                payload={"broker": Broker.TRADING212.value},
                dedupe_key=f"account:{Broker.TRADING212.value}",
                priority=20,
            )

    async def _enqueue_execution(self) -> None:
        if self.execution is not None:
            await self.execution.enqueue_ready()

    async def _recover_execution(self) -> None:
        """Find attempts stranded mid-flight by a crash and mark them ambiguous.

        Never resends. This is the sweep that turns "we recorded a send and then
        died" into a reconciliation task rather than into a duplicate order.
        """
        if self.execution is not None:
            await self.execution.recover_incomplete()

    async def _reconcile_execution(self) -> None:
        if self.reconciliation is not None:
            await self.reconciliation.sweep()

    async def _proposal_sweep(self) -> None:
        """Expire, invalidate and re-price proposals, then queue new ones."""
        if self.proposals is None:
            return
        await self.proposals.sweep()
        await self.proposals.enqueue_pending()

    async def _enqueue_pending_resolutions(self) -> None:
        """Sweep impacts whose resolution never ran.

        Covers the cases the post-classification enqueue cannot: impacts created
        before any instrument metadata existed, and jobs lost to a dying worker.
        Re-resolving an already-resolved impact is harmless but wasteful, so only
        PENDING rows are swept -- an AMBIGUOUS result is a decision awaiting a
        human, not work to retry every two minutes.
        """
        async with self.database.transaction() as session:
            event_ids = (
                await session.execute(
                    sa.select(EventCompanyImpact.event_id)
                    .where(EventCompanyImpact.resolution_status == ResolutionStatus.PENDING)
                    .group_by(EventCompanyImpact.event_id)
                    .limit(25)
                )
            ).scalars()
            enqueued = 0
            for event_id in event_ids:
                job_id = await self.queue.enqueue(
                    session,
                    JobType.RESOLVE_CANDIDATES,
                    payload={"event_id": str(event_id)},
                    dedupe_key=f"resolve:{event_id}",
                    priority=30,
                )
                if job_id is not None:
                    enqueued += 1
        if enqueued:
            log.info("resolution_backlog_enqueued", count=enqueued)

    async def check_market_data_capability(
        self, *, refresh: bool = False
    ) -> ProviderCapability | None:
        """Probe Alpaca and record what it can actually reach.

        Never raises: an entitlement gap degrades pricing and nothing else. The
        precise state (AUTH_FAILED vs ENTITLEMENT_MISSING) is kept in the health
        record's metrics, because the coarse persisted vocabulary cannot express
        the difference and the difference is what an operator needs.
        """
        if self.market_data is None:
            return None
        try:
            capability = await self.market_data.capability(refresh=refresh)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("market_data_capability_error", error_type=type(exc).__name__)
            self.health.record(
                ProviderName.ALPACA_MARKET_DATA,
                ProviderStatus.DOWN,
                detail=f"{type(exc).__name__}",
            )
            return None

        self.health.record(
            ProviderName.ALPACA_MARKET_DATA,
            capability.state.to_provider_status(),
            detail=capability.detail
            or (
                f"feed={capability.feed} realtime_pricing_usable="
                f"{capability.realtime_pricing_usable}"
            ),
            metrics=capability.as_dict(),
        )
        if capability.state is CapabilityState.ENTITLEMENT_MISSING:
            log.warning(
                "market_data_entitlement_missing",
                feed=capability.feed,
                remediation=(
                    "ingestion, classification and instrument resolution continue; "
                    "proposal sizing stays blocked rather than using unsuitable data"
                ),
            )
        return capability

    async def _refresh_market_data_capability(self) -> None:
        await self.check_market_data_capability(refresh=True)

    async def _enqueue_pending_classifications(self) -> None:
        """Sweep events still awaiting classification.

        Covers three cases the ingest-time enqueue cannot: events ingested before
        a key was configured, events whose job was lost, and events left in NEW by
        a budget pause. The dedupe key means re-enqueuing an event that already
        has a pending job is a no-op.
        """
        async with self.database.transaction() as session:
            pending = (
                await session.execute(
                    sa.select(Event.id)
                    .where(Event.status == EventStatus.NEW)
                    .order_by(Event.first_seen_at.asc())
                    .limit(50)
                )
            ).scalars()
            enqueued = 0
            for event_id in pending:
                job_id = await self.queue.enqueue(
                    session,
                    JobType.CLASSIFY_EVENT,
                    payload={"event_id": str(event_id)},
                    dedupe_key=f"classify:{event_id}",
                    priority=20,
                )
                if job_id is not None:
                    enqueued += 1
        if enqueued:
            log.info("classification_backlog_enqueued", count=enqueued)

    async def _release_stalled_classifications(self) -> None:
        """Recover events whose classifying worker died."""
        if self.classification is not None:
            await self.classification.release_stalled()

    async def _refresh_budget_health(self) -> None:
        """Reflect LLM budget state in the research subsystem's health.

        A budget stop degrades research only. Ingestion, deduplication and broker
        reconciliation are untouched, by construction: nothing in those paths
        consults the budget guard.
        """
        if self.budget is None:
            return
        state = await self.budget.state(refresh=True)
        if state.status is BudgetStatus.HARD_EXCEEDED or state.status is BudgetStatus.SOFT_EXCEEDED:
            self.health.record(ProviderName.DEEPSEEK, ProviderStatus.DEGRADED, detail=state.reason)

    # ------------------------------------------------------------------
    # News stream
    # ------------------------------------------------------------------
    async def _run_news_stream(self) -> None:
        """Consume the Alpaca news stream and ingest every article.

        Entitlement failures move discovery into a degraded state and stop the
        stream; they never crash the process. Firecrawl continues to run, which
        is the documented fallback for exactly this case.
        """
        assert self.alpaca_news is not None
        client = self.alpaca_news

        await self._backfill_news_gap()

        try:
            self.health.record(
                ProviderName.ALPACA_NEWS, ProviderStatus.UNKNOWN, detail="connecting"
            )
            async for document in client.stream():
                self.health.record(ProviderName.ALPACA_NEWS, ProviderStatus.HEALTHY)
                result = await self.ingestion.ingest(document)
                if result.outcome is IngestionOutcome.CREATED_EVENT:
                    await self._record_news_watermark(document.published_at or utcnow())
        except asyncio.CancelledError:
            raise
        except (ProviderAuthError, ProviderEntitlementError) as exc:
            self.health.record(
                ProviderName.ALPACA_NEWS,
                ProviderStatus.DOWN,
                detail=f"{type(exc).__name__}: {exc}"[:300],
            )
            log.error(
                "alpaca_news_stream_fatal",
                error_type=type(exc).__name__,
                remediation="discovery continues via Firecrawl and SEC",
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.health.record(ProviderName.ALPACA_NEWS, ProviderStatus.DOWN, detail=str(exc)[:300])
            log.exception("alpaca_news_stream_stopped", error=str(exc))

    async def _backfill_news_gap(self) -> None:
        """Enqueue a backfill covering the downtime since the last article seen."""
        async with self.database.session() as session:
            row = await session.get(AppSetting, NEWS_WATERMARK_KEY)
        watermark = row.value.get("last_seen_at") if row else None
        if not watermark:
            return

        try:
            start = dt.datetime.fromisoformat(str(watermark))
        except ValueError:
            return

        now = utcnow()
        gap = now - start
        if gap <= dt.timedelta(minutes=1):
            return
        if gap > MAX_BACKFILL:
            log.warning(
                "news_backfill_window_truncated",
                gap_hours=round(gap.total_seconds() / 3600, 2),
                max_hours=MAX_BACKFILL.total_seconds() / 3600,
            )
            start = now - MAX_BACKFILL

        async with self.database.transaction() as session:
            await self.queue.enqueue(
                session,
                "ALPACA_NEWS_BACKFILL",
                payload={"start": start.isoformat(), "end": now.isoformat(), "limit": 200},
                dedupe_key="alpaca:backfill",
                priority=30,
            )
        log.info("news_backfill_enqueued", start=start.isoformat(), end=now.isoformat())

    async def _record_news_watermark(self, seen_at: dt.datetime) -> None:
        async with self.database.transaction() as session:
            row = await session.get(AppSetting, NEWS_WATERMARK_KEY)
            if row is None:
                session.add(
                    AppSetting(
                        key=NEWS_WATERMARK_KEY,
                        value={"last_seen_at": seen_at.isoformat()},
                        description="Most recent Alpaca news article timestamp ingested.",
                    )
                )
            else:
                row.value = {"last_seen_at": seen_at.isoformat()}
                row.updated_at = utcnow()
        METRICS.set("stockbrain_news_watermark_epoch", seen_at.timestamp())
