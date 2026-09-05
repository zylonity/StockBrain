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
from stockbrain.enums import JobType, NotificationEvent, NotificationStatus, ProviderStatus
from stockbrain.errors import (
    ProviderAuthError,
    ProviderEntitlementError,
    ProviderError,
    ProviderRateLimited,
)
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

    services.health.record(ProviderName.DEEPSEEK, ProviderStatus.HEALTHY)

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


def register_ingestion_handlers(
    registry: JobRegistry,
    *,
    classifier_available: bool,
    instrument_sync_available: bool = False,
    research_available: bool = False,
    proposals_available: bool = False,
    account_sync_available: bool = False,
) -> None:
    """Register the handlers this deployment can actually run.

    ``CLASSIFY_EVENT`` is registered only when a classifier is configured.
    Registering a handler that would always fail would fill the queue with jobs
    guaranteed to exhaust their retry budget; leaving it unregistered means
    ingestion simply does not enqueue classification, and events wait in ``NEW``
    until a key is supplied.
    """
    registry.register(JobType.FIRECRAWL_TOPIC_SEARCH.value, handle_firecrawl_topic_search)
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


async def handle_run_research(context: HandlerContext) -> None:
    if context.services.research is None:
        raise RuntimeError("research is not configured")
    run_id = uuid.UUID(str(context.payload["run_id"]))
    await context.services.research.run(run_id, job_id=context.job_id)

    # The pipeline continues: a published thesis becomes a proposal candidate.
    # The dedupe key means a redelivered research job cannot queue a second
    # generation, and the scheduler's backlog sweep covers a job lost to a
    # dying worker.
    proposals = getattr(context.services, "proposals", None)
    if proposals is not None:
        await proposals.enqueue_for_run(run_id)
