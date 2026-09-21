# Volatility Stop (Yahoo Daily Bars → ATR) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the one-size −8% stop with a volatility-scaled one — a Chandelier-style floor at `peak − k × ATR(14)` computed from free Yahoo daily bars — for every exchange in the T212 universe, at zero LLM cost.

**Architecture:** A narrow `DailyBarsSource` (one Yahoo implementation, `yfinance`, already a pinned dependency) fetches ~3 months of daily OHLC per open position once a day on a worker thread, asserts the bar currency matches the instrument, computes ATR, and stores it on the existing `position_peaks` row. The exit sweep reads it into `ExitObservation`; a new `volatility_stop` rule sits between `hard_stop` and `trailing_stop`. Yahoo is research-grade: it is never a reference price, and when it is unavailable or stale the rule skips and the flat `hard_stop` keeps standing.

**Tech Stack:** Python 3.12, `yfinance>=1.4.1,<2` (present), SQLAlchemy async, Alembic, pytest. No new dependencies. No LLM calls anywhere on this path.

**Spec:** `STOCKBRAIN_TECHNICAL_SPEC.md` §12 (market-price strategy — broker/yfinance prices are not execution-grade), §14 (deterministic risk engine), §22 (scheduler), §46 (must-not list). Predecessor plan: `docs/superpowers/plans/2026-09-11-deterministic-position-exits.md` (merged at `3e43e70`).

---

## Global Constraints

- **Zero LLM tokens.** Nothing in this plan may import or call `stockbrain.llm`, the classifier, or research. ATR is arithmetic.
- **Yahoo is never a `PriceSource`.** It must not be added to `EXECUTION_GRADE_PRICE_SOURCES` (`enums.py:781`) and no proposal's `reference_price` may come from it. It feeds one rule's *threshold*; the evaluator still prices the proposal.
- **Built to fail.** Follow `research_expectations.py:474-500` exactly: `import yfinance` inside the function, `asyncio.to_thread` + `asyncio.wait_for`, explicit columns, every failure → `ProviderResponseError`; a Yahoo failure degrades one provider and nothing else.
- **Currency is asserted per instrument, never assumed.** LSE trades in pence (`GBX`; Yahoo says `GBp`). A bar series whose currency does not match the instrument is refused, not converted.
- **Every threshold lives on `RiskConfig`** (hashed into `version`); settings are `RESTART_REQUIRED`; no write route.
- **No broker mutation; nothing auto-authorizes.** Unchanged from the predecessor plan.
- **`mypy --strict`, ruff format, ruff check clean.** Full suite via the wrapper; baseline 2055 passed.
- **Alembic head before this work: `2a109374b5c6`.** Filenames `YYYYMMDD_HHMM_description.py`.
- **Decimal for prices/ATR.** Convert from the DataFrame at the adapter boundary via `str()`, never `float` arithmetic in the rule.
- **Rate discipline:** one Yahoo request per open position per day, never on the 5-minute sweep.

## Design decisions

1. **Chandelier, from the existing peak.** `floor = peak_price − exit_atr_multiplier × ATR`. The peak is the broker-sampled high-water mark from the predecessor plan; a position that never rose has `peak ≈ entry`, so the rule degrades to "entry − 3 ATR" — a volatility-scaled hard stop. The flat `hard_stop` stays as the backstop when ATR is missing or stale.
2. **Precedence:** `hard_stop` → **`volatility_stop`** → `trailing_stop` → `thesis_superseded` → `roi_target` → `horizon_elapsed`. It proposes SELL (full exit). `trailing_stop` remains for positions with no usable ATR.
3. **ATR = simple mean of the last N true ranges** (N = 14), TR = max(H−L, |H−prevC|, |L−prevC|). Wilder smoothing is not worth its statefulness here; document the choice.
4. **Symbol mapping is table-driven by exchange**, not guessed from the ticker string. T212 tickers carry a venue letter before `_EQ` (`3SMRl_EQ`, `ETLSd_EQ`, `EMWEp_EQ`, `ALLNs_EQ`) and `_US_EQ` for the US. The base is the ticker minus `_EQ` minus that letter (or minus `_US_EQ`); the suffix comes from `broker_instruments.exchange`. Unknown exchange → no symbol → ATR skipped, counted, never guessed. US `.` becomes `-` (`BRK.B` → `BRK-B`).
5. **ATR lives on `position_peaks`** (nullable columns). It is per-open-position state with the same lifetime; a separate table buys nothing. Rows without a peak yet (position never priced) are skipped until the next refresh.
6. **Ships dark:** `VOLATILITY_REFRESH_ENABLED=false`. The rule cannot fire without an ATR, so leaving the refresh off is the off switch.

