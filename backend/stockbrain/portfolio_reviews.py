"""Operator-requested re-research of positions StockBrain opened."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import sqlalchemy as sa

from stockbrain.db.models.portfolio import Position
from stockbrain.db.models.research import ResearchRun
from stockbrain.enums import ResearchStatus
from stockbrain.intelligence.research_service import ResearchService
from stockbrain.proposals.service import ProposalService


@dataclass(frozen=True, slots=True)
class PositionReviewResult:
    requested: dict[str, uuid.UUID] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)


class PositionReviewService:
    """Queue fresh research against each holding's opening thesis.

    A successor SELL/REDUCE thesis enters the ordinary proposal pipeline, so
    authorization policy and deterministic risk checks remain authoritative.
    """

    def __init__(self, proposals: ProposalService, research: ResearchService) -> None:
        self.database = proposals.database
        self.proposals = proposals
        self.research = research

    async def request(self, ticker: str | None = None) -> PositionReviewResult:
        wanted = ticker.strip().upper() if ticker else None
        requested: dict[str, uuid.UUID] = {}
        skipped: dict[str, str] = {}
        async with self.database.transaction() as session:
            query = sa.select(Position).where(
                Position.broker == self.proposals.broker, Position.quantity > 0
            )
            if wanted:
                query = query.where(sa.func.upper(Position.broker_ticker) == wanted)
            positions = list(await session.scalars(query.order_by(Position.broker_ticker)))
            if wanted and not positions:
                return PositionReviewResult(skipped={wanted: "no active holding found"})

            for position in positions:
                origin = await self.proposals.origin_proposal(session, position.broker_ticker)
                if origin is None or origin.thesis_id is None or origin.research_run_id is None:
                    skipped[position.broker_ticker] = "no StockBrain opening thesis"
                    continue
                origin_run = await session.get(ResearchRun, origin.research_run_id)
                if origin_run is None or origin_run.impact_id is None:
                    skipped[position.broker_ticker] = "opening research cannot be replayed"
                    continue
                active = await session.scalar(
                    sa.select(ResearchRun.id)
                    .where(
                        ResearchRun.trigger == "POSITION_REASSESSMENT",
                        ResearchRun.status.in_((ResearchStatus.PENDING, ResearchStatus.RUNNING)),
                        ResearchRun.broker_instrument_id == origin_run.broker_instrument_id,
                    )
                    .limit(1)
                )
                if active is not None:
                    skipped[position.broker_ticker] = "review already pending"
                    continue
                run_id = await self.research.request(
                    session,
                    origin_run.impact_id,
                    rerun_id=uuid.uuid4(),
                    previous_thesis_id=origin.thesis_id,
                )
                requested[position.broker_ticker] = run_id
        return PositionReviewResult(requested=requested, skipped=skipped)
