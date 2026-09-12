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

import asyncio
import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal
from itertools import pairwise
from typing import Any, Protocol

from stockbrain.errors import ProviderError, ProviderResponseError
from stockbrain.logging import get_logger
from stockbrain.market_data.base import Bar

__all__ = [
    "YAHOO_SUFFIX_BY_EXCHANGE",
    "DailyBars",
    "YahooDailyBars",
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


class DailyBars(Protocol):
    """A source of daily OHLC for one symbol, oldest first, with its currency."""

    async def daily_bars(self, symbol: str, *, days: int) -> tuple[Sequence[Bar], str]: ...


#: Yahoo's spelling of pence.  Trading 212 says ``GBX``; both mean 1/100 GBP.
_PENCE = "GBp"


class YahooDailyBars:
    """Daily bars from Yahoo via ``yfinance``, on a worker thread, built to fail.

    ``yfinance`` is synchronous and does blocking network I/O; it runs under
    :func:`asyncio.to_thread` with a hard timeout so a hung Yahoo endpoint cannot
    stall the scheduler.  Only ``Open/High/Low/Close/Volume`` are read.
    """

    def __init__(self, *, timeout_seconds: float = 20.0) -> None:
        self._timeout = timeout_seconds

    def _fetch(self, symbol: str, days: int) -> tuple[list[Bar], str]:
        import yfinance  # imported here so a broken Yahoo dependency cannot stop startup

        ticker = yfinance.Ticker(symbol)
        frame = ticker.history(period=f"{days}d", interval="1d", auto_adjust=False)
        if frame is None or getattr(frame, "empty", True):
            raise ProviderResponseError(f"yfinance: no daily bars for {symbol}")
        info: Any = getattr(ticker, "fast_info", None) or {}
        raw_currency = (
            info.get("currency") if hasattr(info, "get") else getattr(info, "currency", None)
        )
        if not isinstance(raw_currency, str) or not raw_currency:
            raise ProviderResponseError(f"yfinance: no currency reported for {symbol}")
        currency = "GBX" if raw_currency == _PENCE else raw_currency.upper()

        bars: list[Bar] = []
        for row in frame.itertuples():
            open_ = Decimal(str(row.Open))
            high = Decimal(str(row.High))
            low = Decimal(str(row.Low))
            close = Decimal(str(row.Close))
            if not (
                open_.is_finite() and high.is_finite() and low.is_finite() and close.is_finite()
            ):
                # Yahoo pads a missing session with NaN/Infinity; one such row
                # must not poison the series the ATR is computed from.
                continue
            stamp = row.Index
            if getattr(stamp, "tzinfo", None) is None:
                stamp = stamp.replace(tzinfo=dt.UTC)
            bars.append(
                Bar(
                    symbol=symbol,
                    timestamp=stamp,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=int(row.Volume or 0),
                    feed="yahoo",
                )
            )
        bars.sort(key=lambda bar: bar.timestamp)
        if len(bars) < 2:
            raise ProviderResponseError(f"yfinance: fewer than two finite bars for {symbol}")
        return bars, currency

    async def daily_bars(self, symbol: str, *, days: int) -> tuple[Sequence[Bar], str]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._fetch, symbol, days), timeout=self._timeout
            )
        except ProviderError:
            raise
        except TimeoutError as exc:
            raise ProviderResponseError(
                f"yfinance: timed out after {self._timeout}s for {symbol}"
            ) from exc
        except Exception as exc:  # yfinance raises whatever Yahoo's payload makes it raise
            raise ProviderResponseError(f"yfinance: {type(exc).__name__}: {exc}") from exc
