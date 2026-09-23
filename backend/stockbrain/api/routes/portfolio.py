"""The broker account as StockBrain last saw it.

Reads the **stored mirror**, never the broker.  Trading 212's account summary
endpoint allows one request every five seconds and positions one per second; a
page an operator leaves open would spend that allowance on nothing, and the risk
engine -- which is the only thing that must be certain about cash -- refreshes
on its own schedule and independently refuses a snapshot older than
``RISK_MAX_ACCOUNT_STATE_AGE_SECONDS``.

So the honest thing to show is the snapshot with its age attached, and to say
plainly when it is older than the number a trade would have been sized on.  A
portfolio page that silently displayed a four-hour-old cash balance next to a
live proposal would be inviting exactly the wrong arithmetic.

Read-only in the strictest sense: this module imports nothing from
``stockbrain.execution``, ``stockbrain.risk`` or any broker client, and there is
no route here that changes anything.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from stockbrain.api.dependencies import DbSession, ServicesDep, SettingsDep
from stockbrain.api.schemas import (
    PortfolioPositionResponse,
    PortfolioResponse,
    PositionExitResponse,
)
from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument
from stockbrain.db.models.portfolio import PortfolioSnapshot, Position

if TYPE_CHECKING:
    from stockbrain.proposals.exits import PositionExitStatus

router = APIRouter(prefix="/api/v1", tags=["portfolio"])


class PositionReviewRequest(BaseModel):
    ticker: str | None = Field(default=None, min_length=1, max_length=100)


class PositionReviewResponse(BaseModel):
    requested: dict[str, uuid.UUID]
    skipped: dict[str, str]


@router.post("/portfolio/review", response_model=PositionReviewResponse)
async def review_positions(
    body: PositionReviewRequest, services: ServicesDep
) -> PositionReviewResponse:
    review = services.position_reviews if services is not None else None
    if review is None:
        raise HTTPException(status_code=503, detail="Position reviews are unavailable")
    result = await review.request(body.ticker)
    return PositionReviewResponse(requested=result.requested, skipped=result.skipped)


def _exit_response(status: PositionExitStatus) -> PositionExitResponse:
    """The wire shape of one position's exit floors.

    A ``PositionExitStatus`` the risk layer could not price carries no floors;
    every price stays ``None`` rather than being invented.
    """
    floors = status.floors
    return PositionExitResponse(
        managed=status.managed,
        reason=status.reason,
        hard_stop=floors.hard_stop if floors is not None else None,
        volatility_floor=floors.volatility_floor if floors is not None else None,
        trailing_floor=floors.trailing_floor if floors is not None else None,
        roi_target_price=floors.roi_target_price if floors is not None else None,
        horizon_ends_at=floors.horizon_ends_at if floors is not None else None,
        nearest_floor=floors.nearest_floor if floors is not None else None,
        nearest_rule=floors.nearest_rule if floors is not None else None,
        peak_price=status.peak_price,
        atr=status.atr,
        horizon=status.horizon,
    )


@router.get("/portfolio", response_model=PortfolioResponse, summary="Broker account snapshot")
async def portfolio(
    session: DbSession,
    settings: SettingsDep,
    services: ServicesDep,
    limit: int = Query(default=100, ge=1, le=500),
) -> PortfolioResponse:
    snapshot = (
        (
            await session.execute(
                sa.select(PortfolioSnapshot).order_by(PortfolioSnapshot.captured_at.desc()).limit(1)
            )
        )
        .scalars()
        .first()
    )
    if snapshot is None:
        return PortfolioResponse(
            available=False,
            reason=(
                "No broker account snapshot has been captured yet. Trading 212 "
                "credentials are required before cash and positions can be mirrored."
            ),
            max_age_seconds=settings.risk_max_account_state_age_seconds,
        )

    account_id = snapshot.account_id or "default"
    rows = (
        await session.execute(
            sa.select(Position, BrokerInstrument.name)
            .outerjoin(
                BrokerInstrument,
                sa.and_(
                    BrokerInstrument.broker_ticker == Position.broker_ticker,
                    BrokerInstrument.broker == Position.broker,
                ),
            )
            .where(Position.account_id == account_id)
            .order_by(Position.broker_ticker)
            .limit(limit)
        )
    ).all()
    total = int(
        (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(Position)
                .where(Position.account_id == account_id)
            )
        ).scalar_one()
    )

    # Read-only and best-effort: the floors are a convenience on a page that
    # must still answer when the risk subsystem did not start.  ``status`` reads
    # the stored mirror and the risk rules only; it never prices or transmits.
    exits = services.exits if services is not None else None
    statuses = await exits.status(session) if exits is not None else {}

    age_seconds = (utcnow() - snapshot.captured_at).total_seconds()
    return PortfolioResponse(
        available=True,
        account_id=snapshot.account_id,
        currency=snapshot.currency,
        broker_environment=snapshot.broker_environment,
        total_value=snapshot.total_value,
        invested_value=snapshot.invested_value,
        result_value=snapshot.result_value,
        cash_available=snapshot.cash_available,
        cash_reserved=snapshot.cash_reserved,
        cash_in_pies=snapshot.cash_in_pies,
        captured_at=snapshot.captured_at,
        position_count=total,
        stale=age_seconds > settings.risk_max_account_state_age_seconds,
        max_age_seconds=settings.risk_max_account_state_age_seconds,
        positions=[
            PortfolioPositionResponse(
                broker_ticker=position.broker_ticker,
                name=name,
                quantity=position.quantity,
                quantity_available=position.quantity_available,
                average_price=position.average_price,
                current_price=position.current_price,
                ppl=position.ppl,
                currency=position.currency,
                last_synced_at=position.last_synced_at,
                exit=(
                    _exit_response(statuses[position.broker_ticker])
                    if position.broker_ticker in statuses
                    else None
                ),
            )
            for position, name in rows
        ],
    )