## Out of scope

Intraday Yahoo bars for peaks; a full `MarketDataProvider` for Yahoo (quotes/sessions); making `EXIT_PRECEDENCE` drive the if-chain; per-position exception isolation changes; multi-account scoping.

## Known limitations shipped

Yahoo is 15–20 min delayed and unofficial — irrelevant for yesterday's daily range, but it will break someday; the dashboard shows it degraded and the flat stop continues. IEX-style sparse venues are not an issue for daily bars. OTC names map with no suffix and may return nothing.

---

## File Structure

**Created:**
- `backend/stockbrain/market_data/yahoo.py` — `yahoo_symbol()`, `average_true_range()`, `YahooDailyBars` (the one network class).
- `backend/stockbrain/market_data/volatility.py` — `VolatilityRefreshService`: positions → symbol → bars → ATR → `position_peaks`.
- `backend/alembic/versions/20260912_1200_position_peaks_atr.py`
- `backend/tests/unit/test_yahoo_bars.py`
- `backend/tests/integration/test_volatility_refresh.py`

**Modified:**
- `backend/stockbrain/db/models/portfolio.py` — ATR columns on `PositionPeak`.
- `backend/stockbrain/observability/health.py` — `ProviderName.YAHOO_BARS`.
- `backend/stockbrain/risk/config.py`, `backend/stockbrain/config.py`, `backend/stockbrain/api/settings_model.py` — knobs.
- `backend/stockbrain/risk/exits.py` — `atr`/`atr_as_of` on `ExitObservation`; `volatility_stop`; precedence.
- `backend/stockbrain/proposals/exits.py` — pass ATR into the observation.
- `backend/stockbrain/services.py` — construct + schedule the refresh.
- `backend/tests/unit/test_risk_exits.py`, `backend/tests/unit/test_risk_config.py`, `backend/tests/unit/test_phase9_gates.py`
- `docs/architecture.md`, `docs/operations.md`, `.env.example`

---

### Task 1: Symbol mapping and ATR arithmetic (pure)

**Files:**
- Create: `backend/stockbrain/market_data/yahoo.py` (pure part only)
- Test: `backend/tests/unit/test_yahoo_bars.py`

**Interfaces:**
- Produces: `yahoo_symbol(broker_ticker: str, exchange: str | None) -> str | None`; `average_true_range(bars: Sequence[Bar], period: int) -> Decimal | None`; `YAHOO_SUFFIX_BY_EXCHANGE: Mapping[str, str]`.

- [ ] **Step 1: Failing tests**

```python
# backend/tests/unit/test_yahoo_bars.py
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
```

- [ ] **Step 2: Run, expect `ModuleNotFoundError: stockbrain.market_data.yahoo`**

Run: `<WS>/pytest.sh tests/unit/test_yahoo_bars.py -v`

- [ ] **Step 3: Implement the pure part**

```python
# backend/stockbrain/market_data/yahoo.py
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
from typing import Any

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
    for previous, current in zip(ordered[:-1], ordered[1:], strict=True):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    window = ranges[-period:]
    return sum(window, Decimal(0)) / Decimal(period)
```

(`DailyBars` and `YahooDailyBars` are added in Task 2; leave the `__all__` entries in place — Task 2 fills them, and the tests here import only the pure names.)

- [ ] **Step 4: Run tests → PASS. Then `mypy --strict`, ruff.** If `mypy` complains about `__all__` naming undefined symbols, define placeholders is NOT the fix — remove the two names from `__all__` and let Task 2 add them back.

- [ ] **Step 5: Commit** — `feat(market-data): map Trading 212 listings to Yahoo symbols and compute ATR`

---

### Task 2: The Yahoo daily-bars source

**Files:**
- Modify: `backend/stockbrain/market_data/yahoo.py` (append)
- Modify: `backend/stockbrain/observability/health.py:44-57` — add `YAHOO_BARS = "yahoo_bars"` after `ALPACA_MARKET_DATA`.
- Test: `backend/tests/unit/test_yahoo_bars.py` (append)

