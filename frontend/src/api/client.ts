/**
 * Thin typed fetch wrapper.
 *
 * Same-origin by default so the session cookie works with SameSite=Strict.
 * State-changing calls will carry a CSRF token once authentication lands; the
 * helper is written to make that a single change here rather than at every call
 * site.
 */

import type {
  ExecutionStatusResponse,
  HealthResponse,
  ProvidersResponse,
  ReadinessResponse,
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

export const api = {
  health: () => request<HealthResponse>("/api/health"),
  readiness: () => request<ReadinessResponse>("/api/health/ready"),
  providers: () => request<ProvidersResponse>("/api/health/providers"),
  executionStatus: () =>
    request<ExecutionStatusResponse>("/api/v1/system/execution-status"),
};
