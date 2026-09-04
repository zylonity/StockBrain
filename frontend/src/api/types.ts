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
