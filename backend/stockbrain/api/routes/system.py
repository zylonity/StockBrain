"""System posture endpoints.

``/execution-status`` backs the GUI's permanent execution banner.  It never
returns a credential -- only whether one is configured -- and it reports the
exact reasons live execution is unavailable so the state is never a mystery.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from stockbrain.api.dependencies import SettingsDep
from stockbrain.api.schemas import ExecutionStatusResponse
from stockbrain.observability.metrics import METRICS

router = APIRouter(tags=["system"])

_DEMO_NOTICE = (
    "Trading 212 live execution is disabled. StockBrain is operating against the demo "
    "environment. Trading 212's API Terms prohibit Algorithmic Trading and require prior "
    "written consent before a customised interface is used in the live environment, so live "
    "submission stays hard-disabled until every gate in the configuration is deliberately set."
)

_LIVE_NOTICE = (
    "LIVE execution is enabled. Every order still requires two explicit human confirmations "
    "and a fresh risk, broker and quote revalidation immediately before submission."
)


@router.get(
    "/api/v1/system/execution-status",
    response_model=ExecutionStatusResponse,
    summary="Broker execution posture",
)
async def execution_status(settings: SettingsDep) -> ExecutionStatusResponse:
    permitted = settings.live_execution_permitted
    return ExecutionStatusResponse(
        broker="trading212",
        broker_environment=settings.t212_env.value,
        execution_mode=settings.execution_mode.value,
        live_execution_permitted=permitted,
        broker_credentials_configured=settings.broker_credentials_present,
        blockers=settings.execution_blockers,
        notice=_LIVE_NOTICE if permitted else _DEMO_NOTICE,
    )


@router.get("/metrics", include_in_schema=False, summary="Prometheus metrics")
async def metrics() -> Response:
    return Response(content=METRICS.render(), media_type="text/plain; version=0.0.4")
