"""Read-only thesis memory: calibration buckets and graded outcomes.

Alpha here ignores FX (an instrument's ratio in its own currency against a USD
benchmark) and uses one benchmark for every venue -- accepted limitations of a
hit-rate signal, spelled out in the design's §9.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import asdict
from decimal import Decimal

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import selectinload

from stockbrain.api.dependencies import DbSession, ServicesDep
from stockbrain.db.base import utcnow
from stockbrain.db.models.memory import ThesisOutcome, ThesisOutcomeGrade
from stockbrain.enums import OutcomeStatus, ThesisAction
from stockbrain.intelligence.memory import calibrate

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])


class BucketView(BaseModel):
    key: str
    samples: int
    correct: int
    hit_rate: Decimal
    mean_alpha: Decimal
    latest_graded_at: dt.datetime


class CalibrationView(BaseModel):
    as_of: dt.datetime
    pending_outcomes: int
    graded_outcomes: int
    buckets: list[BucketView]


class GradeView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    checkpoint: str
    trading_days: int
    instrument_return: Decimal
    benchmark_return: Decimal
    alpha: Decimal
    correct: bool
    graded_at: dt.datetime


class OutcomeView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    broker_ticker: str
    event_type: str | None
    action: ThesisAction
    horizon: str
    confidence: Decimal
    is_exit: bool
    exit_rule_id: str | None
    entry_at: dt.datetime
    status: OutcomeStatus
    closed_at: dt.datetime | None
    close_reason: str | None
    grades: list[GradeView]


class OutcomesView(BaseModel):
    outcomes: list[OutcomeView]


@router.get("/calibration", response_model=CalibrationView)
async def calibration(session: DbSession, services: ServicesDep) -> CalibrationView:
    if services is None or services.memory is None:
        raise HTTPException(status_code=503, detail="memory is not configured")
    now = utcnow()
    memory = services.memory
    pending = await session.scalar(
        sa.select(sa.func.count())
        .select_from(ThesisOutcome)
        .where(ThesisOutcome.status == OutcomeStatus.PENDING)
    )
    graded = await session.scalar(
        sa.select(sa.func.count(sa.distinct(ThesisOutcomeGrade.outcome_id)))
    )
    buckets: list[BucketView] = []
    keys = (
        await session.execute(
            sa.select(ThesisOutcome.event_type, ThesisOutcome.action)
            .where(ThesisOutcome.is_exit.is_(False), ThesisOutcome.event_type.is_not(None))
            .distinct()
        )
    ).all()
    for event_type, action in keys:
        bucket = await memory.calibration(session, event_type=event_type, action=action, as_of=now)
        if bucket is not None:
            buckets.append(BucketView(**asdict(bucket)))
    companies = (
        await session.execute(
            sa.select(ThesisOutcome.company_id)
            .where(ThesisOutcome.is_exit.is_(False), ThesisOutcome.action == ThesisAction.BUY)
            .distinct()
        )
    ).scalars()
    for company_id in companies:
        bucket = await memory.company_calibration(
            session, company_id=company_id, action=ThesisAction.BUY, as_of=now
        )
        if bucket is not None:
            buckets.append(BucketView(**asdict(bucket)))
    exit_rules = (
        await session.scalars(
            sa.select(ThesisOutcome.exit_rule_id)
            .where(ThesisOutcome.is_exit.is_(True), ThesisOutcome.exit_rule_id.is_not(None))
            .distinct()
        )
    ).all()
    for rule_id in exit_rules:
        grades = await memory.effective_grades(session, as_of=now, exits=True)
        rows = [
            grade
            for grade in grades
            if grade.outcome_id
            in set(
                await session.scalars(
                    sa.select(ThesisOutcome.id).where(ThesisOutcome.exit_rule_id == rule_id)
                )
            )
        ]
        bucket = calibrate(f"exits×{rule_id}", rows)  # noqa: RUF001
        if bucket is not None:
            buckets.append(BucketView(**asdict(bucket)))
    return CalibrationView(
        as_of=now,
        pending_outcomes=pending or 0,
        graded_outcomes=graded or 0,
        buckets=sorted(buckets, key=lambda row: row.key),
    )


@router.get("/outcomes", response_model=OutcomesView)
async def outcomes(
    session: DbSession, limit: int = Query(default=50, ge=1, le=500)
) -> OutcomesView:
    rows = (
        await session.scalars(
            sa.select(ThesisOutcome)
            .order_by(ThesisOutcome.entry_at.desc())
            .limit(limit)
            .options(selectinload(ThesisOutcome.grades))
        )
    ).all()
    return OutcomesView(
        outcomes=[
            OutcomeView(
                id=row.id,
                broker_ticker=row.broker_ticker,
                event_type=row.event_type,
                action=row.action,
                horizon=row.horizon.value,
                confidence=row.confidence,
                is_exit=row.is_exit,
                exit_rule_id=row.exit_rule_id,
                entry_at=row.entry_at,
                status=row.status,
                closed_at=row.closed_at,
                close_reason=row.close_reason,
                grades=[GradeView.model_validate(grade) for grade in row.grades],
            )
            for row in rows
        ]
    )
