"""The scheduled loop from held positions to exit proposals.

It owns no thresholds and makes no decisions: it assembles an
``ExitObservation`` per holding, asks ``risk.exits`` what to do, and hands any
signal to ``ProposalService.generate_exit``, which re-prices and re-validates
everything on the ordinary path.

Two cheap filters run before any network request, because the expensive part of
a sweep is the quote: a listing that already has a live proposal cannot take
another one, and a position StockBrain never opened has no thesis to exit.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.config import Settings
from stockbrain.db.base import utcnow
from stockbrain.db.models.portfolio import Position, PositionPeak
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.models.research import Thesis
from stockbrain.db.session import Database
from stockbrain.logging import get_logger
from stockbrain.proposals.lifecycle import thesis_is_superseded
from stockbrain.proposals.service import ProposalService
from stockbrain.proposals.state_machine import ACTIVE_STATUSES
from stockbrain.risk.config import RiskConfig
from stockbrain.risk.exits import ExitObservation, evaluate_exit

__all__ = ["ExitSweepService"]

log = get_logger(__name__)
ZERO = Decimal(0)


class ExitSweepService:
    """Evaluates every open position once per tick."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        proposals: ProposalService,
        config: RiskConfig,
    ) -> None:
        self._database = database
        self._settings = settings
        self._proposals = proposals
        self._config = config

    async def sweep(self, *, now: dt.datetime | None = None, limit: int = 25) -> dict[str, int]:
        """Evaluate every open position and propose at most ``limit`` exits.

        Returns a tally rather than raising: one position whose instrument has
        gone missing must not stop the other holdings being looked at.  Every
        open position is evaluated on every tick -- the ``limit`` is a per-tick
        budget on how many proposals may be generated, not a bound on which
        positions are examined, so a stable ordering cannot starve the
        positions beyond the first ``limit``; a signalled position the budget
        did not reach is counted as ``deferred``.
        """
        moment = now or utcnow()
        # Every key is present from the start, so a caller reading a zero count
        # gets 0 rather than a KeyError; the tally is also what the log line
        # reports, and a missing key there would hide that nothing was skipped.
        counts: Counter[str] = Counter(
            {
                "considered": 0,
                "skipped_active_proposal": 0,
                "skipped_no_origin": 0,
                "skipped_unpriced": 0,
                "signalled": 0,
                "proposed": 0,
                "blocked": 0,
                "failed": 0,
                "deferred": 0,
            }
        )

        async with self._database.session() as session:
            rows = (
                await session.execute(
                    sa.select(Position, PositionPeak)
                    .outerjoin(
                        PositionPeak,
                        sa.and_(
                            PositionPeak.broker == Position.broker,
                            PositionPeak.account_id == Position.account_id,
                            PositionPeak.broker_ticker == Position.broker_ticker,
                        ),
                    )
                    .where(Position.broker == self._proposals.broker, Position.quantity > ZERO)
                    .order_by(Position.broker_ticker)
                )
            ).all()

            observations: list[ExitObservation] = []
            for position, peak in rows:
                counts["considered"] += 1

                if await self._has_live_proposal(session, position.broker_ticker):
                    counts["skipped_active_proposal"] += 1
                    continue

                origin = await self._proposals.origin_proposal(session, position.broker_ticker)
                if origin is None or origin.executed_at is None or origin.thesis_id is None:
                    counts["skipped_no_origin"] += 1
                    continue
                thesis = await session.get(Thesis, origin.thesis_id)
                if thesis is None:
                    counts["skipped_no_origin"] += 1
                    continue

                if position.average_price is None or position.current_price is None:
                    counts["skipped_unpriced"] += 1
                    continue

                observations.append(
                    ExitObservation(
                        broker_ticker=position.broker_ticker,
                        quantity=position.quantity,
                        quantity_available=position.quantity_available or ZERO,
                        average_price=position.average_price,
                        current_price=position.current_price,
                        peak_price=peak.peak_price if peak is not None else None,
                        peak_observations=peak.observations if peak is not None else 0,
                        opened_at=origin.executed_at,
                        horizon=thesis.time_horizon,
                        thesis_superseded=await self._superseded(session, origin.thesis_id),
                    )
                )

        attempted = 0
        for observation in observations:
            try:
                signal = evaluate_exit(observation, self._config, now=moment)
                if signal is None:
                    continue
                counts["signalled"] += 1
                if attempted >= limit:
                    # The budget bounds how many exit proposals one tick generates,
                    # never which positions are looked at; the rest are re-signalled
                    # and attempted on the next tick.
                    counts["deferred"] += 1
                    continue
                attempted += 1
                result = await self._proposals.generate_exit(
                    observation.broker_ticker, signal, now=moment
                )
                counts["proposed" if result.created else "blocked"] += 1
            except Exception as exc:
                # One holding's unexpected failure must not abort the tick: every
                # other position still gets its decision.
                log.warning(
                    "exit_sweep_position_failed",
                    broker_ticker=observation.broker_ticker,
                    error_type=type(exc).__name__,
                )
                counts["failed"] += 1

        log.info("exit_sweep_complete", **dict(counts))
        return dict(counts)

    async def _has_live_proposal(self, session: AsyncSession, broker_ticker: str) -> bool:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(TradeProposal)
            .where(
                TradeProposal.broker == self._proposals.broker,
                TradeProposal.broker_ticker == broker_ticker,
                TradeProposal.status.in_(ACTIVE_STATUSES),
            )
        )
        return bool(count)

    async def _superseded(self, session: AsyncSession, thesis_id: uuid.UUID) -> bool:
        """Whether research has published a successor to the opening thesis.

        The same question `proposals.lifecycle` already asks of a pending
        proposal, asked of a holding -- and answered by the same query.
        """
        return await thesis_is_superseded(session, thesis_id)
