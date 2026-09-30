"""Deterministic, market-relative news-to-candle outcomes.

This module answers one narrow, purely arithmetic question for a single
historical news event:

* how far did the instrument move **relative to a benchmark** between the
  publication and the first moment a trade could actually have been entered,
  and
* what happened over the next trading hour, one trading session and five
  trading sessions after that entry?

Everything here is a pure function of the arguments.  There is no clock, no
provider, no database and no LLM: the caller supplies timestamped OHLCV bars
(and, optionally, FX bars and a quoted spread) and receives a compact,
serialisable result.  That is deliberate.  The number this produces is
*evidence* to be aggregated, never a decision, and a measurement that cannot
be reproduced from its inputs is not evidence.

The conservatism is the feature, so it is stated up front:

* **Timestamps must be timezone-aware.**  A naive datetime is rejected with
  ``ValueError`` rather than silently assumed to be UTC; guessing here would
  shift every entry by hours and never announce itself.
* **A bar is used only once it is complete.**  A one-minute bar stamped
  ``t`` is eligible only when ``t + bar_interval <= as_of``.  Bars at or
  after ``as_of`` are never consulted, so a backtest cannot peek.
* **The entry is the open of the first completed bar at or after
  ``published_at + latency``** (default five minutes).  When the market is
  shut at that moment the next available bar is the next session's open,
  which is recorded as a session gap rather than hidden.  A long gap *inside*
  a session is a data defect and yields an unavailable reason.
* **Sessions are counted from bar dates**, in ``session_timezone``, never from
  calendar days.  The trading calendar is the union of the instrument's and
  benchmark's bar dates, so a missing session is visible as a missing session
  rather than as a calendar coincidence.
* **Raw unadjusted split discontinuities are excluded.**  A bar-to-bar price
  ratio close to a simple split factor marks the affected window unavailable;
  an unadjusted 2:1 split must never be reported as a -50% return.
* **The market-relative move is explicit about cost.**  The quoted spread is
  charged once for the round trip and FX is charged at its own explicit rate;
  both are recorded as assumptions, along with the FX quote convention.
* **A single event is never a signal.**  :func:`aggregate_outcomes` refuses to
  emit a signal below ``min_samples`` observations.

The result types are frozen, slotted dataclasses with ``as_dict`` methods that
return only JSON-native values (Decimals as strings, datetimes as ISO-8601),
so the outcome can be stored or shipped without a second translation layer.

Assumptions this module makes and does not try to derive:

* The bars are **minute** bars by default (``bar_interval`` is configurable).
* The benchmark is already denominated in the reporting currency; when the
  instrument's currency differs, the supplied ``fx_bars`` are the
  benchmark-currency value of one instrument-currency unit, aligned to the
  instrument's bar timestamps.
* The trading calendar is inferred from the bars themselves.  Exchange
  holidays therefore appear exactly as they appear in the data: as absent
  session dates.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from stockbrain.market_data.base import Bar

__all__ = [
    "DEFAULT_BAR_INTERVAL",
    "DEFAULT_LATENCY",
    "DEFAULT_SESSION_TIMEZONE",
    "HORIZON_DAY_1",
    "HORIZON_DAY_5",
    "HORIZON_HOUR_1",
    "MIN_SIGNAL_SAMPLES",
    "ConfoundingPolicy",
    "EventOutcome",
    "HorizonAggregate",
    "HorizonOutcome",
    "NewsOutcomeInputs",
    "OutcomeAggregate",
    "OutcomeStatus",
    "PricePoint",
    "PublicationEntryMove",
    "SplitDiscontinuity",
    "aggregate_outcomes",
    "compute_news_outcome",
    "detect_split_discontinuities",
]

#: The default publication-to-entry latency: how long after the news a trade
#: could plausibly have been placed.  Five minutes is a conservative floor, not
#: a claim about execution speed.
DEFAULT_LATENCY = dt.timedelta(minutes=5)

#: The bar aggregation the inputs are expected to carry.
DEFAULT_BAR_INTERVAL = dt.timedelta(minutes=1)

#: Session dates are taken in this exchange-local zone.  A UTC date would split
#: a US after-hours session across two days and silently merge the wrong bars.
DEFAULT_SESSION_TIMEZONE = "America/New_York"

#: Below this many usable observations no aggregate is allowed to call itself a
#: signal.  The number is a floor against over-reading a handful of events, not
#: a statistical promise.
MIN_SIGNAL_SAMPLES = 30

HORIZON_HOUR_1 = "1h"
HORIZON_DAY_1 = "1d"
HORIZON_DAY_5 = "5d"

#: The post-entry windows, in the order results are reported.
HORIZONS: tuple[str, ...] = (HORIZON_HOUR_1, HORIZON_DAY_1, HORIZON_DAY_5)

_RETURN_QUANTUM = Decimal("0.00000001")

#: Bar-to-bar price ratios close to any of these are treated as unadjusted
#: split discontinuities.  Integer factors cover ordinary splits; the simple
#: fractions cover the 3:2 and 5:2 flavours that do occur.
_SPLIT_FACTORS: tuple[Decimal, ...] = (
    Decimal(2),
    Decimal(3),
    Decimal(4),
    Decimal(5),
    Decimal(6),
    Decimal(7),
    Decimal(8),
    Decimal(9),
    Decimal(10),
    Decimal(12),
    Decimal("0.5"),
    Decimal("0.3333333333333333333333333333"),
    Decimal("0.25"),
    Decimal("0.2"),
    Decimal("0.1666666666666666666666666667"),
    Decimal("0.1428571428571428571428571429"),
    Decimal("0.125"),
    Decimal("0.1111111111111111111111111111"),
    Decimal("0.1"),
    Decimal("0.0833333333333333333333333333"),
    Decimal("1.5"),
    Decimal("0.6666666666666666666666666667"),
    Decimal("1.3333333333333333333333333333"),
    Decimal("0.75"),
    Decimal("2.5"),
    Decimal("0.4"),
    Decimal("1.25"),
    Decimal("0.8"),
)


def _quantize(value: Decimal) -> Decimal:
    """Round a return to eight decimal places, or leave an extreme value alone."""
    try:
        return value.quantize(_RETURN_QUANTUM)
    except InvalidOperation:  # pragma: no cover - only for absurd magnitudes
        return value


def _safe_return(entry: Decimal, exit_: Decimal) -> Decimal | None:
    """``exit / entry - 1`` as a quantized Decimal, or None for a broken price."""
    if not entry.is_finite() or not exit_.is_finite() or entry <= 0 or exit_ <= 0:
        return None
    return _quantize(exit_ / entry - Decimal(1))


def _require_aware(moment: dt.datetime, label: str) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _session_date(moment: dt.datetime, tz: ZoneInfo) -> dt.date:
    return moment.astimezone(tz).date()


class OutcomeStatus(StrEnum):
    """Whether an outcome could be measured, and if not, why not.

    ``OK`` is the only value that carries a market-relative return.  Every
    other value is an honest refusal: the window was not complete, the data
    was missing, a split or a second publication contaminated it, or the FX
    needed to compare the two series was absent.
    """

    OK = "OK"
    EVENT_IN_FUTURE = "EVENT_IN_FUTURE"
    NOT_ELAPSED = "NOT_ELAPSED"
    NO_ENTRY_BAR = "NO_ENTRY_BAR"
    ENTRY_GAP = "ENTRY_GAP"
    NO_REFERENCE_BAR = "NO_REFERENCE_BAR"
    STALE_BAR = "STALE_BAR"
    MISSING_BARS = "MISSING_BARS"
    SESSION_GAP = "SESSION_GAP"
    SPLIT_DISCONTINUITY = "SPLIT_DISCONTINUITY"
    CONFOUNDED = "CONFOUNDED"
    MISSING_FX = "MISSING_FX"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class ConfoundingPolicy(StrEnum):
    """What to do when a second publication lands inside a measurement window."""

    EXCLUDE = "EXCLUDE"
    """Mark the contaminated window unavailable.  The conservative default."""

    FLAG = "FLAG"
    """Measure the window but set ``confounded=True`` so it can be filtered."""


@dataclass(frozen=True, slots=True)
class PricePoint:
    """One price at one instant, and the bar component it was read from."""

    timestamp: dt.datetime
    price: Decimal
    basis: str
    """``open`` or ``close`` -- which side of the bar the number is."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "price": str(self.price),
            "basis": self.basis,
        }