**Interfaces:**
- Produces: `class DailyBars(Protocol)` with `async def daily_bars(self, symbol: str, *, days: int) -> tuple[Sequence[Bar], str]` returning `(bars_oldest_first, currency_code)`; `YahooDailyBars(timeout_seconds: float = 20.0)` implementing it. Currency is Yahoo's code upper-cased with `GBP`/`GBp` distinguished: return `"GBX"` when Yahoo reports `GBp`, so it matches `broker_instruments.currency`.

- [ ] **Step 1: Failing tests** (monkeypatch `yfinance`; no network)

```python
# append to backend/tests/unit/test_yahoo_bars.py
import sys
import types

from stockbrain.errors import ProviderResponseError
from stockbrain.market_data.yahoo import YahooDailyBars


class _Frame:
    """The four columns the adapter is allowed to read, shaped like a DataFrame."""

    def __init__(self, rows: list[tuple[dt.datetime, float, float, float, float, int]]) -> None:
        self._rows = rows
        self.empty = not rows

    def itertuples(self):  # noqa: ANN201 - mimics pandas
        for ts, o, h, lo, c, v in self._rows:
            yield types.SimpleNamespace(Index=ts, Open=o, High=h, Low=lo, Close=c, Volume=v)


def _install_fake_yfinance(monkeypatch: pytest.MonkeyPatch, *, frame: _Frame, currency: str) -> None:
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
    frame = _Frame([(t0, 1.0, 1.0, 1.0, 1.0, 1), (t0 + dt.timedelta(days=1), 1.0, 1.0, 1.0, 1.0, 1)])
    _install_fake_yfinance(monkeypatch, frame=frame, currency="")
    with pytest.raises(ProviderResponseError, match="currency"):
        await YahooDailyBars().daily_bars("X", days=5)


async def test_any_yfinance_exception_becomes_a_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Broken:
        def __init__(self, symbol: str) -> None:
            raise RuntimeError("Yahoo reshaped the payload again")

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_Broken))
    with pytest.raises(ProviderResponseError, match="yfinance"):
        await YahooDailyBars().daily_bars("X", days=5)
```

If the test suite's async mode requires a marker, match what `tests/unit/test_market_data.py` does.

- [ ] **Step 2: Run → fail on import of `YahooDailyBars`**

- [ ] **Step 3: Implement**

```python
# append to backend/stockbrain/market_data/yahoo.py
from typing import Protocol  # move to the import block


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
        raw_currency = info.get("currency") if hasattr(info, "get") else getattr(info, "currency", None)
        if not isinstance(raw_currency, str) or not raw_currency:
            raise ProviderResponseError(f"yfinance: no currency reported for {symbol}")
        currency = "GBX" if raw_currency == _PENCE else raw_currency.upper()

        bars: list[Bar] = []
        for row in frame.itertuples():
            stamp = row.Index
            if getattr(stamp, "tzinfo", None) is None:
                stamp = stamp.replace(tzinfo=dt.UTC)
            bars.append(
                Bar(
                    symbol=symbol,
                    timestamp=stamp,
                    open=Decimal(str(row.Open)),
                    high=Decimal(str(row.High)),
                    low=Decimal(str(row.Low)),
                    close=Decimal(str(row.Close)),
                    volume=int(row.Volume or 0),
                    feed="yahoo",
                )
            )
        bars.sort(key=lambda bar: bar.timestamp)
        return bars, currency

    async def daily_bars(self, symbol: str, *, days: int) -> tuple[Sequence[Bar], str]:
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._fetch, symbol, days), timeout=self._timeout)
        except ProviderError:
            raise
        except TimeoutError as exc:
            raise ProviderResponseError(f"yfinance: timed out after {self._timeout}s for {symbol}") from exc
        except Exception as exc:  # yfinance raises whatever Yahoo's payload makes it raise
            raise ProviderResponseError(f"yfinance: {type(exc).__name__}: {exc}") from exc
```

Pandas `Timestamp` rows may carry a tz; `.replace(tzinfo=...)` exists on both `Timestamp` and `datetime`. If mypy objects to `row.Index`, type `row` as `Any`.

