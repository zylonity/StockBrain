"""Health and readiness endpoints.

Three distinct questions, three endpoints:

* ``/api/health/live``  -- is the process running?  Used by the container
  healthcheck; must not touch the database, or a database blip would restart an
  otherwise-working application.
* ``/api/health/ready`` -- can it serve traffic?  Requires PostgreSQL *and* a
  schema at the expected migration revision.
* ``/api/health``       -- what is actually working?  Per-subsystem detail.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from stockbrain.api.dependencies import DatabaseDep, HealthRegistry, SettingsDep
from stockbrain.api.schemas import (
    HealthResponse,
    LivenessResponse,
    ProviderHealthResponse,
    ProvidersResponse,
    ReadinessResponse,
    SubsystemHealth,
)
from stockbrain.db.base import utcnow
from stockbrain.enums import ProviderStatus
from stockbrain.observability.health import SUBSYSTEM_PROVIDERS, ProviderName
from stockbrain.startup import refresh_database_health

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("/live", response_model=LivenessResponse, summary="Process liveness")
async def liveness(settings: SettingsDep) -> LivenessResponse:
    return LivenessResponse(app=settings.app_name, version=settings.app_version)


@router.get("/ready", response_model=ReadinessResponse, summary="Readiness to serve traffic")
async def readiness(
    registry: HealthRegistry,
    database: DatabaseDep,
    response: Response,
) -> ReadinessResponse:
    # Probe rather than trust a cached startup value: a readiness endpoint that
    # reports a status from ten minutes ago is worse than no endpoint at all.
    await refresh_database_health(database, registry)
    database_status = registry.get_database_status()
    schema_current, schema_detail = registry.schema_state
    if database_status is not ProviderStatus.HEALTHY:
        # The recorded schema revision cannot be confirmed while the database is
        # unreachable, so it must not be reported as current.
        schema_current = False
        schema_detail = registry.get(ProviderName.POSTGRES).detail or "database unavailable"
    ready = database_status is ProviderStatus.HEALTHY and schema_current
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        ready=ready,
        database=database_status,
        schema_current=schema_current,
        detail=schema_detail,
    )


@router.get("", response_model=HealthResponse, summary="Aggregate health")
async def health(
    registry: HealthRegistry, database: DatabaseDep, settings: SettingsDep
) -> HealthResponse:
    await refresh_database_health(database, registry)
    return HealthResponse(
        status=registry.overall_status(),
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env.value,
        checked_at=utcnow(),
        subsystems=[
            SubsystemHealth(
                subsystem=subsystem.value,
                status=subsystem_status,
                providers=[p.value for p in SUBSYSTEM_PROVIDERS[subsystem]],
            )
            for subsystem, subsystem_status in registry.subsystems().items()
        ],
    )


@router.get("/providers", response_model=ProvidersResponse, summary="Per-provider health")
async def providers(registry: HealthRegistry, database: DatabaseDep) -> ProvidersResponse:
    await refresh_database_health(database, registry)
    return ProvidersResponse(
        checked_at=utcnow(),
        providers=[
            ProviderHealthResponse(
                provider=state.provider.value,
                status=state.status,
                detail=state.detail,
                last_ok_at=state.last_ok_at,
                last_checked_at=state.last_checked_at,
                consecutive_failures=state.consecutive_failures,
                metrics=dict(state.metrics),
            )
            for state in registry.snapshot().values()
        ],
    )