@dataclass(frozen=True, slots=True)
class SplitDiscontinuity:
    """A bar-to-bar ratio that looks like an unadjusted split."""

    at: dt.datetime
    ratio: Decimal
    factor: Decimal
    direction: str
    """``forward`` (price fell) or ``reverse`` (price rose)."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "ratio": str(self.ratio),
            "factor": str(self.factor),
            "direction": self.direction,
        }


@dataclass(frozen=True, slots=True)
class PublicationEntryMove:
    """The market-relative move from the pre-publication price to the entry.

    This is the part of the reaction that was *not* capturable: it is the gap
    between the last price before the news and the first price at which a trade
    could have been placed.  A large positive value means the tradeable move
    happened before entry.
    """

    status: OutcomeStatus
    reference: PricePoint | None = None
    entry: PricePoint | None = None
    benchmark_reference: PricePoint | None = None
    benchmark_entry: PricePoint | None = None
    instrument_return: Decimal | None = None
    benchmark_return: Decimal | None = None
    market_relative_return: Decimal | None = None
    instrument_return_net: Decimal | None = None
    market_relative_return_net: Decimal | None = None
    confounded: bool = False
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reference": self.reference.as_dict() if self.reference else None,
            "entry": self.entry.as_dict() if self.entry else None,
            "benchmark_reference": (
                self.benchmark_reference.as_dict() if self.benchmark_reference else None
            ),
            "benchmark_entry": self.benchmark_entry.as_dict() if self.benchmark_entry else None,
            "instrument_return": _decimal_str(self.instrument_return),
            "benchmark_return": _decimal_str(self.benchmark_return),
            "market_relative_return": _decimal_str(self.market_relative_return),
            "instrument_return_net": _decimal_str(self.instrument_return_net),
            "market_relative_return_net": _decimal_str(self.market_relative_return_net),
            "confounded": self.confounded,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class HorizonOutcome:
    """One post-entry window's market-relative outcome."""

    horizon: str
    status: OutcomeStatus
    entry: PricePoint | None = None
    checkpoint: PricePoint | None = None
    benchmark_entry: PricePoint | None = None
    benchmark_checkpoint: PricePoint | None = None
    instrument_return: Decimal | None = None
    benchmark_return: Decimal | None = None
    market_relative_return: Decimal | None = None
    instrument_return_net: Decimal | None = None
    market_relative_return_net: Decimal | None = None
    session_date: dt.date | None = None
    confounded: bool = False
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "horizon": self.horizon,
            "status": self.status.value,
            "session_date": self.session_date.isoformat() if self.session_date else None,
            "entry": self.entry.as_dict() if self.entry else None,
            "checkpoint": self.checkpoint.as_dict() if self.checkpoint else None,
            "benchmark_entry": (self.benchmark_entry.as_dict() if self.benchmark_entry else None),
            "benchmark_checkpoint": (
                self.benchmark_checkpoint.as_dict() if self.benchmark_checkpoint else None
            ),
            "instrument_return": _decimal_str(self.instrument_return),
            "benchmark_return": _decimal_str(self.benchmark_return),
            "market_relative_return": _decimal_str(self.market_relative_return),
            "instrument_return_net": _decimal_str(self.instrument_return_net),
            "market_relative_return_net": _decimal_str(self.market_relative_return_net),
            "confounded": self.confounded,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class EventOutcome:
    """Everything measurable about one event, with its own provenance.

    A single event is a sample, never a signal: ``is_signal_eligible`` is
    always False here, and aggregation is what decides otherwise.

    ``status`` is ``OK`` when the event could be *anchored* -- a completed entry
    bar exists -- and is otherwise the single blocking status shared by every
    component.  The per-component statuses are authoritative once an entry
    exists: of two events both reporting ``OK`` here, one may have a clean 1h
    window and a split-contaminated 5d window while the other is clean
    throughout.  Consumers must inspect :attr:`publication_to_entry` and each
    :class:`HorizonOutcome`, never the top-level status alone.
    """

    event_id: str
    published_at: dt.datetime
    as_of: dt.datetime
    status: OutcomeStatus
    entry: PricePoint | None
    entry_delay_seconds: float | None
    entry_was_session_gap: bool
    publication_to_entry: PublicationEntryMove
    horizons: tuple[HorizonOutcome, ...]
    splits: tuple[SplitDiscontinuity, ...] = ()
    feed: str | None = None
    benchmark_feed: str | None = None
    flags: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    is_signal_eligible: bool = False
    signal_blocker: str | None = "single_event"

    def horizon(self, name: str) -> HorizonOutcome | None:
        """The outcome for ``1h``/``1d``/``5d``, or None when absent."""
        for outcome in self.horizons:
            if outcome.horizon == name:
                return outcome
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "published_at": self.published_at.isoformat(),
            "as_of": self.as_of.isoformat(),
            "status": self.status.value,
            "entry": self.entry.as_dict() if self.entry else None,
            "entry_delay_seconds": self.entry_delay_seconds,
            "entry_was_session_gap": self.entry_was_session_gap,
            "publication_to_entry": self.publication_to_entry.as_dict(),
            "horizons": [outcome.as_dict() for outcome in self.horizons],
            "splits": [split.as_dict() for split in self.splits],
            "feed": self.feed,
            "benchmark_feed": self.benchmark_feed,
            "flags": list(self.flags),
            "assumptions": list(self.assumptions),
            "reasons": list(self.reasons),
            "is_signal_eligible": self.is_signal_eligible,
            "signal_blocker": self.signal_blocker,
        }


