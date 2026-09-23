/**
 * Thin typed fetch wrapper.
 *
 * Same-origin by default so the session cookie works with SameSite=Strict.
 * Every state-changing call carries the CSRF token from the `sb_csrf` cookie in
 * the `X-StockBrain-CSRF` header; the server rejects a POST without it. The
 * token is read from the cookie on each call rather than cached, so a session
 * that was re-established in another tab keeps working.
 */

import type {
  BrokerOrderRecord,
  ControlStateResponse,
  ExecutionAttempt,
  ExecutionStatusSummary,
  ProposalExecution,
  ExecutionPolicyResponse,
  Proposal,
  ProposalListResponse,
  ProposalRiskDetail,
  ProposalStatus,
  ResearchRun,
  BrokerInstrumentRecord,
  CompanyAliasRecord,
  DiscoveryStatus,
  DiscoveryTopic,
  EventDetail,
  EventFilters,
  EventListResponse,
  ExecutionStatusResponse,
  HealthResponse,
  InstrumentSyncStatus,
  LivenessResponse,
  MarketDataHealth,
  PriceReaction,
  ProvidersResponse,
  ReadinessResponse,
  DiscoveryHold,
  LogFacets,
  LogFilters,
  LogQueryResponse,
  NotificationPreferences,
  PortfolioResponse,
  PositionReviewResponse,
  Resolution,
  ResolutionListResponse,
  ResolutionStatus,
  SessionView,
  SettingsResponse,
  TelegramStatusResponse,
  FxStatus,
  WebSecurityStatus,
} from "./types";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * POST a JSON body.
 *
 * The body deliberately never carries order parameters. The ticker, side,
 * quantity, price and account are read from the proposal row on the server,
 * under lock: a client that could name a quantity would be a client that could
 * size a trade.
 */
/** Read the readable half of the double-submit CSRF pair. */
export function csrfToken(): string {
  const match = /(?:^|;\s*)sb_csrf=([^;]*)/.exec(document.cookie);
  const value = match?.[1];
  return value ? decodeURIComponent(value) : "";
}

async function post<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  return mutate<T>("POST", path, body);
}

async function put<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  return mutate<T>("PUT", path, body);
}

async function mutate<T>(
  method: "POST" | "PUT",
  path: string,
  body: Record<string, unknown>,
): Promise<T> {
  return request<T>(path, {
    method,
    headers: {
      "Content-Type": "application/json",
      // Present on every state-changing call. A cross-site attacker can cause
      // the cookie to be sent but cannot read it to build this header.
      "X-StockBrain-CSRF": csrfToken(),
    },
    body: JSON.stringify(body),
  });
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      credentials: "same-origin",
      headers: { Accept: "application/json", ...(init?.headers ?? {}) },
      ...init,
    });
  } catch (cause) {
    throw new ApiError(0, `Network error contacting ${path}: ${String(cause)}`);
  }

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // Non-JSON error body; the status line is the best we have.
    }
    throw new ApiError(response.status, detail);
  }

  return (await response.json()) as T;
}

/**
 * Build the log query string.
 *
 * Every parameter narrows. There is deliberately no path, file or container
 * parameter to build: the backend reads a bounded in-memory ring and has no
 * filesystem surface for this page to reach.
 */
export function logQuery(filters: LogFilters): string {
  const params = new URLSearchParams();
  if (filters.minLevel) params.set("min_level", filters.minLevel);
  if (filters.services?.length) params.set("services", filters.services.join(","));
  if (filters.categories?.length) params.set("categories", filters.categories.join(","));
  if (filters.sinceMinutes) params.set("since_minutes", String(filters.sinceMinutes));
  if (filters.search?.trim()) params.set("search", filters.search.trim());
  params.set("limit", String(filters.limit ?? 100));
  params.set("offset", String(filters.offset ?? 0));
  return params.toString();
}

function eventQuery(filters: EventFilters): string {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.provider) params.set("provider", filters.provider);
  if (filters.search?.trim()) params.set("search", filters.search.trim());
  if (filters.sinceHours) params.set("since_hours", String(filters.sinceHours));
  params.set("limit", String(filters.limit ?? 50));
  params.set("offset", String(filters.offset ?? 0));
  return params.toString();
}