- [ ] **Step 4: Add `ProviderName.YAHOO_BARS`** in `observability/health.py` after `ALPACA_MARKET_DATA`. Check whether `api/routes/health.py` or `settings_model.py` enumerates providers in a way a test asserts (grep `ProviderName` in `tests/`); if a test lists every member, add the new one there.

- [ ] **Step 5: Tests → PASS; mypy; ruff.**
- [ ] **Step 6: Commit** — `feat(market-data): add a fenced Yahoo daily-bars source`

---

### Task 3: Schema and thresholds

**Files:**
- Modify: `backend/stockbrain/db/models/portfolio.py` (`PositionPeak`)
- Create: `backend/alembic/versions/20260912_1200_position_peaks_atr.py`
- Modify: `backend/stockbrain/risk/config.py`, `backend/stockbrain/config.py`, `backend/stockbrain/api/settings_model.py`
- Test: `backend/tests/unit/test_risk_config.py`, `backend/tests/unit/test_config.py`

**Interfaces:**
- `PositionPeak.atr: Decimal | None` (Numeric 24,8), `atr_period: int | None`, `atr_currency: str | None` (String 3), `atr_as_of: dt.date | None` (Date), `atr_source: str | None` (Text).
- `RiskConfig.exit_atr_multiplier: Decimal = Decimal("3")`, `exit_atr_period: int = 14`, `exit_atr_max_age_days: int = 3`.
- `Settings`: `RISK_EXIT_ATR_MULTIPLIER`, `RISK_EXIT_ATR_PERIOD`, `RISK_EXIT_ATR_MAX_AGE_DAYS`, `VOLATILITY_REFRESH_ENABLED` (False), `VOLATILITY_REFRESH_INTERVAL_SECONDS` (21600.0, bounds 3600–86400), `VOLATILITY_BARS_DAYS` (90, bounds 30–365).

- [ ] **Step 1: Failing tests**

```python
# append to backend/tests/unit/test_risk_config.py
def test_atr_thresholds_change_the_policy_version() -> None:
    assert h.config().version != h.config(exit_atr_multiplier=Decimal("2.5")).version
    assert h.config().version != h.config(exit_atr_period=20).version

# append to backend/tests/unit/test_config.py (match the file's style and fixtures)
def test_volatility_refresh_ships_disabled(make_settings: Any) -> None:
    settings = make_settings()
    assert settings.volatility_refresh_enabled is False
    assert settings.risk_exit_atr_period >= 2
```

- [ ] **Step 2: Run → fail (unknown kwargs / attributes)**
- [ ] **Step 3: Model columns** — add to `PositionPeak` after `observations`:

```python
    atr: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 8))
    """Average true range in the instrument's own quote unit (pence for LSE),
    from research-grade daily bars.  ``None`` until the volatility refresh has
    run, and never a reference price."""

    atr_period: Mapped[int | None] = mapped_column(sa.Integer)
    atr_currency: Mapped[str | None] = mapped_column(sa.String(3))
    atr_as_of: Mapped[dt.date | None] = mapped_column(sa.Date)
    """Date of the last bar that fed the ATR.  Staleness is judged against it."""

    atr_source: Mapped[str | None] = mapped_column(sa.Text)
```

- [ ] **Step 4: Migration** — `<WS>/alembic.sh revision --autogenerate -m "position peaks atr"`, rename to `20260912_1200_position_peaks_atr.py`, confirm `down_revision = "2a109374b5c6"`, keep only the five `add_column` operations (and matching `drop_column`s in `downgrade`). `<WS>/alembic.sh check` and `heads` → one head.
- [ ] **Step 5: RiskConfig fields** after `exit_roi_decay`:

```python
    exit_atr_multiplier: Decimal = Decimal("3")
    """Chandelier width: the volatility floor sits this many ATRs below the peak."""

    exit_atr_period: int = 14
    exit_atr_max_age_days: int = 3
    """An ATR older than this (by its last bar's date) is not trusted; the rule
    skips and the flat hard stop stands.  Three days spans a weekend."""
```

Then `Settings` fields beside the other `risk_exit_*` / `exit_sweep_*` declarations, matching their style (bounds: multiplier `>0 and <=10`, period `2..60`, max age `1..14`), a `_validate_volatility_policy` after-validator enforcing those bounds in the house style, the three risk mappings in `risk_config_from_settings`, and six `RESTART_REQUIRED` rows in `settings_model.py`.

