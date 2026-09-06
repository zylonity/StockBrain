/**
 * A stubbed `fetch` and a router, shared by every component test.
 *
 * The stub matches on `METHOD path` and records every call, so a test can
 * assert what the real API client actually sent -- the CSRF header, the query
 * string it built, the JSON body -- rather than asserting against a mock of the
 * client. That distinction matters here: the client is where the CSRF token and
 * the same-origin credential policy live, and a test that mocked it would prove
 * nothing about either.
 */

import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { render } from "@testing-library/react";
import { vi } from "vitest";

export interface RecordedCall {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: unknown;
}

export interface FetchStub {
  calls: RecordedCall[];
  /** Every call whose path (ignoring the query string) matches. */
  callsTo(path: string): RecordedCall[];
  /** Replace or add a route after the stub was installed. */
  set(route: string, responder: Responder): void;
}

type Responder =
  | object
  | ((call: RecordedCall) => object)
  | { __status: number; body?: unknown };

function isStatusResponse(value: Responder): value is { __status: number; body?: unknown } {
  return typeof value === "object" && value !== null && "__status" in value;
}

/** Reply with an HTTP error, so a test can drive the client's error path. */
export function failWith(status: number, detail: string) {
  return { __status: status, body: { detail } };
}

/**
 * Install a `fetch` stub for the given routes.
 *
 * Keys are `"GET /api/v1/..."`. A request to an unrouted path fails loudly
 * rather than returning an empty object: a page that quietly renders from `{}`
 * is a page whose test proves nothing.
 */
export function stubFetch(routes: Record<string, Responder>): FetchStub {
  const table = new Map(Object.entries(routes));
  const calls: RecordedCall[] = [];

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      const headers = Object.fromEntries(
        Object.entries((init?.headers ?? {}) as Record<string, string>),
      );
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      const call: RecordedCall = { method, url, headers, body };
      calls.push(call);

      const path = url.split("?")[0];
      const responder = table.get(`${method} ${url}`) ?? table.get(`${method} ${path}`);
      if (responder === undefined) {
        return new Response(JSON.stringify({ detail: `no stub for ${method} ${url}` }), {
          status: 501,
          headers: { "content-type": "application/json" },
        });
      }
      if (isStatusResponse(responder)) {
        return new Response(JSON.stringify(responder.body ?? {}), {
          status: responder.__status,
          headers: { "content-type": "application/json" },
        });
      }
      const payload = typeof responder === "function" ? responder(call) : responder;
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );

  return {
    calls,
    callsTo: (path: string) => calls.filter((call) => call.url.split("?")[0] === path),
    set: (route, responder) => table.set(route, responder),
  };
}

export function renderAt(element: ReactElement, path = "/") {
  return render(<MemoryRouter initialEntries={[path]}>{element}</MemoryRouter>);
}

/* --------------------------------------------------------------------------
 * Fixtures. Minimal but *shaped like the API*: a fixture that omitted a field
 * the page reads would make the test pass against a payload the server never
 * sends.
 * ----------------------------------------------------------------------- */

export const executionStatus = {
  broker: "trading212",
  broker_environment: "demo",
  execution_mode: "manual_approval",
  live_execution_permitted: false,
  manual_approval_required: true,
  broker_credentials_configured: false,
  blockers: ["T212_ENV is not 'live' (demo environment in use)"],
  notice: "Trading 212 live execution is disabled.",
};

export const controlState = {
  trading_halted: false,
  blockers: [],
  paused: { flag: "control.trading_paused", active: false, changed_at: null, actor: null, source: null, reason: null },
  kill_switch: { flag: "control.kill_switch", active: false, changed_at: null, actor: null, source: null, reason: null },
  notice: "Pausing and the kill switch stop new proposals.",
};

