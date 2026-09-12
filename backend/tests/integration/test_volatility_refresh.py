"""ATR refresh: symbol mapping, currency assertion, staleness, and what the sweep sees."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa

from stockbrain.db.models.companies import BrokerInstrument
from stockbrain.db.models.portfolio import Position, PositionPeak
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.session import Database
from stockbrain.enums import Broker, OrderSide
from stockbrain.market_data.base import Bar
from stockbrain.market_data.volatility import VolatilityRefreshService
from stockbrain.market_data.yahoo import DailyBars
from stockbrain.observability.health import ProviderHealthRegistry
from stockbrain.risk.config import RiskConfig, risk_config_from_settings
from tests import proposal_helpers as ph
from tests import risk_helpers as h
from tests.integration.test_exit_sweep import (
    _exit_sweep,
    _seed_executed_buy,
    _seed_position,
)

NOW = dt.datetime(2026, 9, 12, 8, 0, tzinfo=dt.UTC)


class _FakeBars:
    def __init__(self, currency: str = "USD", n: int = 20) -> None:
        self.currency = currency
        self.n = n
        self.calls: list[str] = []

    async def daily_bars(self, symbol: str, *, days: int) -> tuple[Sequence[Bar], str]:
        self.calls.append(symbol)
        t0 = NOW - dt.timedelta(days=self.n)
        bars = [
            Bar(
                symbol=symbol,
                timestamp=t0 + dt.timedelta(days=i),
                open=Decimal("100"),
                high=Decimal("102"),
                low=Decimal("98"),
                close=Decimal("100"),
                volume=1,
            )
            for i in range(self.n)
        ]
        return bars, self.currency


class _BadBarBars:
    """A source that hands back a NaN bar: only the refresh's try may contain it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def daily_bars(self, symbol: str, *, days: int) -> tuple[Sequence[Bar], str]:
        self.calls.append(symbol)
        t0 = NOW - dt.timedelta(days=20)
        high = Decimal("NaN") if symbol == "AAPL" else Decimal("102")
        bars = [
            Bar(
                symbol=symbol,
                timestamp=t0 + dt.timedelta(days=i),
                open=Decimal("100"),
                high=high,
                low=Decimal("98"),
                close=Decimal("100"),
                volume=1,
            )
            for i in range(20)
        ]
        return bars, "USD"


async def _seed_position_with_peak(
    database: Database,
    *,
    broker_ticker: str,
    exchange: str,
    currency: str | None,
) -> None:
    """A funded holding, its listing's exchange, and the peak an ATR hangs on.

    ``currency`` is stamped on both the account snapshot and the ``Position``
    row; pass ``None`` for a position whose own currency the broker omitted.
    """
    await ph.fund(
        database,
        currency=currency or "USD",
        positions={broker_ticker: (Decimal("9"), Decimal("9"))},
    )
    if currency is None:
        async with database.transaction() as session:
            await session.execute(
                sa.update(Position)
                .where(
                    Position.broker == Broker.TRADING212,
                    Position.broker_ticker == broker_ticker,
                )
                .values(currency=None)
            )
    async with database.transaction() as session:
        session.add(
            BrokerInstrument(
                broker=Broker.TRADING212,
                broker_ticker=broker_ticker,
                name=broker_ticker,
                exchange=exchange,
                currency=currency,
                instrument_type="STOCK",
                is_active=True,
            )
        )
        await session.flush()
        session.add(
            PositionPeak(
                broker=Broker.TRADING212,
                account_id=ph.ACCOUNT_ID,
                broker_ticker=broker_ticker,
                peak_price=Decimal("110"),
                peak_at=NOW,
                observations=5,
            )
        )


