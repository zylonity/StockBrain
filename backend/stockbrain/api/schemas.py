"""Pydantic response models for the REST API.

The frontend consumes these and nothing else: no raw provider JSON and no
secret ever crosses this boundary.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field

from stockbrain.enums import ProviderStatus

__all__ = [
    "DiscoveryQueryResponse",
    "DiscoveryStatusResponse",
    "DiscoveryTopicResponse",
    "EventDetailResponse",
    "EventListResponse",
    "EventSummaryResponse",
    "ExecutionStatusResponse",
    "HealthResponse",
    "IngestionStatsResponse",
    "LivenessResponse",
    "ProviderHealthResponse",
    "ProvidersResponse",
    "ReadinessResponse",
    "SourceResponse",
    "SubsystemHealth",
]


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class LivenessResponse(ApiModel):
    status: str = "ok"
    app: str
    version: str


class ReadinessResponse(ApiModel):
    ready: bool
    database: ProviderStatus
    schema_current: bool = Field(
        description="True when the applied Alembic revision matches the code's head revision."
    )
    detail: str | None = None


class ProviderHealthResponse(ApiModel):
    provider: str
    status: ProviderStatus
    detail: str | None = None
    last_ok_at: dt.datetime | None = None
    last_checked_at: dt.datetime | None = None
    consecutive_failures: int = 0
    metrics: dict[str, object] = Field(default_factory=dict)


class SubsystemHealth(ApiModel):
    subsystem: str
    status: ProviderStatus
    providers: list[str]


class HealthResponse(ApiModel):
    status: ProviderStatus
    app: str
    version: str
    environment: str
    checked_at: dt.datetime
    subsystems: list[SubsystemHealth]


class ProvidersResponse(ApiModel):
    checked_at: dt.datetime
    providers: list[ProviderHealthResponse]


class ExecutionStatusResponse(ApiModel):
    """Execution posture, rendered as an unavoidable banner in the GUI.

    Contains no credentials -- only whether they are present.
    """

    broker: str
    broker_environment: str
    execution_mode: str
    live_execution_permitted: bool
    manual_approval_required: bool = True
    broker_credentials_configured: bool
    blockers: list[str]
    notice: str


# ---------------------------------------------------------------------------
# Discovery and events
# ---------------------------------------------------------------------------


class SourceResponse(ApiModel):
    """One piece of evidence.

    ``normalized_text`` is plain text extracted from the provider body; the
    frontend renders it as text and never as HTML, because source content is
    untrusted.
    """

    id: uuid.UUID
    provider: str
    source_name: str | None
    source_category: str | None
    headline: str | None
    author: str | None
    canonical_url: str | None
    original_url: str | None
    published_at: dt.datetime | None
    received_at: dt.datetime
    relationship: str | None = None
    excerpt: str | None = None
    symbols: list[str] = Field(default_factory=list)


class EventSummaryResponse(ApiModel):
    id: uuid.UUID
    title: str
    summary: str | None
    status: str
    event_type: str | None
    first_seen_at: dt.datetime
    event_time: dt.datetime | None
    importance_score: float | None
    novelty_score: float | None
    source_count: int
    providers: list[str] = Field(default_factory=list)
    top_category: str | None = None


class EventListResponse(ApiModel):
    total: int
    limit: int
    offset: int
    events: list[EventSummaryResponse]


class EventDetailResponse(ApiModel):
    event: EventSummaryResponse
    sources: list[SourceResponse]


class DiscoveryQueryResponse(ApiModel):
    id: uuid.UUID
    query: str
    enabled: bool
    last_run_at: dt.datetime | None
    last_success_at: dt.datetime | None
    last_error: str | None
    consecutive_failures: int
    results_seen: int
    credits_used: int


class DiscoveryTopicResponse(ApiModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    enabled: bool
    interval_minutes: int
    result_limit: int
    freshness: str
    last_run_at: dt.datetime | None
    queries: list[DiscoveryQueryResponse]


class IngestionStatsResponse(ApiModel):
    sources_total: int
    events_total: int
    sources_last_24h: int
    events_last_24h: int
    events_by_status: dict[str, int]
    sources_by_provider: dict[str, int]
    latest_source_at: dt.datetime | None


class DiscoveryStatusResponse(ApiModel):
    discovery_enabled: bool
    paused: bool
    subsystem_running: bool
    news_stream_active: bool
    jobs_pending: int
    scheduled_tasks: list[dict[str, object]]
    stats: IngestionStatsResponse
