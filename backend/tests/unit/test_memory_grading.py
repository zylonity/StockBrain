"""Grading is arithmetic on daily closes.  These tests pin the arithmetic."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from stockbrain.enums import OutcomeCheckpoint, ThesisAction, TimeHorizon
from stockbrain.intelligence.memory import (
    bar_on_or_before,
    checkpoints_for,
    entry_bar,
    is_correct,
    returns,
    trading_days_after,
)
from stockbrain.market_data.base import Bar


def bar(day: dt.date, close: str) -> Bar:
    return Bar(
        symbol="AAPL",
        timestamp=dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=1,
    )


MON = dt.date(2026, 9, 14)
BARS = [
    bar(dt.date(2026, 9, 11), "100"),  # Friday before entry
    bar(MON, "102"),
    bar(dt.date(2026, 9, 15), "104"),
    bar(dt.date(2026, 9, 16), "103"),
    bar(dt.date(2026, 9, 17), "108"),
]


@pytest.mark.parametrize(
    ("horizon", "expected"),
    [
        (TimeHorizon.INTRADAY, (OutcomeCheckpoint.D1, OutcomeCheckpoint.D5)),
        (TimeHorizon.DAYS, (OutcomeCheckpoint.D1, OutcomeCheckpoint.D5)),
        (TimeHorizon.WEEKS, (OutcomeCheckpoint.D5, OutcomeCheckpoint.D20)),
        (TimeHorizon.MONTHS, (OutcomeCheckpoint.D20, OutcomeCheckpoint.D60)),
    ],
)
def test_checkpoints_are_horizon_relative(
    horizon: TimeHorizon, expected: tuple[OutcomeCheckpoint, ...]
) -> None:
    assert checkpoints_for(horizon) == expected


def test_the_entry_bar_is_the_close_on_the_entry_date() -> None:
    assert entry_bar(BARS, MON) is BARS[1]


def test_the_entry_bar_falls_forward_to_the_next_session_when_the_entry_day_has_no_bar() -> None:
    saturday = dt.date(2026, 9, 12)
    assert entry_bar(BARS, saturday) is BARS[1]


def test_no_entry_bar_when_every_bar_predates_entry() -> None:
    assert entry_bar(BARS, dt.date(2026, 9, 18)) is None


def test_the_current_bar_is_the_last_close_on_or_before_the_day() -> None:
    assert bar_on_or_before(BARS, dt.date(2026, 9, 16)) is BARS[3]
    assert bar_on_or_before(BARS, dt.date(2026, 9, 19)) is BARS[4]
    assert bar_on_or_before(BARS, dt.date(2026, 9, 10)) is None


def test_trading_days_count_bars_strictly_after_entry() -> None:
    assert trading_days_after(BARS, MON) == 3
    assert trading_days_after(BARS, dt.date(2026, 9, 17)) == 0


def test_returns_are_decimal_ratios_and_alpha_is_their_difference() -> None:
    instrument, benchmark, alpha = returns(
        Decimal("100"), Decimal("110"), Decimal("500"), Decimal("505")
    )
    assert instrument == Decimal("0.1")
    assert benchmark == Decimal("0.01")
    assert alpha == Decimal("0.09")


def test_returns_refuse_a_zero_entry_close() -> None:
    with pytest.raises(ValueError, match="entry close"):
        returns(Decimal(0), Decimal(1), Decimal(1), Decimal(1))


@pytest.mark.parametrize(
    ("action", "alpha", "expected"),
    [
        (ThesisAction.BUY, Decimal("0.01"), True),
        (ThesisAction.BUY, Decimal("0"), False),
        (ThesisAction.BUY, Decimal("-0.01"), False),
        (ThesisAction.REDUCE, Decimal("-0.01"), True),
        (ThesisAction.REDUCE, Decimal("0"), False),
        (ThesisAction.SELL, Decimal("-0.02"), True),
        (ThesisAction.SELL, Decimal("0.02"), False),
    ],
)
def test_correctness_is_signed_by_action(
    action: ThesisAction, alpha: Decimal, expected: bool
) -> None:
    assert is_correct(action, alpha) is expected


@pytest.mark.parametrize("action", [ThesisAction.HOLD, ThesisAction.NO_ACTION])
def test_non_trading_actions_cannot_be_graded(action: ThesisAction) -> None:
    with pytest.raises(ValueError, match="cannot be graded"):
        is_correct(action, Decimal("0.1"))
