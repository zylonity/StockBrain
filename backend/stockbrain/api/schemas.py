"""Pydantic response models for the REST API.

The frontend consumes these and nothing else: no raw provider JSON and no
secret ever crosses this boundary.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from stockbrain.enums import ProviderStatus

__all__ = [
    "CompanyImpactResponse",
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
    "LlmBudgetResponse",
    "LlmCallResponse",
    "LlmUsageResponse",
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
    """Scores are ranking features used to prioritise analysis.

    They are not calibrated probabilities and are not trading signals; the
    frontend labels them accordingly.
    """

    id: uuid.UUID
    title: str
    summary: str | None
    status: str
    event_type: str | None
    first_seen_at: dt.datetime
    event_time: dt.datetime | None
    importance_score: float | None
    novelty_score: float | None
    confidence_score: float | None = None
    candidate_score: float | None = None
    relevant_to_public_equities: bool | None = None
    needs_corroboration: bool | None = None
    topics: list[str] = Field(default_factory=list)
    classified_at: dt.datetime | None = None
    classifier_model: str | None = None
    classifier_prompt_version: str | None = None
    classifier_error: str | None = None
    merged_into_event_id: uuid.UUID | None = None
    source_count: int
    company_count: int = 0
    providers: list[str] = Field(default_factory=list)
    top_category: str | None = None


class EventListResponse(ApiModel):
    total: int
    limit: int
    offset: int
    events: list[EventSummaryResponse]


class CompanyImpactResponse(ApiModel):
    """A company the classifier believes an event affects.

    ``ticker_hint`` is a hint only. It is never sufficient to place an order:
    instrument resolution against verified broker metadata is a separate stage.
    """

    id: uuid.UUID
    company_name_hint: str
    ticker_hint: str | None
    exchange_hint: str | None
    direction: str
    impact_path: str
    relationship_type: str | None
    materiality_score: float
    confidence: float
    explanation: str | None
    resolved_company_id: uuid.UUID | None = None
    resolution_confidence: float | None = None


class LlmUsageResponse(ApiModel):
    """Aggregate model spend for one event."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    estimated_cost_usd: Decimal = Decimal("0")


class LlmCallResponse(ApiModel):
    """One recorded model attempt.

    Deliberately excludes any hidden reasoning the provider returned; only
    whether it was present is reported. The structured ``rationale`` in the
    classifier output is the explanation StockBrain shows.
    """

    id: uuid.UUID
    purpose: str
    provider: str
    model: str
    prompt_version: str | None
    thinking_enabled: bool
    succeeded: bool
    used: bool
    attempt: int
    retry_count: int
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    estimated_cost_usd: Decimal | None
    latency_ms: int | None
    finish_reason: str | None
    provider_request_id: str | None
    had_reasoning_content: bool
    error_class: str | None
    error: str | None
    created_at: dt.datetime


class EventDetailResponse(ApiModel):
    event: EventSummaryResponse
    sources: list[SourceResponse]
    companies: list[CompanyImpactResponse] = Field(default_factory=list)
    rationale: str | None = None
    """The classifier's structured justification. Not chain-of-thought."""

    llm_usage: LlmUsageResponse = Field(default_factory=LlmUsageResponse)
    llm_calls: list[LlmCallResponse] = Field(default_factory=list)


class LlmBudgetResponse(ApiModel):
    status: str
    daily_spend_usd: Decimal
    monthly_spend_usd: Decimal
    daily_soft_usd: Decimal
    daily_hard_usd: Decimal
    monthly_soft_usd: Decimal
    monthly_hard_usd: Decimal
    reason: str | None = None


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
    classifier_active: bool = False
    classifier_model: str | None = None
    jobs_pending: int
    scheduled_tasks: list[dict[str, object]]
    stats: IngestionStatsResponse
    budget: LlmBudgetResponse | None = None
