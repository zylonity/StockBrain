"""Chronological and cost-aware validation of historical news outcomes."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from stockbrain.intelligence.news_outcome_validation import (
    HistoricalEvent,
    assess_failed_reaction,
    comparable_entry_cohort,
    compare_deadline_exit_to_hold,
    derive_catalyst_deadline,
    threshold_timing,
    validation_counts,
)
from stockbrain.market_data.base import Bar
from stockbrain.market_data.news_outcomes import (
    EventOutcome,
    HorizonOutcome,
    OutcomeStatus,
    PricePoint,
    PublicationEntryMove,
)

UTC = dt.UTC
BASE = dt.datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


def _bar(symbol: str, when: dt.datetime, close: str) -> Bar:
    price = Decimal(close)
    return Bar(symbol, when, price, price, price, price, 100)


def _event(
    identity: str,
    *,
    published: dt.datetime = BASE,
    live_delay: dt.timedelta | None = dt.timedelta(minutes=1),
    closes: tuple[str, ...] = ("100", "100", "101.5"),
    net: str | None = "0.02",
    prior_move: str = "0.005",
    five_day_status: OutcomeStatus = OutcomeStatus.OK,
    event_type: str = "earnings",
) -> HistoricalEvent:
    entry_at = published + dt.timedelta(minutes=1)
    reference = PricePoint(published - dt.timedelta(minutes=1), Decimal(100), "close")
    entry = PricePoint(entry_at, Decimal(100), "open")
    benchmark = PricePoint(entry_at, Decimal(100), "open")
    move = PublicationEntryMove(
        OutcomeStatus.OK,
        reference=reference,
        entry=entry,
        benchmark_reference=PricePoint(reference.timestamp, Decimal(100), "close"),
        benchmark_entry=benchmark,
        market_relative_return=Decimal(prior_move),
    )
    horizon = HorizonOutcome(
        "1d",
        OutcomeStatus.OK if net is not None else OutcomeStatus.MISSING_FX,
        entry=entry,
        market_relative_return_net=Decimal(net) if net is not None else None,
    )
    five_day = HorizonOutcome(
        "5d",
        five_day_status,
        entry=entry,
        checkpoint=PricePoint(published + dt.timedelta(days=7), Decimal(100), "close"),
    )
    outcome = EventOutcome(
        identity,
        published,
        published + dt.timedelta(days=10),
        OutcomeStatus.OK,
        entry,
        60.0,
        False,
        move,
        (horizon, five_day),
    )
    times = [published + dt.timedelta(minutes=index) for index in range(len(closes))]
    return HistoricalEvent(
        identity,
        "AAPL",
        event_type,
        published,
        published + live_delay if live_delay is not None else None,
        outcome,
        [_bar("AAPL", time, price) for time, price in zip(times, closes, strict=True)],
        [_bar("SPY", time, "100") for time in times],
    )


def test_threshold_reports_live_phase_and_trading_time() -> None:
    event = _event("one", live_delay=dt.timedelta(minutes=1, seconds=30))
    timing = threshold_timing(event, Decimal("0.01"))
    assert timing.status == "crossed"
    assert timing.phase == "after_feasible_entry"
    assert timing.clock_seconds == 180
    assert timing.trading_seconds == 180
    assert threshold_timing(_event("no_receipt", live_delay=None), Decimal("0.01")).phase == (
        "live_receipt_unknown"
    )


def test_missing_five_day_window_is_not_a_nonresponse() -> None:
    event = _event(
        "missing",
        closes=("100", "100", "100"),
        five_day_status=OutcomeStatus.MISSING_BARS,
    )
    assert threshold_timing(event, Decimal("0.01")).status == "unknown_incomplete"
    assert derive_catalyst_deadline([event], minimum_samples=1).unknown == 1


def test_comparable_cohort_includes_losses_flats_and_unknown_costs() -> None:
    target = _event("target", published=BASE + dt.timedelta(days=20))
    history = (
        [_event(f"p{i}", net="0.02") for i in range(4)]
        + [_event(f"n{i}", net="-0.03") for i in range(3)]
        + [_event(f"f{i}", net="0") for i in range(3)]
        + [_event("fx", net=None)]
    )
    result = comparable_entry_cohort(target, history)
    assert (result.total, result.usable, result.positive, result.flat, result.negative) == (
        11,
        10,
        4,
        3,
        3,
    )
    assert result.unavailable == 1
    assert result.positive_probability == Decimal("0.4")
    assert comparable_entry_cohort(target, history[:3]).positive_probability is None
    assert comparable_entry_cohort(_event("unknown", live_delay=None), history).total == 0


def test_deadline_counts_nonresponse_and_adds_trading_session_buffer() -> None:
    responding = [_event(f"r{i}") for i in range(8)]
    nonresponding = [_event(f"n{i}", closes=("100", "100", "100")) for i in range(2)]
    result = derive_catalyst_deadline(responding + nonresponding)
    assert (result.sample_size, result.responders, result.nonresponses) == (10, 8, 2)
    assert result.deadline_sessions_after_entry == 1
    weak = derive_catalyst_deadline(
        responding[:7] + nonresponding + [_event("n2", closes=("100", "100", "100"))]
    )
    assert weak.deadline_sessions_after_entry is None
    assert weak.reason == "response_fraction_below_threshold"


def test_validation_split_is_chronological_and_deduplicated() -> None:
    as_of = dt.datetime(2026, 9, 30, tzinfo=UTC)
    early = _event("early", published=dt.datetime(2025, 1, 5, tzinfo=UTC))
    later = _event("later", published=dt.datetime(2026, 5, 5, tzinfo=UTC))
    old = _event("old", published=dt.datetime(2024, 9, 29, tzinfo=UTC))
    result = validation_counts([early, early, later, old], as_of=as_of)
    assert result["exploration"]["events"] == 1
    assert result["validation"]["events"] == 1


def test_failed_reaction_requires_deadline_and_fresh_evidence() -> None:
    evidence = derive_catalyst_deadline([_event(f"r{i}") for i in range(10)])
    assert evidence.deadline_sessions_after_entry == 1
    before = assess_failed_reaction(
        evidence,
        elapsed_trading_sessions=0,
        observed_market_relative_move=Decimal("-0.02"),
        new_evidence_sustains_thesis=False,
    )
    assert before.recommendation == "WAIT"
    failed = assess_failed_reaction(
        evidence,
        elapsed_trading_sessions=1,
        observed_market_relative_move=Decimal("-0.02"),
        new_evidence_sustains_thesis=False,
    )
    assert failed.recommendation == "FAVOUR_SELL_OR_REDUCE_REASSESSMENT"
    sustained = assess_failed_reaction(
        evidence,
        elapsed_trading_sessions=1,
        observed_market_relative_move=Decimal("-0.02"),
        new_evidence_sustains_thesis=True,
    )
    assert sustained.recommendation == "REASSESS"
    sparse = derive_catalyst_deadline([_event("one")])
    assert (
        assess_failed_reaction(
            sparse,
            elapsed_trading_sessions=9,
            observed_market_relative_move=Decimal("-0.02"),
            new_evidence_sustains_thesis=False,
        ).recommendation
        == "NO_QUANTITATIVE_DEADLINE"
    )


def test_offline_exit_comparison_keeps_automatic_exit_disabled() -> None:
    pairs = [(Decimal("-0.01"), Decimal("-0.03")) for _ in range(10)]
    report = compare_deadline_exit_to_hold(pairs)
    assert report.deadline_exit_better == 10
    assert report.median_net_advantage == Decimal("0.02")
    assert report.automatic_exit_eligible is False


def test_threshold_uses_the_arithmetic_market_relative_return() -> None:
    """Stock +6% with SPY +5% is a +1% move, as the calculator and monitor define it."""
    event = _event("rally", closes=("100", "100", "106"))
    times = [bar.timestamp for bar in event.stock_bars]
    rally = HistoricalEvent(
        event.canonical_id,
        event.symbol,
        event.event_type,
        event.published_at,
        event.live_received_at,
        event.outcome,
        event.stock_bars,
        [
            _bar("SPY", time, close)
            for time, close in zip(times, ("100", "100", "105"), strict=True)
        ],
    )
    assert threshold_timing(rally, Decimal("0.01")).status == "crossed"