export const haltedControlState = {
  ...controlState,
  trading_halted: true,
  blockers: ["trading is paused (budget review)"],
  paused: {
    flag: "control.trading_paused",
    active: true,
    changed_at: "2026-09-06T10:00:00Z",
    actor: "web:owner",
    source: "HUMAN_WEB",
    reason: "budget review",
  },
};

export const logsResponse = {
  entries: [
    {
      sequence: 2,
      timestamp: "2026-09-06T12:00:01Z",
      level: "warning",
      logger: "stockbrain.ingestion.brave",
      event: "brave_rate_limited",
      service: "brave",
      category: "discovery",
      message: "",
      fields: { http_status: "429" },
    },
    {
      sequence: 1,
      timestamp: "2026-09-06T12:00:00Z",
      level: "info",
      logger: "stockbrain.ingestion.exa",
      event: "exa_search_complete",
      service: "exa",
      category: "discovery",
      message: "",
      fields: { results: "7" },
    },
  ],
  total: 2,
  limit: 100,
  offset: 0,
  capacity: 4000,
  stored: 2,
  dropped: 0,
  captured_since: "2026-09-06T11:00:00Z",
  oldest_at: "2026-09-06T12:00:00Z",
  newest_at: "2026-09-06T12:00:01Z",
  min_captured_level: "info",
  enabled: true,
};

export const logFacets = {
  services: { brave: 1, exa: 1 },
  categories: { discovery: 2 },
  levels: { info: 1, warning: 1 },
};

export const settingsResponse = {
  generated_at: "2026-09-06T12:00:00Z",
  groups: [
    {
      key: "execution",
      title: "Broker and execution gates",
      description: "Four independent gates stand between this deployment and a live order.",
      warning: "Changing any gate below requires editing .env and restarting the application.",
      blockers: ["T212_LIVE_EXECUTION_ENABLED is false"],
      settings: [
        {
          key: "t212_env",
          label: "Broker environment",
          value: "demo",
          mutability: "RESTART_REQUIRED",
          description: "demo is Trading 212's paper environment.",
          env_var: "T212_ENV",
          unit: null,
          impact: "The single largest difference in the system's behaviour.",
          control: null,
          configured: null,
        },
        {
          key: "t212_api_key",
          label: "Trading 212 API key",
          value: null,
          mutability: "SECRET",
          description: "Broker credential.",
          env_var: "T212_API_KEY",
          unit: null,
          impact: null,
          control: null,
          configured: true,
        },
      ],
    },
  ],
};

export const notificationPreferences = {
  notifications_enabled: true,
  delivery_available: true,
  blockers: [],
  updated_at: null,
  updated_by: null,
  categories: [
    {
      category: "EVENT_DISCOVERED",
      label: "Article discovered",
      description: "Every new canonical event.",
      volume: "high",
      enabled: false,
      locked: false,
    },
    {
      category: "EXECUTION_CRITICAL",
      label: "Order state unknown",
      description: "An order may or may not exist at the broker.",
      volume: "rare",
      enabled: true,
      locked: true,
    },
  ],
};

export const providersResponse = {
  checked_at: "2026-09-06T12:00:00Z",
  providers: [
    {
      provider: "brave",
      status: "DOWN",
      detail: "brave: HTTP 401",
      last_ok_at: null,
      last_checked_at: "2026-09-06T12:00:00Z",
      consecutive_failures: 3,
      metrics: {},
    },
    {
      provider: "postgres",
      status: "HEALTHY",
      detail: null,
      last_ok_at: "2026-09-06T12:00:00Z",
      last_checked_at: "2026-09-06T12:00:00Z",
      consecutive_failures: 0,
      metrics: {},
    },
    {
      provider: "exa",
      status: "BUDGET_EXHAUSTED",
      detail: "daily search cap reached",
      last_ok_at: "2026-09-06T09:00:00Z",
      last_checked_at: "2026-09-06T12:00:00Z",
      consecutive_failures: 0,
      metrics: {},
    },
    {
      provider: "llm",
      status: "HEALTHY",
      detail: "GET /v1/models 200",
      last_ok_at: "2026-09-06T12:00:00Z",
      last_checked_at: "2026-09-06T12:00:00Z",
      consecutive_failures: 0,
      metrics: {},
    },
    {
      provider: "trading212",
      status: "DISABLED",
      detail: "no credential configured",
      last_ok_at: null,
      last_checked_at: null,
      consecutive_failures: 0,
      metrics: {},
    },
    {
      provider: "telegram",
      status: "DISABLED",
      detail: "TELEGRAM_ENABLED is false",
      last_ok_at: null,
      last_checked_at: null,
      consecutive_failures: 0,
      metrics: {},
    },
  ],
};

