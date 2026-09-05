/**
 * Thin typed fetch wrapper.
 *
 * Same-origin by default so the session cookie works with SameSite=Strict.
 * State-changing calls will carry a CSRF token once authentication lands; the
 * helper is written to make that a single change here rather than at every call
 * site.
 */

import type {
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
  MarketDataHealth,
  PriceReaction,
  ProvidersResponse,
  ReadinessResponse,
  Resolution,
  ResolutionListResponse,
  ResolutionStatus,
} from "./types";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
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
  health: () => request<HealthResponse>("/api/health"),
  readiness: () => request<ReadinessResponse>("/api/health/ready"),
  providers: () => request<ProvidersResponse>("/api/health/providers"),
  executionStatus: () =>
    request<ExecutionStatusResponse>("/api/v1/system/execution-status"),
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
  priceReaction: (eventId: string) =>
    request<PriceReaction[]>(
      `/api/v1/events/${encodeURIComponent(eventId)}/price-reaction`,
    ),
};
