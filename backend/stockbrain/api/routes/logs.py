"""Query this process's own structured logs.

Two endpoints, both read-only, both bounded, and neither of them touching a
filesystem.  ``/logs`` filters the in-memory ring buffer; ``/logs/facets``
reports what it currently contains so the filter UI offers only values that
would return something.

The service names here are the same keys the health board uses for a provider,
which is what makes "View logs" on a DOWN provider a link rather than a feature
request: ``/api/v1/system/logs?services=brave`` is the same ``brave`` that
appears in ``/api/health/providers``.

Nothing here can widen what was captured.  A credential never entered the
buffer (the capture processor scrubs before it stores), the level floor is the
process's configured log level, and a filter can only narrow the result.  There
is no path, glob, file, container or command parameter anywhere in this module.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Query

from stockbrain.api.schemas import (
    LogEntryResponse,
    LogFacetsResponse,
    LogQueryResponse,
)
from stockbrain.logging import log_buffer
from stockbrain.observability.logstore import LEVEL_SEVERITY, LogQuery, csv_tuple

router = APIRouter(prefix="/api/v1/system", tags=["system"])

#: Hard ceiling on one page.  The buffer itself is bounded, but a client asking
#: for all of it in one response would serialise several megabytes of JSON for a
#: table nobody scrolls that far down.
MAX_LIMIT = 500


@router.get(
    "/logs",
    response_model=LogQueryResponse,
    summary="Recent structured application logs",
)
async def logs(
    min_level: str | None = Query(
        default=None,
        description="Lowest severity to return: debug, info, warning, error, critical.",
    ),
    services: str | None = Query(
        default=None,
        description="Comma-separated service keys, matching the health board's provider names.",
    ),
    categories: str | None = Query(
        default=None, description="Comma-separated functional categories."
    ),
    since_minutes: int | None = Query(
        default=None, ge=1, le=20160, description="Only entries newer than this many minutes."
    ),
    search: str | None = Query(
        default=None,
        max_length=200,
        description="Case-insensitive substring match over the event, logger, service and fields.",
    ),
    limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0, le=100000),
) -> LogQueryResponse:
    buffer = log_buffer()
    since: dt.datetime | None = None
    if since_minutes is not None:
        since = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=since_minutes)

    # An unrecognised level name is dropped rather than rejected: the filter is
    # a convenience, and answering 422 because a bookmarked URL says "warn"
    # would be worse than answering with everything.
    level = min_level.lower() if min_level and min_level.lower() in LEVEL_SEVERITY else None

    result = buffer.query(
        LogQuery(
            min_level=level,
            services=csv_tuple(services),
            categories=csv_tuple(categories),
            since=since,
            search=(search or "").strip() or None,
            limit=limit,
            offset=offset,
        )
    )
    return LogQueryResponse(
        entries=[
            LogEntryResponse(
                sequence=entry.sequence,
                timestamp=entry.timestamp,
                level=entry.level,
                logger=entry.logger,
                event=entry.event,
                service=entry.service,
                category=entry.category,
                message=entry.message,
                fields=entry.fields,
            )
            for entry in result.entries
        ],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
        capacity=result.capacity,
        stored=result.stored,
        dropped=result.dropped,
        captured_since=result.captured_since,
        oldest_at=result.oldest_at,
        newest_at=result.newest_at,
        min_captured_level=result.min_captured_level,
        enabled=buffer.enabled,
    )


@router.get(
    "/logs/facets",
    response_model=LogFacetsResponse,
    summary="Services, categories and levels currently present in the log buffer",
)
async def log_facets() -> LogFacetsResponse:
    facets = log_buffer().facets()
    return LogFacetsResponse(
        services=facets.services,
        categories=facets.categories,
        levels=facets.levels,
    )
