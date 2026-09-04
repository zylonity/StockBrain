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

from stockbrain.config import Settings
from stockbrain.db.base import utcnow
from stockbrain.db.models.sources import Event
from stockbrain.db.models.system import AppSetting, DiscoveryQuery, DiscoveryTopic
from stockbrain.db.session import Database
from stockbrain.enums import EventStatus, JobType, ProviderStatus
from stockbrain.errors import ProviderAuthError, ProviderEntitlementError
from stockbrain.ingestion.alpaca_news import AlpacaNewsClient
from stockbrain.ingestion.firecrawl import FirecrawlClient
from stockbrain.ingestion.sec_edgar import SecEdgarClient
from stockbrain.ingestion.service import IngestionOutcome, IngestionService
from stockbrain.ingestion.topics import seed_default_topics
from stockbrain.intelligence.classifier import EventClassifier
from stockbrain.intelligence.semantic_dedupe import SemanticDeduplicator
from stockbrain.intelligence.service import ClassificationService
from stockbrain.jobs.handlers import register_ingestion_handlers
from stockbrain.jobs.queue import JobQueue
from stockbrain.jobs.registry import JobRegistry
from stockbrain.jobs.runner import JobRunner
from stockbrain.jobs.scheduler import ScheduledTask, Scheduler
from stockbrain.llm.budget import BudgetGuard, BudgetStatus
from stockbrain.llm.deepseek import DeepSeekClient
from stockbrain.llm.telemetry import LlmTelemetry
from stockbrain.logging import get_logger
from stockbrain.observability.health import ProviderHealthRegistry, ProviderName
from stockbrain.observability.metrics import METRICS

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
    sec: SecEdgarClient | None = field(default=None, init=False)

    deepseek: DeepSeekClient | None = field(default=None, init=False)
    classification: ClassificationService | None = field(default=None, init=False)
    budget: BudgetGuard | None = field(default=None, init=False)

    _stream_task: asyncio.Task[None] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.queue = JobQueue()
        self.registry = JobRegistry()
        self._build_providers()
        self.ingestion = IngestionService(
            self.database,
            queue=self.queue,
            # Only enqueue classification when something can actually run it.
            classification_enabled=self.classification is not None,
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

        if settings.firecrawl_enabled and settings.firecrawl_api_key.get_secret_value():
            self.firecrawl = FirecrawlClient(settings)

        # data.sec.gov needs no key, but it does need a contact in the
        # User-Agent or it answers 403.
        if settings.sec_enabled and settings.sec_contact_email.strip():
            self.sec = SecEdgarClient(settings)

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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self, *, instance_id: str) -> None:
        register_ingestion_handlers(
            self.registry, classifier_available=self.classification is not None
        )

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

        self.scheduler = Scheduler(self.database)
        self._register_schedules(self.scheduler)
        await self.scheduler.start()

        if self.alpaca_news is not None:
            self._stream_task = asyncio.create_task(self._run_news_stream(), name="alpaca-news")
        else:
            log.info("alpaca_news_stream_not_started", reason="provider not configured")

    async def stop(self) -> None:
        if self._stream_task is not None:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stream_task
            self._stream_task = None
        if self.scheduler is not None:
            await self.scheduler.stop()
        if self.runner is not None:
            await self.runner.stop()
        for client in (self.alpaca_news, self.firecrawl, self.sec, self.deepseek):
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.aclose()

    # ------------------------------------------------------------------
    # Schedules
    # ------------------------------------------------------------------
    def _register_schedules(self, scheduler: Scheduler) -> None:
        if self.firecrawl is not None:
            scheduler.add(
                ScheduledTask(
                    name="firecrawl_topic_sweep",
                    interval_seconds=60.0,
                    run=self._enqueue_due_topic_searches,
                    initial_delay_seconds=20.0,
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

    async def _enqueue_due_topic_searches(self) -> None:
        """Enqueue every enabled query whose topic interval has elapsed.

        The scheduler ticks every minute and decides *which* queries are due from
        their own ``last_run_at``, so a per-topic interval needs no per-topic
        timer and survives a restart.
        """
        if not self.settings.discovery_enabled or await self._discovery_paused():
            return

        now = utcnow()
        async with self.database.transaction() as session:
            rows = (
                await session.execute(
                    sa.select(DiscoveryQuery, DiscoveryTopic)
                    .join(DiscoveryTopic, DiscoveryTopic.id == DiscoveryQuery.topic_id)
                    .where(DiscoveryQuery.enabled.is_(True), DiscoveryTopic.enabled.is_(True))
                )
            ).all()

            enqueued = 0
            for query, topic in rows:
                interval = dt.timedelta(minutes=max(1, topic.interval_minutes))
                if query.last_run_at is not None and (now - query.last_run_at) < interval:
                    continue
                job_id = await self.queue.enqueue(
                    session,
                    JobType.FIRECRAWL_TOPIC_SEARCH,
                    payload={"query_id": str(query.id), "topic": topic.slug},
                    # One outstanding job per query: a slow provider must not let
                    # a backlog of identical searches build up.
                    dedupe_key=f"firecrawl:{query.id}",
                    priority=60,
                )
                if job_id is not None:
                    enqueued += 1
            if enqueued:
                log.info("firecrawl_searches_enqueued", count=enqueued)

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
