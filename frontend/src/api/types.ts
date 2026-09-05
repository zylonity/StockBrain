/**
 * Types mirroring the backend's Pydantic response models.
 *
 * The REST API is the single source of truth. The frontend never recomputes
 * financial or risk values; it renders what the server decided.
 */

export type ProviderStatus =
  | "HEALTHY"
  | "DEGRADED"
  | "DOWN"
  | "DISABLED"
  | "UNKNOWN";

export interface SubsystemHealth {
  subsystem: string;
  status: ProviderStatus;
  providers: string[];
}

export interface HealthResponse {
  status: ProviderStatus;
  app: string;
  version: string;
  environment: string;
  checked_at: string;
  subsystems: SubsystemHealth[];
}

export interface ProviderHealth {
  provider: string;
  status: ProviderStatus;
  detail: string | null;
  last_ok_at: string | null;
  last_checked_at: string | null;
  consecutive_failures: number;
  metrics: Record<string, unknown>;
}

export interface ProvidersResponse {
  checked_at: string;
  providers: ProviderHealth[];
}

export interface ReadinessResponse {
  ready: boolean;
  database: ProviderStatus;
  schema_current: boolean;
  detail: string | null;
}

export interface ExecutionStatusResponse {
  broker: string;
  broker_environment: string;
  execution_mode: string;
  live_execution_permitted: boolean;
  manual_approval_required: boolean;
  broker_credentials_configured: boolean;
  blockers: string[];
  notice: string;
}

// ---------------------------------------------------------------------------
// Discovery and events
// ---------------------------------------------------------------------------

export type SourceProvider = "ALPACA" | "FIRECRAWL" | "SEC" | "MANUAL";

export type EventStatus =
  | "NEW"
  | "CLASSIFYING"
  | "CLASSIFIED"
  | "CLASSIFICATION_FAILED"
  | "IRRELEVANT"
  | "CANDIDATE"
  | "RESEARCHING"
  | "RESEARCHED"
  | "ARCHIVED";

export type SourceCategory =
  | "REGULATOR"
  | "ISSUER"
  | "GOVERNMENT"
  | "NEWSWIRE"
  | "PRESS"
  | "UNKNOWN";

/**
 * Scores are ranking features used to prioritise analysis. They are not
 * calibrated probabilities and are not trading signals.
 */
export interface EventSummary {
  id: string;
  title: string;
  summary: string | null;
  status: EventStatus;
  event_type: string | null;
  first_seen_at: string;
  event_time: string | null;
  importance_score: number | null;
  novelty_score: number | null;
  confidence_score: number | null;
  candidate_score: number | null;
  relevant_to_public_equities: boolean | null;
  needs_corroboration: boolean | null;
  topics: string[];
  classified_at: string | null;
  classifier_model: string | null;
  classifier_prompt_version: string | null;
  classifier_error: string | null;
  merged_into_event_id: string | null;
  source_count: number;
  company_count: number;
  providers: string[];
  top_category: SourceCategory | null;
}

export type ImpactDirection = "POSITIVE" | "NEGATIVE" | "MIXED" | "UNKNOWN";
export type ImpactPath = "direct" | "indirect" | "unknown";

/**
 * `ticker_hint` is a hint only. Resolving it to a real tradable instrument is a
 * separate, later stage; nothing here is sufficient to place an order.
 */
export interface CompanyImpact {
  id: string;
  company_name_hint: string;
  ticker_hint: string | null;
  exchange_hint: string | null;
  direction: ImpactDirection;
  impact_path: ImpactPath;
  relationship_type: string | null;
  materiality_score: number;
  confidence: number;
  explanation: string | null;
  resolved_company_id: string | null;
  resolution_confidence: number | null;
}

export interface LlmUsage {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
  estimated_cost_usd: string;
}

export interface LlmCall {
  id: string;
  purpose: string;
  provider: string;
  model: string;
  prompt_version: string | null;
  thinking_enabled: boolean;
  succeeded: boolean;
  used: boolean;
  attempt: number;
  retry_count: number;
  input_tokens: number | null;
  output_tokens: number | null;
  cached_input_tokens: number | null;
  estimated_cost_usd: string | null;
  latency_ms: number | null;
  finish_reason: string | null;
  provider_request_id: string | null;
  /** Whether the provider returned hidden reasoning. The text itself is never
   *  stored or exposed; the structured rationale is what is shown. */
  had_reasoning_content: boolean;
  error_class: string | null;
  error: string | null;
  created_at: string;
}

export interface LlmBudget {
  status: "OK" | "SOFT_EXCEEDED" | "HARD_EXCEEDED";
  daily_spend_usd: string;
  monthly_spend_usd: string;
  daily_soft_usd: string;
  daily_hard_usd: string;
  monthly_soft_usd: string;
  monthly_hard_usd: string;
  reason: string | null;
}

export interface EventListResponse {
  total: number;
  limit: number;
  offset: number;
  events: EventSummary[];
}

export interface SourceRecord {
  id: string;
  provider: string;
  source_name: string | null;
  source_category: string | null;
  headline: string | null;
  author: string | null;
  canonical_url: string | null;
  original_url: string | null;
  published_at: string | null;
  received_at: string;
  relationship: string | null;
  /** Plain text extracted server-side. Never raw provider HTML. */
  excerpt: string | null;
  symbols: string[];
}

export interface EventDetail {
  event: EventSummary;
  sources: SourceRecord[];
  companies: CompanyImpact[];
  /** The classifier's structured justification. Not chain-of-thought. */
  rationale: string | null;
  llm_usage: LlmUsage;
  llm_calls: LlmCall[];
}

export interface IngestionStats {
  sources_total: number;
  events_total: number;
  sources_last_24h: number;
  events_last_24h: number;
  events_by_status: Record<string, number>;
  sources_by_provider: Record<string, number>;
  latest_source_at: string | null;
}