- [ ] **Step 6: Tests → PASS; also `tests/unit/test_settings_model.py`; mypy; ruff.**
- [ ] **Step 7: Commit** — `feat(risk): store ATR per open position and add volatility-stop thresholds`

---

### Task 4: The `volatility_stop` rule

**Files:**
- Modify: `backend/stockbrain/risk/exits.py`
- Test: `backend/tests/unit/test_risk_exits.py`

**Interfaces:**
- `ExitObservation` gains `atr: Decimal | None = None` and `atr_as_of: dt.date | None = None` (defaults, so existing constructors keep working).
- `EXIT_PRECEDENCE` becomes `("hard_stop", "volatility_stop", "trailing_stop", "thesis_superseded", "roi_target", "horizon_elapsed")`.

- [ ] **Step 1: Failing tests**

```python
# append to backend/tests/unit/test_risk_exits.py
def test_precedence_now_includes_the_volatility_stop_second() -> None:
    assert EXIT_PRECEDENCE == (
        "hard_stop", "volatility_stop", "trailing_stop",
        "thesis_superseded", "roi_target", "horizon_elapsed",
    )


def test_the_volatility_stop_fires_below_peak_minus_k_atr() -> None:
    # peak 110, ATR 2, k=3 → floor 104.  Price 103 is below it and above the -8% hard stop.
    signal = evaluate_exit(
        observe(current_price=Decimal("103"), peak_price=Decimal("110"), atr=Decimal("2"),
                atr_as_of=NOW.date()),
        h.config(), now=NOW,
    )
    assert signal is not None and signal.rule_id == "volatility_stop"
    assert signal.action is ThesisAction.SELL


def test_the_volatility_stop_holds_at_exactly_the_floor() -> None:
    signal = evaluate_exit(
        observe(current_price=Decimal("104"), peak_price=Decimal("110"), atr=Decimal("2"),
                atr_as_of=NOW.date()),
        h.config(), now=NOW,
    )
    assert signal is None


def test_a_stale_atr_is_ignored_and_the_flat_rules_stand() -> None:
    signal = evaluate_exit(
        observe(current_price=Decimal("103"), peak_price=Decimal("110"), atr=Decimal("2"),
                atr_as_of=NOW.date() - dt.timedelta(days=10)),
        h.config(), now=NOW,
    )
    assert signal is None  # 103 vs cost 100 is +3%: no hard stop, trailing not armed


def test_a_missing_atr_skips_the_rule() -> None:
    assert evaluate_exit(observe(current_price=Decimal("103"), peak_price=Decimal("110")), h.config(), now=NOW) is None


def test_the_hard_stop_still_outranks_the_volatility_stop() -> None:
    signal = evaluate_exit(
        observe(current_price=Decimal("80"), peak_price=Decimal("110"), atr=Decimal("2"),
                atr_as_of=NOW.date()),
        h.config(), now=NOW,
    )
    assert signal is not None and signal.rule_id == "hard_stop"


def test_a_volatile_name_gets_room_a_quiet_one_does_not() -> None:
    quiet = observe(current_price=Decimal("96"), peak_price=Decimal("100"), atr=Decimal("1"), atr_as_of=NOW.date())
    wild = observe(current_price=Decimal("96"), peak_price=Decimal("100"), atr=Decimal("3"), atr_as_of=NOW.date())
    assert evaluate_exit(quiet, h.config(), now=NOW) is not None  # floor 97
    assert evaluate_exit(wild, h.config(), now=NOW) is None  # floor 91


def test_the_volatility_stop_respects_min_peak_observations() -> None:
    signal = evaluate_exit(
        observe(current_price=Decimal("103"), peak_price=Decimal("110"), peak_observations=1,
                atr=Decimal("2"), atr_as_of=NOW.date()),
        h.config(), now=NOW,
    )
    assert signal is None
```

Update the existing `test_precedence_is_declared_and_complete` to the six-tuple (or delete it in favour of the new one — not both asserting different tuples).

- [ ] **Step 2: Run → fail (unknown field `atr`, precedence mismatch)**
- [ ] **Step 3: Implement** — in `exits.py`: add the two fields with defaults at the end of `ExitObservation`; update `EXIT_PRECEDENCE`; insert between rule 1 and rule 2:

```python
    # 2. volatility_stop -- a Chandelier floor, `k` ATRs under the high-water
    #    mark.  Volatility-scaled where the flat rules are not: a quiet name is
    #    stopped tight, a wild one is given room.  ATR is research-grade data
    #    and may be missing or stale; then this rule skips and the flat stops
    #    stand.  Needs the same trustworthy peak the trailing rule needs.
    peak = observation.peak_price
    atr = observation.atr
    atr_fresh = (
        observation.atr_as_of is not None
        and (now.date() - observation.atr_as_of).days <= config.exit_atr_max_age_days
    )
    if (
        peak is not None
        and atr is not None
        and atr > ZERO
        and atr_fresh
        and observation.peak_observations >= config.exit_min_peak_observations
    ):
        floor = peak - config.exit_atr_multiplier * atr
        if observation.current_price < floor:
            return ExitSignal(
                rule_id="volatility_stop",
                action=ThesisAction.SELL,
                reason=(
                    f"the price fell to {observation.current_price}, through the volatility "
                    f"floor at {floor} ({config.exit_atr_multiplier} x ATR {atr} below the "
                    f"peak of {peak})"
                ),
                rule=_fired(
                    "volatility_stop",
                    "the volatility floor was breached",
                    str(observation.current_price),
                    str(floor),
                ),
            )
```

Renumber the following comments (`# 3. trailing_stop` …). The existing `peak = observation.peak_price` line in the trailing rule can be removed since `peak` is now bound above.

- [ ] **Step 4: Tests → PASS (whole file); mypy; ruff.**
- [ ] **Step 5: Commit** — `feat(risk): add a volatility-scaled Chandelier stop ahead of the flat trailing stop`

---

### Task 5: The volatility refresh service and wiring

**Files:**
- Create: `backend/stockbrain/market_data/volatility.py`
- Modify: `backend/stockbrain/proposals/exits.py:126-137` (pass `atr`, `atr_as_of`)
- Modify: `backend/stockbrain/services.py`
- Test: `backend/tests/integration/test_volatility_refresh.py`, `backend/tests/unit/test_phase9_gates.py`

**Interfaces:**
- `VolatilityRefreshService(database, settings, *, bars: DailyBars, config: RiskConfig, health)` with `async def refresh(self, *, now: dt.datetime | None = None) -> dict[str, int]` tallies: `considered, refreshed, skipped_fresh, skipped_no_symbol, skipped_no_peak, currency_mismatch, insufficient_bars, failed`.
- Scheduled task name `volatility_refresh`, guarded on `settings.volatility_refresh_enabled`.

- [ ] **Step 1: Failing tests**

```python
# backend/tests/integration/test_volatility_refresh.py
"""ATR refresh: symbol mapping, currency assertion, staleness, and what the sweep sees."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa

from stockbrain.db.models.portfolio import PositionPeak
from stockbrain.db.session import Database
from stockbrain.market_data.base import Bar
from stockbrain.market_data.volatility import VolatilityRefreshService

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
            Bar(symbol=symbol, timestamp=t0 + dt.timedelta(days=i), open=Decimal("100"),
                high=Decimal("102"), low=Decimal("98"), close=Decimal("100"), volume=1)
            for i in range(self.n)
        ]
        return bars, self.currency


async def test_refresh_stores_an_atr_in_the_instruments_currency(clean_tables: Database) -> None:
    database = clean_tables
    await _seed_position_with_peak(database, broker_ticker="AAPL_US_EQ", exchange="NASDAQ", currency="USD")
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
    await _seed_position_with_peak(database, broker_ticker="VODl_EQ", exchange="London Stock Exchange", currency="GBX")
    service = _volatility_service(database, _FakeBars(currency="GBP"))

    counts = await service.refresh(now=NOW)

    assert counts["currency_mismatch"] == 1 and counts["refreshed"] == 0
    async with database.session() as session:
        peak = (await session.execute(sa.select(PositionPeak))).scalar_one()
    assert peak.atr is None


async def test_a_fresh_atr_is_not_refetched(clean_tables: Database) -> None:
    database = clean_tables
    await _seed_position_with_peak(database, broker_ticker="AAPL_US_EQ", exchange="NASDAQ", currency="USD")
    bars = _FakeBars()
    service = _volatility_service(database, bars)
    await service.refresh(now=NOW)
    counts = await service.refresh(now=NOW + dt.timedelta(hours=6))
    assert counts["skipped_fresh"] == 1 and len(bars.calls) == 1


async def test_an_unmapped_exchange_is_skipped_and_counted(clean_tables: Database) -> None:
    database = clean_tables
    await _seed_position_with_peak(database, broker_ticker="ABCx_EQ", exchange="Bourse de Casablanca", currency="MAD")
    counts = await _volatility_service(database, _FakeBars()).refresh(now=NOW)
    assert counts["skipped_no_symbol"] == 1


async def test_the_exit_sweep_sees_the_stored_atr(clean_tables: Database) -> None:
    """End to end: stored ATR → observation → volatility_stop proposal."""
    database = clean_tables
    # Cost 100, peak 110 (observed 5 times), price 103: below 110 - 3*2 = 104, above the -8% stop.
    await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")
    await _seed_position(database, broker_ticker="AAPL_US_EQ", average_price=Decimal("100"), current_price=Decimal("103"))
    await _seed_peak(database, broker_ticker="AAPL_US_EQ", peak_price=Decimal("110"), observations=5,
                     atr=Decimal("2"), atr_as_of=NOW.date())
    counts = await (await _exit_sweep(database)).sweep(now=NOW)
    assert counts["proposed"] == 1
    async with database.session() as session:
        rules = (await session.execute(sa.select(TradeProposal.risk_rules))).scalar_one()
    assert any(rule["rule_id"] == "volatility_stop" for rule in rules)
```

