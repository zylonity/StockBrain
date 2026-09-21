"""Yahoo symbol mapping and ATR: table-driven, currency-blind, Decimal-exact."""

from __future__ import annotations

import datetime as dt
import sys
import types
from collections.abc import Iterator
from decimal import Decimal

import pytest

from stockbrain.errors import ProviderResponseError
from stockbrain.market_data.base import Bar
from stockbrain.market_data.yahoo import (
    YAHOO_SUFFIX_BY_EXCHANGE,
    YahooDailyBars,
    average_true_range,
    yahoo_symbol,
)


@pytest.mark.parametrize(
    ("ticker", "exchange", "expected"),
    [
        ("AAPL_US_EQ", "NASDAQ", "AAPL"),
        ("BRK.B_US_EQ", "NYSE", "BRK-B"),
        ("3SMRl_EQ", "London Stock Exchange", "3SMR.L"),
        ("VODl_EQ", "London Stock Exchange AIM", "VOD.L"),
        ("ETLSd_EQ", "Deutsche Börse Xetra", "ETLS.DE"),
        ("EMWEp_EQ", "Euronext Paris", "EMWE.PA"),
        ("ALLNs_EQ", "SIX Swiss Exchange", "ALLN.SW"),
        ("SHOPt_EQ", "Toronto Stock Exchange", "SHOP.TO"),
        ("ASMLa_EQ", "Euronext Amsterdam", "ASML.AS"),
        ("ENIm_EQ", "Borsa Italiana", "ENI.MI"),
        ("SANe_EQ", "Bolsa de Madrid", "SAN.MC"),
        ("SOLBb_EQ", "Euronext Brussels", "SOLB.BR"),
        ("EDPl_EQ", "Euronext Lisbon", "EDP.LS"),
        ("OMVv_EQ", "Wiener Börse", "OMV.VI"),
        ("SAPd_EQ", "Gettex", "SAP.MU"),
        ("TCEHY_US_EQ", "OTC Markets", "TCEHY"),
    ],
)
def test_known_exchanges_map_to_yahoo_suffixes(ticker: str, exchange: str, expected: str) -> None:
    assert yahoo_symbol(ticker, exchange) == expected


@pytest.mark.parametrize("exchange", [None, "", "Bourse de Casablanca"])
def test_an_unknown_exchange_yields_no_symbol_rather_than_a_guess(exchange: str | None) -> None:
    assert yahoo_symbol("ABCl_EQ", exchange) is None


@pytest.mark.parametrize(
    ("ticker", "exchange", "market_symbol", "expected"),
    [
        # Trading 212's ticker stem is not the market symbol for a fifth of the
        # US universe; the broker's own market_symbol is the truth.
        ("AGC_US_EQ", "NASDAQ", "GRAB", "GRAB"),
        ("BHI_US_EQ", "NASDAQ", "BKR", "BKR"),
        ("VG1_US_EQ", "NYSE", "VG", "VG"),
        # The share-class rule applies to the market symbol too.
        ("BRKB_US_EQ", "NYSE", "BRK.B", "BRK-B"),
        # Non-US: the market symbol has no venue letter; the suffix still applies.
        ("KNOSl_EQ", "London Stock Exchange", "KNOS", "KNOS.L"),
        ("ZPDFd_EQ", "Deutsche Börse Xetra", "ZPDF", "ZPDF.DE"),
        # An empty market symbol falls back to the ticker stem.
        ("AAPL_US_EQ", "NASDAQ", "", "AAPL"),
        ("AAPL_US_EQ", "NASDAQ", None, "AAPL"),
    ],
)
def test_the_market_symbol_wins_over_the_ticker_stem(
    ticker: str, exchange: str, market_symbol: str | None, expected: str
) -> None:
    assert yahoo_symbol(ticker, exchange, market_symbol=market_symbol) == expected


def test_an_unknown_exchange_yields_no_symbol_even_with_a_market_symbol() -> None:
    assert yahoo_symbol("AGC_US_EQ", "Nowhere", market_symbol="GRAB") is None


def test_a_ticker_without_the_eq_suffix_yields_no_symbol() -> None:
    assert yahoo_symbol("AAPL", "NASDAQ") is None


def test_every_suffix_table_entry_is_a_dot_prefixed_code_or_empty() -> None:
    for suffix in YAHOO_SUFFIX_BY_EXCHANGE.values():
        assert suffix == "" or (suffix.startswith(".") and suffix[1:].isalpha())


def _bar(day: int, high: str, low: str, close: str) -> Bar:
    return Bar(
        symbol="X",
        timestamp=dt.datetime(2026, 9, day, tzinfo=dt.UTC),
        open=Decimal(low),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=1,
    )


