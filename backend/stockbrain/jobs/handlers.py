"""Job handlers for discovery ingestion.

Every handler here is read-only with respect to external services: it fetches,
normalises and stores. No handler in this phase performs any broker action, and
none has access to broker credentials.
"""

from __future__ import annotations

import datetime as dt
import uuid

import sqlalchemy as sa

from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import Company
from stockbrain.db.models.system import DiscoveryQuery, DiscoveryTopic
from stockbrain.enums import JobType, ProviderStatus
from stockbrain.errors import ProviderAuthError, ProviderEntitlementError, ProviderError
from stockbrain.ingestion.base import DiscoveryQuerySpec
from stockbrain.ingestion.service import IngestionOutcome
from stockbrain.jobs.registry import HandlerContext, JobRegistry
from stockbrain.logging import get_logger
from stockbrain.observability.health import ProviderName

__all__ = ["register_ingestion_handlers"]

log = get_logger(__name__)


async def handle_firecrawl_topic_search(context: HandlerContext) -> None:
    """Run one stored discovery query and ingest the results."""
    services = context.services
    client = services.firecrawl
    if client is None:
        raise RuntimeError("firecrawl is not configured")

    query_id = uuid.UUID(str(context.payload["query_id"]))

    async with context.database.session() as session:
        row = await session.get(DiscoveryQuery, query_id)
        if row is None or not row.enabled:
            log.info("discovery_query_skipped", query_id=str(query_id))
            return
        topic = await session.get(DiscoveryTopic, row.topic_id)
        spec = DiscoveryQuerySpec(
            query=row.query,
            limit=topic.result_limit if topic else 10,
            freshness=topic.freshness if topic else "qdr:d",
            include_domains=list(topic.include_domains) if topic else [],
            exclude_domains=list(topic.exclude_domains) if topic else [],
        )
        topic_slug = topic.slug if topic else None

    try:
        documents = await client.search(spec)
    except ProviderError as exc:
        async with context.database.transaction() as session:
            await session.execute(
                sa.update(DiscoveryQuery)
                .where(DiscoveryQuery.id == query_id)
                .values(
                    last_run_at=utcnow(),
                    last_error=f"{type(exc).__name__}: {exc}"[:1000],
                    consecutive_failures=DiscoveryQuery.consecutive_failures + 1,
                    updated_at=utcnow(),
                )
            )
        services.health.record(
            ProviderName.FIRECRAWL,
            ProviderStatus.DOWN if isinstance(exc, ProviderAuthError) else ProviderStatus.DEGRADED,
            detail=str(exc)[:300],
        )
        raise

    results = await services.ingestion.ingest_many(list(documents))
    created = sum(1 for r in results if r.outcome is IngestionOutcome.CREATED_EVENT)
    linked = sum(1 for r in results if r.outcome is IngestionOutcome.LINKED_TO_EVENT)
    duplicates = sum(1 for r in results if r.outcome is IngestionOutcome.DUPLICATE_SOURCE)

    async with context.database.transaction() as session:
        await session.execute(
            sa.update(DiscoveryQuery)
            .where(DiscoveryQuery.id == query_id)
            .values(
                last_run_at=utcnow(),
                last_success_at=utcnow(),
                last_error=None,
                consecutive_failures=0,
                results_seen=DiscoveryQuery.results_seen + len(results),
                credits_used=DiscoveryQuery.credits_used + client.last_credits_used,
                updated_at=utcnow(),
            )
        )
        if topic_slug:
            await session.execute(
                sa.update(DiscoveryTopic)
                .where(DiscoveryTopic.slug == topic_slug)
                .values(last_run_at=utcnow(), updated_at=utcnow())
            )

    services.health.record(ProviderName.FIRECRAWL, ProviderStatus.HEALTHY)
    log.info(
        "firecrawl_topic_search_complete",
        topic=topic_slug,
        results=len(results),
        created=created,
        linked=linked,
        duplicates=duplicates,
        credits_used=client.last_credits_used,
    )


async def handle_sec_refresh(context: HandlerContext) -> None:
    """Poll SEC submissions for one company, or for the whole watchlist."""
    services = context.services
    client = services.sec
    if client is None:
        raise RuntimeError("sec client is not configured")

    cik = context.payload.get("cik")
    since_days = int(context.payload.get("since_days", 7))
    since = (utcnow() - dt.timedelta(days=since_days)).date()

    if cik:
        ciks = [str(cik)]
    else:
        async with context.database.session() as session:
            result = await session.execute(
                sa.select(Company.cik).where(
                    Company.cik.is_not(None),
                    sa.or_(Company.is_watchlisted.is_(True), Company.primary_symbol.is_not(None)),
                )
            )
            ciks = [row for row in result.scalars() if row]

    if not ciks:
        log.debug("sec_refresh_no_companies")
        return

    total_created = 0
    failures = 0
    for company_cik in ciks:
        try:
            documents = await client.recent_for_cik(company_cik, since=since, limit=20)
        except ProviderAuthError:
            # A 403 from EDGAR usually means the User-Agent is missing or the IP
            # is blocked for exceeding 10 req/s. Stop the whole sweep rather than
            # walking the watchlist and deepening the block.
            services.health.record(
                ProviderName.SEC,
                ProviderStatus.DOWN,
                detail="EDGAR rejected the request (User-Agent or IP block)",
            )
            raise
        except ProviderError as exc:
            failures += 1
            log.warning(
                "sec_refresh_company_failed", cik=company_cik, error_type=type(exc).__name__
            )
            continue

        results = await services.ingestion.ingest_many(list(documents))
        total_created += sum(1 for r in results if r.outcome is IngestionOutcome.CREATED_EVENT)

    services.health.record(
        ProviderName.SEC,
        ProviderStatus.HEALTHY if failures == 0 else ProviderStatus.DEGRADED,
        detail=None if failures == 0 else f"{failures}/{len(ciks)} companies failed",
    )
    log.info("sec_refresh_complete", companies=len(ciks), created=total_created, failures=failures)


async def handle_alpaca_backfill(context: HandlerContext) -> None:
    """Close a news gap after a WebSocket disconnect."""
    services = context.services
    client = services.alpaca_news
    if client is None:
        raise RuntimeError("alpaca news client is not configured")

    start = dt.datetime.fromisoformat(str(context.payload["start"]))
    end = dt.datetime.fromisoformat(str(context.payload.get("end") or utcnow().isoformat()))

    try:
        documents = await client.backfill(start, end, limit=int(context.payload.get("limit", 200)))
    except ProviderEntitlementError as exc:
        services.health.record(
            ProviderName.ALPACA_NEWS, ProviderStatus.DEGRADED, detail=str(exc)[:300]
        )
        raise

    results = await services.ingestion.ingest_many(list(documents))
    created = sum(1 for r in results if r.outcome is IngestionOutcome.CREATED_EVENT)
    log.info(
        "alpaca_backfill_complete",
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        fetched=len(documents),
        created=created,
    )


def register_ingestion_handlers(registry: JobRegistry) -> None:
    """Register every handler this phase provides.

    ``CLASSIFY_EVENT`` is deliberately **not** registered: the DeepSeek
    classifier lands in the next phase. The ingestion service therefore does not
    enqueue it yet, so no job is created that nothing can run. Ingested events
    wait in ``NEW`` status, which is exactly where the classifier will pick them
    up.
    """
    registry.register(JobType.FIRECRAWL_TOPIC_SEARCH.value, handle_firecrawl_topic_search)
    registry.register(JobType.SEC_REFRESH.value, handle_sec_refresh)
    registry.register("ALPACA_NEWS_BACKFILL", handle_alpaca_backfill)
