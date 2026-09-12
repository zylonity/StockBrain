"""Daily ATR refresh for open positions.

One Yahoo request per open position per day, never on the exit sweep's tick.
The result is stored on ``position_peaks`` and read by the sweep; a failure
here degrades one provider and leaves the flat stops standing.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter

import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument
from stockbrain.db.models.portfolio import Position, PositionPeak
from stockbrain.db.session import Database
from stockbrain.enums import Broker, ProviderStatus
from stockbrain.errors import ProviderError
from stockbrain.logging import get_logger
from stockbrain.market_data.yahoo import DailyBars, average_true_range, yahoo_symbol
from stockbrain.observability.health import ProviderHealthRegistry, ProviderName
from stockbrain.risk.config import RiskConfig

__all__ = ["VolatilityRefreshService"]

log = get_logger(__name__)
_TALLY_KEYS = (
    "considered",
    "refreshed",
    "skipped_fresh",
    "skipped_no_symbol",
    "skipped_no_currency",
    "skipped_no_peak",
    "currency_mismatch",
    "insufficient_bars",
    "failed",
)


class VolatilityRefreshService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        bars: DailyBars,
        config: RiskConfig,
        health: ProviderHealthRegistry,
        broker: Broker = Broker.TRADING212,
    ) -> None:
        self._database = database
        self._settings = settings
        self._bars = bars
        self._config = config
        self._health = health
        self._broker = broker

    async def refresh(self, *, now: dt.datetime | None = None) -> dict[str, int]:
        moment = now or utcnow()
        today = moment.date()
        counts: Counter[str] = Counter(dict.fromkeys(_TALLY_KEYS, 0))

        async with self._database.session() as session:
            rows = (
                await session.execute(
                    sa.select(Position, PositionPeak, BrokerInstrument.exchange)
                    .join(
                        PositionPeak,
                        sa.and_(
                            PositionPeak.broker == Position.broker,
                            PositionPeak.account_id == Position.account_id,
                            PositionPeak.broker_ticker == Position.broker_ticker,
                        ),
                        isouter=True,
                    )
                    .join(
                        BrokerInstrument,
                        BrokerInstrument.broker_ticker == Position.broker_ticker,
                        isouter=True,
                    )
                    .where(Position.broker == self._broker, Position.quantity > 0)
                    .order_by(Position.broker_ticker)
                )
            ).all()

        work: list[tuple[str, str, str | None]] = []
        for position, peak, exchange in rows:
            counts["considered"] += 1
            if peak is None:
                counts["skipped_no_peak"] += 1
                continue
            # Yesterday's bar is the newest a daily series can hold; an ATR whose
            # last bar is yesterday or today is as fresh as it gets.
            if peak.atr_as_of is not None and (today - peak.atr_as_of).days <= 1:
                counts["skipped_fresh"] += 1
                continue
            symbol = yahoo_symbol(position.broker_ticker, exchange)
            if symbol is None:
                counts["skipped_no_symbol"] += 1
                continue
            if position.currency is None:
                # Without the position's currency there is nothing to assert the
                # fetched ATR against, so refuse to fetch at all rather than
                # stamp Yahoo's currency on the row unverified.
                counts["skipped_no_currency"] += 1
                continue
            work.append((position.broker_ticker, symbol, position.currency))

        errors = 0
        for broker_ticker, symbol, instrument_currency in work:
            try:
                bars, currency = await self._bars.daily_bars(
                    symbol, days=self._settings.volatility_bars_days
                )
            except ProviderError as exc:
                errors += 1
                counts["failed"] += 1
                log.warning(
                    "volatility_refresh_failed",
                    broker_ticker=broker_ticker,
                    symbol=symbol,
                    error=str(exc)[:200],
                )
                continue
            if currency != instrument_currency:
                counts["currency_mismatch"] += 1
                log.warning(
                    "volatility_currency_mismatch",
                    broker_ticker=broker_ticker,
                    symbol=symbol,
                    instrument=instrument_currency,
                    bars=currency,
                )
                continue
            atr = average_true_range(bars, self._config.exit_atr_period)
            if atr is None:
                counts["insufficient_bars"] += 1
                continue
            async with self._database.transaction() as session:
                await session.execute(
                    sa.update(PositionPeak)
                    .where(
                        PositionPeak.broker == self._broker,
                        PositionPeak.broker_ticker == broker_ticker,
                    )
                    .values(
                        atr=atr,
                        atr_period=self._config.exit_atr_period,
                        atr_currency=currency,
                        atr_as_of=bars[-1].timestamp.date(),
                        atr_source="yahoo",
                        updated_at=moment,
                    )
                )
            counts["refreshed"] += 1

        if work:
            self._health.record(
                ProviderName.YAHOO_BARS,
                ProviderStatus.DEGRADED if errors else ProviderStatus.HEALTHY,
                detail=f"{errors} of {len(work)} daily-bar fetches failed" if errors else None,
            )
        log.info("volatility_refresh_complete", **dict(counts))
        return dict(counts)
