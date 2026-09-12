"""Pydantic response models for the REST API.

The frontend consumes these and nothing else: no raw provider JSON and no
secret ever crosses this boundary.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stockbrain.enums import ProposalStatus, ProviderStatus

__all__ = [
    "AliasResponse",
    "BrokerInstrumentResponse",
    "BrokerOrderResponse",
    "CompanyImpactResponse",
    "ControlChangeRequest",
    "ControlFlagResponse",
    "ControlStateResponse",
    "DiscoveryHoldResponse",
    "DiscoveryQueryResponse",
    "DiscoveryStatusResponse",
    "DiscoveryTopicResponse",
    "EventDetailResponse",
    "EventListResponse",
    "EventSummaryResponse",
    "ExecutionAttemptResponse",
    "ExecutionStatusResponse",
    "ExecutionStatusSummaryResponse",
    "HealthResponse",
    "IngestionStatsResponse",
    "InstrumentCandidateResponse",
    "InstrumentSyncStatusResponse",
    "KillSwitchRequest",
    "LivenessResponse",
    "LlmBudgetResponse",
    "LlmCallResponse",
    "LlmUsageResponse",
    "LogEntryResponse",
    "LogFacetsResponse",
    "LogQueryResponse",
    "MarketDataHealthResponse",
    "NotificationCategoryResponse",
    "NotificationPreferencesRequest",
    "NotificationPreferencesResponse",
    "PortfolioPositionResponse",
    "PortfolioResponse",
    "PositionExitResponse",
    "PriceReactionResponse",
    "ProposalExecutionResponse",
    "ProviderHealthResponse",
    "ProvidersResponse",
    "QuoteResponse",
    "ReadinessResponse",
    "ReconciliationTriggerRequest",
    "ResolutionListResponse",
    "ResolutionResponse",
    "SettingGroupResponse",
    "SettingResponse",
    "SettingsResponse",
    "SourceResponse",
    "SubsystemHealth",
    "TelegramStatusResponse",
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


class ControlFlagResponse(ApiModel):
    """One durable execution-control flag and where its value came from."""

    flag: str
    active: bool
    changed_at: dt.datetime | None = None
    actor: str | None = None
    source: str | None = None
    reason: str | None = None


class ControlStateResponse(ApiModel):
    """The pause and kill-switch state, read from PostgreSQL.

    ``trading_halted`` is defined as "there is at least one blocker", so the
    banner and the explanation cannot disagree.  Neither flag closes a position
    or cancels a broker order: no such path exists in this phase.
    """

    trading_halted: bool
    blockers: list[str]
    paused: ControlFlagResponse
    kill_switch: ControlFlagResponse
    notice: str


class ControlChangeRequest(ApiModel):
    """The complete input a control change accepts.

    A reason and, for the kill switch, a direction.  Nothing here can influence
    a proposal, a quantity or a price.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


class KillSwitchRequest(ControlChangeRequest):
    engaged: bool = True


class ExecutionAttemptResponse(ApiModel):
    """One transmission attempt, including the ones that never transmitted.

    ``sent_to_broker`` is written *before* the HTTP request, so it means "bytes
    may have left" rather than "bytes left".  A client must read it that way:
    ``true`` with an ``AMBIGUOUS`` outcome is precisely the state in which no
    order may be sent again.
    """

    id: uuid.UUID
    proposal_id: uuid.UUID
    attempt_number: int
    broker_environment: str
    outcome: str
    ambiguous: bool
    sent_to_broker: bool
    sent_at: dt.datetime | None = None
    started_at: dt.datetime
    preflight_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None
    http_status: int | None = None
    broker_order_id: str | None = None
    request_fingerprint: str
    error: str | None = None
    error_category: str | None = None
    reconciled_at: dt.datetime | None = None
    reconciliation_result: str | None = None
    reconciliation_attempts: int = 0
    reconciliation_detail: dict[str, Any] = Field(default_factory=dict)
    rate_limit: dict[str, Any] = Field(default_factory=dict)
    execution_snapshot: dict[str, Any] = Field(default_factory=dict)
    resend_permitted: bool = False
    """Always false for a transmitted attempt.  Returned rather than implied,
    because the absence of a retry path is worth asserting on every poll."""