def _decimal_str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class NewsOutcomeInputs:
    """The complete, immutable input to :func:`compute_news_outcome`.

    ``stock_bars`` and ``benchmark_bars`` are the only required series.  FX
    bars are needed only when ``instrument_currency`` and ``benchmark_currency``
    differ; ``spread_bps`` is optional and defaults to no spread cost.
    """

    event_id: str
    published_at: dt.datetime
    as_of: dt.datetime
    stock_bars: Sequence[Bar]
    benchmark_bars: Sequence[Bar]
    latency: dt.timedelta = DEFAULT_LATENCY
    bar_interval: dt.timedelta = DEFAULT_BAR_INTERVAL
    session_timezone: str = DEFAULT_SESSION_TIMEZONE
    expected_close_time: dt.time | None = None
    spread_bps: Decimal | None = None
    fx_cost_bps: Decimal = Decimal(0)
    fx_bars: Sequence[Bar] = ()
    instrument_currency: str | None = None
    benchmark_currency: str | None = None
    confounding_publications: Sequence[dt.datetime] = ()
    confounding_policy: ConfoundingPolicy = ConfoundingPolicy.EXCLUDE
    max_intraday_gap: dt.timedelta = dt.timedelta(minutes=5)
    max_entry_gap: dt.timedelta = dt.timedelta(minutes=30)
    max_session_gap: dt.timedelta = dt.timedelta(days=5)
    max_reference_staleness: dt.timedelta = dt.timedelta(days=4)
    max_fx_gap: dt.timedelta = dt.timedelta(hours=4)
    detect_splits: bool = True
    split_min_ratio: Decimal = Decimal("1.4")
    split_tolerance: Decimal = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class HorizonAggregate:
    """A per-horizon summary that refuses to speak below ``min_samples``."""

    horizon: str
    samples: int
    mean_market_relative_return_net: Decimal | None = None
    median_market_relative_return_net: Decimal | None = None
    hit_rate: Decimal | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "horizon": self.horizon,
            "samples": self.samples,
            "mean_market_relative_return_net": _decimal_str(self.mean_market_relative_return_net),
            "median_market_relative_return_net": _decimal_str(
                self.median_market_relative_return_net
            ),
            "hit_rate": _decimal_str(self.hit_rate),
        }


@dataclass(frozen=True, slots=True)
class OutcomeAggregate:
    """The tiny-sample guard around a set of event outcomes."""

    total_events: int
    usable_events: int
    min_samples: int
    is_signal_eligible: bool
    blocker: str | None
    horizons: tuple[HorizonAggregate, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_events": self.total_events,
            "usable_events": self.usable_events,
            "min_samples": self.min_samples,
            "is_signal_eligible": self.is_signal_eligible,
            "blocker": self.blocker,
            "horizons": [horizon.as_dict() for horizon in self.horizons],
        }


def _bar_is_valid(bar: Bar) -> bool:
    values = (bar.open, bar.high, bar.low, bar.close)
    if not all(value.is_finite() and value > 0 for value in values):
        return False
    if bar.volume < 0:
        return False
    if bar.high < bar.low:
        return False
    if bar.open > bar.high or bar.open < bar.low:
        return False
    return not (bar.close > bar.high or bar.close < bar.low)


def _eligible_bars(
    bars: Sequence[Bar],
    interval: dt.timedelta,
    as_of: dt.datetime,
    flags: set[str],
    label: str,
) -> tuple[Bar, ...]:
    """Completed, well-formed, timezone-aware bars, oldest first.

    A bar is complete only when ``timestamp + interval <= as_of``; a bar at or
    after ``as_of`` is not consulted.  Duplicates and malformed bars are
    dropped and the fact is flagged rather than hidden.

    Timezone-awareness is checked before sorting so a mixed naive/aware series
    fails with the useful error rather than an opaque ``TypeError`` from the
    comparison.
    """
    for bar in bars:
        _require_aware(bar.timestamp, f"{label} bar timestamp")
    ordered = sorted(bars, key=lambda bar: bar.timestamp)
    result: list[Bar] = []
    seen: set[dt.datetime] = set()
    dropped = 0
    for bar in ordered:
        if bar.timestamp in seen:
            dropped += 1
            continue
        seen.add(bar.timestamp)
        if bar.timestamp + interval > as_of:
            continue
        if not _bar_is_valid(bar):
            dropped += 1
            continue
        result.append(bar)
    if dropped:
        flags.add(f"{label}_bars_dropped")
    return tuple(result)


def _first_at_or_after(bars: Sequence[Bar], moment: dt.datetime) -> Bar | None:
    for bar in bars:
        if bar.timestamp >= moment:
            return bar
    return None


def _last_at_or_before(bars: Sequence[Bar], moment: dt.datetime) -> Bar | None:
    candidate: Bar | None = None
    for bar in bars:
        if bar.timestamp <= moment:
            candidate = bar
        else:
            break
    return candidate


def _single_feed(bars: Sequence[Bar]) -> str | None:
    feeds = {bar.feed for bar in bars if bar.feed}
    if len(feeds) == 1:
        return next(iter(feeds))
    return None


def _feed_flags(
    stock: Sequence[Bar], benchmark: Sequence[Bar], flags: set[str]
) -> tuple[str | None, str | None]:
    stock_feeds = {bar.feed for bar in stock if bar.feed}
    benchmark_feeds = {bar.feed for bar in benchmark if bar.feed}
    if len(stock_feeds) > 1:
        flags.add("stock_feed_mismatch")
    if len(benchmark_feeds) > 1:
        flags.add("benchmark_feed_mismatch")
    if len(stock_feeds) == 1 and len(benchmark_feeds) == 1 and stock_feeds != benchmark_feeds:
        flags.add("benchmark_feed_mismatch_across_series")
    return _single_feed(stock), _single_feed(benchmark)


def detect_split_discontinuities(
    bars: Sequence[Bar],
    *,
    min_ratio: Decimal = Decimal("1.4"),
    tolerance: Decimal = Decimal("0.01"),
) -> tuple[SplitDiscontinuity, ...]:
    """Find bar-to-bar ratios close to a simple split factor.

    Only the ratio between the previous close and the current open is examined,
    because a split shows up as an opening discontinuity; normal intra-session
    movement is orders of magnitude smaller.  ``min_ratio`` keeps modest moves
    from being reclassified, and ``tolerance`` is a relative one.
    """
    discontinuities: list[SplitDiscontinuity] = []
    for previous, current in pairwise(bars):
        if previous.close <= 0:
            continue
        ratio = current.open / previous.close
        for factor in _SPLIT_FACTORS:
            if abs(ratio - factor) / factor > tolerance:
                continue
            if ratio < min_ratio and ratio > Decimal(1) / min_ratio:
                break
            discontinuities.append(
                SplitDiscontinuity(
                    at=current.timestamp,
                    ratio=_quantize(ratio),
                    factor=factor,
                    direction="forward" if factor < 1 else "reverse",
                )
            )
            break
    return tuple(discontinuities)


