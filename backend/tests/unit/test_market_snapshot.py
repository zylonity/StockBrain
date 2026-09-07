"""Verified market snapshot: deterministic indicators from bars already fetched."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from stockbrain.enums import BarTimeframe
from stockbrain.errors import ProviderResponseError
from stockbrain.intelligence.research_data import (
    SNAPSHOT_INDICATORS,
    AlpacaResearchProvider,
)
from stockbrain.market_data.base import Bar
from tests.research_helpers import packet


class _Bars:
    """Deterministic synthetic history; enough rows for a 200-period average."""

    name = "alpaca"

    def __init__(self, count: int = 260) -> None:
        self.count = count

    async def bars(
        self, symbol: str, timeframe: BarTimeframe, start, end, *, limit=None
    ) -> list[Bar]:
        if timeframe is BarTimeframe.MIN_1:
            return []
        base = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        out = []
        for i in range(self.count):
            price = Decimal(100 + i % 17)
            out.append(
                Bar(
                    symbol=symbol,
                    timestamp=base + dt.timedelta(days=i),
                    open=price,
                    high=price + 2,
                    low=price - 2,
                    close=price + 1,
                    volume=1_000 + i,
                    feed="iex",
                )
            )
        return out

    async def latest_quote(self, symbol: str):
        raise ProviderResponseError("no quote")

    async def latest_trade(self, symbol: str):
        raise ProviderResponseError("no trade")

    async def capability(self, *, refresh: bool = False):
        raise ProviderResponseError("n/a")

    async def aclose(self) -> None:
        pass


def _snapshot(data) -> dict:
    for datum in data:
        if datum.kind == "verified_snapshot_and_indicators":
            return json.loads(datum.text)
    raise AssertionError("no snapshot datum produced")


async def test_snapshot_reports_every_indicator_upstream_asks_for() -> None:
    value = packet().model_copy(update={"as_of": dt.datetime(2026, 12, 1, tzinfo=dt.UTC)})
    data = await AlpacaResearchProvider(_Bars()).context(value)
    body = _snapshot(data)
    assert set(body["indicators"]) == set(SNAPSHOT_INDICATORS)
    # A 200-period average needs 200 rows; the provider must compute on the full
    # history it fetched, not on the truncated window it ships to the model.
    assert isinstance(body["indicators"]["close_200_sma"], float)


async def test_snapshot_is_the_last_completed_bar_at_the_cutoff() -> None:
    value = packet().model_copy(update={"as_of": dt.datetime(2026, 12, 1, tzinfo=dt.UTC)})
    data = await AlpacaResearchProvider(_Bars()).context(value)
    body = _snapshot(data)
    assert body["latest_row"]["date"] <= value.as_of.date().isoformat()
    for field in ("open", "high", "low", "close", "volume"):
        assert field in body["latest_row"]


async def test_short_history_degrades_the_indicator_not_the_snapshot() -> None:
    """Ten bars cannot support a 200-period average; the rest must still ship."""
    value = packet().model_copy(update={"as_of": dt.datetime(2026, 12, 1, tzinfo=dt.UTC)})
    data = await AlpacaResearchProvider(_Bars(count=10)).context(value)
    body = _snapshot(data)
    assert body["indicators"]["close_200_sma"] is None
    assert body["latest_row"]["close"] is not None
    assert "close_200_sma" in body["unavailable"]


@pytest.mark.parametrize("count", [1, 3])
async def test_snapshot_never_invents_a_value(count: int) -> None:
    value = packet().model_copy(update={"as_of": dt.datetime(2026, 12, 1, tzinfo=dt.UTC)})
    data = await AlpacaResearchProvider(_Bars(count=count)).context(value)
    body = _snapshot(data)
    for name, computed in body["indicators"].items():
        assert computed is None or isinstance(computed, float), name