export interface ScheduledTask {
  name: string;
  interval_seconds: number;
  enabled: boolean;
  last_run_at: string | null;
  last_error: string | null;
}

export interface DiscoveryStatus {
  discovery_enabled: boolean;
  paused: boolean;
  subsystem_running: boolean;
  news_stream_active: boolean;
  classifier_active: boolean;
  classifier_model: string | null;
  jobs_pending: number;
  scheduled_tasks: ScheduledTask[];
  stats: IngestionStats;
  budget: LlmBudget | null;
}

export interface DiscoveryQueryRecord {
  id: string;
  query: string;
  enabled: boolean;
  last_run_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  consecutive_failures: number;
  results_seen: number;
  credits_used: number;
}

export interface DiscoveryTopic {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  enabled: boolean;
  interval_minutes: number;
  result_limit: number;
  freshness: string;
  last_run_at: string | null;
  queries: DiscoveryQueryRecord[];
}

export interface EventFilters {
  status?: EventStatus;
  provider?: SourceProvider;
  search?: string;
  limit?: number;
  offset?: number;
  sinceHours?: number;
}

// ---------------------------------------------------------------------------
// Instrument resolution (phase 4)
//
// The model's ticker hint and the resolved broker instrument are separate
// fields on purpose: the UI shows both so it is visible that the hint was a
// search key and the executable identity came from verified broker metadata.
// ---------------------------------------------------------------------------

export type ResolutionStatus =
  | "PENDING"
  | "RESOLVED"
  | "AMBIGUOUS"
  | "NOT_FOUND"
  | "UNSUPPORTED";

export interface InstrumentCandidate {
  broker_instrument_id: string;
  broker_ticker: string;
  name: string | null;
  market_symbol: string | null;
  exchange: string | null;
  currency: string | null;
  isin: string | null;
  instrument_type: string | null;
  matched_by: string;
}

export interface Resolution {
  impact_id: string;
  event_id: string;
  event_title: string | null;
  company_name_hint: string;
  model_ticker_hint: string | null;
  model_exchange_hint: string | null;
  status: ResolutionStatus;
  method: string | null;
  confidence: number | null;
  notes: string | null;
  resolved_at: string | null;
  company_id: string | null;
  company_name: string | null;
  broker_instrument_id: string | null;
  broker_ticker: string | null;
  market_symbol: string | null;
  exchange: string | null;
  currency: string | null;
  isin: string | null;
  instrument_type: string | null;
  alternatives: InstrumentCandidate[];
}

export interface ResolutionListResponse {
  items: Resolution[];
  total: number;
  limit: number;
  offset: number;
  counts_by_status: Record<string, number>;
}

export interface BrokerInstrumentRecord {
  id: string;
  broker: string;
  broker_ticker: string;
  name: string | null;
  short_name: string | null;
  market_symbol: string | null;
  market_code: string | null;
  exchange: string | null;
  currency: string | null;
  isin: string | null;
  instrument_type: string | null;
  extended_hours: boolean;
  min_trade_quantity: string | null;
  max_open_quantity: string | null;
  working_schedule_id: number | null;
  added_on: string | null;
  is_active: boolean;
  company_id: string | null;
  last_refreshed_at: string | null;
}

export interface InstrumentSyncStatus {
  broker: string;
  configured: boolean;
  instruments_total: number;
  instruments_active: number;
  with_isin: number;
  with_exchange: number;
  exchanges: number;
  working_schedules: number;
  last_refreshed_at: string | null;
  rate_limit: Record<string, unknown>;
}

export interface CompanyAliasRecord {
  id: string;
  company_id: string;
  company_name: string | null;
  alias: string;
  alias_normalized: string;
  alias_type: string;
  exchange: string | null;
  currency: string | null;
  isin: string | null;
  is_authoritative: boolean;
  confidence: number;
  source: string;
  notes: string | null;
}

// ---------------------------------------------------------------------------
// Market data
//
// Prices arrive as strings, not numbers: they are Decimals server-side and
// JSON.parse would turn them into binary floats.
// ---------------------------------------------------------------------------

export type CapabilityState =
  | "HEALTHY"
  | "AUTH_FAILED"
  | "ENTITLEMENT_MISSING"
  | "DEGRADED"
  | "DOWN"
  | "DISABLED"
  | "UNKNOWN";

export interface MarketDataHealth {
  provider: string | null;
  configured: boolean;
  state: CapabilityState;
  feed: string | null;
  detail: string | null;
  checked_at: string | null;
  realtime_pricing_usable: boolean;
  probe_symbol: string | null;
  probe_quote_age_ms: number | null;
  probe_quote_stale: boolean;
  max_quote_age_seconds: number;
  blockers: string[];
}

export interface Quote {
  symbol: string;
  provider: string;
  feed: string;
  price_source: string;
  price: string | null;
  bid: string | null;
  ask: string | null;
  bid_size: number | null;
  ask_size: number | null;
  currency: string;
  provider_timestamp: string;
  received_at: string;
  quote_age_ms: number;
  is_two_sided: boolean;
  execution_grade: boolean;
  sizing_blockers: string[];
}

export interface PriceReaction {
  symbol: string;
  event_time: string;
  status: string;
  provider: string | null;
  feed: string | null;
  price_at_event: string | null;
  price_at_event_time: string | null;
  price_at_event_basis: string | null;
  reference_price: string | null;
  reference_price_time: string | null;
  reference_price_basis: string | null;
  reference_quote_age_ms: number | null;
  absolute_move: string | null;
  percent_move: string | null;
  elapsed_seconds: number;
  session_at_event: string;
  session_source: string;
  session_holiday_aware: boolean;
  notes: string[];
}