class BrokerOrderResponse(ApiModel):
    """StockBrain's mirror of an order the broker owns."""

    broker: str
    broker_order_id: str
    broker_environment: str | None = None
    broker_ticker: str
    side: str
    order_type: str
    quantity: Decimal
    filled_quantity: Decimal | None = None
    filled_value: Decimal | None = None
    currency: str | None = None
    broker_status: str | None = None
    initiated_from: str | None = None
    is_terminal: bool = False
    discovered_by_reconciliation: bool = False
    submitted_at: dt.datetime | None = None
    last_synced_at: dt.datetime


class ProposalExecutionResponse(ApiModel):
    """Everything known about one proposal's journey to the broker."""

    proposal_id: uuid.UUID
    proposal_status: ProposalStatus
    broker_environment: str
    authorization_source: str | None = None
    execution_policy: str
    transmitted: bool
    ambiguous: bool
    reconciliation_required: bool
    attempts: list[ExecutionAttemptResponse]
    orders: list[BrokerOrderResponse]
    notice: str


class ExecutionStatusSummaryResponse(ApiModel):
    """Deployment-level execution posture and the current attempt counts.

    ``order_transmission_permitted`` is defined as "no blockers remain", so this
    response can never show a green light beside a blocker.
    """

    broker: str
    broker_environment: str
    order_transmission_permitted: bool
    blockers: list[str]
    execution_mode: str
    execution_policy: str
    live_execution_permitted: bool
    automated_trading_consent_confirmed: bool
    trading_halted: bool
    control_blockers: list[str]
    attempts_by_outcome: dict[str, int]
    ambiguous_attempts: int
    reconciliation_pending: int
    order_endpoint: str
    order_endpoint_idempotent: bool
    notice: str


class ReconciliationTriggerRequest(ApiModel):
    """The complete input a manual reconciliation accepts: nothing.

    Reconciliation reads the broker; it has no parameters, and deliberately no
    "resend" flag.  A body that could ask for a resend would be a body that
    could duplicate an order.
    """

    model_config = ConfigDict(extra="forbid")


class DiscoveryHoldResponse(ApiModel):
    """The durable hold on scheduled discovery work.

    Distinct from the trading pause: holding discovery stops the system spending
    money on new information; pausing trading stops it acting on information it
    already has.
    """

    paused: bool
    changed_at: dt.datetime | None = None
    actor: str | None = None
    reason: str | None = None


class TelegramStatusResponse(ApiModel):
    """Bot health, containing no token and no chat content."""

    status: ProviderStatus
    bot_configured: bool
    bot_identified: bool = False
    """Whether ``getMe`` authenticated.  Deliberately a boolean: a bot's numeric
    id is the part of its token before the colon, so publishing it would put half
    the credential in a health response."""

    transport: str
    webhook_configured: bool
    polling: bool
    started_at: str | None = None
    last_contact_at: str | None = None
    last_error_category: str | None = None
    consecutive_failures: int = 0
    authorized_users: int = 0
    authorized_chats: int = 0
    notification_targets: int = 0
    group_chats_allowed: bool = False
    blockers: list[str] = Field(default_factory=list)

    fatal: str | None = None
    """Why the bot could not start, when it could not.

    Its absence here is what made this endpoint answer 500 for every deployment
    that actually had Telegram running: the runtime reports the key, the model
    forbids extras, and validation failed -- so the System Health panel was
    broken in exactly the configuration anyone would look at it in. A category,
    never a provider body: "Telegram rejected the bot token" says what to do
    without quoting a response that echoes the request."""


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

    discovered_by: list[str] = Field(default_factory=list)
    """Every provider that surfaced this artefact, first one first.  Two
    providers finding one page is corroboration, and the panel shows it."""

    extraction_method: str | None = None
    """How the body was obtained, or ``None`` while only a snippet is held."""

    content_fetched_at: dt.datetime | None = None


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
    probe_quote_stale: bool = False
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
    kind: str
    provider: str
    enabled: bool
    last_run_at: dt.datetime | None
    last_success_at: dt.datetime | None
    last_error: str | None
    consecutive_failures: int
    results_seen: int
    units_used: int