def test_atr_is_the_mean_of_the_last_n_true_ranges() -> None:
    # Day 1 has no previous close, so TR = H-L = 2.  Days 2-3: TR uses prev close.
    bars = [_bar(1, "12", "10", "11"), _bar(2, "14", "11", "13"), _bar(3, "13", "9", "10")]
    # Day 2: max(14-11, |14-11|, |11-11|) = 3.  Day 3: max(13-9, |13-13|, |9-13|) = 4.
    assert average_true_range(bars, period=2) == Decimal("3.5")


def test_atr_needs_period_plus_one_bars() -> None:
    bars = [_bar(1, "12", "10", "11"), _bar(2, "14", "11", "13")]
    assert average_true_range(bars, period=2) is None
    assert average_true_range([], period=14) is None


def test_atr_uses_the_most_recent_bars_and_returns_a_decimal() -> None:
    bars = [_bar(d, "101", "99", "100") for d in range(1, 30)]
    bars.append(_bar(30, "110", "90", "100"))  # a shock on the last day
    atr = average_true_range(bars, period=14)
    assert isinstance(atr, Decimal)
    assert atr > Decimal("2") and atr < Decimal("4")  # 13 days of TR=2 and one of 20 → ~3.29


class _Frame:
    """The four columns the adapter is allowed to read, shaped like a DataFrame."""

    def __init__(self, rows: list[tuple[dt.datetime, float, float, float, float, int]]) -> None:
        self._rows = rows
        self.empty = not rows

    def itertuples(self) -> Iterator[types.SimpleNamespace]:
        for ts, o, h, lo, c, v in self._rows:
            yield types.SimpleNamespace(Index=ts, Open=o, High=h, Low=lo, Close=c, Volume=v)


def _install_fake_yfinance(
    monkeypatch: pytest.MonkeyPatch, *, frame: _Frame, currency: str
) -> None:
    class _Ticker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol
            self.fast_info = {"currency": currency}

        def history(self, **kwargs: object) -> _Frame:
            assert kwargs.get("interval") == "1d"
            assert kwargs.get("auto_adjust") is False
            return frame

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_Ticker))


async def test_bars_are_decimal_oldest_first_and_pence_is_reported_as_gbx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t0 = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    frame = _Frame([(t0 + dt.timedelta(days=i), 100.0, 101.5, 99.25, 100.75, 10) for i in range(3)])
    _install_fake_yfinance(monkeypatch, frame=frame, currency="GBp")

    bars, currency = await YahooDailyBars().daily_bars("VOD.L", days=5)

    assert currency == "GBX"
    assert [bar.timestamp for bar in bars] == sorted(bar.timestamp for bar in bars)
    assert bars[0].high == Decimal("101.5") and isinstance(bars[0].high, Decimal)
    assert bars[0].symbol == "VOD.L"


async def test_an_empty_frame_is_a_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_yfinance(monkeypatch, frame=_Frame([]), currency="USD")
    with pytest.raises(ProviderResponseError, match="no daily bars"):
        await YahooDailyBars().daily_bars("NOPE", days=5)


async def test_a_missing_currency_is_a_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    t0 = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    frame = _Frame(
        [(t0, 1.0, 1.0, 1.0, 1.0, 1), (t0 + dt.timedelta(days=1), 1.0, 1.0, 1.0, 1.0, 1)]
    )
    _install_fake_yfinance(monkeypatch, frame=frame, currency="")
    with pytest.raises(ProviderResponseError, match="currency"):
        await YahooDailyBars().daily_bars("X", days=5)


async def test_any_yfinance_exception_becomes_a_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Broken:
        def __init__(self, symbol: str) -> None:
            raise RuntimeError("Yahoo reshaped the payload again")

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_Broken))
    with pytest.raises(ProviderResponseError, match="yfinance"):
        await YahooDailyBars().daily_bars("X", days=5)


async def test_a_non_finite_row_is_dropped_not_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bad bar must not poison the series the ATR is computed from."""
    t0 = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    frame = _Frame(
        [
            (t0, 100.0, 101.5, 99.25, 100.75, 10),
            (t0 + dt.timedelta(days=1), 100.0, float("nan"), 99.25, 100.75, 10),
            (t0 + dt.timedelta(days=2), 100.0, 101.5, 99.25, 100.75, 10),
        ]
    )
    _install_fake_yfinance(monkeypatch, frame=frame, currency="USD")

    bars, _ = await YahooDailyBars().daily_bars("X", days=5)

    assert [bar.timestamp for bar in bars] == [t0, t0 + dt.timedelta(days=2)]


async def test_fewer_than_two_finite_bars_is_a_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t0 = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    frame = _Frame(
        [
            (t0, 100.0, float("nan"), 99.25, 100.75, 10),
            (t0 + dt.timedelta(days=1), 100.0, float("inf"), 99.25, 100.75, 10),
        ]
    )
    _install_fake_yfinance(monkeypatch, frame=frame, currency="USD")

    with pytest.raises(ProviderResponseError, match="fewer than two finite bars"):
        await YahooDailyBars().daily_bars("X", days=5)
