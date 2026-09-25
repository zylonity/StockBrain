"""Job handlers for discovery ingestion.

Every handler here is read-only with respect to external services: it fetches,
normalises and stores. No handler in this phase performs any broker action, and
none has access to broker credentials.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import Company
from stockbrain.db.models.sources import Source
from stockbrain.db.models.system import DiscoveryQuery, DiscoveryTopic, Notification
from stockbrain.enums import (
    EventStatus,
    ExtractionMethod,
    JobType,
    NotificationEvent,
    NotificationStatus,
    ProviderCallKind,
    ProviderStatus,
    WebDiscoveryKind,
    WebDiscoveryProviderName,
)
from stockbrain.errors import (
    ProviderAuthError,
    ProviderEntitlementError,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
)
from stockbrain.extraction.base import ExtractionFailure, ExtractionResult
from stockbrain.ingestion.provider_budget import estimate_firecrawl_scrape_credits
from stockbrain.ingestion.service import IngestionOutcome
from stockbrain.ingestion.web_search import WebSearchQuery, to_raw_document
from stockbrain.jobs.notifications import enqueue_pipeline_notification
from stockbrain.jobs.registry import HandlerContext, JobRegistry
from stockbrain.logging import get_logger
from stockbrain.observability.health import ProviderName
from stockbrain.observability.metrics import METRICS
from stockbrain.telegram.preferences import PipelineEvent

__all__ = [
    "QueryPlan",
    "build_query_plan",
    "effective_query_interval_minutes",
    "register_ingestion_handlers",
]

from stockbrain.intelligence.research_service import RETRYABLE_RESEARCH_ERRORS

log = get_logger(__name__)


async def handle_web_discovery_search(context: HandlerContext) -> None:
    """Run one stored discovery query and ingest the *metadata* it returns.

    Stage one of two, and provider-agnostic: which backend answers is decided by
    the query's ``search_kind`` and the deployment's configuration, not by this
    handler and not by the job type.  Adding a fourth search provider needs a
    ``WebDiscoveryProvider`` and a config value, and nothing here.

    The search asks for no page content.  What comes back -- title, URL,
    snippet, date -- goes through the same deduplication and the same cheap
    classifier as every other source, and only what survives that triage earns a
    content fetch (:func:`handle_content_extract`).  Asking a search to fetch
    every result page is what emptied the Firecrawl allowance in Phase 2, and
    both remaining providers offer the same trap under different names.

    Every paid call is reserved against the durable budget *before* it is made.
    A refusal is a normal outcome, not an error: the query's cooldown is written
    and the handler returns, so Alpaca news and SEC EDGAR carry on untouched.
    """
    services = context.services
    settings: Settings = services.settings
    query_id = uuid.UUID(str(context.payload["query_id"]))

    async with context.database.session() as session:
        row = await session.get(DiscoveryQuery, query_id)
        if row is None or not row.enabled:
            log.info("discovery_query_skipped", query_id=str(query_id))
            return
        topic = await session.get(DiscoveryTopic, row.topic_id)
        if topic is not None and not topic.enabled:
            log.info("discovery_query_skipped", query_id=str(query_id), reason="topic disabled")
            return
        plan = build_query_plan(row, topic, settings)
        topic_slug = topic.slug if topic else None

    provider = services.web_discovery_provider(plan.provider_name)
    budget = services.provider_budget(plan.provider_name.value)
    if provider is None or budget is None:
        # The query names a provider this deployment has not configured. That is
        # a deferral, never a silent hand-off to a different paid backend: a
        # fan-out on unavailability is how one provider's outage becomes
        # another provider's bill.
        log.info(
            "discovery_query_provider_unavailable",
            query_id=str(query_id),
            provider=plan.provider_name.value,
            kind=plan.query.kind.value,
        )
        await _defer_discovery_query(
            context,
            query_id,
            cooldown_minutes=settings.web_discovery_failure_cooldown_minutes,
            error=f"provider {plan.provider_name.value!r} is not configured",
            count_failure=False,
        )
        return

    reservation = await budget.reserve(
        ProviderCallKind.SEARCH,
        # One request, one billable unit, on both search providers. The estimate
        # and the charge coincide here; on Firecrawl they did not, which is why
        # the ledger keeps both numbers.
        units_needed=1,
        query_id=query_id,
        topic_slug=topic_slug,
        target_url=None,
        requested_limit=plan.query.limit,
        requested_sources=plan.requested_sources,
        scrape_requested=False,
    )
    if reservation is None:
        # Budget exhausted or provider disabled. Push the query out by a full
        # cooldown so the scheduler stops re-enqueueing a call that cannot
        # happen, and degrade *this provider only*.
        await _defer_discovery_query(
            context,
            query_id,
            cooldown_minutes=settings.web_discovery_failure_cooldown_minutes,
            error=f"{plan.provider_name.value} budget refused the call",
            count_failure=False,
        )
        state = await budget.state()
        services.health.record(
            plan.health_name,
            ProviderStatus.BUDGET_EXHAUSTED if state.exhausted else ProviderStatus.DISABLED,
            detail="; ".join(state.exhausted_reasons or state.blockers)[:300]
            or "budget refused the call",
        )
        return

    try:
        outcome = await provider.search(plan.query)
    except ProviderError as exc:
        await budget.record_failure(
            reservation,
            error_category=type(exc).__name__,
            http_status=getattr(exc, "status_code", None),
            # Brave documents that only successful requests are billed, so a
            # classified provider error there genuinely costs nothing. A
            # transport failure is excluded even for Brave: a timeout is exactly
            # the case where nobody knows whether the far side served it.
            refund=plan.refunds_failed_calls and not isinstance(exc, ProviderUnavailable),
        )
        await _defer_discovery_query(
            context,
            query_id,
            cooldown_minutes=settings.web_discovery_failure_cooldown_minutes,
            error=f"{type(exc).__name__}: {exc}",
            count_failure=True,
        )
        services.health.record(
            plan.health_name,
            ProviderStatus.DOWN if isinstance(exc, ProviderAuthError) else ProviderStatus.DEGRADED,
            detail=str(exc)[:300],
        )
        # Deliberately not re-raised. The paid call has been accounted, the
        # cooldown is written, and a job failure here would only add the queue's
        # own retry on top of a call that costs money each time it is tried.
        log.warning(
            "web_discovery_search_failed",
            provider=plan.provider_name.value,
            topic=topic_slug,
            error_type=type(exc).__name__,
        )
        return

    await budget.record_success(
        reservation,
        units_reported=outcome.billed_units_reported,
        cost_usd_reported=outcome.cost_usd_reported,
        results_returned=outcome.results_returned,
        pages_scraped=0,
    )

    documents = [
        to_raw_document(result, discovery_query=plan.query.query, kind=plan.query.kind)
        for result in outcome.results
    ]
    results = await services.ingestion.ingest_many(documents)
    created = sum(1 for r in results if r.outcome is IngestionOutcome.CREATED_EVENT)
    linked = sum(1 for r in results if r.outcome is IngestionOutcome.LINKED_TO_EVENT)
    duplicates = sum(1 for r in results if r.outcome is IngestionOutcome.DUPLICATE_SOURCE)

    async with context.database.transaction() as session:
        # `now()` is the database clock on purpose: the scheduler compares
        # `next_eligible_at` against the same clock, and mixing the two is how a
        # cooldown becomes negotiable by container clock skew.
        await session.execute(
            sa.update(DiscoveryQuery)
            .where(DiscoveryQuery.id == query_id)
            .values(
                last_run_at=sa.func.now(),
                last_success_at=sa.func.now(),
                last_error=None,
                consecutive_failures=0,
                results_seen=DiscoveryQuery.results_seen + outcome.results_returned,
                searches_performed=DiscoveryQuery.searches_performed + 1,
                credits_used=DiscoveryQuery.credits_used
                + (outcome.billed_units_reported or reservation.units_reserved),
                next_eligible_at=sa.func.now()
                + dt.timedelta(minutes=plan.effective_interval_minutes),
                updated_at=sa.func.now(),
            )
        )
        if topic_slug:
            await session.execute(
                sa.update(DiscoveryTopic)
                .where(DiscoveryTopic.slug == topic_slug)
                .values(last_run_at=sa.func.now(), updated_at=sa.func.now())
            )

    services.health.record(plan.health_name, ProviderStatus.HEALTHY)
    log.info(
        "web_discovery_search_complete",
        provider=plan.provider_name.value,
        kind=plan.query.kind.value,
        topic=topic_slug,
        results_returned=outcome.results_returned,
        documents=len(documents),
        created=created,
        linked=linked,
        duplicates=duplicates,
        cost_usd_reported=(
            str(outcome.cost_usd_reported) if outcome.cost_usd_reported is not None else None
        ),
        next_eligible_in_minutes=plan.effective_interval_minutes,
    )


async def handle_content_extract(context: HandlerContext) -> None:
    """Stage two: obtain the body of one already-triaged source.

    Reached only for a source whose event the classifier promoted, so the work
    is spent on something the cheap layers already agreed was worth reading.

    Local extraction first, always.  It is free, so in normal operation this
    handler spends nothing at all.  The paid Firecrawl fallback runs only when
    *every* one of these is true:

    * local extraction failed for a reason a different fetcher could plausibly
      fix -- an HTTP error, a transport failure, or a page that returned a 200
      and almost no text.  A refusal or an unsupported content type is not
      eligible: a second fetcher gets the same answer.
    * the fallback is enabled *and* Firecrawl is enabled -- two settings,
      because "the credential exists" and "spend it on this page" are two
      decisions
    * the durable Firecrawl budget grants a reservation
    * the source has not already been attempted

    Idempotent: a source that already has ``content_fetched_at`` set is skipped
    without any fetch, so a redelivered job cannot pay twice for one page.  That
    column is written even when extraction produced nothing, which is what makes
    "at most one paid attempt per URL" true rather than aspirational.
    """
    services = context.services
    extractor = services.content_extractor
    if extractor is None:
        log.info("content_extract_disabled")
        return

    source_id = uuid.UUID(str(context.payload["source_id"]))

    async with context.database.session() as session:
        source = await session.get(Source, source_id)
        if source is None:
            log.info("content_extract_skipped", source_id=str(source_id), reason="missing")
            return
        if source.content_fetched_at is not None:
            log.info("content_extract_skipped", source_id=str(source_id), reason="already fetched")
            return
        url = source.canonical_url or source.original_url
        language = _source_language(source)
        if not url:
            log.info("content_extract_skipped", source_id=str(source_id), reason="no url")
            return

    result: ExtractionResult = await extractor.extract(url, language=language)
    if result.succeeded:
        await _store_extraction(context, source_id, result)
        services.health.record(ProviderName.CONTENT_EXTRACTION, ProviderStatus.HEALTHY)
        log.info(
            "content_extract_complete",
            source_id=str(source_id),
            method=result.method.value,
            characters=len(result.text or ""),
        )
        return

    failure = result.failure
    services.health.record(
        ProviderName.CONTENT_EXTRACTION,
        ProviderStatus.DEGRADED,
        detail=f"{failure.value if failure else 'UNKNOWN'}: {result.detail or ''}"[:300],
    )

    fallback = await _firecrawl_fallback(context, source_id, url, failure, language)
    if fallback is not None and fallback.succeeded:
        await _store_extraction(context, source_id, fallback)
        log.info(
            "content_extract_complete",
            source_id=str(source_id),
            method=fallback.method.value,
            characters=len(fallback.text or ""),
        )
        return

    # Nothing usable, from either route. The attempt is still recorded: the
    # point of the column is to stop it happening again, and the second attempt
    # is the one that might cost money.
    await _store_extraction(context, source_id, fallback or result, stored=False)
    log.info(
        "content_extract_empty",
        source_id=str(source_id),
        local_failure=failure.value if failure else None,
        fallback_failure=(
            fallback.failure.value if fallback is not None and fallback.failure else None
        ),
    )


def _source_language(source: Source) -> str:
    """The source's own language, defaulting to English.

    A disclosure feed records ``metadata.language`` on ``provider_metadata``;
    every other provider leaves it absent and gets ``en``.
    """
    metadata = source.provider_metadata or {}
    value = metadata.get("language")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "en"


async def _firecrawl_fallback(
    context: HandlerContext,
    source_id: uuid.UUID,
    url: str,
    failure: ExtractionFailure | None,
    language: str,
) -> ExtractionResult | None:
    """One paid fetch, or ``None`` with the reason logged.

    Every gate is checked before the reservation, and the reservation before the
    request.  There is no retry: Firecrawl bills for a request its
    infrastructure processed even when the target answered an error, so a second
    attempt is a second charge on a call that already failed.
    """
    services = context.services
    fallback = services.firecrawl_extractor
    budget = services.provider_budget("firecrawl")
    if fallback is None or budget is None:
        return None
    if failure is None or not failure.fallback_eligible:
        # A refused URL or a PDF does not become extractable by paying someone
        # else to fetch it. Spending on a failure the fallback will reproduce is
        # precisely how a fallback becomes a cost storm.
        log.info(
            "firecrawl_fallback_skipped",
            source_id=str(source_id),
            reason=failure.value if failure else "no failure recorded",
        )
        return None

    reservation = await budget.reserve(
        ProviderCallKind.SCRAPE,
        units_needed=estimate_firecrawl_scrape_credits(pages=1),
        source_id=source_id,
        target_url=url,
        scrape_requested=True,
    )
    if reservation is None:
        return None

    result: ExtractionResult = await fallback.extract(url, language=language)
    if result.succeeded:
        await budget.record_success(
            reservation,
            # /v2/scrape reports no creditsUsed, so the reservation stands as
            # the charge. Documented, not assumed.
            units_reported=None,
            results_returned=1,
            pages_scraped=1,
            http_status=result.status_code,
        )
        services.health.record(ProviderName.FIRECRAWL, ProviderStatus.HEALTHY)
    else:
        await budget.record_failure(
            reservation,
            error_category=(result.failure.value if result.failure else "UNKNOWN"),
            http_status=result.status_code,
        )
        services.health.record(
            ProviderName.FIRECRAWL,
            ProviderStatus.DEGRADED,
            detail=(result.detail or "")[:300],
        )
    METRICS.inc("stockbrain_firecrawl_fallbacks_total")
    return result


async def _store_extraction(
    context: HandlerContext,
    source_id: uuid.UUID,
    result: ExtractionResult,
    *,
    stored: bool = True,
) -> None:
    await context.services.ingestion.attach_fetched_content(
        source_id,
        body=result.text if stored else None,
        fetched_url=result.url,
        status_code=result.status_code,
        method=result.method if stored and result.succeeded else ExtractionMethod.NONE,
        failure=result.failure.value if result.failure else None,
        detail=result.detail,
    )


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """Everything one stored query resolves to before any call is made."""

    query: WebSearchQuery
    provider_name: WebDiscoveryProviderName
    health_name: ProviderName
    effective_interval_minutes: int
    requested_sources: list[str]
    refunds_failed_calls: bool
    """Whether this provider documents that failed requests are not billed.
    Brave does; Exa and Firecrawl do not, and the conservative reading applies
    to anyone who does not say so in writing."""


def build_query_plan(
    row: DiscoveryQuery, topic: DiscoveryTopic | None, settings: Settings
) -> QueryPlan:
    """Resolve a stored row into the call it would make.

    Pure and synchronous so the resolution -- which provider, what cadence, what
    limit -- can be asserted in a test without a database, a network or a clock.
    Every ceiling is applied here as a *reduction*: a row asking for a hundred
    results or a five-minute cadence gets the configured maximum and the
    configured floor, because an old row, a restored backup or a hand-written
    ``UPDATE`` must not be able to spend more than the operator agreed to.
    """
    kind = row.search_kind
    provider_name = _resolve_provider(row.provider, kind, settings)
    if provider_name is WebDiscoveryProviderName.EXA:
        health_name = ProviderName.EXA
        max_limit = settings.exa_result_limit
        refunds = False
        sources = [c for c in [topic.slug if topic else None] if c]
    else:
        health_name = ProviderName.BRAVE
        max_limit = settings.brave_result_limit
        # Brave: "only successful requests (non-error responses) are counted
        # against your quota and billed" -- verified 2026-09-05.
        refunds = provider_name is WebDiscoveryProviderName.BRAVE
        sources = list(settings.brave_result_filter)

    requested_limit = row.result_limit or (topic.result_limit if topic else max_limit)
    requested_interval = row.interval_minutes or (
        topic.interval_minutes if topic else settings.min_query_interval_minutes(kind)
    )
    freshness = topic.freshness_days if topic else settings.web_discovery_freshness_days

    return QueryPlan(
        query=WebSearchQuery(
            query=row.query,
            kind=kind,
            limit=max(1, min(requested_limit, max_limit)),
            freshness_days=freshness,
            include_domains=list(topic.include_domains) if topic else [],
            exclude_domains=list(topic.exclude_domains) if topic else [],
            # Exa's own topical narrowing. Left unset: a category is a filter the
            # provider applies before scoring, and narrowing a second-order
            # question to "news" is how you stop hearing about the supplier.
            category=None,
        ),
        provider_name=provider_name,
        health_name=health_name,
        effective_interval_minutes=effective_query_interval_minutes(
            requested_interval, kind, settings
        ),
        requested_sources=sources,
        refunds_failed_calls=refunds,
    )


def _resolve_provider(
    pinned: str | None, kind: WebDiscoveryKind, settings: Settings
) -> WebDiscoveryProviderName:
    """Which backend answers this query.

    A pin the deployment does not recognise is *not* silently replaced by the
    default: it resolves to ``none`` and the query does not run.  Falling back
    to whatever is configured would let a typo redirect a query onto a provider
    it was deliberately kept off.
    """
    if pinned:
        try:
            return WebDiscoveryProviderName(pinned.strip().lower())
        except ValueError:
            log.warning("discovery_query_unknown_provider", provider=pinned)
            return WebDiscoveryProviderName.NONE
    return settings.provider_for_kind(kind)


def effective_query_interval_minutes(
    requested_minutes: int, kind: WebDiscoveryKind, settings: Settings
) -> int:
    """The interval a query actually runs on.

    A row may ask for a *slower* cadence than the configured floor but never a
    faster one.  Web discovery is thematic: hours for routine, a day for
    semantic.  Enforcing the floor here rather than in the topic editor means an
    old row, a restored backup or a hand-written UPDATE cannot reintroduce the
    Phase 2 cadence.
    """
    return max(int(requested_minutes), int(settings.min_query_interval_minutes(kind)))


async def _defer_discovery_query(
    context: HandlerContext,
    query_id: uuid.UUID,
    *,
    cooldown_minutes: int,
    error: str,
    count_failure: bool,
) -> None:
    """Write a durable cooldown for a query that did not produce results.

    Uses the database clock for both the timestamp and the deadline, so the
    cooldown means the same thing to the scheduler that reads it.  A budget
    refusal does not increment ``consecutive_failures``: the provider did not
    fail, StockBrain declined to spend.
    """
    async with context.database.transaction() as session:
        values: dict[str, object] = {
            "last_run_at": sa.func.now(),
            "last_error": error[:1000],
            "next_eligible_at": sa.func.now() + dt.timedelta(minutes=max(1, cooldown_minutes)),
            "updated_at": sa.func.now(),
        }
        if count_failure:
            values["consecutive_failures"] = DiscoveryQuery.consecutive_failures + 1
        await session.execute(
            sa.update(DiscoveryQuery).where(DiscoveryQuery.id == query_id).values(**values)
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


async def _announce_stage(
    context: HandlerContext, entity_id: uuid.UUID, event: PipelineEvent
) -> None:
    """Queue one pipeline notification, if this deployment has the parts for it.

    Both dependencies are read with ``getattr`` for the same reason
    ``handle_send_notification`` reads the Telegram runtime that way: a handler
    must degrade to "told nobody" rather than raise when an optional subsystem
    is absent. A research run that succeeded must not be recorded as failed
    because there was no notification preference row to consult.
    """
    queue = getattr(context.services, "queue", None)
    preferences = getattr(context.services, "notification_preferences", None)
    if queue is None or preferences is None:
        return
    async with context.database.transaction() as session:
        await enqueue_pipeline_notification(
            session, queue, preferences, entity_id=entity_id, event=event
        )


#: Which classification outcome announces which stage.  ``IRRELEVANT`` is
#: absent on purpose: "we read it and it does not matter" is the pipeline
#: working, and a message for every article the classifier dismissed would be
#: the firehose with extra steps.
_CLASSIFICATION_STAGES: dict[EventStatus, PipelineEvent] = {
    EventStatus.CLASSIFIED: PipelineEvent.EVENT_RELEVANT,
    EventStatus.CANDIDATE: PipelineEvent.EVENT_CANDIDATE,
}


async def handle_classify_event(context: HandlerContext) -> None:
    """Classify one ingested event with the LLM classifier.

    The service is idempotent: an event already past ``NEW``/``CLASSIFYING`` is
    skipped, and company impacts are upserted, so a redelivered job cannot
    duplicate anything.
    """
    services = context.services
    classification = services.classification
    if classification is None:
        raise RuntimeError("classifier is not configured")

    event_id = uuid.UUID(str(context.payload["event_id"]))
    result = await classification.classify_event(
        event_id,
        job_id=context.job_id,
        attempt=context.attempt,
        is_final_attempt=context.is_final_attempt,
    )

    if result.skipped:
        log.info(
            "classify_event_skipped",
            event_id=str(event_id),
            reason=result.reason,
            status=result.status.value,
        )
        return

    services.health.record(ProviderName.LLM, ProviderStatus.HEALTHY)

    # The pipeline continues here: a classified event with affected companies
    # gets its hints resolved to verified instruments. The dedupe key means a
    # redelivered classify job cannot queue a second resolve for the same event.
    if result.company_count:
        target = result.merged_into or event_id
        async with context.database.transaction() as session:
            await services.queue.enqueue(
                session,
                JobType.RESOLVE_CANDIDATES,
                payload={"event_id": str(target)},
                dedupe_key=f"resolve:{target}",
                priority=30,
            )

    # Two distinct facts, announced as two distinct stages. "Considered
    # relevant" is a judgement the classifier made; "promoted to candidate" is
    # the moment this story is about to cost research money. An operator who
    # wants only the second must not have to subscribe to the first.
    stage = _CLASSIFICATION_STAGES.get(result.status)
    if stage is not None:
        await _announce_stage(context, result.merged_into or event_id, stage)

    log.info(
        "classify_event_complete",
        event_id=str(event_id),
        status=result.status.value,
        merged_into=str(result.merged_into) if result.merged_into else None,
        companies=result.company_count,
    )


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


async def handle_instrument_refresh(context: HandlerContext) -> None:
    """Refresh Trading 212 instrument and exchange metadata.

    Read-only in the strictest sense: the client this reaches has no order
    method to call. The sync is an upsert, so a redelivered job re-writes the
    same rows rather than duplicating or briefly emptying the table that
    instrument resolution reads.
    """
    services = context.services
    sync = services.instrument_sync
    if sync is None:
        raise RuntimeError("trading212 metadata client is not configured")

    try:
        result = await sync.sync()
    except ProviderAuthError as exc:
        services.health.record(ProviderName.TRADING212, ProviderStatus.DOWN, detail=str(exc)[:300])
        raise
    except ProviderRateLimited as exc:
        # The documented limits are one request per 30-50 seconds; a 429 means
        # something else already spent the budget, not that the sync is broken.
        services.health.record(
            ProviderName.TRADING212, ProviderStatus.DEGRADED, detail=str(exc)[:300]
        )
        raise
    except ProviderError as exc:
        services.health.record(
            ProviderName.TRADING212, ProviderStatus.DEGRADED, detail=str(exc)[:300]
        )
        raise

    services.health.record(
        ProviderName.TRADING212,
        ProviderStatus.HEALTHY,
        detail=f"{result.instruments_written} instruments, {result.exchanges} exchanges",
        metrics=result.as_dict(),
    )
    log.info("instrument_refresh_complete", **result.as_dict())


async def handle_resolve_candidates(context: HandlerContext) -> None:
    """Resolve every company hint on one event to a verified broker instrument.

    Never fails the job for an unresolved company: NOT_FOUND and AMBIGUOUS are
    *answers*, recorded on the row for a human to see. Only an infrastructure
    failure raises.
    """
    services = context.services
    resolution = services.resolution
    if resolution is None:  # pragma: no cover - always constructed
        raise RuntimeError("resolution service is not configured")

    event_id = uuid.UUID(str(context.payload["event_id"]))
    result = await resolution.resolve_event(event_id)
    if getattr(services, "research", None) is not None:
        await services.research.enqueue_event(event_id)
    log.info(
        "resolve_candidates_job_complete",
        event_id=str(event_id),
        considered=result.considered,
        resolved=result.resolved,
        ambiguous=result.ambiguous,
        not_found=result.not_found,
        unsupported=result.unsupported,
    )


async def handle_broker_account_refresh(context: HandlerContext) -> None:
    """Re-read broker cash and open positions.

    Two documented GETs and nothing else. The client this reaches has no order,
    amend or cancel method to call, and the handler receives no credentials of
    its own -- it reaches the already-constructed read-only client through the
    service container.
    """
    services = context.services
    state = services.account_state
    if not state.configured:
        raise RuntimeError("trading212 account client is not configured")

    try:
        result = await state.sync()
    except ProviderAuthError as exc:
        services.health.record(ProviderName.TRADING212, ProviderStatus.DOWN, detail=str(exc)[:300])
        raise
    except ProviderError as exc:
        services.health.record(
            ProviderName.TRADING212, ProviderStatus.DEGRADED, detail=str(exc)[:300]
        )
        raise

    log.info("broker_account_refresh_complete", **result.as_dict())


async def handle_generate_proposal(context: HandlerContext) -> None:
    """Turn one published thesis into a durable proposal, or a durable refusal.

    Idempotent at three layers, because at-least-once delivery guarantees this
    job runs twice eventually: ``uq_jobs_dedupe_key_active`` allows one pending
    generation per thesis, ``uq_trade_proposals_dedupe_key`` and
    ``uq_trade_proposals_active_thesis`` make a second insert a no-op, and a
    blocked evaluation is recorded rather than retried into existence.

    A risk block is an *answer*, not a job failure: the job succeeds and the
    refusal is visible in ``risk_evaluations`` and in the GUI. Only an
    infrastructure failure raises.
    """
    services = context.services
    proposals = services.proposals
    if proposals is None:
        raise RuntimeError("proposal service is not configured")

    thesis_id = uuid.UUID(str(context.payload["thesis_id"]))
    result = await proposals.generate(thesis_id)
    log.info(
        "generate_proposal_job_complete",
        thesis_id=str(thesis_id),
        created=result.created,
        proposal_id=str(result.proposal_id) if result.proposal_id else None,
        outcome=result.outcome.value,
        authorized=result.authorized,
        reason=result.reason[:300],
    )


async def handle_send_notification(context: HandlerContext) -> None:
    """Deliver one proposal notification to Telegram.

    Delivery is a *job*, not an inline call, for two reasons. An authorization
    that already happened must not fail because Telegram is unreachable; and a
    job that is redelivered is harmless, because the notification row's unique
    dedupe key -- not this handler's memory -- is what makes the message
    once-only across workers and restarts.

    Registered unconditionally. Proposal transitions enqueue this job whatever
    the deployment looks like, so leaving it unregistered when Telegram is off
    would fill the queue with work nothing can ever claim; with no bot running
    the job simply succeeds having told nobody, which is the truth.
    """
    runtime = getattr(context.services, "telegram", None)
    if "pipeline_event" in context.payload:
        # A discovery, classification or research stage rather than a proposal
        # transition. Same job type because the durability, the dedupe key and
        # the never-auto-resend rule are identical; only the rendering differs.
        entity_id = uuid.UUID(str(context.payload["entity_id"]))
        stage = PipelineEvent(str(context.payload["pipeline_event"]))
        if runtime is None:
            log.debug(
                "pipeline_notification_skipped",
                entity_id=str(entity_id),
                pipeline_event=stage.value,
                reason="telegram disabled",
            )
            return
        outcome = await runtime.notifier.deliver_pipeline(entity_id, stage)
        log.info(
            "pipeline_notification_job_complete",
            entity_id=str(entity_id),
            pipeline_event=stage.value,
            status=outcome.status.value,
            delivered=outcome.delivered,
        )
        return
    if "notification_id" in context.payload:
        # An operational alert rather than a proposal transition. Delivered by
        # the same job type because the durability, the dedupe key and the
        # "never auto-resend" rule are identical -- only the rendering differs.
        await _deliver_alert(context, uuid.UUID(str(context.payload["notification_id"])))
        return
    proposal_id = uuid.UUID(str(context.payload["proposal_id"]))
    event = NotificationEvent(str(context.payload["event"]))
    detail = context.payload.get("detail")
    if runtime is None:
        log.debug("notification_skipped", proposal_id=str(proposal_id), reason="telegram disabled")
        return

    result = await runtime.notifier.deliver(
        proposal_id, event, detail=str(detail) if detail else None
    )
    if event in _TERMINAL_NOTIFICATIONS and result.status is not NotificationStatus.SUPPRESSED:
        # Tidy the buttons on any message that still shows them. Cosmetic: the
        # tokens behind them were consumed by the transition itself.
        await runtime.notifier.blank_keyboards(proposal_id)
    log.info(
        "notification_job_complete",
        proposal_id=str(proposal_id),
        notification_event=event.value,
        status=result.status.value,
        delivered=result.delivered,
    )


async def _deliver_alert(context: HandlerContext, notification_id: uuid.UUID) -> None:
    """Send one operational alert, and record what happened to it.

    No buttons and no approval token: an alert is information, and offering an
    action on it would be offering an action nobody validated. A failed send is
    recorded and **never** retried -- the Phase 7 rule, unchanged: a resend
    cannot distinguish "never arrived" from "arrived, status write failed".
    """
    services = context.services
    runtime = getattr(services, "telegram", None)

    async with context.database.session() as session:
        notification = await session.get(Notification, notification_id)
        if notification is None:
            log.info("alert_notification_missing", notification_id=str(notification_id))
            return
        if notification.status is not NotificationStatus.PENDING:
            log.debug("alert_notification_already_handled", notification_id=str(notification_id))
            return
        title = notification.title
        body = notification.body

    if runtime is None:
        async with context.database.transaction() as session:
            await session.execute(
                sa.update(Notification)
                .where(Notification.id == notification_id)
                .values(
                    status=NotificationStatus.SUPPRESSED,
                    error="the Telegram bot is not running",
                )
            )
        log.info("alert_suppressed", notification_id=str(notification_id))
        return

    try:
        reference = await runtime.notifier.send_operational_alert(title=title, body=body)
    except Exception as exc:
        async with context.database.transaction() as session:
            await session.execute(
                sa.update(Notification)
                .where(Notification.id == notification_id)
                .values(status=NotificationStatus.FAILED, error=type(exc).__name__)
            )
        log.warning(
            "alert_send_failed",
            notification_id=str(notification_id),
            error_type=type(exc).__name__,
        )
        return

    async with context.database.transaction() as session:
        await session.execute(
            sa.update(Notification)
            .where(Notification.id == notification_id)
            .values(
                status=NotificationStatus.SENT,
                sent_at=utcnow(),
                delivery_reference=reference,
                error=None,
            )
        )
    log.info("alert_sent", notification_id=str(notification_id))


#: Transitions after which no button on an existing message can still be valid.
_TERMINAL_NOTIFICATIONS = frozenset(
    {
        NotificationEvent.PROPOSAL_AUTO_AUTHORIZED,
        NotificationEvent.PROPOSAL_REJECTED,
        NotificationEvent.PROPOSAL_INVALIDATED,
        NotificationEvent.PROPOSAL_EXPIRED,
        NotificationEvent.AUTHORIZATION_REFUSED,
    }
)


async def handle_execute_proposal(context: HandlerContext) -> None:
    """Drive one authorized proposal to at most one broker transmission.

    Idempotent at four layers, because at-least-once delivery guarantees this
    job runs twice eventually and the endpoint it drives is non-idempotent:

    * ``uq_jobs_dedupe_key_active`` -- one pending execution job per proposal;
    * a transaction-scoped advisory lock on the proposal, so two workers
      serialise rather than interleave;
    * an explicit "has anything already been recorded as sent?" check inside
      that lock, which turns a redelivery into a reconciliation;
    * ``uq_execution_attempts_sent_once`` -- the database having the last word
      if all three of the above were somehow bypassed.

    A refusal, a rejection and an ambiguity are all *answers*, not job failures:
    the job succeeds and the outcome is visible on the attempt. Only an
    infrastructure fault raises, and it must never raise *after* a transmission
    -- a raise there would earn the job a retry, and a retried execution job on
    a proposal whose send was recorded simply reconciles.
    """
    execution = getattr(context.services, "execution", None)
    if execution is None:
        raise RuntimeError("the execution service is not configured")

    proposal_id = uuid.UUID(str(context.payload["proposal_id"]))
    result = await execution.execute(proposal_id)
    log.info(
        "execute_proposal_job_complete",
        proposal_id=str(proposal_id),
        transmitted=result.transmitted,
        outcome=result.outcome.value if result.outcome else None,
        broker_order_id=result.broker_order_id,
        proposal_status=result.status.value if result.status else None,
        reconcile_required=result.reconcile_required,
        reason=result.reason[:300],
    )


async def handle_reconcile_execution(context: HandlerContext) -> None:
    """Resolve one execution attempt by reading the broker.

    Transmits nothing.  An inconclusive pass is a successful job: the attempt
    stays ambiguous, its reconciliation counter advances, and the sweep will
    look again until the configured ceiling -- after which it waits for a
    person, because an order the broker cannot account for is not a thing to
    poll forever.
    """
    reconciliation = getattr(context.services, "reconciliation", None)
    if reconciliation is None:
        raise RuntimeError("the reconciliation service is not configured")

    attempt_id = uuid.UUID(str(context.payload["attempt_id"]))
    outcome = await reconciliation.reconcile(attempt_id)
    log.info(
        "reconcile_execution_job_complete",
        attempt_id=str(attempt_id),
        result=outcome.result.value,
        broker_order_id=outcome.broker_order_id,
        candidates=outcome.candidates,
        proposal_status=outcome.proposal_status.value if outcome.proposal_status else None,
    )


def register_ingestion_handlers(
    registry: JobRegistry,
    *,
    classifier_available: bool,
    instrument_sync_available: bool = False,
    research_available: bool = False,
    proposals_available: bool = False,
    account_sync_available: bool = False,
    execution_available: bool = False,
) -> None:
    """Register the handlers this deployment can actually run.

    ``CLASSIFY_EVENT`` is registered only when a classifier is configured.
    Registering a handler that would always fail would fill the queue with jobs
    guaranteed to exhaust their retry budget; leaving it unregistered means
    ingestion simply does not enqueue classification, and events wait in ``NEW``
    until a key is supplied.
    """
    registry.register(JobType.WEB_DISCOVERY_SEARCH.value, handle_web_discovery_search)
    registry.register(JobType.CONTENT_EXTRACT.value, handle_content_extract)
    registry.register(JobType.SEC_REFRESH.value, handle_sec_refresh)
    registry.register("ALPACA_NEWS_BACKFILL", handle_alpaca_backfill)
    if classifier_available:
        registry.register(JobType.CLASSIFY_EVENT.value, handle_classify_event)
    if instrument_sync_available:
        registry.register(JobType.INSTRUMENT_REFRESH.value, handle_instrument_refresh)
    # Resolution needs no provider -- it reads metadata already in the database --
    # so it is always registered. Without a sync it simply reports NOT_FOUND,
    # which is the honest answer rather than a job that cannot run.
    registry.register(JobType.RESOLVE_CANDIDATES.value, handle_resolve_candidates)
    if research_available:
        registry.register(JobType.RUN_RESEARCH.value, handle_run_research)
    if account_sync_available:
        registry.register(JobType.BROKER_RECONCILE.value, handle_broker_account_refresh)
    if proposals_available:
        registry.register(JobType.GENERATE_PROPOSAL.value, handle_generate_proposal)
    # Always registered. A proposal transition enqueues a notification whatever
    # the deployment looks like, and an unregistered type would leave those jobs
    # unclaimable rather than merely undelivered.
    registry.register(JobType.SEND_NOTIFICATION.value, handle_send_notification)
    # Registered only with a broker execution provider constructed. Without one
    # there is nothing to transmit to, and a handler that always failed would
    # fill the queue with jobs guaranteed to exhaust their retry budget.
    if execution_available:
        registry.register(JobType.EXECUTE_PROPOSAL.value, handle_execute_proposal)
        registry.register(JobType.RECONCILE_EXECUTION.value, handle_reconcile_execution)


async def handle_run_research(context: HandlerContext) -> None:
    if context.services.research is None:
        raise RuntimeError("research is not configured")
    run_id = uuid.UUID(str(context.payload["run_id"]))

    final_attempt = context.attempt >= context.max_attempts
    if context.attempt == 1:
        await _announce_stage(context, run_id, PipelineEvent.RESEARCH_STARTED)
    try:
        await context.services.research.run(
            run_id, job_id=context.job_id, final_attempt=final_attempt
        )
    except RETRYABLE_RESEARCH_ERRORS:
        # The run went back to PENDING and the job will retry; announcing
        # completion now would report a run that has not finished.
        if final_attempt:
            await _announce_stage(context, run_id, PipelineEvent.RESEARCH_COMPLETED)
        raise
    except BaseException:
        # A run that raised still finished, and its failure is exactly the
        # outcome an operator wants to hear about. The notifier reads the run's
        # recorded status, so the message says what happened.
        await _announce_stage(context, run_id, PipelineEvent.RESEARCH_COMPLETED)
        raise
    await _announce_stage(context, run_id, PipelineEvent.RESEARCH_COMPLETED)

    # The pipeline continues: a published thesis becomes a proposal candidate.
    # The dedupe key means a redelivered research job cannot queue a second
    # generation, and the scheduler's backlog sweep covers a job lost to a
    # dying worker.
    proposals = getattr(context.services, "proposals", None)
    if proposals is not None:
        await proposals.enqueue_for_run(run_id)