export const api = {
  session: () => request<SessionView>("/api/v1/auth/session"),
  login: (username: string, password: string) =>
    post<SessionView>("/api/v1/auth/login", { username, password }),
  logout: () => post<SessionView>("/api/v1/auth/logout"),
  research: (eventId?: string) => request<ResearchRun[]>(`/api/v1/research${eventId ? `?event_id=${encodeURIComponent(eventId)}` : ""}`),
  researchRun: (id: string) => request<ResearchRun>(`/api/v1/research/${encodeURIComponent(id)}`),
  // The cheapest read in the API: no database, no provider. Used for the build
  // identity in the top bar, where a full health poll would be wasteful.
  liveness: () => request<LivenessResponse>("/api/health/live"),
  health: () => request<HealthResponse>("/api/health"),
  readiness: () => request<ReadinessResponse>("/api/health/ready"),
  providers: () => request<ProvidersResponse>("/api/health/providers"),
  executionStatus: () =>
    request<ExecutionStatusResponse>("/api/v1/system/execution-status"),
  controlState: () => request<ControlStateResponse>("/api/v1/system/control"),
  telegramStatus: () =>
    request<TelegramStatusResponse>("/api/v1/system/telegram"),
  fxStatus: () => request<FxStatus>("/api/v1/system/fx"),
  webSecurity: () => request<WebSecurityStatus>("/api/v1/system/web-security"),
  // Control changes carry a free-text reason and nothing else. Like the
  // proposal routes, they cannot name a ticker, a side, a quantity or a price.
  pauseTrading: (reason?: string) =>
    post<ControlStateResponse>("/api/v1/system/pause", reason ? { reason } : {}),
  resumeTrading: (reason?: string) =>
    post<ControlStateResponse>("/api/v1/system/resume", reason ? { reason } : {}),
  setKillSwitch: (engaged: boolean, reason?: string) =>
    post<ControlStateResponse>("/api/v1/system/kill-switch", {
      engaged,
      ...(reason ? { reason } : {}),
    }),
  events: (filters: EventFilters = {}) =>
    request<EventListResponse>(`/api/v1/events?${eventQuery(filters)}`),
  event: (id: string) =>
    request<EventDetail>(`/api/v1/events/${encodeURIComponent(id)}`),
  discoveryStatus: () =>
    request<DiscoveryStatus>("/api/v1/discovery/status"),
  discoveryTopics: () =>
    request<DiscoveryTopic[]>("/api/v1/discovery/topics"),

  resolutions: (status?: ResolutionStatus, limit = 100) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (status) params.set("resolution_status", status);
    return request<ResolutionListResponse>(
      `/api/v1/instruments/resolutions?${params.toString()}`,
    );
  },
  resolution: (impactId: string) =>
    request<Resolution>(
      `/api/v1/instruments/resolutions/${encodeURIComponent(impactId)}`,
    ),
  instruments: (search: string, limit = 25) =>
    request<BrokerInstrumentRecord[]>(
      `/api/v1/instruments?search=${encodeURIComponent(search)}&limit=${limit}`,
    ),
  instrumentSyncStatus: () =>
    request<InstrumentSyncStatus>("/api/v1/instruments/sync-status"),
  aliases: () => request<CompanyAliasRecord[]>("/api/v1/aliases"),
  marketDataHealth: () =>
    request<MarketDataHealth>("/api/v1/market-data/health"),
  proposals: (status?: ProposalStatus, limit = 50) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (status) params.set("proposal_status", status);
    return request<ProposalListResponse>(`/api/v1/proposals?${params.toString()}`);
  },
  proposal: (id: string) =>
    request<Proposal>(`/api/v1/proposals/${encodeURIComponent(id)}`),
  proposalRisk: (id: string) =>
    request<ProposalRiskDetail>(
      `/api/v1/proposals/${encodeURIComponent(id)}/risk`,
    ),
  proposalPolicy: () =>
    request<ExecutionPolicyResponse>("/api/v1/proposals/policy"),
  executionSummary: () =>
    request<ExecutionStatusSummary>("/api/v1/execution/status"),
  proposalExecution: (id: string) =>
    request<ProposalExecution>(
      `/api/v1/proposals/${encodeURIComponent(id)}/execution`,
    ),
  executionAttempts: (ambiguousOnly = false, limit = 50) =>
    request<ExecutionAttempt[]>(
      `/api/v1/execution/attempts?ambiguous_only=${ambiguousOnly}&limit=${limit}`,
    ),
  brokerOrders: (limit = 50) =>
    request<BrokerOrderRecord[]>(`/api/v1/execution/orders?limit=${limit}`),
  /**
   * Ask the broker again what happened to an attempt.
   *
   * The only mutating execution call, and it is a *read* of the broker: it
   * fetches pending orders and order history and compares them against the
   * attempt. There is deliberately no resend counterpart — Trading 212's order
   * POST is non-idempotent, so a retry button would create a second position.
   */
  reconcileAttempt: (attemptId: string) =>
    post<ExecutionAttempt>(
      `/api/v1/execution/attempts/${encodeURIComponent(attemptId)}/reconcile`,
    ),
  approveProposal: (id: string) =>
    post<Proposal>(`/api/v1/proposals/${encodeURIComponent(id)}/approve`),
  rejectProposal: (id: string, reason?: string) =>
    post<Proposal>(`/api/v1/proposals/${encodeURIComponent(id)}/reject`,
      reason ? { reason } : {}),
  cancelProposal: (id: string, reason?: string) =>
    post<Proposal>(`/api/v1/proposals/${encodeURIComponent(id)}/cancel`,
      reason ? { reason } : {}),

  logs: (filters: LogFilters = {}) =>
    request<LogQueryResponse>(`/api/v1/system/logs?${logQuery(filters)}`),
  logFacets: () => request<LogFacets>("/api/v1/system/logs/facets"),
  settings: () => request<SettingsResponse>("/api/v1/system/settings"),
  notificationPreferences: () =>
    request<NotificationPreferences>("/api/v1/system/telegram/preferences"),
  /**
   * Save a *partial* set of category switches.
   *
   * Partial on purpose: the server merges, so two browser tabs cannot silently
   * revert each other's untouched categories. A locked category is forced back
   * on by the server and the response says so.
   */
  updateNotificationPreferences: (categories: Record<string, boolean>) =>
    put<NotificationPreferences>("/api/v1/system/telegram/preferences", { categories }),
  portfolio: () => request<PortfolioResponse>("/api/v1/portfolio"),
  reviewPositions: (ticker?: string) =>
    post<PositionReviewResponse>("/api/v1/portfolio/review", ticker ? { ticker } : {}),
  // Holds *scheduled discovery*: no new paid searches, filings sweeps or
  // backfills are enqueued. Deliberately not the trading pause.
  pauseDiscovery: (reason?: string) =>
    post<DiscoveryHold>("/api/v1/discovery/pause", reason ? { reason } : {}),
  resumeDiscovery: (reason?: string) =>
    post<DiscoveryHold>("/api/v1/discovery/resume", reason ? { reason } : {}),

  priceReaction: (eventId: string) =>
    request<PriceReaction[]>(
      `/api/v1/events/${encodeURIComponent(eventId)}/price-reaction`,
    ),
};