@dataclass(frozen=True, slots=True)
class _Context:
    stock: tuple[Bar, ...]
    benchmark: tuple[Bar, ...]
    fx: tuple[Bar, ...]
    interval: dt.timedelta
    tz: ZoneInfo
    expected_close_time: dt.time | None
    as_of: dt.datetime
    max_intraday_gap: dt.timedelta
    max_fx_gap: dt.timedelta
    needs_fx: bool
    spread_cost: Decimal
    fx_cost: Decimal
    splits: tuple[SplitDiscontinuity, ...]
    confounders: tuple[dt.datetime, ...]
    policy: ConfoundingPolicy
    calendar: tuple[dt.date, ...]


def _fx_rate_at(context: _Context, instant: dt.datetime) -> Decimal | None:
    """The alignment FX rate for an instant, or None when there is none.

    The nearest FX bar at or before the instant is preferred; a bar just after
    it is an acceptable substitute.  A bar further than ``max_fx_gap`` away is
    treated as absent rather than stretched across the gap.
    """
    if not context.needs_fx:
        return Decimal(1)
    chosen: Bar | None = None
    for bar in context.fx:
        if bar.timestamp <= instant:
            chosen = bar
        else:
            break
    if chosen is None:
        chosen = _first_at_or_after(context.fx, instant)
    if chosen is None:
        return None
    if abs((chosen.timestamp - instant).total_seconds()) > context.max_fx_gap.total_seconds():
        return None
    return chosen.close


def _converted_instrument_return(
    context: _Context,
    entry_price: Decimal,
    exit_price: Decimal,
    entry_fx: Decimal | None,
    exit_fx: Decimal | None,
) -> tuple[Decimal | None, bool]:
    """Instrument return, converted into the benchmark currency when needed.

    Returns ``(return, missing_fx)``.  ``missing_fx`` is True only when the
    conversion was required and a rate was not available.
    """
    if context.needs_fx:
        if entry_fx is None or exit_fx is None:
            return None, True
        return _safe_return(entry_price * entry_fx, exit_price * exit_fx), False
    return _safe_return(entry_price, exit_price), False


def _net_of_costs(context: _Context, gross: Decimal | None) -> Decimal | None:
    if gross is None:
        return None
    cost = context.spread_cost
    if context.needs_fx:
        cost += context.fx_cost
    return _quantize(gross - cost)


def _confounded(context: _Context, low: dt.datetime, high: dt.datetime) -> bool:
    """A second publication strictly after ``low`` and at or before ``high``."""
    return any(low < moment <= high for moment in context.confounders)


def _split_in(context: _Context, low: dt.datetime, high: dt.datetime) -> SplitDiscontinuity | None:
    for split in context.splits:
        if low < split.at <= high:
            return split
    return None


def _has_gap(bars: Sequence[Bar], interval: dt.timedelta, max_gap: dt.timedelta) -> bool:
    """True when consecutive bars within one session are further apart than allowed."""
    for previous, current in pairwise(bars):
        if current.timestamp - previous.timestamp > max(interval, max_gap):
            return True
    return False


def _session_bars(bars: Sequence[Bar], date: dt.date, tz: ZoneInfo) -> list[Bar]:
    return [bar for bar in bars if _session_date(bar.timestamp, tz) == date]


def _calendar(stock: Sequence[Bar], benchmark: Sequence[Bar], tz: ZoneInfo) -> tuple[dt.date, ...]:
    dates: set[dt.date] = set()
    for bar in stock:
        dates.add(_session_date(bar.timestamp, tz))
    for bar in benchmark:
        dates.add(_session_date(bar.timestamp, tz))
    return tuple(sorted(dates))


def _assumptions(inputs: NewsOutcomeInputs) -> tuple[str, ...]:
    currency_note = "currencies match; no FX conversion applied"
    if (
        inputs.instrument_currency is not None
        and inputs.benchmark_currency is not None
        and inputs.instrument_currency.strip().upper() != inputs.benchmark_currency.strip().upper()
    ):
        currency_note = (
            "FX bars are the benchmark-currency value of one instrument-currency "
            "unit, aligned to the instrument bar timestamps"
        )
    return (
        f"entry is the open of the first completed bar at or after publication + {inputs.latency}",
        "the entry bar and every checkpoint bar must be complete "
        f"(timestamp + {inputs.bar_interval} <= as_of); later bars are never used",
        "the 1h horizon is the first completed bar at or after entry + 1h in the "
        "same session; a window that would cross a session boundary is unavailable",
        "the 1d and 5d horizons are the last completed bar (session close) of the "
        "1st and 5th distinct trading session after the entry session",
        "trading sessions are counted from bar dates in "
        f"{inputs.session_timezone}, not from calendar days",
        "missing sessions are detected against the union of instrument and benchmark bar dates",
        f"raw unadjusted split discontinuities (bar-to-bar ratio within "
        f"{inputs.split_tolerance} of a simple split factor) are excluded from the "
        "affected window when detect_splits is set",
        "spread cost is charged once for the round trip as spread_bps/10000 on the instrument",
        f"FX cost is charged once for the round trip as fx_cost_bps/10000; {currency_note}",
        f"publications inside a window are {inputs.confounding_policy.value.lower()}"
        "ed (excluded by default)",
    )


def _reasons_for_split(split: SplitDiscontinuity | None) -> tuple[str, ...]:
    if split is None:
        return ()
    return (f"unadjusted split discontinuity at {split.at.isoformat()} (ratio {split.ratio})",)


def _unavailable_move(
    status: OutcomeStatus,
    reasons: tuple[str, ...],
    *,
    entry: PricePoint | None = None,
    confounded: bool = False,
) -> PublicationEntryMove:
    return PublicationEntryMove(
        status=status,
        entry=entry,
        confounded=confounded,
        reasons=reasons,
    )


def _unavailable_horizon(
    horizon: str,
    status: OutcomeStatus,
    reasons: tuple[str, ...],
    *,
    entry: PricePoint | None = None,
    confounded: bool = False,
    session_date: dt.date | None = None,
) -> HorizonOutcome:
    return HorizonOutcome(
        horizon=horizon,
        status=status,
        entry=entry,
        session_date=session_date,
        confounded=confounded,
        reasons=reasons,
    )


