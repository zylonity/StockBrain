"""Pydantic response models for the REST API.

The frontend consumes these and nothing else: no raw provider JSON and no
secret ever crosses this boundary.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field

from stockbrain.enums import ProviderStatus

__all__ = [
    "ExecutionStatusResponse",
    "HealthResponse",
    "LivenessResponse",
    "ProviderHealthResponse",
    "ProvidersResponse",
    "ReadinessResponse",
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
