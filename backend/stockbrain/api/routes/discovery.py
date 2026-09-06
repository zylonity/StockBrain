"""Discovery subsystem status, topic inspection and the discovery hold.

Almost everything here is read-only.  The one exception is the durable
``discovery.paused`` flag, which suspends *scheduled* discovery -- the web
searches, the SEC sweep and the news backfill -- without touching
classification, research, proposals or broker reconciliation.

That flag has always been read by the scheduler and by the status endpoint, and
until now there was no way to set it: a runtime control that existed in the
database, was honoured by the code, and had no switch anywhere.  These two
routes are that switch.  They are deliberately *not* the trading pause: holding
discovery stops the system spending money on new information, while pausing
trading stops it acting on information it already has, and conflating them would
mean an operator who wanted one silently got the other.
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import selectinload

from stockbrain.api.dependencies import DatabaseDep, DbSession, ServicesDep, SettingsDep
from stockbrain.api.schemas import (
    ContentExtractionStatusResponse,
    ControlChangeRequest,
    DiscoveryHoldResponse,
    DiscoveryQueryResponse,
    DiscoveryQueryUsageResponse,
    DiscoveryStatusResponse,
    DiscoveryTopicResponse,
    IngestionStatsResponse,
    JobQueueHealthResponse,
    LlmBudgetResponse,
    ProviderBudgetResponse,
    ProviderUsageResponse,
    WebDiscoveryStatusResponse,
)
from stockbrain.db.base import utcnow
from stockbrain.db.models.sources import Source
from stockbrain.db.models.system import (
    AppSetting,
    AuditLog,
    DiscoveryQuery,
    DiscoveryTopic,
    ProviderCall,
)
from stockbrain.db.repositories.events import EventRepository
from stockbrain.enums import ActorType, ExtractionMethod, ProviderCallOutcome
from stockbrain.jobs.handlers import build_query_plan
from stockbrain.jobs.queue import JobQueue
from stockbrain.logging import get_logger
from stockbrain.observability.health import ProviderName
from stockbrain.services import DISCOVERY_PAUSED_KEY, ServiceContainer

router = APIRouter(prefix="/api/v1/discovery", tags=["discovery"])

log = get_logger(__name__)

#: The same server-side constant every other web-originated control uses.
WEB_ACTOR = "web:owner"


@router.get("/status", response_model=DiscoveryStatusResponse, summary="Discovery status")
async def discovery_status(
    session: DbSession, services: ServicesDep, settings: SettingsDep
) -> DiscoveryStatusResponse:
    paused_row = await session.get(AppSetting, DISCOVERY_PAUSED_KEY)
    paused = bool(paused_row and paused_row.value.get("paused"))

    stats = await EventRepository(session).ingestion_stats()
    pending = await JobQueue().pending_count(session)

    scheduled: list[dict[str, object]] = []
    if services is not None and services.scheduler is not None:
        scheduled = [
            {
                "name": task.name,
                "interval_seconds": task.interval_seconds,
                "enabled": task.enabled,
                "last_run_at": task.last_run_at.isoformat() if task.last_run_at else None,
                "last_error": task.last_error,
            }
            for task in services.scheduler.tasks()
        ]

    budget: LlmBudgetResponse | None = None
    if services is not None and services.budget is not None:
        state = await services.budget.state()
        budget = LlmBudgetResponse(
            status=state.status.value,
            daily_spend_usd=state.daily_spend,
            monthly_spend_usd=state.monthly_spend,
            daily_soft_usd=state.daily_soft,
            daily_hard_usd=state.daily_hard,
            monthly_soft_usd=state.monthly_soft,
            monthly_hard_usd=state.monthly_hard,
            reason=state.reason,
        )

    queue = JobQueueHealthResponse(
        **(
            await JobQueue().health(session, stuck_after_seconds=settings.job_claim_timeout_seconds)
        ).as_dict()
    )

    web_discovery: WebDiscoveryStatusResponse | None = None
    if services is not None:
        web_discovery = await _web_discovery_view(session, services, settings)

    return DiscoveryStatusResponse(
        discovery_enabled=settings.discovery_enabled,
        paused=paused,
        subsystem_running=services is not None,
        news_stream_active=services is not None and services.alpaca_news is not None,
        classifier_active=services is not None and services.classification is not None,
        # The *active* quick model, not DeepSeek's. Since the LLM layer became
        # provider-agnostic this panel reported `DEEPSEEK_FLASH_MODEL` whatever
        # endpoint was actually configured, so a deployment running a different
        # backend was told it was classifying with a model it never called.
        classifier_model=(
            settings.active_llm_quick_model
            if services is not None and services.classification is not None
            else None
        ),
        jobs_pending=pending,
        scheduled_tasks=scheduled,
        stats=IngestionStatsResponse(**stats),
        budget=budget,
        web_discovery=web_discovery,
        queue=queue,
    )


async def _web_discovery_view(
    session: DbSession, services: ServiceContainer, settings: SettingsDep
) -> WebDiscoveryStatusResponse:
    """The provider split, its budgets and every query's cadence.

    No key and no secret in any field.  The per-query block answers the question
    an operator actually has -- "why has this topic not run?" -- from the same
    durable ``next_eligible_at`` column the scheduler reads, rather than from a
    recomputed guess.
    """
    providers = [
        await _provider_budget_view(session, services, name)
        for name in ("brave", "exa", "firecrawl")
        if services.provider_budget(name) is not None
    ]

    rows = (
        await session.execute(
            sa.select(DiscoveryQuery, DiscoveryTopic)
            .join(DiscoveryTopic, DiscoveryTopic.id == DiscoveryQuery.topic_id)
            .order_by(
                DiscoveryQuery.search_kind,
                DiscoveryTopic.slug,
                DiscoveryQuery.query,
            )
        )
    ).all()
    queries = []
    for query, topic in rows:
        # The same resolution the handler would perform, so the panel shows the
        # provider and cadence that would actually be used rather than the ones
        # the row asked for.
        plan = build_query_plan(query, topic, settings)
        queries.append(
            DiscoveryQueryUsageResponse(
                topic=topic.slug,
                query=query.query,
                kind=query.search_kind.value,
                provider=plan.provider_name.value,
                enabled=query.enabled and topic.enabled,
                last_run_at=query.last_run_at,
                last_success_at=query.last_success_at,
                next_eligible_at=query.next_eligible_at,
                effective_interval_minutes=plan.effective_interval_minutes,
                result_limit=plan.query.limit,
                priority=query.priority,
                consecutive_failures=query.consecutive_failures,
                searches_performed=query.searches_performed,
                results_seen=query.results_seen,
                units_used=query.credits_used,
                # Provider text, truncated. Never the API key -- the search
                # clients' errors carry a status and a class name, not a
                # request.
                last_error=query.last_error[:300] if query.last_error else None,
            )
        )

    return WebDiscoveryStatusResponse(
        enabled=settings.web_discovery_enabled,
        routine_provider=settings.web_discovery_routine_provider.value,
        semantic_provider=settings.web_discovery_semantic_provider.value,
        routine_min_interval_minutes=settings.web_discovery_min_query_interval_minutes,
        semantic_min_interval_minutes=settings.web_discovery_min_semantic_interval_minutes,
        providers=providers,
        queries=queries,
        extraction=await _extraction_view(session, services, settings),
    )


async def _provider_budget_view(
    session: DbSession, services: ServiceContainer, name: str
) -> ProviderBudgetResponse:
    budget = services.provider_budget(name)
    assert budget is not None
    state = await budget.state_in_session(session)

    last_call = (
        await session.execute(
            sa.select(ProviderCall.reserved_at)
            .where(ProviderCall.provider == name)
            .order_by(ProviderCall.reserved_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    last_success = (
        await session.execute(
            sa.select(ProviderCall.completed_at)
            .where(
                ProviderCall.provider == name,
                ProviderCall.outcome == ProviderCallOutcome.SUCCEEDED,
            )
            .order_by(ProviderCall.reserved_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    last_error = (
        await session.execute(
            sa.select(ProviderCall.error_category)
            .where(
                ProviderCall.provider == name,
                ProviderCall.error_category.is_not(None),
            )
            .order_by(ProviderCall.reserved_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    recent_results = (
        await session.execute(
            sa.select(ProviderCall.results_returned)
            .where(
                ProviderCall.provider == name,
                ProviderCall.results_returned.is_not(None),
            )
            .order_by(ProviderCall.reserved_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    health = services.health.get(_HEALTH_NAMES[name])
    return ProviderBudgetResponse(
        provider=state.provider,
        unit_label=state.unit_label,
        enabled=state.enabled,
        status=health.status.value,
        blockers=list(state.blockers),
        exhausted=state.exhausted,
        exhausted_reasons=list(state.exhausted_reasons),
        search_exhausted=state.search_exhausted,
        scrape_exhausted=state.scrape_exhausted,
        today=ProviderUsageResponse(**state.today.as_dict()),
        month=ProviderUsageResponse(**state.month.as_dict()),
        max_searches_per_day=state.max_searches_per_day,
        max_scrapes_per_day=state.max_scrapes_per_day,
        daily_unit_cap=state.daily_unit_cap,
        monthly_unit_cap=state.monthly_unit_cap,
        searches_remaining_today=state.searches_remaining,
        scrapes_remaining_today=state.scrapes_remaining,
        daily_units_remaining=state.daily_units_remaining,
        monthly_units_remaining=state.monthly_units_remaining,
        day_start=state.day_start,
        month_start=state.month_start,
        last_call_at=last_call,
        last_successful_call_at=last_success,
        last_error=last_error,
        recent_results_returned=recent_results,
    )


#: Which health row belongs to which budget. A map rather than a lookup by name
#: so a provider whose health key differs from its budget key cannot silently
#: report someone else's status.
_HEALTH_NAMES = {
    "brave": ProviderName.BRAVE,
    "exa": ProviderName.EXA,
    "firecrawl": ProviderName.FIRECRAWL,
}


async def _extraction_view(
    session: DbSession, services: ServiceContainer, settings: SettingsDep
) -> ContentExtractionStatusResponse:
    """How pages were read today, and how often that cost anything.

    Counted from ``sources.extraction_method`` rather than from a metric,
    because a counter resets on restart and the question -- "did we pay to read
    anything today" -- has to survive one.
    """
    day_start = (
        await session.execute(
            sa.text("SELECT (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS d")
        )
    ).scalar_one()
    rows = (
        await session.execute(
            sa.select(Source.extraction_method, sa.func.count())
            .where(Source.content_fetched_at >= day_start)
            .group_by(Source.extraction_method)
        )
    ).all()
    by_method = {str(method or "UNKNOWN"): int(count) for method, count in rows}
    return ContentExtractionStatusResponse(
        enabled=settings.content_extraction_available,
        blockers=list(settings.content_extraction_blockers),
        extractor="trafilatura (local fetch, SSRF-guarded)",
        max_per_day=settings.content_extract_max_per_day,
        fetched_today=sum(by_method.values()),
        by_method_today=by_method,
        local_failures_today=by_method.get(ExtractionMethod.NONE.value, 0),
        firecrawl_fallbacks_today=by_method.get(ExtractionMethod.FIRECRAWL.value, 0),
        fallback_enabled=settings.firecrawl_available,
    )


@router.get(
    "/topics",
    response_model=list[DiscoveryTopicResponse],
    summary="Configured discovery topics",
)
async def list_topics(session: DbSession, settings: SettingsDep) -> list[DiscoveryTopicResponse]:
    stmt = (
        sa.select(DiscoveryTopic)
        .options(selectinload(DiscoveryTopic.queries))
        .order_by(DiscoveryTopic.name)
    )
    topics = list((await session.execute(stmt)).scalars())
    return [
        DiscoveryTopicResponse(
            id=topic.id,
            slug=topic.slug,
            name=topic.name,
            description=topic.description,
            enabled=topic.enabled,
            interval_minutes=topic.interval_minutes,
            result_limit=topic.result_limit,
            freshness_days=topic.freshness_days,
            last_run_at=topic.last_run_at,
            queries=[
                DiscoveryQueryResponse(
                    id=query.id,
                    query=query.query,
                    kind=query.search_kind.value,
                    provider=(
                        query.provider or settings.provider_for_kind(query.search_kind).value
                    ),
                    enabled=query.enabled,
                    last_run_at=query.last_run_at,
                    last_success_at=query.last_success_at,
                    last_error=query.last_error,
                    consecutive_failures=query.consecutive_failures,
                    results_seen=query.results_seen,
                    units_used=query.credits_used,
                )
                for query in sorted(topic.queries, key=lambda q: q.query)
            ],
        )
        for topic in topics
    ]


@router.post(
    "/pause",
    response_model=DiscoveryHoldResponse,
    summary="Hold scheduled discovery work",
)
async def pause_discovery(
    body: ControlChangeRequest, database: DatabaseDep
) -> DiscoveryHoldResponse:
    """Stop enqueueing scheduled discovery.

    Work already in the queue still runs: a job that has been claimed is a paid
    call that may already have left, and cancelling it would lose the result
    without unspending the money.
    """
    return await _set_discovery_hold(database, True, reason=body.reason)


@router.post(
    "/resume",
    response_model=DiscoveryHoldResponse,
    summary="Lift the discovery hold",
)
async def resume_discovery(
    body: ControlChangeRequest, database: DatabaseDep
) -> DiscoveryHoldResponse:
    return await _set_discovery_hold(database, False, reason=body.reason)


async def _set_discovery_hold(
    database: DatabaseDep, paused: bool, *, reason: str | None
) -> DiscoveryHoldResponse:
    """Write the one key this route may write, and audit who did it.

    The key is a module constant, never anything derived from the request: a
    settings route whose target a client could name would be a generic
    configuration mutation endpoint, which this deliberately is not.
    """
    now = utcnow()
    payload: dict[str, object] = {
        "paused": paused,
        "changed_at": now.isoformat(),
        "actor": WEB_ACTOR,
        "reason": (reason or None) and reason[:500],
    }
    async with database.transaction() as db_session:
        statement = pg_insert(AppSetting).values(
            key=DISCOVERY_PAUSED_KEY,
            value=payload,
            description=(
                "Operator hold on scheduled discovery. Does not stop classification, "
                "research, proposals or broker reconciliation."
            ),
            updated_by=WEB_ACTOR,
        )
        await db_session.execute(
            statement.on_conflict_do_update(
                index_elements=[AppSetting.key],
                set_={
                    "value": statement.excluded.value,
                    "description": statement.excluded.description,
                    "updated_by": statement.excluded.updated_by,
                    "updated_at": now,
                },
            )
        )
        db_session.add(
            AuditLog(
                actor_type=ActorType.USER,
                actor_id=WEB_ACTOR,
                action="discovery.hold." + ("engaged" if paused else "released"),
                entity_type="app_setting",
                details={
                    "paused": paused,
                    "reason": (reason or "")[:500] or None,
                    "broker_orders_touched": False,
                },
            )
        )
    log.info("discovery_hold_changed", paused=paused, actor=WEB_ACTOR)
    return DiscoveryHoldResponse(
        paused=paused,
        changed_at=now,
        actor=WEB_ACTOR,
        reason=(reason or "")[:500] or None,
    )