def _compute_intraday_horizon(
    context: _Context,
    entry_bar: Bar,
    entry_fx: Decimal | None,
) -> HorizonOutcome:
    """The 1-trading-hour outcome."""
    entry = PricePoint(entry_bar.timestamp, entry_bar.open, "open")
    entry_date = _session_date(entry_bar.timestamp, context.tz)
    checkpoint_target = entry_bar.timestamp + dt.timedelta(hours=1)

    if checkpoint_target > context.as_of:
        return _unavailable_horizon(
            HORIZON_HOUR_1,
            OutcomeStatus.NOT_ELAPSED,
            (f"1h checkpoint {checkpoint_target.isoformat()} is after as_of",),
            entry=entry,
            session_date=entry_date,
        )
    if _session_date(checkpoint_target, context.tz) != entry_date:
        return _unavailable_horizon(
            HORIZON_HOUR_1,
            OutcomeStatus.SESSION_GAP,
            ("the 1h window would cross a session boundary",),
            entry=entry,
            session_date=entry_date,
        )

    checkpoint_bar = _first_at_or_after(context.stock, checkpoint_target)
    if checkpoint_bar is None:
        return _unavailable_horizon(
            HORIZON_HOUR_1,
            OutcomeStatus.MISSING_BARS,
            ("no completed stock bar at or after the 1h checkpoint",),
            entry=entry,
            session_date=entry_date,
        )
    if _session_date(checkpoint_bar.timestamp, context.tz) != entry_date:
        return _unavailable_horizon(
            HORIZON_HOUR_1,
            OutcomeStatus.SESSION_GAP,
            ("the first bar at or after the 1h checkpoint is in a later session",),
            entry=entry,
            session_date=entry_date,
        )
    if checkpoint_bar.timestamp - checkpoint_target > context.max_intraday_gap:
        return _unavailable_horizon(
            HORIZON_HOUR_1,
            OutcomeStatus.MISSING_BARS,
            (
                f"the first stock bar after the 1h checkpoint is "
                f"{checkpoint_bar.timestamp - checkpoint_target} late",
            ),
            entry=entry,
            session_date=entry_date,
        )

    window = [
        bar
        for bar in context.stock
        if entry_bar.timestamp <= bar.timestamp <= checkpoint_bar.timestamp
    ]
    if _has_gap(window, context.interval, context.max_intraday_gap):
        return _unavailable_horizon(
            HORIZON_HOUR_1,
            OutcomeStatus.MISSING_BARS,
            ("a gap larger than the tolerance sits inside the 1h window",),
            entry=entry,
            session_date=entry_date,
        )

    split = _split_in(context, entry_bar.timestamp, checkpoint_bar.timestamp)
    if split is not None:
        return _unavailable_horizon(
            HORIZON_HOUR_1,
            OutcomeStatus.SPLIT_DISCONTINUITY,
            _reasons_for_split(split),
            entry=entry,
            session_date=entry_date,
        )

    confounded = _confounded(context, entry_bar.timestamp, checkpoint_bar.timestamp)
    if confounded and context.policy is ConfoundingPolicy.EXCLUDE:
        return _unavailable_horizon(
            HORIZON_HOUR_1,
            OutcomeStatus.CONFOUNDED,
            ("a confounding publication falls inside the 1h window",),
            entry=entry,
            confounded=True,
            session_date=entry_date,
        )

    benchmark_entry_bar = _first_at_or_after(context.benchmark, entry_bar.timestamp)
    benchmark_checkpoint_bar = _first_at_or_after(context.benchmark, checkpoint_target)
    if (
        benchmark_entry_bar is None
        or benchmark_checkpoint_bar is None
        or _session_date(benchmark_entry_bar.timestamp, context.tz) != entry_date
        or _session_date(benchmark_checkpoint_bar.timestamp, context.tz) != entry_date
        or benchmark_entry_bar.timestamp - entry_bar.timestamp > context.max_intraday_gap
        or benchmark_checkpoint_bar.timestamp - checkpoint_target > context.max_intraday_gap
    ):
        return _unavailable_horizon(
            HORIZON_HOUR_1,
            OutcomeStatus.MISSING_BARS,
            ("the benchmark has no completed bar spanning the 1h window",),
            entry=entry,
            confounded=confounded,
            session_date=entry_date,
        )
    benchmark_window = [
        bar
        for bar in context.benchmark
        if benchmark_entry_bar.timestamp <= bar.timestamp <= benchmark_checkpoint_bar.timestamp
    ]
    if _has_gap(benchmark_window, context.interval, context.max_intraday_gap):
        return _unavailable_horizon(
            HORIZON_HOUR_1,
            OutcomeStatus.MISSING_BARS,
            ("a benchmark gap larger than the tolerance sits inside the 1h window",),
            entry=entry,
            confounded=confounded,
            session_date=entry_date,
        )

    checkpoint = PricePoint(checkpoint_bar.timestamp, checkpoint_bar.open, "open")
    benchmark_entry = PricePoint(benchmark_entry_bar.timestamp, benchmark_entry_bar.open, "open")
    benchmark_checkpoint = PricePoint(
        benchmark_checkpoint_bar.timestamp, benchmark_checkpoint_bar.open, "open"
    )
    return _finalise_horizon(
        context,
        HORIZON_HOUR_1,
        entry,
        checkpoint,
        benchmark_entry,
        benchmark_checkpoint,
        entry_fx,
        _fx_rate_at(context, checkpoint_bar.timestamp),
        confounded=confounded,
        session_date=entry_date,
    )


