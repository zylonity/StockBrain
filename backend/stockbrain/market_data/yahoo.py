"""Yahoo Finance daily bars, for volatility only.

``yfinance`` rides Yahoo's undocumented endpoints: no key, no SLA, no stability
guarantee -- the same source :mod:`stockbrain.intelligence.research_expectations`
already uses for analyst targets, fenced the same way.  It earns its place here
because it is the one free source of daily OHLC that covers every venue in the
Trading 212 universe: LSE in pence, Xetra, Euronext, SIX, TSX.

It is research-grade data.  Nothing here may become a proposal's reference
price; it feeds one exit rule's *threshold* and, when it fails, that rule skips.

Symbols are mapped by *exchange*, from a table, never guessed from the ticker
string.  Trading 212 encodes the venue as one lowercase letter before ``_EQ``
(``3SMRl_EQ``), or ``_US_EQ`` for the United States; Yahoo wants ``3SMR.L``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from itertools import pairwise

from stockbrain.logging import get_logger
from stockbrain.market_data.base import Bar

__all__ = [
    "YAHOO_SUFFIX_BY_EXCHANGE",
    "average_true_range",
    "yahoo_symbol",
]

log = get_logger(__name__)

#: Yahoo ticker suffix per Trading 212 exchange name.  An exchange absent from
#: this table has no Yahoo symbol; the caller skips it rather than guessing.
YAHOO_SUFFIX_BY_EXCHANGE: Mapping[str, str] = {
    "NASDAQ": "",
    "NYSE": "",
    "OTC Markets": "",
    "London Stock Exchange": ".L",
    "London Stock Exchange AIM": ".L",
    "Deutsche Börse Xetra": ".DE",
    "Gettex": ".MU",
    "Euronext Paris": ".PA",
    "Euronext Amsterdam": ".AS",
    "Euronext Brussels": ".BR",
    "Euronext Lisbon": ".LS",
    "Borsa Italiana": ".MI",
    "Bolsa de Madrid": ".MC",
    "Wiener Börse": ".VI",
    "SIX Swiss Exchange": ".SW",
    "Toronto Stock Exchange": ".TO",
}

_US_SUFFIX = "_US_EQ"
_EQ_SUFFIX = "_EQ"


def yahoo_symbol(broker_ticker: str, exchange: str | None) -> str | None:
    """The Yahoo symbol for a Trading 212 listing, or ``None`` when unknown."""
    if not exchange or exchange not in YAHOO_SUFFIX_BY_EXCHANGE:
        return None
    suffix = YAHOO_SUFFIX_BY_EXCHANGE[exchange]
    if broker_ticker.endswith(_US_SUFFIX):
        base = broker_ticker[: -len(_US_SUFFIX)]
        # Yahoo spells share classes with a dash: BRK.B -> BRK-B.
        return base.replace(".", "-") + suffix
    if broker_ticker.endswith(_EQ_SUFFIX) and len(broker_ticker) > len(_EQ_SUFFIX) + 1:
        base = broker_ticker[: -len(_EQ_SUFFIX)]
        # The venue letter is lowercase and the last character of the base.
        if base[-1].islower():
            base = base[:-1]
        return base + suffix if base else None
    return None


def average_true_range(bars: Sequence[Bar], period: int) -> Decimal | None:
    """Mean of the last ``period`` true ranges, or ``None`` with too few bars.

    True range = max(high - low, |high - previous close|, |low - previous close|).
    A simple mean rather than Wilder smoothing: it needs no carried state, and
    the difference is noise beside the multiplier applied downstream.  Needs
    ``period + 1`` bars so every true range has a previous close.
    """
    if period <= 0 or len(bars) < period + 1:
        return None
    ordered = sorted(bars, key=lambda bar: bar.timestamp)
    ranges: list[Decimal] = []
    for previous, current in pairwise(ordered):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    window = ranges[-period:]
    return sum(window, Decimal(0)) / Decimal(period)
