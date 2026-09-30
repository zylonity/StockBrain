"""Offline, point-in-time validation of cached news outcomes.

The functions here never read a database, call a provider, or create an order.
One canonical event is one observation, including events with missing outcomes.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from zoneinfo import ZoneInfo

from stockbrain.market_data.base import Bar
from stockbrain.market_data.news_outcomes import EventOutcome, OutcomeStatus

THRESHOLDS = (Decimal("0.005"), Decimal("0.01"), Decimal("0.02"))
MIN_COHORT = 10
SESSION_ZONE = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class HistoricalEvent:
    """One deduplicated event candidate and its reproducible candle calculation."""

    canonical_id: str
    symbol: str
    event_type: str
    published_at: dt.datetime
    live_received_at: dt.datetime | None
    outcome: EventOutcome
    stock_bars: Sequence[Bar]
    benchmark_bars: Sequence[Bar]
    article_count: int = 1


@dataclass(frozen=True, slots=True)
class ThresholdTiming:
    threshold: Decimal
    basis: str
    status: str
    first_at: dt.datetime | None
    clock_seconds: int | None
    trading_seconds: int | None
    phase: str


@dataclass(frozen=True, slots=True)
class CohortResult:
    total: int
    usable: int
    positive: int
    flat: int
    negative: int
    unavailable: int
    positive_probability: Decimal | None
    median_net_return: Decimal | None
    sparse: bool


@dataclass(frozen=True, slots=True)
class DeadlineEvidence:
    threshold: Decimal
    sample_size: int
    responders: int
    nonresponses: int
    unknown: int
    response_quantile_sessions: int | None
    buffer_sessions: int
    deadline_sessions_after_entry: int | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class FailedReactionAssessment:
    deadline_reached: bool
    observed_market_relative_move: Decimal | None
    expected_threshold: Decimal | None
    recommendation: str
    reason: str


@dataclass(frozen=True, slots=True)
class ExitComparison:
    sample_size: int
    deadline_exit_better: int
    thesis_hold_better: int
    tied: int
    median_net_advantage: Decimal | None
    automatic_exit_eligible: bool = False


def _aware(value: dt.datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _paired_closes(event: HistoricalEvent) -> list[tuple[dt.datetime, Decimal, Decimal]]:
    """Use only bars completed by as_of and exactly aligned benchmark closes."""
    benchmark = {
        bar.timestamp: bar
        for bar in event.benchmark_bars
        if bar.timestamp + dt.timedelta(minutes=1) <= event.outcome.as_of
    }
    result: list[tuple[dt.datetime, Decimal, Decimal]] = []
    for stock in sorted(event.stock_bars, key=lambda bar: bar.timestamp):
        if stock.timestamp + dt.timedelta(minutes=1) > event.outcome.as_of:
            continue
        market = benchmark.get(stock.timestamp)
        if market is None or stock.close <= 0 or market.close <= 0:
            continue
        result.append((stock.timestamp + dt.timedelta(minutes=1), stock.close, market.close))
    return result


def threshold_timing(
    event: HistoricalEvent, threshold: Decimal, *, basis: str = "publication"
) -> ThresholdTiming:
    """First observed relative upward crossing in a complete, aligned minute series.

    ``trading_seconds`` counts observed paired minutes; it is unknown if there
    are in-session gaps. A missing live receipt is never replaced by cache import.
    """
    if threshold <= 0 or basis not in {"publication", "entry"}:
        raise ValueError("threshold must be positive and basis must be publication or entry")
    _aware(event.published_at, "published_at")
    if event.live_received_at is not None:
        _aware(event.live_received_at, "live_received_at")
    move = event.outcome.publication_to_entry
    if basis == "publication":
        stock_ref, market_ref = move.reference, move.benchmark_reference
        start = event.published_at
    else:
        stock_ref, market_ref = move.entry, move.benchmark_entry
        start = event.outcome.entry.timestamp if event.outcome.entry else event.published_at
    if stock_ref is None or market_ref is None or stock_ref.price <= 0 or market_ref.price <= 0:
        return ThresholdTiming(threshold, basis, "unknown_reference", None, None, None, "unknown")
    five_day = event.outcome.horizon("5d")
    cutoff = (
        five_day.checkpoint.timestamp + dt.timedelta(minutes=1)
        if five_day and five_day.checkpoint
        else event.outcome.as_of
    )
    paired = [row for row in _paired_closes(event) if start < row[0] <= cutoff]
    if not paired:
        return ThresholdTiming(threshold, basis, "missing_bars", None, None, None, "unknown")
    last: dt.datetime | None = None
    trading_seconds = 0
    gap = False
    for time, stock_close, market_close in paired:
        if last is not None:
            delta = time - last
            if time.astimezone(SESSION_ZONE).date() == last.astimezone(SESSION_ZONE).date():
                if delta > dt.timedelta(minutes=5):
                    gap = True
                trading_seconds += min(int(delta.total_seconds()), 60)
            else:
                trading_seconds += 60
        else:
            trading_seconds = 60
        last = time
        # The same arithmetic market-relative return the outcome calculator
        # and the catalyst monitor use, so a deadline is assessed as derived.
        relative = stock_close / stock_ref.price - market_close / market_ref.price
        if relative < threshold:
            continue
        if event.live_received_at is None:
            phase = "live_receipt_unknown"
        elif time <= event.live_received_at:
            phase = "before_live_receipt"
        elif event.outcome.entry is None:
            phase = "entry_unknown"
        elif time <= event.outcome.entry.timestamp:
            phase = "before_feasible_entry"
        else:
            phase = "after_feasible_entry"
        return ThresholdTiming(
            threshold,
            basis,
            "crossed",
            time,
            int((time - start).total_seconds()),
            None if gap else trading_seconds,
            phase,
        )
    # An observed nonresponse needs a complete five-session thesis window.
    status = (
        "not_reached"
        if five_day and five_day.status is OutcomeStatus.OK and not gap
        else "unknown_incomplete"
    )
    return ThresholdTiming(threshold, basis, status, None, None, None, "none")


def comparable_entry_cohort(
    target: HistoricalEvent,
    history: Sequence[HistoricalEvent],
    *,
    horizon: str = "1d",
    max_delay_difference: dt.timedelta = dt.timedelta(minutes=10),
    max_prior_move_difference: Decimal = Decimal("0.005"),
    min_samples: int = MIN_COHORT,
) -> CohortResult:
    """Net results for all earlier exact-type events with similar arrival state.

    A receipt-less event cannot enter a live-delay cohort. Zero return is flat.
    Missing cost or FX adjustment remains unavailable, never zero.
    """
    if target.live_received_at is None:
        return CohortResult(0, 0, 0, 0, 0, 0, None, None, True)
    _aware(target.live_received_at, "target live_received_at")
    target_move = target.outcome.publication_to_entry.market_relative_return
    if target_move is None:
        return CohortResult(0, 0, 0, 0, 0, 0, None, None, True)
    target_delay = target.live_received_at - target.published_at
    seen: set[str] = set()
    values: list[Decimal] = []
    total = unavailable = 0
    for item in history:
        if item.canonical_id == target.canonical_id or item.canonical_id in seen:
            continue
        seen.add(item.canonical_id)
        if item.published_at >= target.published_at or item.event_type != target.event_type:
            continue
        if item.live_received_at is None:
            continue
        delay = item.live_received_at - item.published_at
        move = item.outcome.publication_to_entry.market_relative_return
        if move is None or abs(delay - target_delay) > max_delay_difference:
            continue
        if abs(move - target_move) > max_prior_move_difference:
            continue
        total += 1
        result = item.outcome.horizon(horizon)
        if (
            result is None
            or result.status is not OutcomeStatus.OK
            or result.market_relative_return_net is None
        ):
            unavailable += 1
        else:
            values.append(result.market_relative_return_net)
    positive = sum(value > 0 for value in values)
    flat = sum(value == 0 for value in values)
    negative = sum(value < 0 for value in values)
    usable = len(values)
    sparse = usable < min_samples
    return CohortResult(
        total,
        usable,
        positive,
        flat,
        negative,
        unavailable,
        Decimal(positive) / Decimal(usable) if usable and not sparse else None,
        median(values) if values and not sparse else None,
        sparse,
    )


def derive_catalyst_deadline(
    events: Sequence[HistoricalEvent],
    *,
    threshold: Decimal = Decimal("0.01"),
    buffer_sessions: int = 1,
    minimum_samples: int = MIN_COHORT,
    response_fraction: Decimal = Decimal("0.75"),
) -> DeadlineEvidence:
    """Return a session count only when enough *all-event* evidence responds.

    Nonresponses enter the denominator. An unknown observation cannot be
    counted as a failure or as a response. The deadline is fixed at entry.
    """
    if buffer_sessions < 0 or not Decimal(0) < response_fraction <= Decimal(1):
        raise ValueError("invalid deadline parameters")
    seen: set[str] = set()
    response_sessions: list[int] = []
    nonresponses = unknown = 0
    for item in events:
        if item.canonical_id in seen:
            continue
        seen.add(item.canonical_id)
        five_day = item.outcome.horizon("5d")
        if five_day is None or five_day.status is not OutcomeStatus.OK:
            unknown += 1
            continue
        timing = threshold_timing(item, threshold, basis="entry")
        if timing.status == "crossed" and timing.first_at and item.outcome.entry:
            entry_date = item.outcome.entry.timestamp.astimezone(SESSION_ZONE).date()
            crossing_date = timing.first_at.astimezone(SESSION_ZONE).date()
            dates = sorted(
                {bar.timestamp.astimezone(SESSION_ZONE).date() for bar in item.stock_bars}
            )
            response_sessions.append(sum(entry_date < date <= crossing_date for date in dates))
        elif timing.status == "not_reached":
            nonresponses += 1
        else:
            unknown += 1
    sample_size = len(response_sessions) + nonresponses
    reason: str | None = None
    quantile: int | None = None
    if sample_size < minimum_samples:
        reason = "sparse_comparable_history"
    elif Decimal(len(response_sessions)) / Decimal(sample_size) < response_fraction:
        reason = "response_fraction_below_threshold"
    else:
        ordered = sorted(response_sessions)
        rank = (Decimal(sample_size) * response_fraction).to_integral_value(
            rounding="ROUND_CEILING"
        )
        quantile = ordered[int(rank) - 1]
    return DeadlineEvidence(
        threshold,
        sample_size,
        len(response_sessions),
        nonresponses,
        unknown,
        quantile,
        buffer_sessions,
        quantile + buffer_sessions if quantile is not None else None,
        reason,
    )


def assess_failed_reaction(
    evidence: DeadlineEvidence,
    *,
    elapsed_trading_sessions: int,
    observed_market_relative_move: Decimal | None,
    new_evidence_sustains_thesis: bool,
) -> FailedReactionAssessment:
    """Describe a fresh position reassessment; never issue a sell instruction."""
    if elapsed_trading_sessions < 0:
        raise ValueError("elapsed_trading_sessions must be nonnegative")
    deadline = evidence.deadline_sessions_after_entry
    if deadline is None:
        return FailedReactionAssessment(
            False,
            observed_market_relative_move,
            None,
            "NO_QUANTITATIVE_DEADLINE",
            evidence.reason or "insufficient_history",
        )
    if elapsed_trading_sessions < deadline:
        return FailedReactionAssessment(
            False, observed_market_relative_move, evidence.threshold, "WAIT", "deadline_not_reached"
        )
    if observed_market_relative_move is None:
        return FailedReactionAssessment(
            True, None, evidence.threshold, "REASSESS", "reaction_measurement_missing"
        )
    if observed_market_relative_move >= evidence.threshold:
        return FailedReactionAssessment(
            True,
            observed_market_relative_move,
            evidence.threshold,
            "REASSESS",
            "expected_reaction_observed",
        )
    if new_evidence_sustains_thesis:
        return FailedReactionAssessment(
            True,
            observed_market_relative_move,
            evidence.threshold,
            "REASSESS",
            "new_evidence_sustains_thesis",
        )
    return FailedReactionAssessment(
        True,
        observed_market_relative_move,
        evidence.threshold,
        "FAVOUR_SELL_OR_REDUCE_REASSESSMENT",
        "expected_reaction_failed",
    )


def compare_deadline_exit_to_hold(
    net_pairs: Sequence[tuple[Decimal, Decimal]], *, min_samples: int = MIN_COHORT
) -> ExitComparison:
    """Offline paired comparison of net deadline exit versus original thesis hold.

    Each pair must include all trading and FX costs under the same assumptions.
    The output never authorizes automatic exit, even with a favourable median.
    """
    differences = [deadline - hold for deadline, hold in net_pairs]
    return ExitComparison(
        len(differences),
        sum(value > 0 for value in differences),
        sum(value < 0 for value in differences),
        sum(value == 0 for value in differences),
        median(differences) if len(differences) >= min_samples else None,
    )


def validation_counts(
    events: Sequence[HistoricalEvent], *, as_of: dt.datetime, validation_months: int = 6
) -> dict[str, dict[str, int]]:
    """Auditable 24-month event counts with a fixed chronological holdout."""
    _aware(as_of, "as_of")
    if validation_months != 6:
        raise ValueError("the registered holdout is six months")

    # Calendar-month boundaries avoid a drifting 30-day approximation.
    def months_ago(moment: dt.datetime, count: int) -> dt.datetime:
        month_index = moment.year * 12 + moment.month - 1 - count
        year, month_index = divmod(month_index, 12)
        month = month_index + 1
        import calendar

        day = min(moment.day, calendar.monthrange(year, month)[1])
        return moment.replace(year=year, month=month, day=day)

    start, split = months_ago(as_of, 24), months_ago(as_of, 6)
    counts: dict[str, Counter[str]] = {"exploration": Counter(), "validation": Counter()}
    seen: set[str] = set()
    for item in events:
        if item.canonical_id in seen or not start <= item.published_at < as_of:
            continue
        seen.add(item.canonical_id)
        bucket = "exploration" if item.published_at < split else "validation"
        counts[bucket]["events"] += 1
        counts[bucket][f"symbol_{item.symbol}"] += 1
        if item.outcome.status is not OutcomeStatus.OK:
            counts[bucket]["unavailable_entries"] += 1
        for name in ("1h", "1d", "5d"):
            result = item.outcome.horizon(name)
            if result is not None and result.market_relative_return_net is not None:
                counts[bucket][f"{name}_net_usable"] += 1
            else:
                counts[bucket][f"{name}_net_unknown"] += 1
    return {name: dict(count) for name, count in counts.items()}
