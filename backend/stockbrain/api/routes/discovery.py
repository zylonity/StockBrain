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
    IngestionStatsResponse,
)
from stockbrain.db.models.system import AppSetting, DiscoveryTopic
from stockbrain.db.repositories.events import EventRepository
from stockbrain.jobs.queue import JobQueue
from stockbrain.services import DISCOVERY_PAUSED_KEY

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

    return DiscoveryStatusResponse(
        discovery_enabled=settings.discovery_enabled,
        paused=paused,
        subsystem_running=services is not None,
        news_stream_active=services is not None and services.alpaca_news is not None,
        jobs_pending=pending,
        scheduled_tasks=scheduled,
        stats=IngestionStatsResponse(**stats),
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