def _compute_session_horizon(
    context: _Context,
    horizon: str,
    sessions: int,
    entry_bar: Bar,
    entry_fx: Decimal | None,
) -> HorizonOutcome:
    """The 1d or 5d outcome, counted in trading sessions from bar dates."""
    entry = PricePoint(entry_bar.timestamp, entry_bar.open, "open")
    entry_date = _session_date(entry_bar.timestamp, context.tz)
    future = [date for date in context.calendar if date > entry_date]
    if len(future) < sessions:
        return _unavailable_horizon(
            horizon,
            OutcomeStatus.NOT_ELAPSED,
            (
                f"only {len(future)} of {sessions} trading sessions after the entry "
                f"session exist at or before as_of",
            ),
            entry=entry,
            session_date=entry_date,
        )
    target_date = future[sessions - 1]
    required = future[:sessions]
    if target_date >= _session_date(context.as_of, context.tz):
        return _unavailable_horizon(
            horizon,
            OutcomeStatus.NOT_ELAPSED,
            (f"the target session {target_date.isoformat()} has not closed by as_of",),
            entry=entry,
            session_date=target_date,
        )

    missing_sessions = [
        date
        for date in required
        if not _session_bars(context.stock, date, context.tz)
        or not _session_bars(context.benchmark, date, context.tz)
    ]
    if missing_sessions:
        return _unavailable_horizon(
            horizon,
            OutcomeStatus.MISSING_BARS,
            (
                "missing instrument or benchmark bars for session(s) "
                + ", ".join(date.isoformat() for date in missing_sessions),
            ),
            entry=entry,
            session_date=target_date,
        )

    stock_session = _session_bars(context.stock, target_date, context.tz)
    benchmark_session = _session_bars(context.benchmark, target_date, context.tz)
    if _has_gap(stock_session, context.interval, context.max_intraday_gap) or _has_gap(
        benchmark_session, context.interval, context.max_intraday_gap
    ):
        return _unavailable_horizon(
            horizon,
            OutcomeStatus.MISSING_BARS,
            ("a gap larger than the tolerance sits inside the target session",),
            entry=entry,
            session_date=target_date,
        )

    checkpoint_bar = stock_session[-1]
    benchmark_checkpoint_bar = benchmark_session[-1]
    if context.expected_close_time is not None:
        for last in (checkpoint_bar, benchmark_checkpoint_bar):
            completed_local = (last.timestamp + context.interval).astimezone(context.tz).time()
            if completed_local < context.expected_close_time:
                return _unavailable_horizon(
                    horizon,
                    OutcomeStatus.MISSING_BARS,
                    ("target session has no bar completed at the expected close",),
                    entry=entry,
                    session_date=target_date,
                )
    checkpoint = PricePoint(checkpoint_bar.timestamp, checkpoint_bar.close, "close")
    benchmark_entry_bar = _first_at_or_after(context.benchmark, entry_bar.timestamp)
    if benchmark_entry_bar is None:
        return _unavailable_horizon(
            horizon,
            OutcomeStatus.MISSING_BARS,
            ("the benchmark has no completed bar at the entry",),
            entry=entry,
            session_date=target_date,
        )
    benchmark_entry = PricePoint(benchmark_entry_bar.timestamp, benchmark_entry_bar.open, "open")
    benchmark_checkpoint = PricePoint(
        benchmark_checkpoint_bar.timestamp, benchmark_checkpoint_bar.close, "close"
    )

    split = _split_in(context, entry_bar.timestamp, checkpoint_bar.timestamp)
    if split is not None:
        return _unavailable_horizon(
            horizon,
            OutcomeStatus.SPLIT_DISCONTINUITY,
            _reasons_for_split(split),
            entry=entry,
            session_date=target_date,
        )

    confounded = _confounded(context, entry_bar.timestamp, checkpoint_bar.timestamp)
    if confounded and context.policy is ConfoundingPolicy.EXCLUDE:
        return _unavailable_horizon(
            horizon,
            OutcomeStatus.CONFOUNDED,
            ("a confounding publication falls inside the window",),
            entry=entry,
            confounded=True,
            session_date=target_date,
        )

    return _finalise_horizon(
        context,
        horizon,
        entry,
        checkpoint,
        benchmark_entry,
        benchmark_checkpoint,
        entry_fx,
        _fx_rate_at(context, checkpoint_bar.timestamp),
        confounded=confounded,
        session_date=target_date,
    )


def _finalise_horizon(
    context: _Context,
    horizon: str,
    entry: PricePoint,
    checkpoint: PricePoint,
    benchmark_entry: PricePoint,
    benchmark_checkpoint: PricePoint,
    entry_fx: Decimal | None,
    checkpoint_fx: Decimal | None,
    *,
    confounded: bool,
    session_date: dt.date,
) -> HorizonOutcome:
    instrument_return, missing_fx = _converted_instrument_return(
        context, entry.price, checkpoint.price, entry_fx, checkpoint_fx
    )
    benchmark_return = _safe_return(benchmark_entry.price, benchmark_checkpoint.price)
    if missing_fx:
        return HorizonOutcome(
            horizon=horizon,
            status=OutcomeStatus.MISSING_FX,
            entry=entry,
            checkpoint=checkpoint,
            benchmark_entry=benchmark_entry,
            benchmark_checkpoint=benchmark_checkpoint,
            benchmark_return=benchmark_return,
            session_date=session_date,
            confounded=confounded,
            reasons=("FX conversion is required but no aligned FX bar was available",),
        )
    if instrument_return is None or benchmark_return is None:
        return HorizonOutcome(
            horizon=horizon,
            status=OutcomeStatus.INSUFFICIENT_DATA,
            entry=entry,
            checkpoint=checkpoint,
            benchmark_entry=benchmark_entry,
            benchmark_checkpoint=benchmark_checkpoint,
            session_date=session_date,
            confounded=confounded,
            reasons=("a non-positive price left the return undefined",),
        )
    instrument_return_net = _net_of_costs(context, instrument_return)
    market_relative = _quantize(instrument_return - benchmark_return)
    market_relative_net = (
        _quantize(instrument_return_net - benchmark_return)
        if instrument_return_net is not None
        else None
    )
    return HorizonOutcome(
        horizon=horizon,
        status=OutcomeStatus.OK,
        entry=entry,
        checkpoint=checkpoint,
        benchmark_entry=benchmark_entry,
        benchmark_checkpoint=benchmark_checkpoint,
        instrument_return=instrument_return,
        benchmark_return=benchmark_return,
        market_relative_return=market_relative,
        instrument_return_net=instrument_return_net,
        market_relative_return_net=market_relative_net,
        session_date=session_date,
        confounded=confounded,
    )


def _empty_horizon(horizon: str, status: OutcomeStatus, reason: str) -> HorizonOutcome:
    return HorizonOutcome(horizon=horizon, status=status, reasons=(reason,))


def _blocked_result(
    inputs: NewsOutcomeInputs,
    status: OutcomeStatus,
    reasons: tuple[str, ...],
    *,
    flags: set[str],
    assumptions: tuple[str, ...],
    splits: tuple[SplitDiscontinuity, ...] = (),
    feed: str | None = None,
    benchmark_feed: str | None = None,
    entry: PricePoint | None = None,
    entry_delay_seconds: float | None = None,
    entry_was_session_gap: bool = False,
) -> EventOutcome:
    return EventOutcome(
        event_id=inputs.event_id,
        published_at=inputs.published_at,
        as_of=inputs.as_of,
        status=status,
        entry=entry,
        entry_delay_seconds=entry_delay_seconds,
        entry_was_session_gap=entry_was_session_gap,
        publication_to_entry=_unavailable_move(status, reasons, entry=entry),
        horizons=tuple(_empty_horizon(horizon, status, reasons[0]) for horizon in HORIZONS),
        splits=splits,
        feed=feed,
        benchmark_feed=benchmark_feed,
        flags=tuple(sorted(flags)),
        assumptions=assumptions,
        reasons=reasons,
    )


