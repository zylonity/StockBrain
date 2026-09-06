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

import sqlalchemy as sa
from fastapi import APIRouter, Query

from stockbrain.api.dependencies import DbSession, SettingsDep
from stockbrain.api.schemas import PortfolioPositionResponse, PortfolioResponse
from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument
from stockbrain.db.models.portfolio import PortfolioSnapshot, Position

router = APIRouter(prefix="/api/v1", tags=["portfolio"])


@router.get("/portfolio", response_model=PortfolioResponse, summary="Broker account snapshot")
async def portfolio(
    session: DbSession,
    settings: SettingsDep,
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
            )
            for position, name in rows
        ],
    )