async def _seed_peak(
    database: Database,
    *,
    broker_ticker: str,
    peak_price: Decimal,
    observations: int,
    atr: Decimal | None = None,
    atr_as_of: dt.date | None = None,
    atr_period: int | None = None,
    atr_currency: str | None = None,
) -> None:
    async with database.transaction() as session:
        session.add(
            PositionPeak(
                broker=Broker.TRADING212,
                account_id=ph.ACCOUNT_ID,
                broker_ticker=broker_ticker,
                peak_price=peak_price,
                peak_at=NOW,
                observations=observations,
                atr=atr,
                atr_period=atr_period
                if atr_period is not None
                else (14 if atr is not None else None),
                atr_currency=(
                    atr_currency
                    if atr_currency is not None
                    else ("USD" if atr is not None else None)
                ),
                atr_as_of=atr_as_of,
                atr_source="yahoo" if atr is not None else None,
            )
        )


def _volatility_service(
    database: Database,
    bars: DailyBars,
    *,
    config: RiskConfig | None = None,
) -> VolatilityRefreshService:
    settings = ph.settings()
    return VolatilityRefreshService(
        database,
        settings,
        bars=bars,
        config=config or risk_config_from_settings(settings),
        health=ProviderHealthRegistry(),
    )


async def _set_position_currency(database: Database, *, broker_ticker: str, currency: str) -> None:
    async with database.transaction() as session:
        await session.execute(
            sa.update(Position)
            .where(
                Position.broker == Broker.TRADING212,
                Position.broker_ticker == broker_ticker,
            )
            .values(currency=currency)
        )


async def test_refresh_stores_an_atr_in_the_instruments_currency(clean_tables: Database) -> None:
    database = clean_tables
    await _seed_position_with_peak(
        database, broker_ticker="AAPL_US_EQ", exchange="NASDAQ", currency="USD"
    )
    bars = _FakeBars(currency="USD")
    service = _volatility_service(database, bars)

    counts = await service.refresh(now=NOW)

    assert counts["refreshed"] == 1 and bars.calls == ["AAPL"]
    async with database.session() as session:
        peak = (await session.execute(sa.select(PositionPeak))).scalar_one()
    assert peak.atr == Decimal("4")  # every TR is 102-98 = 4
    assert peak.atr_period == 14 and peak.atr_currency == "USD" and peak.atr_source == "yahoo"
    assert peak.atr_as_of == (NOW - dt.timedelta(days=1)).date()
    assert peak.atr_refreshed_at == NOW  # the attempt clock, not the bar date


async def test_a_currency_mismatch_is_refused_not_converted(clean_tables: Database) -> None:
    database = clean_tables
    await _seed_position_with_peak(
        database, broker_ticker="VODl_EQ", exchange="London Stock Exchange", currency="GBX"
    )
    bars = _FakeBars(currency="GBP")
    service = _volatility_service(database, bars)

    counts = await service.refresh(now=NOW)

    assert counts["currency_mismatch"] == 1 and counts["refreshed"] == 0
    async with database.session() as session:
        peak = (await session.execute(sa.select(PositionPeak))).scalar_one()
    assert peak.atr is None
    # Even a refused fetch stamps the attempt clock, so the symbol is not
    # hammered again on the next sweep.
    counts = await service.refresh(now=NOW + dt.timedelta(hours=6))
    assert counts["skipped_fresh"] == 1 and len(bars.calls) == 1


async def test_a_position_with_an_unknown_currency_is_not_fetched(clean_tables: Database) -> None:
    database = clean_tables
    await _seed_position_with_peak(
        database,
        broker_ticker="AAPL_US_EQ",
        exchange="NASDAQ",
        currency=None,
    )
    bars = _FakeBars(currency="USD")

    counts = await _volatility_service(database, bars).refresh(now=NOW)

    assert counts["skipped_no_currency"] == 1 and counts["refreshed"] == 0
    assert bars.calls == []
    async with database.session() as session:
        peak = (await session.execute(sa.select(PositionPeak))).scalar_one()
    assert peak.atr is None


