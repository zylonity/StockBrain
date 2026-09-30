"""Deterministic news-to-candle outcomes.

These tests pin the conservative behaviours rather than the arithmetic alone:
the exact entry instant after latency, a weekend publication entering the next
session open, a data gap inside a session refusing to be measured, a future
window staying unavailable, unadjusted splits being excluded, market-relative
returns against a benchmark, explicit FX and spread costs, and a confounding
publication either excluding or flagging the window.  Every timestamp that must
be timezone-aware is asserted to be, and a naive one is rejected outright.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest

from stockbrain.market_data.base import Bar
from stockbrain.market_data.news_outcomes import (
    DEFAULT_LATENCY,
    HORIZON_DAY_1,
    HORIZON_DAY_5,
    HORIZON_HOUR_1,
    MIN_SIGNAL_SAMPLES,
    ConfoundingPolicy,
    EventOutcome,
    HorizonOutcome,
    NewsOutcomeInputs,
    OutcomeStatus,
    aggregate_outcomes,
    compute_news_outcome,
    detect_split_discontinuities,
)

FRIDAY = dt.datetime(2026, 9, 4, tzinfo=dt.UTC)
SATURDAY = dt.datetime(2026, 9, 5, tzinfo=dt.UTC)
MONDAY = dt.datetime(2026, 9, 7, tzinfo=dt.UTC)
TUESDAY = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
WEDNESDAY = dt.datetime(2026, 9, 9, tzinfo=dt.UTC)
THURSDAY = dt.datetime(2026, 9, 10, tzinfo=dt.UTC)
FRIDAY_2 = dt.datetime(2026, 9, 11, tzinfo=dt.UTC)
MONDAY_2 = dt.datetime(2026, 9, 14, tzinfo=dt.UTC)

PUBLICATION = FRIDAY + dt.timedelta(hours=14)  # 10:00 ET

#: US regular session in UTC: 13:30 through 19:59 (09:30-15:59 ET).
SESSION_START = 810
SESSION_COUNT = 390


def _bar(
    symbol: str, day: dt.datetime, minute: int, open_: Any, close: Any, *, feed: str = "sip"
) -> Bar:
    o = Decimal(str(open_))
    c = Decimal(str(close))
    return Bar(
        symbol=symbol,
        timestamp=day + dt.timedelta(minutes=minute),
        open=o,
        high=max(o, c),
        low=min(o, c),
        close=c,
        volume=1000,
        feed=feed,
    )


def _session(
    symbol: str,
    day: dt.datetime,
    *,
    base: Any = "100",
    jumps: Sequence[tuple[int, Any]] = (),
    start: int = SESSION_START,
    count: int = SESSION_COUNT,
    skip: Sequence[int] = (),
    feed: str = "sip",
) -> list[Bar]:
    """A contiguous flat session with optional price steps and missing minutes."""
    bars: list[Bar] = []
    current = Decimal(str(base))
    jump_map = dict(jumps)
    skipped = set(skip)
    for offset in range(count):
        minute = start + offset
        if minute in skipped:
            continue
        if minute in jump_map:
            current = Decimal(str(jump_map[minute]))
        bars.append(_bar(symbol, day, minute, current, current, feed=feed))
    return bars


def _at(day: dt.datetime, hour: int, minute: int = 0) -> dt.datetime:
    return day + dt.timedelta(hours=hour, minutes=minute)


def _horizon(outcome: EventOutcome, name: str) -> HorizonOutcome:
    horizon = outcome.horizon(name)
    assert horizon is not None
    return horizon


def _inputs(
    *,
    published_at: dt.datetime = PUBLICATION,
    as_of: dt.datetime | None = None,
    stock: Sequence[Bar] | None = None,
    benchmark: Sequence[Bar] | None = None,
    **kwargs: Any,
) -> NewsOutcomeInputs:
    return NewsOutcomeInputs(
        event_id="evt",
        published_at=published_at,
        as_of=as_of if as_of is not None else _at(FRIDAY, 21),
        stock_bars=stock if stock is not None else _session("AAPL", FRIDAY),
        benchmark_bars=(
            benchmark if benchmark is not None else _session("SPY", FRIDAY, base="400")
        ),
        **kwargs,
    )


def test_entry_is_the_first_open_after_publication_plus_the_default_latency() -> None:
    stock = _session("AAPL", FRIDAY, base="100", jumps=[(845, "102")])
    benchmark = _session("SPY", FRIDAY, base="400")

    outcome = compute_news_outcome(_inputs(stock=stock, benchmark=benchmark))

    assert dt.timedelta(minutes=5) == DEFAULT_LATENCY
    assert outcome.status is OutcomeStatus.OK
    assert outcome.entry is not None
    assert outcome.entry.timestamp == FRIDAY + dt.timedelta(minutes=845)
    assert outcome.entry.price == Decimal("102")
    assert outcome.entry.basis == "open"
    assert outcome.entry_delay_seconds == 0.0
    assert outcome.entry_was_session_gap is False

    move = outcome.publication_to_entry
    assert move.status is OutcomeStatus.OK
    assert move.reference is not None and move.reference.price == Decimal("100")
    assert move.instrument_return == Decimal("0.02")


def test_a_configured_latency_shifts_the_entry_bar() -> None:
    stock = _session("AAPL", FRIDAY, base="100", jumps=[(845, "102"), (850, "104")])
    benchmark = _session("SPY", FRIDAY, base="400")

    outcome = compute_news_outcome(
        _inputs(stock=stock, benchmark=benchmark, latency=dt.timedelta(minutes=10))
    )

    assert outcome.entry is not None
    assert outcome.entry.timestamp == FRIDAY + dt.timedelta(minutes=850)
    assert outcome.entry.price == Decimal("104")


def test_mid_minute_publication_uses_only_completed_stock_and_benchmark_closes() -> None:
    publication = _at(FRIDAY, 14, 0) + dt.timedelta(seconds=30)
    stock = _session("AAPL", FRIDAY, jumps=[(840, "110")])
    benchmark = _session("SPY", FRIDAY, base="400", jumps=[(840, "440")])

    outcome = compute_news_outcome(
        _inputs(published_at=publication, stock=stock, benchmark=benchmark)
    )

    move = outcome.publication_to_entry
    assert move.status is OutcomeStatus.OK
    assert move.reference is not None
    assert move.benchmark_reference is not None
    assert move.reference.timestamp == _at(FRIDAY, 13, 59)
    assert move.reference.price == Decimal("100")
    assert move.benchmark_reference.timestamp == _at(FRIDAY, 13, 59)
    assert move.benchmark_reference.price == Decimal("400")


def test_a_saturday_publication_enters_the_next_session_open() -> None:
    publication = SATURDAY + dt.timedelta(hours=12)
    stock = _session("AAPL", FRIDAY) + _session("AAPL", MONDAY)
    benchmark = _session("SPY", FRIDAY, base="400") + _session("SPY", MONDAY, base="400")

    outcome = compute_news_outcome(
        _inputs(
            published_at=publication,
            as_of=_at(MONDAY, 21),
            stock=stock,
            benchmark=benchmark,
        )
    )

    assert outcome.status is OutcomeStatus.OK
    assert outcome.entry is not None
    assert outcome.entry.timestamp == MONDAY + dt.timedelta(minutes=SESSION_START)
    assert outcome.entry_was_session_gap is True
    # The reference is Friday's close; the overnight/weekend gap is expected.
    assert outcome.publication_to_entry.status is OutcomeStatus.OK
    assert outcome.publication_to_entry.reference is not None
    assert outcome.publication_to_entry.reference.timestamp == FRIDAY + dt.timedelta(minutes=1199)

    # Monday is the only session after entry, so the 1d/5d windows are not elapsed.


def test_an_after_close_publication_enters_the_next_session_open() -> None:
    stock = _session("AAPL", FRIDAY) + _session("AAPL", MONDAY)
    benchmark = _session("SPY", FRIDAY, base="400") + _session("SPY", MONDAY, base="400")
    outcome = compute_news_outcome(
        _inputs(
            published_at=_at(FRIDAY, 21),  # 17:00 ET
            as_of=_at(MONDAY, 21),
            stock=stock,
            benchmark=benchmark,
        )
    )
    assert outcome.status is OutcomeStatus.OK
    assert outcome.entry is not None
    assert outcome.entry.timestamp == MONDAY + dt.timedelta(minutes=SESSION_START)
    assert outcome.entry_was_session_gap is True


def test_last_minutes_publication_enters_next_session_after_latency() -> None:
    stock = _session("AAPL", FRIDAY) + _session("AAPL", MONDAY)
    benchmark = _session("SPY", FRIDAY, base="400") + _session("SPY", MONDAY, base="400")
    outcome = compute_news_outcome(
        _inputs(
            published_at=_at(FRIDAY, 19, 55),  # 15:55 ET; latency ends at close
            as_of=_at(MONDAY, 21),
            stock=stock,
            benchmark=benchmark,
        )
    )
    assert outcome.status is OutcomeStatus.OK
    assert outcome.entry is not None
    assert outcome.entry.timestamp == MONDAY + dt.timedelta(minutes=SESSION_START)
    assert _horizon(outcome, HORIZON_HOUR_1).status is OutcomeStatus.OK
    assert _horizon(outcome, HORIZON_DAY_1).status is OutcomeStatus.NOT_ELAPSED


def test_a_data_gap_after_the_entry_target_is_unavailable() -> None:
    stock = _session("AAPL", FRIDAY, count=34)  # 13:30..14:03
    stock.extend(_bar("AAPL", FRIDAY, minute, "100", "100") for minute in range(900, 960))
    benchmark = _session("SPY", FRIDAY, base="400")

    outcome = compute_news_outcome(_inputs(stock=stock, benchmark=benchmark, as_of=_at(FRIDAY, 21)))

    assert outcome.status is OutcomeStatus.ENTRY_GAP
    assert outcome.entry is None
    assert any("data gap" in reason for reason in outcome.reasons)


def test_a_gap_inside_the_hour_window_is_unavailable() -> None:
    stock = _session("AAPL", FRIDAY, base="100", jumps=[(845, "102")], skip=range(860, 871))
    benchmark = _session("SPY", FRIDAY, base="400")

    outcome = compute_news_outcome(_inputs(stock=stock, benchmark=benchmark))

    hour = outcome.horizon(HORIZON_HOUR_1)
    assert hour is not None
    assert hour.status is OutcomeStatus.MISSING_BARS
    assert any("gap" in reason for reason in hour.reasons)


def test_a_benchmark_gap_inside_the_hour_window_is_unavailable() -> None:
    stock = _session("AAPL", FRIDAY, base="100")
    benchmark = _session("SPY", FRIDAY, base="400", skip=range(860, 871))

    outcome = compute_news_outcome(_inputs(stock=stock, benchmark=benchmark))

    hour = _horizon(outcome, HORIZON_HOUR_1)
    assert hour.status is OutcomeStatus.MISSING_BARS
    assert any("benchmark gap" in reason for reason in hour.reasons)


def test_a_window_that_has_not_elapsed_is_not_measured() -> None:
    stock = _session("AAPL", FRIDAY, base="100", jumps=[(845, "102")], count=60)
    benchmark = _session("SPY", FRIDAY, base="400", count=60)
    as_of = FRIDAY + dt.timedelta(minutes=869)  # 14:29, before the 1h checkpoint

    outcome = compute_news_outcome(_inputs(stock=stock, benchmark=benchmark, as_of=as_of))

    assert outcome.entry is not None
    assert outcome.entry.timestamp == FRIDAY + dt.timedelta(minutes=845)
    assert _horizon(outcome, HORIZON_HOUR_1).status is OutcomeStatus.NOT_ELAPSED
    assert _horizon(outcome, HORIZON_DAY_1).status is OutcomeStatus.NOT_ELAPSED
    assert _horizon(outcome, HORIZON_DAY_5).status is OutcomeStatus.NOT_ELAPSED


def test_one_trading_day_is_the_next_session_close() -> None:
    stock = _session("AAPL", FRIDAY) + _session("AAPL", MONDAY, base="121")
    benchmark = _session("SPY", FRIDAY, base="400") + _session("SPY", MONDAY, base="400")

    outcome = compute_news_outcome(
        _inputs(as_of=_at(TUESDAY, 12), stock=stock, benchmark=benchmark)
    )

    day = outcome.horizon(HORIZON_DAY_1)
    assert day is not None
    assert day.status is OutcomeStatus.OK
    assert day.session_date == dt.date(2026, 9, 7)
    assert day.checkpoint is not None and day.checkpoint.basis == "close"
    assert day.instrument_return == Decimal("0.21")
    assert day.market_relative_return == Decimal("0.21")


def test_a_truncated_target_session_is_not_called_its_close() -> None:
    stock = _session("AAPL", FRIDAY) + _session("AAPL", MONDAY, count=300)
    benchmark = _session("SPY", FRIDAY, base="400") + _session("SPY", MONDAY, base="400", count=300)
    outcome = compute_news_outcome(
        _inputs(
            as_of=_at(TUESDAY, 12),
            stock=stock,
            benchmark=benchmark,
            expected_close_time=dt.time(16, 0),
        )
    )
    assert _horizon(outcome, HORIZON_DAY_1).status is OutcomeStatus.MISSING_BARS


def test_five_trading_days_counts_sessions_not_calendar_days() -> None:
    stock: list[Bar] = []
    benchmark: list[Bar] = []
    for session_day, close in (
        (FRIDAY, "100"),
        (MONDAY, "101"),
        (TUESDAY, "102"),
        (WEDNESDAY, "103"),
        (THURSDAY, "104"),
        (FRIDAY_2, "105"),
    ):
        stock.extend(_session("AAPL", session_day, base=close))
        benchmark.extend(_session("SPY", session_day, base="400"))

    outcome = compute_news_outcome(
        _inputs(as_of=_at(MONDAY_2, 12), stock=stock, benchmark=benchmark)
    )

    day = _horizon(outcome, HORIZON_DAY_5)
    assert day.status is OutcomeStatus.OK
    # Five sessions after Friday 4 Sep is Friday 11 Sep, not Wednesday 9 Sep.
    assert day.session_date == dt.date(2026, 9, 11)
    assert day.instrument_return == Decimal("0.05")


def test_an_unadjusted_split_discontinuity_is_excluded() -> None:
    stock = _session("AAPL", FRIDAY) + _session("AAPL", MONDAY, base="50")
    benchmark = _session("SPY", FRIDAY, base="400") + _session("SPY", MONDAY, base="400")

    outcome = compute_news_outcome(
        _inputs(as_of=_at(TUESDAY, 12), stock=stock, benchmark=benchmark)
    )

    assert any(split.direction == "forward" for split in outcome.splits)
    assert "split_discontinuities_detected" in outcome.flags
    day = outcome.horizon(HORIZON_DAY_1)
    assert day is not None
    assert day.status is OutcomeStatus.SPLIT_DISCONTINUITY
    # The publication-to-entry window closed before the split, so it survives.
    assert outcome.publication_to_entry.status is OutcomeStatus.OK


def test_a_normal_large_move_is_not_mistaken_for_a_split() -> None:
    stock = _session("AAPL", FRIDAY, base="100", jumps=[(845, "140")])
    benchmark = _session("SPY", FRIDAY, base="400")

    outcome = compute_news_outcome(_inputs(stock=stock, benchmark=benchmark))

    assert outcome.splits == ()
    assert outcome.publication_to_entry.status is OutcomeStatus.OK


def test_detect_split_discontinuities_flags_forward_and_reverse() -> None:
    forward = [_bar("X", FRIDAY, 0, "100", "100"), _bar("X", FRIDAY, 1, "50", "50")]
    assert detect_split_discontinuities(forward)[0].direction == "forward"

    reverse = [_bar("X", FRIDAY, 0, "50", "50"), _bar("X", FRIDAY, 1, "100", "100")]
    assert detect_split_discontinuities(reverse)[0].direction == "reverse"

    ordinary = [_bar("X", FRIDAY, 0, "100", "100"), _bar("X", FRIDAY, 1, "103", "103")]
    assert detect_split_discontinuities(ordinary) == ()


def test_the_market_relative_return_subtracts_the_benchmark() -> None:
    stock = _session("AAPL", FRIDAY) + _session("AAPL", MONDAY, base="110")
    benchmark = _session("SPY", FRIDAY, base="400") + _session("SPY", MONDAY, base="416")

    outcome = compute_news_outcome(
        _inputs(as_of=_at(TUESDAY, 12), stock=stock, benchmark=benchmark)
    )

    day = outcome.horizon(HORIZON_DAY_1)
    assert day is not None
    assert day.instrument_return == Decimal("0.1")
    assert day.benchmark_return == Decimal("0.04")
    assert day.market_relative_return == Decimal("0.06")


def test_the_spread_is_charged_once_for_the_round_trip() -> None:
    stock = _session("AAPL", FRIDAY) + _session("AAPL", MONDAY, base="110")
    benchmark = _session("SPY", FRIDAY, base="400") + _session("SPY", MONDAY, base="416")

    outcome = compute_news_outcome(
        _inputs(
            as_of=_at(TUESDAY, 12),
            stock=stock,
            benchmark=benchmark,
            spread_bps=Decimal("20"),
        )
    )

    day = outcome.horizon(HORIZON_DAY_1)
    assert day is not None
    assert day.instrument_return_net == Decimal("0.098")
    assert day.market_relative_return_net == Decimal("0.058")


def test_fx_bars_convert_the_instrument_and_costs_are_explicit() -> None:
    stock = _session("AAPL", FRIDAY) + _session("AAPL", MONDAY, base="110")
    benchmark = _session("SPY", FRIDAY, base="400") + _session("SPY", MONDAY, base="416")
    fx = [
        _bar("EURUSD", FRIDAY, 845, "1.10", "1.10"),
        _bar("EURUSD", MONDAY, 1199, "1.20", "1.20"),
    ]

    outcome = compute_news_outcome(
        _inputs(
            as_of=_at(TUESDAY, 12),
            stock=stock,
            benchmark=benchmark,
            instrument_currency="EUR",
            benchmark_currency="USD",
            fx_bars=fx,
            fx_cost_bps=Decimal("10"),
        )
    )

    assert "fx_conversion_applied" in outcome.flags
    day = outcome.horizon(HORIZON_DAY_1)
    assert day is not None
    assert day.status is OutcomeStatus.OK
    # 100 EUR at 1.10 -> 110 USD; 110 EUR at 1.20 -> 132 USD; +20%, less 10bps.
    assert day.instrument_return == Decimal("0.2")
    assert day.instrument_return_net == Decimal("0.199")
    assert day.benchmark_return == Decimal("0.04")
    assert day.market_relative_return_net == Decimal("0.159")


def test_a_currency_mismatch_without_fx_bars_is_unavailable() -> None:
    stock = _session("AAPL", FRIDAY) + _session("AAPL", MONDAY, base="110")
    benchmark = _session("SPY", FRIDAY, base="400") + _session("SPY", MONDAY, base="416")

    outcome = compute_news_outcome(
        _inputs(
            as_of=_at(TUESDAY, 12),
            stock=stock,
            benchmark=benchmark,
            instrument_currency="EUR",
            benchmark_currency="USD",
        )
    )

    assert outcome.publication_to_entry.status is OutcomeStatus.MISSING_FX
    day = outcome.horizon(HORIZON_DAY_1)
    assert day is not None
    assert day.status is OutcomeStatus.MISSING_FX


def test_future_fx_bar_cannot_set_the_entry_conversion_rate() -> None:
    stock = _session("AAPL", FRIDAY) + _session("AAPL", MONDAY, base="110")
    benchmark = _session("SPY", FRIDAY, base="400") + _session("SPY", MONDAY, base="416")
    outcome = compute_news_outcome(
        _inputs(
            as_of=_at(TUESDAY, 12),
            stock=stock,
            benchmark=benchmark,
            instrument_currency="EUR",
            benchmark_currency="USD",
            fx_bars=[_bar("EURUSD", FRIDAY, 900, "1.10", "1.10")],
        )
    )
    assert outcome.publication_to_entry.status is OutcomeStatus.MISSING_FX
    day = outcome.horizon(HORIZON_DAY_1)
    assert day is not None
    assert day.status is OutcomeStatus.MISSING_FX


def test_a_confounding_publication_excludes_the_affected_window_by_default() -> None:
    stock = _session("AAPL", FRIDAY, base="100") + _session("AAPL", MONDAY, base="110")
    benchmark = _session("SPY", FRIDAY, base="400") + _session("SPY", MONDAY, base="400")
    confounder = FRIDAY + dt.timedelta(hours=15)  # inside the 1h window

    outcome = compute_news_outcome(
        _inputs(
            as_of=_at(TUESDAY, 12),
            stock=stock,
            benchmark=benchmark,
            confounding_publications=[confounder],
        )
    )

    hour = outcome.horizon(HORIZON_HOUR_1)
    assert hour is not None
    assert hour.status is OutcomeStatus.CONFOUNDED
    assert hour.confounded is True


def test_a_confounding_publication_can_be_flagged_instead_of_excluded() -> None:
    stock = _session("AAPL", FRIDAY, base="100") + _session("AAPL", MONDAY, base="110")
    benchmark = _session("SPY", FRIDAY, base="400") + _session("SPY", MONDAY, base="400")
    confounder = FRIDAY + dt.timedelta(hours=15)

    outcome = compute_news_outcome(
        _inputs(
            as_of=_at(TUESDAY, 12),
            stock=stock,
            benchmark=benchmark,
            confounding_publications=[confounder],
            confounding_policy=ConfoundingPolicy.FLAG,
        )
    )

    hour = outcome.horizon(HORIZON_HOUR_1)
    assert hour is not None
    assert hour.status is OutcomeStatus.OK
    assert hour.confounded is True


def test_a_feed_mismatch_is_flagged_not_hidden() -> None:
    stock = _session("AAPL", FRIDAY, base="100", feed="sip")
    benchmark = _session("SPY", FRIDAY, base="400", feed="iex")

    outcome = compute_news_outcome(_inputs(stock=stock, benchmark=benchmark))

    assert outcome.feed == "sip"
    assert outcome.benchmark_feed == "iex"
    assert "benchmark_feed_mismatch_across_series" in outcome.flags


def test_a_stale_reference_bar_is_unavailable() -> None:
    stock = _session("AAPL", FRIDAY) + _session("AAPL", MONDAY)
    benchmark = _session("SPY", FRIDAY, base="400") + _session("SPY", MONDAY, base="400")
    publication = MONDAY + dt.timedelta(hours=12)

    outcome = compute_news_outcome(
        _inputs(
            published_at=publication,
            as_of=_at(MONDAY, 21),
            stock=stock,
            benchmark=benchmark,
            max_reference_staleness=dt.timedelta(hours=12),
        )
    )

    assert outcome.entry is not None
    assert outcome.publication_to_entry.status is OutcomeStatus.STALE_BAR


def test_a_benchmark_missing_a_session_is_unavailable() -> None:
    stock = _session("AAPL", FRIDAY) + _session("AAPL", MONDAY, base="110")
    benchmark = _session("SPY", FRIDAY, base="400")

    outcome = compute_news_outcome(
        _inputs(as_of=_at(TUESDAY, 12), stock=stock, benchmark=benchmark)
    )

    day = outcome.horizon(HORIZON_DAY_1)
    assert day is not None
    assert day.status is OutcomeStatus.MISSING_BARS


def test_bars_at_or_after_as_of_are_never_used() -> None:
    stock = _session("AAPL", FRIDAY, base="100", jumps=[(845, "102"), (905, "999")])
    benchmark = _session("SPY", FRIDAY, base="400", jumps=[(905, "999")])
    as_of = FRIDAY + dt.timedelta(minutes=870)

    outcome = compute_news_outcome(_inputs(stock=stock, benchmark=benchmark, as_of=as_of))

    assert outcome.entry is not None and outcome.entry.price == Decimal("102")
    assert _horizon(outcome, HORIZON_HOUR_1).status is OutcomeStatus.NOT_ELAPSED


def test_a_naive_timestamp_is_rejected() -> None:
    naive_publication = dt.datetime.fromisoformat("2026-09-04T14:00:00")
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_news_outcome(_inputs(published_at=naive_publication))

    naive_bar = Bar(
        symbol="AAPL",
        timestamp=dt.datetime.fromisoformat("2026-09-04T14:00:00"),
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=1000,
        feed="sip",
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_news_outcome(_inputs(stock=[naive_bar]))

    aware_bar = _bar("AAPL", FRIDAY, 840, "100", "100")
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_news_outcome(_inputs(stock=[aware_bar, naive_bar]))


def test_a_future_publication_is_unavailable() -> None:
    as_of = FRIDAY + dt.timedelta(hours=12)
    outcome = compute_news_outcome(_inputs(as_of=as_of))

    assert outcome.status is OutcomeStatus.EVENT_IN_FUTURE
    assert outcome.entry is None


def test_the_result_is_json_serialisable() -> None:
    stock = _session("AAPL", FRIDAY) + _session("AAPL", MONDAY, base="110")
    benchmark = _session("SPY", FRIDAY, base="400") + _session("SPY", MONDAY, base="416")

    outcome = compute_news_outcome(
        _inputs(as_of=_at(TUESDAY, 12), stock=stock, benchmark=benchmark)
    )
    payload = outcome.as_dict()
    encoded = json.dumps(payload)

    assert payload["event_id"] == "evt"
    assert payload["status"] == "OK"
    assert payload["is_signal_eligible"] is False
    assert payload["signal_blocker"] == "single_event"
    assert len(payload["horizons"]) == 3
    assert "market_relative_return_net" in encoded


def _outcome_with_day_return() -> EventOutcome:
    stock = _session("AAPL", FRIDAY) + _session("AAPL", MONDAY, base="110")
    benchmark = _session("SPY", FRIDAY, base="400") + _session("SPY", MONDAY, base="416")
    return compute_news_outcome(_inputs(as_of=_at(TUESDAY, 12), stock=stock, benchmark=benchmark))


def test_a_single_event_is_never_a_signal() -> None:
    outcome = _outcome_with_day_return()

    aggregate = aggregate_outcomes([outcome])

    assert aggregate.total_events == 1
    assert aggregate.usable_events == 1
    assert aggregate.is_signal_eligible is False
    assert aggregate.blocker == "tiny_sample"
    day = aggregate.horizons[1]
    assert day.horizon == HORIZON_DAY_1
    assert day.samples == 1
    assert day.mean_market_relative_return_net is None
    assert MIN_SIGNAL_SAMPLES == 30


def test_an_aggregate_remains_research_only_at_the_minimum_sample() -> None:
    outcome = _outcome_with_day_return()

    aggregate = aggregate_outcomes([outcome], min_samples=1)

    assert aggregate.is_signal_eligible is False
    assert aggregate.blocker == "research_only"
    day = aggregate.horizons[1]
    assert day.samples == 1
    assert day.mean_market_relative_return_net == Decimal("0.06")
    assert day.hit_rate == Decimal("1")


def test_regular_session_bars_drop_extended_hours() -> None:
    from stockbrain.market_data.news_outcomes import regular_session_bars

    day = dt.date(2026, 9, 29)  # New York is UTC-4.
    stamps = [
        dt.datetime.combine(day, time, dt.UTC)
        for time in (
            dt.time(8, 0),
            dt.time(13, 29),
            dt.time(13, 30),
            dt.time(19, 59),
            dt.time(20, 0),
        )
    ]
    one = Decimal(1)
    bars = [Bar("AAPL", stamp, one, one, one, one, 1) for stamp in stamps]
    kept = [bar.timestamp.time() for bar in regular_session_bars(bars)]
    assert kept == [dt.time(13, 30), dt.time(19, 59)]
