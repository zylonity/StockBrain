"""Read-only research inspection. Raw graph messages never cross this boundary."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, ValidationError

from stockbrain.api.dependencies import DbSession, HealthRegistry, ServicesDep, SettingsDep
from stockbrain.db.models.research import LlmCall, ResearchRun, Thesis
from stockbrain.enums import ResearchStatus
from stockbrain.intelligence.research import (
    DEEP_ROLES,
    PROMPT_VERSION,
    ROLES,
    UPSTREAM_COMMIT,
    ResearchDecision,
    ResearchPacket,
    ResearchValidationError,
    public_text,
)
from stockbrain.observability.health import ProviderName

router = APIRouter(prefix="/api/v1/research", tags=["research"])


class CallView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    purpose: str
    provider: str
    model: str
    prompt_version: str | None
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    cache_miss_input_tokens: int | None
    estimated_cost_usd: Decimal | None
    latency_ms: int | None
    provider_request_id: str | None
    finish_reason: str | None
    error_class: str | None
    succeeded: bool
    thinking_enabled: bool


class RunView(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID | None
    company_id: uuid.UUID
    broker_instrument_id: uuid.UUID | None
    status: ResearchStatus
    as_of: dt.datetime
    started_at: dt.datetime | None
    completed_at: dt.datetime | None
    tradingagents_version: str | None
    quick_model: str | None
    deep_model: str | None
    prompt_version: str | None
    config_version: str | None
    estimated_cost_usd: Decimal | None
    error_class: str | None
    error: str | None
    packet: ResearchPacket | None
    decision: ResearchDecision | None
    reports: dict[str, str]
    calls: list[CallView] = []
    thesis_id: uuid.UUID | None = None


def run_view(row: ResearchRun) -> RunView:
    packet = None
    decision = None
    reports: dict[str, str] = {}
    try:
        if row.research_packet:
            packet = ResearchPacket.model_validate(row.research_packet)
        if row.structured_decision:
            decision = ResearchDecision.model_validate(row.structured_decision)
        for role in ROLES:
            if row.raw_reports and role in row.raw_reports:
                reports[role] = public_text(row.raw_reports[role])
    except (ValidationError, ResearchValidationError):
        # Legacy/unvalidated blobs are not a public API contract.
        packet, decision, reports = None, None, {}
    return RunView(
        **{
            name: getattr(row, name)
            for name in (
                "id",
                "event_id",
                "company_id",
                "broker_instrument_id",
                "status",
                "as_of",
                "started_at",
                "completed_at",
                "tradingagents_version",
                "quick_model",
                "deep_model",
                "prompt_version",
                "config_version",
                "estimated_cost_usd",
                "error_class",
            )
        },
        error=f"Research stopped: {row.error_class}" if row.error_class else None,
        packet=packet,
        decision=decision,
        reports=reports,
    )


@router.get("/health")
async def research_health(
    session: DbSession, services: ServicesDep, settings: SettingsDep, registry: HealthRegistry
) -> dict[str, Any]:
    models = {}
    for model in (settings.deepseek_flash_model, settings.deepseek_pro_model):
        call = await session.scalar(
            sa.select(LlmCall)
            .where(LlmCall.purpose.like("RESEARCH:%"), LlmCall.model == model)
            .order_by(LlmCall.created_at.desc())
            .limit(1)
        )
        models[model] = {
            "status": "DISABLED"
            if not settings.deepseek_api_key.get_secret_value()
            else "UNKNOWN"
            if call is None
            else "HEALTHY"
            if call.succeeded
            else "DEGRADED",
            "last_error_class": call.error_class if call else None,
        }
    return {
        "available": services is not None and services.research is not None,
        "upstream_commit": UPSTREAM_COMMIT,
        "prompt_version": PROMPT_VERSION,
        "roles": {
            role: settings.deepseek_pro_model
            if role in DEEP_ROLES
            else settings.deepseek_flash_model
            for role in ROLES
        },
        "models": models,
        "tools": ["read_research_context"],
        "providers": ["deepseek", "fred", "alpaca"],
        "provider_health": {
            name.value: {
                "status": registry.get(name).status,
                "last_checked_at": registry.get(name).last_checked_at,
                "last_ok_at": registry.get(name).last_ok_at,
            }
            for name in (
                ProviderName.TRADINGAGENTS,
                ProviderName.FRED,
                ProviderName.ALPACA_MARKET_DATA,
            )
        },
        "fred_configured": bool(settings.fred_api_key.get_secret_value()),
        "advisory_only": True,
    }


@router.get("", response_model=list[RunView])
async def list_research(
    session: DbSession,
    event_id: uuid.UUID | None = None,
    status: ResearchStatus | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[RunView]:
    query = (
        sa.select(ResearchRun).order_by(ResearchRun.created_at.desc()).limit(limit).offset(offset)
    )
    if event_id is not None:
        query = query.where(ResearchRun.event_id == event_id)
    if status is not None:
        query = query.where(ResearchRun.status == status)
    return [run_view(row) for row in await session.scalars(query)]


@router.get("/{run_id}", response_model=RunView)
async def research_detail(run_id: uuid.UUID, session: DbSession) -> RunView:
    row = await session.get(ResearchRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Research run not found")
    view = run_view(row)
    view.calls = [
        CallView.model_validate(call)
        for call in await session.scalars(
            sa.select(LlmCall).where(LlmCall.research_run_id == run_id).order_by(LlmCall.created_at)
        )
    ]
    view.thesis_id = await session.scalar(
        sa.select(Thesis.id).where(Thesis.research_run_id == run_id)
    )
    return view