def _currency_needed(inputs: NewsOutcomeInputs, flags: set[str]) -> bool:
    instrument = inputs.instrument_currency
    benchmark = inputs.benchmark_currency
    if instrument is None or benchmark is None:
        if instrument != benchmark:
            flags.add("currency_unverified")
        return False
    if instrument.strip().upper() == benchmark.strip().upper():
        return False
    flags.add("fx_conversion_applied")
    return True


def _spread_cost(inputs: NewsOutcomeInputs) -> Decimal:
    if inputs.spread_bps is None:
        return Decimal(0)
    return _quantize(Decimal(inputs.spread_bps) / Decimal(10000))


def _fx_cost(inputs: NewsOutcomeInputs) -> Decimal:
    return _quantize(Decimal(inputs.fx_cost_bps) / Decimal(10000))


def compute_news_outcome(inputs: NewsOutcomeInputs) -> EventOutcome:
    """Measure one event's publication-to-entry and post-entry outcomes.

    Pure: every fact comes from ``inputs``, including ``as_of``.  The result is
    always an :class:`EventOutcome`; failures are expressed as statuses and
    reasons, not exceptions (except for programmer errors such as a naive
    timestamp or a non-positive interval, which raise ``ValueError``).
    """
    _require_aware(inputs.published_at, "published_at")
    _require_aware(inputs.as_of, "as_of")
    if inputs.bar_interval <= dt.timedelta(0):
        raise ValueError("bar_interval must be positive")
    if inputs.latency < dt.timedelta(0):
        raise ValueError("latency must not be negative")
    for moment in inputs.confounding_publications:
        _require_aware(moment, "confounding publication")

    tz = ZoneInfo(inputs.session_timezone)
    flags: set[str] = set()
    assumptions = _assumptions(inputs)

    stock = _eligible_bars(inputs.stock_bars, inputs.bar_interval, inputs.as_of, flags, "stock")
    benchmark = _eligible_bars(
        inputs.benchmark_bars, inputs.bar_interval, inputs.as_of, flags, "benchmark"
    )
    fx = (
        _eligible_bars(inputs.fx_bars, inputs.bar_interval, inputs.as_of, flags, "fx")
        if inputs.fx_bars
        else ()
    )
    feed, benchmark_feed = _feed_flags(stock, benchmark, flags)
    needs_fx = _currency_needed(inputs, flags)
    splits = (
        detect_split_discontinuities(
            stock,
            min_ratio=inputs.split_min_ratio,
            tolerance=inputs.split_tolerance,
        )
        if inputs.detect_splits
        else ()
    )
    if splits:
        flags.add("split_discontinuities_detected")

    context = _Context(
        stock=stock,
        benchmark=benchmark,
        fx=fx,
        interval=inputs.bar_interval,
        tz=tz,
        expected_close_time=inputs.expected_close_time,
        as_of=inputs.as_of,
        max_intraday_gap=inputs.max_intraday_gap,
        max_fx_gap=inputs.max_fx_gap,
        needs_fx=needs_fx,
        spread_cost=_spread_cost(inputs),
        fx_cost=_fx_cost(inputs),
        splits=splits,
        confounders=tuple(
            moment for moment in inputs.confounding_publications if moment != inputs.published_at
        ),
        policy=inputs.confounding_policy,
        calendar=_calendar(stock, benchmark, tz),
    )

    if inputs.published_at > inputs.as_of:
        return _blocked_result(
            inputs,
            OutcomeStatus.EVENT_IN_FUTURE,
            ("publication timestamp is after as_of",),
            flags=flags,
            assumptions=assumptions,
            splits=splits,
            feed=feed,
            benchmark_feed=benchmark_feed,
        )

    if not stock:
        return _blocked_result(
            inputs,
            OutcomeStatus.INSUFFICIENT_DATA,
            ("no completed instrument bars were supplied",),
            flags=flags,
            assumptions=assumptions,
            splits=splits,
            feed=feed,
            benchmark_feed=benchmark_feed,
        )

    target = inputs.published_at + inputs.latency
    if target > inputs.as_of:
        return _blocked_result(
            inputs,
            OutcomeStatus.NOT_ELAPSED,
            ("the publication-to-entry latency has not elapsed by as_of",),
            flags=flags,
            assumptions=assumptions,
            splits=splits,
            feed=feed,
            benchmark_feed=benchmark_feed,
        )

    entry_bar = _first_at_or_after(stock, target)
    if entry_bar is None:
        return _blocked_result(
            inputs,
            OutcomeStatus.NO_ENTRY_BAR,
            ("no completed instrument bar exists at or after the entry target",),
            flags=flags,
            assumptions=assumptions,
            splits=splits,
            feed=feed,
            benchmark_feed=benchmark_feed,
        )

    target_date = _session_date(target, tz)
    entry_date = _session_date(entry_bar.timestamp, tz)
    entry_delay = entry_bar.timestamp - target
    target_local_time = target.astimezone(tz).time()
    regular_close = inputs.expected_close_time or dt.time(16, 0)
    bars_before_target = [
        bar
        for bar in stock
        if (
            target_local_time < regular_close
            and _session_date(bar.timestamp, tz) == target_date
            and bar.timestamp <= target
        )
    ]
    if entry_delay > inputs.max_entry_gap and (
        bars_before_target or entry_delay > inputs.max_session_gap
    ):
        return _blocked_result(
            inputs,
            OutcomeStatus.ENTRY_GAP,
            (
                f"the first instrument bar after the entry target is {entry_delay} late, "
                "which is a data gap rather than a closed market",
            ),
            flags=flags,
            assumptions=assumptions,
            splits=splits,
            feed=feed,
            benchmark_feed=benchmark_feed,
        )

    entry = PricePoint(entry_bar.timestamp, entry_bar.open, "open")
    entry_fx = _fx_rate_at(context, entry_bar.timestamp)
    entry_was_session_gap = entry_date != target_date or entry_delay > inputs.max_intraday_gap
    reasons: list[str] = []

    reference_bar = _last_at_or_before(stock, inputs.published_at)
    if reference_bar is None:
        publication_to_entry = _unavailable_move(
            OutcomeStatus.NO_REFERENCE_BAR,
            ("no completed instrument bar exists at or before publication",),
            entry=entry,
        )
    elif inputs.published_at - reference_bar.timestamp > inputs.max_reference_staleness:
        publication_to_entry = _unavailable_move(
            OutcomeStatus.STALE_BAR,
            (
                f"the last bar before publication is "
                f"{inputs.published_at - reference_bar.timestamp} old",
            ),
            entry=entry,
        )
    else:
        reference = PricePoint(reference_bar.timestamp, reference_bar.close, "close")
        benchmark_reference_bar = _last_at_or_before(benchmark, inputs.published_at)
        benchmark_entry_bar = _first_at_or_after(benchmark, target)
        if benchmark_reference_bar is None or benchmark_entry_bar is None:
            publication_to_entry = _unavailable_move(
                OutcomeStatus.MISSING_BARS,
                ("the benchmark has no completed bar spanning publication to entry",),
                entry=entry,
            )
        else:
            reference_fx = _fx_rate_at(context, reference_bar.timestamp)
            benchmark_reference = PricePoint(
                benchmark_reference_bar.timestamp, benchmark_reference_bar.close, "close"
            )
            benchmark_entry = PricePoint(
                benchmark_entry_bar.timestamp, benchmark_entry_bar.open, "open"
            )
            split = _split_in(context, reference_bar.timestamp, entry_bar.timestamp)
            confounded = _confounded(context, inputs.published_at, entry_bar.timestamp)
            instrument_return, missing_fx = _converted_instrument_return(
                context,
                reference_bar.close,
                entry_bar.open,
                reference_fx,
                entry_fx,
            )
            benchmark_return = _safe_return(benchmark_reference_bar.close, benchmark_entry_bar.open)
            if split is not None:
                publication_to_entry = PublicationEntryMove(
                    status=OutcomeStatus.SPLIT_DISCONTINUITY,
                    reference=reference,
                    entry=entry,
                    benchmark_reference=benchmark_reference,
                    benchmark_entry=benchmark_entry,
                    confounded=confounded,
                    reasons=_reasons_for_split(split),
                )
            elif confounded and context.policy is ConfoundingPolicy.EXCLUDE:
                publication_to_entry = PublicationEntryMove(
                    status=OutcomeStatus.CONFOUNDED,
                    reference=reference,
                    entry=entry,
                    benchmark_reference=benchmark_reference,
                    benchmark_entry=benchmark_entry,
                    confounded=True,
                    reasons=("a confounding publication falls between publication and entry",),
                )
            elif missing_fx:
                publication_to_entry = PublicationEntryMove(
                    status=OutcomeStatus.MISSING_FX,
                    reference=reference,
                    entry=entry,
                    benchmark_reference=benchmark_reference,
                    benchmark_entry=benchmark_entry,
                    benchmark_return=benchmark_return,
                    confounded=confounded,
                    reasons=("FX conversion is required but no aligned FX bar was available",),
                )
            elif instrument_return is None or benchmark_return is None:
                publication_to_entry = PublicationEntryMove(
                    status=OutcomeStatus.INSUFFICIENT_DATA,
                    reference=reference,
                    entry=entry,
                    benchmark_reference=benchmark_reference,
                    benchmark_entry=benchmark_entry,
                    confounded=confounded,
                    reasons=("a non-positive price left the return undefined",),
                )
            else:
                instrument_return_net = _net_of_costs(context, instrument_return)
                market_relative = _quantize(instrument_return - benchmark_return)
                market_relative_net = (
                    _quantize(instrument_return_net - benchmark_return)
                    if instrument_return_net is not None
                    else None
                )
                publication_to_entry = PublicationEntryMove(
                    status=OutcomeStatus.OK,
                    reference=reference,
                    entry=entry,
                    benchmark_reference=benchmark_reference,
                    benchmark_entry=benchmark_entry,
                    instrument_return=instrument_return,
                    benchmark_return=benchmark_return,
                    market_relative_return=market_relative,
                    instrument_return_net=instrument_return_net,
                    market_relative_return_net=market_relative_net,
                    confounded=confounded,
                )

    if splits:
        reasons.append(f"{len(splits)} split discontinuity candidate(s) detected in the bars")

    horizons = (
        _compute_intraday_horizon(context, entry_bar, entry_fx),
        _compute_session_horizon(context, HORIZON_DAY_1, 1, entry_bar, entry_fx),
        _compute_session_horizon(context, HORIZON_DAY_5, 5, entry_bar, entry_fx),
    )

    return EventOutcome(
        event_id=inputs.event_id,
        published_at=inputs.published_at,
        as_of=inputs.as_of,
        status=OutcomeStatus.OK,
        entry=entry,
        entry_delay_seconds=entry_delay.total_seconds(),
        entry_was_session_gap=entry_was_session_gap,
        publication_to_entry=publication_to_entry,
        horizons=horizons,
        splits=splits,
        feed=feed,
        benchmark_feed=benchmark_feed,
        flags=tuple(sorted(flags)),
        assumptions=assumptions,
        reasons=tuple(reasons),
    )