export const healthResponse = {
  status: "DEGRADED",
  app: "StockBrain",
  version: "0.1.0",
  environment: "local",
  checked_at: "2026-09-06T12:00:00Z",
  subsystems: [
    { subsystem: "database", status: "HEALTHY", providers: ["postgres"] },
    { subsystem: "discovery", status: "DEGRADED", providers: ["brave", "exa"] },
  ],
};

export const executionSummary = {
  broker: "trading212",
  broker_environment: "demo",
  order_transmission_permitted: false,
  blockers: ["T212_EXECUTION_ENABLED is false"],
  execution_mode: "manual_approval",
  execution_policy: "MANUAL",
  live_execution_permitted: false,
  automated_trading_consent_confirmed: false,
  trading_halted: false,
  control_blockers: [],
  attempts_by_outcome: {},
  ambiguous_attempts: 0,
  reconciliation_pending: 0,
  order_endpoint: "POST /api/v0/equity/orders/market",
  order_endpoint_idempotent: false,
  notice: "Order transmission is disabled.",
};

export const discoveryStatus = {
  discovery_enabled: true,
  paused: false,
  subsystem_running: true,
  news_stream_active: false,
  classifier_active: true,
  classifier_model: "muse-spark-1.3-contributor",
  jobs_pending: 0,
  scheduled_tasks: [],
  stats: {
    sources_total: 12,
    events_total: 8,
    sources_last_24h: 3,
    events_last_24h: 2,
    events_by_status: {},
    sources_by_provider: {},
    latest_source_at: "2026-09-06T11:30:00Z",
  },
  budget: null,
  web_discovery: null,
  queue: {
    pending: 0,
    running: 0,
    failed: 0,
    dead: 0,
    oldest_pending_age_seconds: null,
    oldest_pending_job_type: null,
    stuck: 0,
    stuck_job_types: [],
    counts_by_type: {},
    counts_by_status: {},
  },
};

export const telegramStatus = {
  status: "DISABLED",
  bot_configured: false,
  bot_identified: false,
  transport: "long_polling",
  webhook_configured: false,
  polling: false,
  started_at: null,
  last_contact_at: null,
  last_error_category: null,
  consecutive_failures: 0,
  authorized_users: 0,
  authorized_chats: 0,
  notification_targets: 0,
  group_chats_allowed: false,
  blockers: ["TELEGRAM_ENABLED is false"],
};

export const fxStatus = {
  provider: "none",
  grade: null,
  configured: false,
  available: false,
  blockers: ["FX_PROVIDER is 'none'"],
  detail: null,
  max_age_seconds: 900,
  reference_max_age_seconds: 90000,
  allow_reference_grade: false,
  max_rate_drift_pct: "0.005",
  probe_pair: "GBPUSD",
  probe_rate: null,
  probe_age_seconds: null,
  probe_provider_timestamp: null,
};

export const webSecurity = {
  auth_enabled: true,
  auth_effective: true,
  blockers: [],
  trusted_network_acknowledged: false,
  session_ttl_seconds: 43200,
  cookie_secure: false,
  cookie_samesite: "strict",
  csrf_header: "X-StockBrain-CSRF",
  public_paths: ["/api/health/live"],
  cors_allow_origins: [],
};

