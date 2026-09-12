"""Yahoo symbol mapping and ATR: table-driven, currency-blind, Decimal-exact."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from stockbrain.market_data.base import Bar
from stockbrain.market_data.yahoo import YAHOO_SUFFIX_BY_EXCHANGE, average_true_range, yahoo_symbol


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