Helpers: reuse `_seed_executed_buy`, `_seed_position`, `_exit_sweep` from `tests/integration/test_exit_sweep.py` — import them if they are module-level, otherwise move them into `tests/proposal_helpers.py` (which already exists) and import from there in both files. Write `_seed_position_with_peak`, `_seed_peak` and `_volatility_service` locally; `_volatility_service` builds the service with a real `Database`, `ph.settings(...)`, the fake bars, `h.config()` and a `ProviderHealth` instance the way `services.py` constructs one (read it).

- [ ] **Step 2: Run → fail on import of `stockbrain.market_data.volatility`**
- [ ] **Step 3: Implement the service**

```python
# backend/stockbrain/market_data/volatility.py
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
from stockbrain.enums import Broker
from stockbrain.errors import ProviderError
from stockbrain.logging import get_logger
from stockbrain.market_data.yahoo import DailyBars, average_true_range, yahoo_symbol
from stockbrain.observability.health import ProviderHealth, ProviderName, ProviderStatus
from stockbrain.risk.config import RiskConfig

__all__ = ["VolatilityRefreshService"]

log = get_logger(__name__)
_TALLY_KEYS = (
    "considered", "refreshed", "skipped_fresh", "skipped_no_symbol", "skipped_no_peak",
    "currency_mismatch", "insufficient_bars", "failed",
)


class VolatilityRefreshService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        bars: DailyBars,
        config: RiskConfig,
        health: ProviderHealth,
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
        counts: Counter[str] = Counter({key: 0 for key in _TALLY_KEYS})

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
                    .join(BrokerInstrument, BrokerInstrument.broker_ticker == Position.broker_ticker, isouter=True)
                    .where(Position.broker == self._broker, Position.quantity > 0)
                    .order_by(Position.broker_ticker)
                )
            ).all()

        work: list[tuple[str, str, str | None, dt.date | None]] = []
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
            work.append((position.broker_ticker, symbol, position.currency, peak.atr_as_of))

        errors = 0
        for broker_ticker, symbol, instrument_currency, _previous in work:
            try:
                bars, currency = await self._bars.daily_bars(symbol, days=self._settings.volatility_bars_days)
            except ProviderError as exc:
                errors += 1
                counts["failed"] += 1
                log.warning("volatility_refresh_failed", broker_ticker=broker_ticker, symbol=symbol, error=str(exc)[:200])
                continue
            if instrument_currency is not None and currency != instrument_currency:
                counts["currency_mismatch"] += 1
                log.warning("volatility_currency_mismatch", broker_ticker=broker_ticker, symbol=symbol,
                            instrument=instrument_currency, bars=currency)
                continue
            atr = average_true_range(bars, self._config.exit_atr_period)
            if atr is None:
                counts["insufficient_bars"] += 1
                continue
            async with self._database.transaction() as session:
                await session.execute(
                    sa.update(PositionPeak)
                    .where(PositionPeak.broker == self._broker, PositionPeak.broker_ticker == broker_ticker)
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
```

