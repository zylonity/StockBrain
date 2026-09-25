"""What StockBrain currently believes about each holding.

Conviction sizing needs one number per holding: the confidence of the most
recent thesis in the lineage the position was opened on. A position StockBrain
did not open has no thesis; it still occupies capital, so it is counted at the
lowest conviction weight rather than ignored.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.models.portfolio import Position
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.models.research import Thesis
from stockbrain.enums import Broker, OrderSide, ProposalStatus, ThesisAction
from stockbrain.risk.config import RiskConfig
from stockbrain.risk.rules import conviction_weight

__all__ = ["HoldingBelief", "holding_weights", "load_holding_beliefs"]

#: Latest-thesis actions that argue for *adding*. A HOLD keeps the position
#: but earns only the minimum weight: it is not a reason to buy more.
_HOLDING_ACTIONS = frozenset({ThesisAction.BUY})


@dataclass(frozen=True, slots=True)
class HoldingBelief:
    broker_ticker: str
    origin_thesis_id: uuid.UUID | None
    thesis_id: uuid.UUID | None
    action: ThesisAction | None
    confidence: Decimal | None
    thesis_at: dt.datetime | None

    @property
    def managed(self) -> bool:
        """Whether StockBrain opened this position and so may resize it."""
        return self.origin_thesis_id is not None

    def weight(self, config: RiskConfig) -> Decimal:
        if self.confidence is None or self.action not in _HOLDING_ACTIONS:
            return config.conviction_min_weight
        return conviction_weight(config, self.confidence)


async def load_holding_beliefs(
    session: AsyncSession, *, broker: Broker, environment: str
) -> list[HoldingBelief]:
    positions = list(
        await session.scalars(
            sa.select(Position)
            .where(Position.broker == broker, Position.quantity > 0)
            .order_by(Position.broker_ticker)
        )
    )
    beliefs: list[HoldingBelief] = []
    for position in positions:
        origin = await session.scalar(
            sa.select(TradeProposal.thesis_id)
            .where(
                TradeProposal.broker == broker,
                TradeProposal.broker_ticker == position.broker_ticker,
                TradeProposal.broker_environment == environment,
                TradeProposal.side == OrderSide.BUY,
                TradeProposal.status == ProposalStatus.EXECUTED,
                TradeProposal.thesis_id.is_not(None),
                TradeProposal.executed_at.is_not(None),
            )
            .order_by(TradeProposal.executed_at.desc())
            .limit(1)
        )
        latest = (
            await session.scalar(
                sa.select(Thesis)
                .where(sa.or_(Thesis.id == origin, Thesis.original_thesis_id == origin))
                .order_by(Thesis.created_at.desc())
                .limit(1)
            )
            if origin is not None
            else None
        )
        beliefs.append(
            HoldingBelief(
                broker_ticker=position.broker_ticker,
                origin_thesis_id=origin,
                thesis_id=latest.id if latest else None,
                action=latest.action if latest else None,
                confidence=Decimal(str(latest.confidence)) if latest else None,
                thesis_at=latest.created_at if latest else None,
            )
        )
    return beliefs


async def holding_weights(
    session: AsyncSession,
    *,
    broker: Broker,
    environment: str,
    config: RiskConfig,
    exclude_ticker: str | None = None,
) -> tuple[Decimal, ...]:
    beliefs = await load_holding_beliefs(session, broker=broker, environment=environment)
    return tuple(
        belief.weight(config) for belief in beliefs if belief.broker_ticker != exclude_ticker
    )
