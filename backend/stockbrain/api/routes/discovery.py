"""Discovery subsystem status and topic inspection.

Everything here is read-only in this phase. Editing topics from the GUI arrives
with the settings screen; the data model already supports it.
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter
from sqlalchemy.orm import selectinload

from stockbrain.api.dependencies import DbSession, ServicesDep, SettingsDep
from stockbrain.api.schemas import (
    DiscoveryQueryResponse,
    DiscoveryStatusResponse,
    DiscoveryTopicResponse,
    FirecrawlBudgetResponse,
    FirecrawlTopicUsageResponse,
    FirecrawlUsageResponse,
    IngestionStatsResponse,
    JobQueueHealthResponse,
    LlmBudgetResponse,
)
from stockbrain.db.models.system import AppSetting, DiscoveryQuery, DiscoveryTopic, FirecrawlCall
from stockbrain.db.repositories.events import EventRepository
from stockbrain.enums import FirecrawlCallOutcome
from stockbrain.jobs.handlers import effective_topic_interval_minutes
from stockbrain.jobs.queue import JobQueue
from stockbrain.services import DISCOVERY_PAUSED_KEY, ServiceContainer

router = APIRouter(prefix="/api/v1/discovery", tags=["discovery"])


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

    firecrawl: FirecrawlBudgetResponse | None = None
    if services is not None and services.firecrawl_budget is not None:
        firecrawl = await _firecrawl_budget_view(session, services, settings)

    return DiscoveryStatusResponse(
        discovery_enabled=settings.discovery_enabled,
        paused=paused,
        subsystem_running=services is not None,
        news_stream_active=services is not None and services.alpaca_news is not None,
        classifier_active=services is not None and services.classification is not None,
        classifier_model=(
            settings.deepseek_flash_model
            if services is not None and services.classification is not None
            else None
        ),
        jobs_pending=pending,
        scheduled_tasks=scheduled,
        stats=IngestionStatsResponse(**stats),
        budget=budget,
        firecrawl=firecrawl,
        queue=queue,
    )


async def _firecrawl_budget_view(
    session: DbSession, services: ServiceContainer, settings: SettingsDep
) -> FirecrawlBudgetResponse:
    """Firecrawl's cadence and spend, with no key and no secret in it.

    The per-topic block answers the question an operator actually has -- "why
    has this topic not run?" -- from the same durable ``next_eligible_at``
    column the scheduler reads, rather than from a recomputed guess.
    """
    assert services.firecrawl_budget is not None
    state = await services.firecrawl_budget.state_in_session(session)

    last_call = (
        await session.execute(
            sa.select(FirecrawlCall.reserved_at).order_by(FirecrawlCall.reserved_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    last_success = (
        await session.execute(
            sa.select(FirecrawlCall.completed_at)
            .where(FirecrawlCall.outcome == FirecrawlCallOutcome.SUCCEEDED)
            .order_by(FirecrawlCall.reserved_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    recent_results = (
        await session.execute(
            sa.select(FirecrawlCall.results_returned)
            .where(FirecrawlCall.results_returned.is_not(None))
            .order_by(FirecrawlCall.reserved_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    rows = (
        await session.execute(
            sa.select(DiscoveryQuery, DiscoveryTopic)
            .join(DiscoveryTopic, DiscoveryTopic.id == DiscoveryQuery.topic_id)
            .order_by(DiscoveryTopic.slug, DiscoveryQuery.query)
        )
    ).all()
    per_topic = [
        FirecrawlTopicUsageResponse(
            topic=topic.slug,
            query=query.query,
            enabled=query.enabled and topic.enabled,
            last_run_at=query.last_run_at,
            last_success_at=query.last_success_at,
            next_eligible_at=query.next_eligible_at,
            effective_interval_minutes=effective_topic_interval_minutes(
                topic.interval_minutes, settings
            ),
            consecutive_failures=query.consecutive_failures,
            searches_performed=query.searches_performed,
            results_seen=query.results_seen,
            credits_used=query.credits_used,
            # Provider text, truncated. Never the API key -- the Firecrawl
            # client's errors carry a status and a class name, not a request.
            last_error=query.last_error[:300] if query.last_error else None,
        )
        for query, topic in rows
    ]

    return FirecrawlBudgetResponse(
        enabled=state.enabled,
        blockers=list(state.blockers),
        exhausted=state.exhausted,
        exhausted_reasons=list(state.exhausted_reasons),
        search_exhausted=state.search_exhausted,
        scrape_exhausted=state.scrape_exhausted,
        today=FirecrawlUsageResponse(**state.today.as_dict()),
        month=FirecrawlUsageResponse(**state.month.as_dict()),
        max_searches_per_day=state.max_searches_per_day,
        max_scrapes_per_day=state.max_scrapes_per_day,
        daily_credit_cap=state.daily_credit_cap,
        monthly_credit_cap=state.monthly_credit_cap,
        searches_remaining_today=state.searches_remaining,
        scrapes_remaining_today=state.scrapes_remaining,
        daily_credits_remaining=state.daily_credits_remaining,
        monthly_credits_remaining=state.monthly_credits_remaining,
        min_topic_interval_minutes=settings.firecrawl_min_topic_interval_minutes,
        search_result_limit=settings.firecrawl_search_result_limit,
        search_sources=list(settings.firecrawl_search_sources),
        scrape_enabled=settings.firecrawl_scrape_enabled,
        day_start=state.day_start,
        month_start=state.month_start,
        last_call_at=last_call,
        last_successful_call_at=last_success,
        recent_results_returned=recent_results,
        per_topic=per_topic,
    )


@router.get(
    "/topics",
    response_model=list[DiscoveryTopicResponse],
    summary="Configured discovery topics",
)
async def list_topics(session: DbSession) -> list[DiscoveryTopicResponse]:
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
            freshness=topic.freshness,
            last_run_at=topic.last_run_at,
            queries=[
                DiscoveryQueryResponse(
                    id=query.id,
                    query=query.query,
                    enabled=query.enabled,
                    last_run_at=query.last_run_at,
                    last_success_at=query.last_success_at,
                    last_error=query.last_error,
                    consecutive_failures=query.consecutive_failures,
                    results_seen=query.results_seen,
                    credits_used=query.credits_used,
                )
                for query in sorted(topic.queries, key=lambda q: q.query)
            ],
        )
        for topic in topics
    ]