Check the real names: `ProviderHealth` class and `ProviderStatus` members in `observability/health.py`; `BrokerInstrument.broker_ticker` column name in `db/models/companies.py`. Adapt the import/attribute names to what exists — do not invent.

- [ ] **Step 4: Sweep passes ATR** — in `proposals/exits.py` `ExitObservation(...)` construction add `atr=peak.atr if peak is not None else None, atr_as_of=peak.atr_as_of if peak is not None else None,`.
- [ ] **Step 5: Wire in `services.py`** — construct `YahooDailyBars()` and `VolatilityRefreshService(...)` beside `ExitSweepService` (guarded on `self.proposals is not None`; the `health` object is whatever the container already passes to other services); expose `self.volatility`; schedule:

```python
        if self.volatility is not None and self.settings.volatility_refresh_enabled:
            scheduler.add(
                ScheduledTask(
                    name="volatility_refresh",
                    interval_seconds=self.settings.volatility_refresh_interval_seconds,
                    run=self._volatility_refresh,
                    initial_delay_seconds=120.0,
                    jitter_ratio=0.1,
                )
            )
```

with `_volatility_refresh` beside `_exit_sweep`. Add to `tests/unit/test_phase9_gates.py`: `test_the_volatility_refresh_is_not_scheduled_by_default` asserting `settings.volatility_refresh_enabled is False`.

- [ ] **Step 6: Tests → PASS: the new file, `tests/integration/test_exit_sweep.py`, `tests/unit/test_phase9_gates.py`; mypy; ruff.**
- [ ] **Step 7: Commit** — `feat(market-data): refresh a daily ATR per open position and feed it to the exit sweep`

---

### Task 6: Docs, `.env.example`, full gate

**Files:** `docs/architecture.md`, `docs/operations.md`, `.env.example`

- [ ] **Step 1:** In `docs/architecture.md`'s exit section: the six-rule precedence; `volatility_stop` = Chandelier at `peak − k·ATR(14)` from Yahoo daily bars; that Yahoo is research-grade and never a reference price; that a missing/stale ATR makes the rule skip while `hard_stop` stands; that currency is asserted per instrument (pence).
- [ ] **Step 2:** In `docs/operations.md`: `VOLATILITY_REFRESH_ENABLED=true` to turn it on (the sweep must also be on); the three thresholds and what they mean; how to spot Yahoo degraded in System Health (`yahoo_bars`); the tallies in `volatility_refresh_complete`.
- [ ] **Step 3:** `.env.example`: the six new variables with defaults and one-line comments beside the existing `RISK_EXIT_*` / `EXIT_SWEEP_*` rows.
- [ ] **Step 4:** `<WS>/alembic.sh check && <WS>/alembic.sh heads` → no drift, one head. Then `cd backend && .venv/bin/ruff format --check . && .venv/bin/ruff check . && .venv/bin/mypy --strict stockbrain tests && cd .. && <WS>/pytest.sh -q` → expected > 2055 passed, 0 failed.
- [ ] **Step 5: Commit** — `docs: describe the volatility stop and its Yahoo daily-bar source`

---

## Self-review

**Spec coverage.** §12's "not execution-grade" is enforced structurally (no `PriceSource`, Task 2/5 store ATR only). §14's versioned thresholds — Task 3. §22 scheduler-only-enqueues — Task 5. §46 must-nots — nothing here touches a broker, an LLM, or credentials. Zero-token constraint — no imports from `stockbrain.llm`/`intelligence` anywhere in the file list.

**Placeholders.** Three "read and match" instructions, each naming the file: the async-test marker convention (Task 2), the `ProviderHealth`/`ProviderStatus`/`BrokerInstrument` attribute names (Task 5), and the seed helpers to share via `tests/proposal_helpers.py` (Task 5).

**Type consistency.** `DailyBars.daily_bars(symbol, *, days) -> (Sequence[Bar], str)` (Task 2) is what `VolatilityRefreshService` awaits (Task 5). `average_true_range(bars, period)` (Task 1) is called with `self._config.exit_atr_period` (Task 3 field). `ExitObservation.atr/atr_as_of` (Task 4) are populated from `PositionPeak.atr/atr_as_of` (Task 3) in Task 5. `exit_atr_max_age_days` is read only in Task 4.
