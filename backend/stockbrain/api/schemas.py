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
    "AliasResponse",
    "BrokerInstrumentResponse",
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
    "InstrumentCandidateResponse",
    "InstrumentSyncStatusResponse",
    "LivenessResponse",
    "LlmBudgetResponse",
    "LlmCallResponse",
    "LlmUsageResponse",
    "MarketDataHealthResponse",
    "PriceReactionResponse",
    "ProviderHealthResponse",
    "ProvidersResponse",
    "QuoteResponse",
    "ReadinessResponse",
    "ResolutionListResponse",
    "ResolutionResponse",
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


class InstrumentCandidateResponse(ApiModel):
    """One listing that matched, shown so an ambiguity explains itself."""

    broker_instrument_id: uuid.UUID
    broker_ticker: str
    name: str | None = None
    market_symbol: str | None = None
    exchange: str | None = None
    currency: str | None = None
    isin: str | None = None
    instrument_type: str | None = None
    matched_by: str


class BrokerInstrumentResponse(ApiModel):
    id: uuid.UUID
    broker: str
    broker_ticker: str
    name: str | None = None
    short_name: str | None = None
    market_symbol: str | None = None
    market_code: str | None = None
    exchange: str | None = None
    currency: str | None = None
    isin: str | None = None
    instrument_type: str | None = None
    extended_hours: bool = False
    min_trade_quantity: Decimal | None = None
    max_open_quantity: Decimal | None = None
    working_schedule_id: int | None = None
    added_on: dt.datetime | None = None
    is_active: bool = True
    company_id: uuid.UUID | None = None
    last_refreshed_at: dt.datetime | None = None


class ResolutionResponse(ApiModel):
    """How one classifier company hint mapped onto a verified instrument.

    Both sides are shown deliberately: the model's ticker hint next to the
    resolved broker instrument, so a reviewer can see that the hint was a search
    key and never the identity.
    """

    impact_id: uuid.UUID
    event_id: uuid.UUID
    event_title: str | None = None

    company_name_hint: str
    model_ticker_hint: str | None = None
    model_exchange_hint: str | None = None

    status: str
    method: str | None = None
    confidence: float | None = None
    notes: str | None = None
    resolved_at: dt.datetime | None = None

    company_id: uuid.UUID | None = None
    company_name: str | None = None
    broker_instrument_id: uuid.UUID | None = None
    broker_ticker: str | None = None
    market_symbol: str | None = None
    exchange: str | None = None
    currency: str | None = None
    isin: str | None = None
    instrument_type: str | None = None

    alternatives: list[InstrumentCandidateResponse] = Field(default_factory=list)


class ResolutionListResponse(ApiModel):
    items: list[ResolutionResponse]
    total: int
    limit: int
    offset: int
    counts_by_status: dict[str, int] = Field(default_factory=dict)


class AliasResponse(ApiModel):
    id: uuid.UUID
    company_id: uuid.UUID
    company_name: str | None = None
    alias: str
    alias_normalized: str
    alias_type: str
    exchange: str | None = None
    currency: str | None = None
    isin: str | None = None
    is_authoritative: bool = True
    confidence: float = 1.0
    source: str = "MANUAL"
    notes: str | None = None


class InstrumentSyncStatusResponse(ApiModel):
    broker: str
    configured: bool
    instruments_total: int
    instruments_active: int
    with_isin: int
    with_exchange: int
    exchanges: int
    working_schedules: int
    last_refreshed_at: dt.datetime | None = None
    rate_limit: dict[str, object] = Field(default_factory=dict)


class QuoteResponse(ApiModel):
    """A quote as the system records it: never a bare number.

    Prices are serialised as strings so a JSON parser cannot turn a ``Decimal``
    back into a binary float on the way to the browser.
    """

    symbol: str
    provider: str
    feed: str
    price_source: str
    price: str | None = None
    bid: str | None = None
    ask: str | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    currency: str = "USD"
    provider_timestamp: dt.datetime
    received_at: dt.datetime
    quote_age_ms: int
    is_two_sided: bool
    execution_grade: bool
    sizing_blockers: list[str] = Field(default_factory=list)


class MarketDataHealthResponse(ApiModel):
    provider: str | None = None
    configured: bool
    state: str
    feed: str | None = None
    detail: str | None = None
    checked_at: dt.datetime | None = None
    realtime_pricing_usable: bool = False
    probe_symbol: str | None = None
    probe_quote_age_ms: int | None = None
    max_quote_age_seconds: float
    blockers: list[str] = Field(default_factory=list)


class PriceReactionResponse(ApiModel):
    symbol: str
    event_time: dt.datetime
    status: str
    provider: str | None = None
    feed: str | None = None
    price_at_event: str | None = None
    price_at_event_time: dt.datetime | None = None
    price_at_event_basis: str | None = None
    reference_price: str | None = None
    reference_price_time: dt.datetime | None = None
    reference_price_basis: str | None = None
    reference_quote_age_ms: int | None = None
    absolute_move: str | None = None
    percent_move: str | None = None
    elapsed_seconds: float
    session_at_event: str
    session_source: str
    session_holiday_aware: bool
    notes: list[str] = Field(default_factory=list)


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
    resolution_status: str = "PENDING"
    resolution_method: str | None = None
    resolution_notes: str | None = None
    broker_instrument_id: uuid.UUID | None = None
    broker_ticker: str | None = None
    resolved_market_symbol: str | None = None
    resolved_exchange: str | None = None
    resolved_currency: str | None = None
    resolved_isin: str | None = None
    resolution_alternatives: list[InstrumentCandidateResponse] = Field(default_factory=list)


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