async def test_a_fresh_atr_is_not_refetched(clean_tables: Database) -> None:
    database = clean_tables
    await _seed_position_with_peak(
        database, broker_ticker="AAPL_US_EQ", exchange="NASDAQ", currency="USD"
    )
    bars = _FakeBars()
    service = _volatility_service(database, bars)
    await service.refresh(now=NOW)
    counts = await service.refresh(now=NOW + dt.timedelta(hours=6))
    assert counts["skipped_fresh"] == 1 and len(bars.calls) == 1
    # 21 hours after the attempt the clock has lapsed; the bar date is irrelevant.
    counts = await service.refresh(now=NOW + dt.timedelta(hours=21))
    assert counts["refreshed"] == 1 and len(bars.calls) == 2


async def test_a_period_change_refetches_despite_a_fresh_stamp(clean_tables: Database) -> None:
    database = clean_tables
    await _seed_position_with_peak(
        database, broker_ticker="AAPL_US_EQ", exchange="NASDAQ", currency="USD"
    )
    bars = _FakeBars(n=30)
    await _volatility_service(database, bars).refresh(now=NOW)

    service = _volatility_service(database, bars, config=h.config(exit_atr_period=20))
    counts = await service.refresh(now=NOW + dt.timedelta(hours=6))

    assert counts["refreshed"] == 1 and len(bars.calls) == 2


async def test_a_non_finite_bar_fails_only_its_own_position(clean_tables: Database) -> None:
    database = clean_tables
    await _seed_position_with_peak(
        database, broker_ticker="AAPL_US_EQ", exchange="NASDAQ", currency="USD"
    )
    await _seed_position_with_peak(
        database, broker_ticker="MSFT_US_EQ", exchange="NASDAQ", currency="USD"
    )
    bars = _BadBarBars()

    counts = await _volatility_service(database, bars).refresh(now=NOW)

    assert counts["failed"] == 1 and counts["refreshed"] == 1
    async with database.session() as session:
        peaks = {
            peak.broker_ticker: peak
            for peak in (await session.execute(sa.select(PositionPeak))).scalars()
        }
    assert peaks["AAPL_US_EQ"].atr is None
    assert peaks["MSFT_US_EQ"].atr == Decimal("4")


async def test_an_unmapped_exchange_is_skipped_and_counted(clean_tables: Database) -> None:
    database = clean_tables
    await _seed_position_with_peak(
        database, broker_ticker="ABCx_EQ", exchange="Bourse de Casablanca", currency="MAD"
    )
    counts = await _volatility_service(database, _FakeBars()).refresh(now=NOW)
    assert counts["skipped_no_symbol"] == 1


async def test_the_exit_sweep_sees_the_stored_atr(clean_tables: Database) -> None:
    """End to end: stored ATR → observation → volatility_stop proposal."""
    database = clean_tables
    # Cost 100, peak 110 (observed 5 times), price 103: below 110 - 3*2 = 104, above the -8% stop.
    await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")
    await _seed_position(
        database,
        broker_ticker="AAPL_US_EQ",
        average_price=Decimal("100"),
        current_price=Decimal("103"),
    )
    await _seed_peak(
        database,
        broker_ticker="AAPL_US_EQ",
        peak_price=Decimal("110"),
        observations=5,
        atr=Decimal("2"),
        atr_as_of=NOW.date(),
    )
    # The sweep is evaluated at NOW, so the account snapshot it reads must be no
    # older than the freshness limit at that instant.  ``_seed_executed_buy``
    # funded the account at the wall clock; re-fund it at NOW so this test does
    # not depend on the wall clock being past 08:00 UTC.
    await ph.fund(database, captured_at=NOW)
    counts = await (await _exit_sweep(database)).sweep(now=NOW)
    assert counts["proposed"] == 1
    async with database.session() as session:
        rules = (
            await session.execute(
                sa.select(TradeProposal.risk_rules).where(TradeProposal.side == OrderSide.SELL)
            )
        ).scalar_one()
    assert any(rule["rule_id"] == "volatility_stop" for rule in rules)