def _decimal_mean(values: Sequence[Decimal]) -> Decimal:
    return _quantize(sum(values, Decimal(0)) / Decimal(len(values)))


def _decimal_median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return _quantize((ordered[middle - 1] + ordered[middle]) / Decimal(2))


def aggregate_outcomes(
    outcomes: Sequence[EventOutcome],
    *,
    min_samples: int = MIN_SIGNAL_SAMPLES,
    include_confounded: bool = False,
) -> OutcomeAggregate:
    """Summarise many events, refusing to call a tiny sample a signal.

    Only ``OK`` horizons contribute.  A confounded horizon is dropped unless
    ``include_confounded`` is set.  Below ``min_samples`` usable observations a
    horizon's statistics are withheld entirely (``None``) and the aggregate's
    ``is_signal_eligible`` is False with a ``tiny_sample`` blocker.
    """
    if min_samples <= 0:
        raise ValueError("min_samples must be positive")
    usable = sum(
        1
        for outcome in outcomes
        if any(horizon.status is OutcomeStatus.OK for horizon in outcome.horizons)
    )
    horizon_aggregates: list[HorizonAggregate] = []
    for name in HORIZONS:
        values: list[Decimal] = []
        for outcome in outcomes:
            horizon = outcome.horizon(name)
            if horizon is None or horizon.status is not OutcomeStatus.OK:
                continue
            if horizon.confounded and not include_confounded:
                continue
            if horizon.market_relative_return_net is None:
                continue
            values.append(horizon.market_relative_return_net)
        if len(values) >= min_samples:
            wins = sum(1 for value in values if value > 0)
            horizon_aggregates.append(
                HorizonAggregate(
                    horizon=name,
                    samples=len(values),
                    mean_market_relative_return_net=_decimal_mean(values),
                    median_market_relative_return_net=_decimal_median(values),
                    hit_rate=_quantize(Decimal(wins) / Decimal(len(values))),
                )
            )
        else:
            horizon_aggregates.append(HorizonAggregate(horizon=name, samples=len(values)))

    # This first version is descriptive research only. Even a large sample does
    # not make this cohort a validated trading signal.
    eligible = False
    blocker = (
        "no_samples" if usable == 0 else "tiny_sample" if usable < min_samples else "research_only"
    )
    return OutcomeAggregate(
        total_events=len(outcomes),
        usable_events=usable,
        min_samples=min_samples,
        is_signal_eligible=eligible,
        blocker=blocker,
        horizons=tuple(horizon_aggregates),
    )