class DiscoveryTopicResponse(ApiModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    enabled: bool
    interval_minutes: int
    result_limit: int
    freshness_days: int
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


class ProviderUsageResponse(ApiModel):
    """One provider's usage inside one window."""

    searches: int
    scrapes: int
    estimated_units: int
    provider_reported_units: int
    results_returned: int
    pages_scraped: int
    estimated_cost_usd: str
    """A decimal string, not a float: a price is exact or it is not reported."""


class ProviderBudgetResponse(ApiModel):
    """One metered provider's cadence and spend, with no key and no secret in it.

    Every field here exists because Phase 2 had none of them: the only visible
    signal that 21 searches an hour were emptying a credit allowance was a
    Prometheus counter nobody was scraping, and HTTP 402 twenty-nine searches
    later.
    """

    provider: str
    unit_label: str
    """What one unit is for this provider -- ``requests`` or ``credits``. Units
    are never comparable across providers, so the label travels with them."""

    enabled: bool
    status: str
    blockers: list[str]
    exhausted: bool
    exhausted_reasons: list[str]
    search_exhausted: bool
    scrape_exhausted: bool
    today: ProviderUsageResponse
    month: ProviderUsageResponse
    max_searches_per_day: int
    max_scrapes_per_day: int
    daily_unit_cap: int
    monthly_unit_cap: int
    searches_remaining_today: int
    scrapes_remaining_today: int
    daily_units_remaining: int
    monthly_units_remaining: int
    day_start: dt.datetime
    month_start: dt.datetime
    last_call_at: dt.datetime | None = None
    last_successful_call_at: dt.datetime | None = None
    last_error: str | None = None
    recent_results_returned: int | None = None


class DiscoveryQueryUsageResponse(ApiModel):
    """One query's cadence, so "why has this not run" has an answer."""

    topic: str
    query: str
    kind: str
    provider: str
    enabled: bool
    last_run_at: dt.datetime | None
    last_success_at: dt.datetime | None
    next_eligible_at: dt.datetime | None
    effective_interval_minutes: int
    result_limit: int
    priority: int
    consecutive_failures: int
    searches_performed: int
    results_seen: int
    units_used: int
    last_error: str | None


class ContentExtractionStatusResponse(ApiModel):
    """How pages are being read, and how often that costs anything."""

    enabled: bool
    blockers: list[str]
    extractor: str
    """The library and strategy in use, named so the panel is not a black box."""

    max_per_day: int
    fetched_today: int
    by_method_today: dict[str, int]
    """``LOCAL`` / ``FIRECRAWL`` / ``NONE`` counts for the current UTC day. The
    only one of these that costs money is ``FIRECRAWL``."""

    local_failures_today: int
    firecrawl_fallbacks_today: int
    fallback_enabled: bool


class WebDiscoveryStatusResponse(ApiModel):
    """The provider split, made legible in one object.

    Answers the four questions an operator actually has: who is doing routine
    search, who is doing semantic search, how much of each allowance is left,
    and why a given query has not run.
    """

    enabled: bool
    routine_provider: str
    semantic_provider: str
    routine_min_interval_minutes: int
    semantic_min_interval_minutes: int
    providers: list[ProviderBudgetResponse] = Field(default_factory=list)
    queries: list[DiscoveryQueryUsageResponse] = Field(default_factory=list)
    extraction: ContentExtractionStatusResponse | None = None


class FxStatusResponse(ApiModel):
    """Foreign-exchange source, freshness and grade.

    Reported because cross-currency sizing depends on it entirely: on a GBP
    account holding USD listings, "no FX" and "stale FX" are the difference
    between a working system and one that blocks every proposal it can price.
    """

    provider: str
    grade: str | None
    configured: bool
    available: bool
    blockers: list[str]
    detail: str | None = None
    max_age_seconds: float
    reference_max_age_seconds: float
    allow_reference_grade: bool
    max_rate_drift_pct: Decimal
    probe_pair: str
    probe_rate: Decimal | None = None
    probe_age_seconds: Decimal | None = None
    probe_provider_timestamp: dt.datetime | None = None


class JobQueueHealthResponse(ApiModel):
    """Operational visibility for the PostgreSQL job queue.

    "The queue is the audit trail" is only true if somebody can read it. These
    are the five questions an operator actually asks when the pipeline has
    stopped moving.
    """

    pending: int
    running: int
    failed: int
    dead: int
    """Jobs that exhausted ``max_attempts``. Terminal; they never run again
    without an operator, which is exactly why they need surfacing."""

    oldest_pending_age_seconds: float | None
    oldest_pending_job_type: str | None
    stuck: int
    """RUNNING with a lock older than ``JOB_CLAIM_TIMEOUT_SECONDS`` -- a worker
    that died, or a handler that hangs."""

    stuck_job_types: list[str]
    counts_by_type: dict[str, int]
    counts_by_status: dict[str, int]


class WebSecurityResponse(ApiModel):
    """The HTTP surface's own posture, reported rather than assumed."""

    auth_enabled: bool
    auth_effective: bool
    """``auth_enabled`` and nothing missing.  The two differ when
    authentication is switched on but has no password hash or no signing key,
    in which case every protected route answers 503 -- which is safe, and worth
    being able to see."""

    blockers: list[str]
    trusted_network_acknowledged: bool
    session_ttl_seconds: int
    cookie_secure: bool
    cookie_samesite: str
    csrf_header: str
    public_paths: list[str]
    cors_allow_origins: list[str]


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
    web_discovery: WebDiscoveryStatusResponse | None = None
    queue: JobQueueHealthResponse | None = None


# ---------------------------------------------------------------------------
# Application logs
#
# The wire shape of a bounded, in-memory ring of this process's own structured
# log events.  ``captured_since`` and ``dropped`` are part of the contract
# rather than debug extras: a page that silently shows the last four thousand
# lines of a ten thousand line incident is worse than one that says so.
# ---------------------------------------------------------------------------


class LogEntryResponse(ApiModel):
    sequence: int
    """Process-local monotonic id.  Stable for paging; meaningless across restarts."""

    timestamp: dt.datetime
    level: str
    logger: str
    event: str
    service: str
    category: str
    message: str = ""
    """Rendered exception text, where the event carried one.  Scrubbed of every
    configured credential before it was ever stored."""

    fields: dict[str, str] = Field(default_factory=dict)


class LogQueryResponse(ApiModel):
    entries: list[LogEntryResponse]
    total: int
    limit: int
    offset: int
    capacity: int
    stored: int
    dropped: int
    """Entries evicted since start-up because the buffer was full."""

    captured_since: dt.datetime | None = None
    oldest_at: dt.datetime | None = None
    newest_at: dt.datetime | None = None
    min_captured_level: str
    """Nothing below this level is captured at all, whatever a filter asks for."""

    enabled: bool


class LogFacetsResponse(ApiModel):
    services: dict[str, int]
    categories: dict[str, int]
    levels: dict[str, int]


# ---------------------------------------------------------------------------
# Configuration, as an operator reads it
# ---------------------------------------------------------------------------


class SettingResponse(ApiModel):
    key: str
    label: str
    value: str | None = None
    """Rendered for display.  Always ``null`` for a secret."""

    mutability: str
    description: str
    env_var: str | None = None
    unit: str | None = None
    impact: str | None = None
    control: str | None = None
    """For runtime rows, which control changes it."""

    configured: bool | None = None
    """For secrets only: whether one is present.  Never what it is."""


class SettingGroupResponse(ApiModel):
    key: str
    title: str
    description: str
    warning: str | None = None
    blockers: list[str] = Field(default_factory=list)
    settings: list[SettingResponse]


class SettingsResponse(ApiModel):
    """The whole configuration surface, read-only by construction.

    There is deliberately no companion PUT.  The runtime state this page can
    change -- the pause, the kill switch, the discovery hold, the notification
    categories -- each has its own typed, audited route.
    """

    groups: list[SettingGroupResponse]
    generated_at: dt.datetime


# ---------------------------------------------------------------------------
# Telegram notification preferences
# ---------------------------------------------------------------------------


class NotificationCategoryResponse(ApiModel):
    category: str
    label: str
    description: str
    volume: str
    """How chatty this category is: rare, low, medium or high."""

    enabled: bool
    locked: bool = False
    """A locked category cannot be switched off.  Only the unknown-order-state
    message is locked, because the correct response to it is to do nothing and
    the obvious one is to resend."""


class NotificationPreferencesResponse(ApiModel):
    categories: list[NotificationCategoryResponse]
    notifications_enabled: bool
    """The environment-level master switch.  With it off, nothing is delivered
    whatever these categories say."""

    delivery_available: bool
    blockers: list[str] = Field(default_factory=list)
    updated_at: dt.datetime | None = None
    updated_by: str | None = None


class NotificationPreferencesRequest(ApiModel):
    """A partial update: only the named categories change."""

    model_config = ConfigDict(extra="forbid")

    categories: dict[str, bool] = Field(default_factory=dict, max_length=32)


# ---------------------------------------------------------------------------
# Portfolio
#
# The stored mirror of the broker account, never a live broker request: the
# summary endpoint allows one call every five seconds, and a page that refreshes
# would spend that budget on nothing.
# ---------------------------------------------------------------------------


class PositionExitResponse(ApiModel):
    """Where each exit rule would act for one open position.

    Computed by the same predicates the rules fire on, so the floor shown and
    the floor acted on cannot disagree.  ``managed`` is false when StockBrain has
    no executed buy behind the position: ``reason`` then says why and every floor
    is ``None`` -- a position never opened from a thesis has none to exit
    against, and inventing a floor would put a rule's name on a decision it never
    made.
    """

    managed: bool
    reason: str | None = None
    hard_stop: Decimal | None = None
    volatility_floor: Decimal | None = None
    trailing_floor: Decimal | None = None
    roi_target_price: Decimal | None = None
    horizon_ends_at: dt.datetime | None = None
    nearest_floor: Decimal | None = None
    nearest_rule: str | None = None
    peak_price: Decimal | None = None
    atr: Decimal | None = None
    horizon: str | None = None


class PortfolioPositionResponse(ApiModel):
    broker_ticker: str
    name: str | None = None
    quantity: Decimal
    quantity_available: Decimal | None = None
    average_price: Decimal | None = None
    current_price: Decimal | None = None
    ppl: Decimal | None = None
    currency: str | None = None
    last_synced_at: dt.datetime
    exit: PositionExitResponse | None = None


class PortfolioResponse(ApiModel):
    available: bool
    reason: str | None = None
    account_id: str | None = None
    currency: str | None = None
    broker: str = "trading212"
    broker_environment: str | None = None
    total_value: Decimal | None = None
    invested_value: Decimal | None = None
    result_value: Decimal | None = None
    cash_available: Decimal | None = None
    cash_reserved: Decimal | None = None
    cash_in_pies: Decimal | None = None
    captured_at: dt.datetime | None = None
    position_count: int = 0
    positions: list[PortfolioPositionResponse] = Field(default_factory=list)
    stale: bool = False
    """Whether the snapshot is older than the risk engine would accept for
    sizing.  Displayed rather than hidden: an operator reading a portfolio needs
    to know it is the number a trade would *not* have been sized on."""

    max_age_seconds: float | None = None