export const portfolioResponse = {
  available: true,
  reason: null,
  account_id: "acct-1",
  currency: "GBP",
  broker: "trading212",
  broker_environment: "demo",
  total_value: "1000.0000",
  invested_value: "600.0000",
  result_value: "-12.3400",
  cash_available: "400.0000",
  cash_reserved: "0.0000",
  cash_in_pies: "0.0000",
  captured_at: "2026-09-06T11:59:00Z",
  position_count: 1,
  positions: [
    {
      broker_ticker: "AAPL_US_EQ",
      name: "Apple Inc",
      quantity: "2.0000",
      quantity_available: "2.0000",
      average_price: "180.0000",
      current_price: "190.0000",
      ppl: "20.0000",
      currency: "USD",
      last_synced_at: "2026-09-06T11:59:00Z",
    },
  ],
  stale: false,
  max_age_seconds: 300,
};

export const emptyProposals = {
  items: [],
  total: 0,
  limit: 50,
  offset: 0,
  counts_by_status: {},
};

export const executionPolicy = {
  execution_policy: "MANUAL",
  proposals_enabled: true,
  broker: "trading212",
  broker_environment: "demo",
  automatic_authorization_permitted: false,
  automation_blockers: ["EXECUTION_POLICY is 'MANUAL'"],
  broker_automation: {
    broker: "trading212",
    environment: "demo",
    automation_supported: false,
    permitted: false,
    blockers: [],
    detail: "",
  },
  risk_policy_version: "7af326e1",
  risk_config: {},
  proposal_ttl_minutes: 30,
  broker_order_routes: [],
  notice: "Every proposal requires an explicit human authorization.",
};

/** A proposal awaiting authorization, with only the fields the pages read. */
export function proposalFixture(overrides: Record<string, unknown> = {}) {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    status: "READY",
    status_reason: null,
    invalidation_reason: null,
    execution_policy: "MANUAL",
    authorization_source: null,
    approved_by: null,
    approved_at: null,
    rejected_by: null,
    rejected_at: null,
    company_name: "Apple Inc",
    broker: "TRADING212",
    broker_environment: "demo",
    broker_ticker: "AAPL_US_EQ",
    market_symbol: "AAPL",
    exchange: "NASDAQ",
    isin: "US0378331005",
    instrument_currency: "USD",
    account_currency: "GBP",
    account_id: "acct-1",
    side: "BUY",
    order_type: "MARKET",
    proposed_quantity: "2",
    max_quantity: "3",
    estimated_notional: "300.00",
    max_notional: "500.00",
    reference_price: "190.00",
    reference_currency: "USD",
    price_source: "ALPACA_IEX",
    quote_provider: "alpaca",
    quote_feed: "iex",
    quote_bid: "189.99",
    quote_ask: "190.01",
    quote_mid: "190.00",
    quote_spread: "0.02",
    quote_spread_bps: "1.05",
    quote_spread_status: "OK",
    quote_age_ms: 400,
    quote_timestamp: "2026-09-06T11:59:59Z",
    market_session: "REGULAR",
    market_session_source: "exchange calendar",
    research_action: "BUY",
    research_confidence: 0.82,
    research_run_id: "22222222-2222-2222-2222-222222222222",
    time_horizon: "weeks",
    thesis_summary: "A summary.",
    risk_outcome: "ALLOW",
    risk_policy_version: "7af326e1",
    risk_rules: [],
    blockers: [],
    warnings: [],
    reductions: [],
    sizing_reasons: ["capped by RISK_MAX_NOTIONAL_PER_TRADE"],
    created_at: "2026-09-06T11:55:00Z",
    expires_at: "2026-09-06T12:25:00Z",
    broker_order_transmitted: false,
    can_approve: true,
    can_reject: true,
    can_cancel: true,
    notice: "Authorizing does not transmit an order.",
    ...overrides,
  };
}
