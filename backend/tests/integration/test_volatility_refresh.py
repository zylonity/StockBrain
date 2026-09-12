"""ATR refresh: symbol mapping, currency assertion, staleness, and what the sweep sees."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa

from stockbrain.db.models.companies import BrokerInstrument
from stockbrain.db.models.portfolio import PositionPeak
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.session import Database
from stockbrain.enums import Broker, OrderSide
from stockbrain.market_data.base import Bar
from stockbrain.market_data.volatility import VolatilityRefreshService
from stockbrain.observability.health import ProviderHealthRegistry
from stockbrain.risk.config import risk_config_from_settings
from tests import proposal_helpers as ph
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


async def _seed_position_with_peak(
    database: Database,
    *,
    broker_ticker: str,
    exchange: str,
    currency: str,
) -> None:
    """A funded holding, its listing's exchange, and the peak an ATR hangs on."""
    await ph.fund(
        database,
        currency=currency,
        positions={broker_ticker: (Decimal("9"), Decimal("9"))},
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
                atr_period=14 if atr is not None else None,
                atr_currency="USD" if atr is not None else None,
                atr_as_of=atr_as_of,
                atr_source="yahoo" if atr is not None else None,
            )
        )


def _volatility_service(database: Database, bars: _FakeBars) -> VolatilityRefreshService:
    settings = ph.settings()
    return VolatilityRefreshService(
        database,
        settings,
        bars=bars,
        config=risk_config_from_settings(settings),
        health=ProviderHealthRegistry(),
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


async def test_a_currency_mismatch_is_refused_not_converted(clean_tables: Database) -> None:
    database = clean_tables
    await _seed_position_with_peak(
        database, broker_ticker="VODl_EQ", exchange="London Stock Exchange", currency="GBX"
    )
    service = _volatility_service(database, _FakeBars(currency="GBP"))

    counts = await service.refresh(now=NOW)

    assert counts["currency_mismatch"] == 1 and counts["refreshed"] == 0
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
