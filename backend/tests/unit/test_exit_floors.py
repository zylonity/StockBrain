"""Exit floors: the prices the rules would act on, from the same predicates.

``exit_floors`` exists so an operator can see, for each open position, where the
hard stop, the volatility floor, the trailing floor and the ROI target sit --
computed from the very arming conditions ``evaluate_exit`` uses, so the floors
shown and the rules that fire can never disagree.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from stockbrain.enums import TimeHorizon
from stockbrain.risk.exits import ExitObservation, evaluate_exit, exit_floors
from tests import risk_helpers as h

NOW = dt.datetime(2026, 9, 12, 15, 0, tzinfo=dt.UTC)


def observe(**overrides: Any) -> ExitObservation:
    defaults: dict[str, Any] = {
        "broker_ticker": "AAPL_US_EQ",
        "quantity": Decimal("10"),
        "quantity_available": Decimal("10"),
        "average_price": Decimal("100"),
        "current_price": Decimal("100"),
        "peak_price": Decimal("100"),
        "peak_observations": 10,
        "opened_at": NOW - dt.timedelta(days=2),
        "horizon": TimeHorizon.WEEKS,
        "thesis_superseded": False,
    }
    defaults.update(overrides)
    return ExitObservation(**defaults)


def test_the_hard_stop_and_horizon_are_always_present() -> None:
    f = exit_floors(observe(), h.config(), now=NOW)
    assert f is not None
    assert f.hard_stop == Decimal("92")
    assert f.horizon_ends_at == NOW - dt.timedelta(days=2) + dt.timedelta(minutes=30240)
    assert f.volatility_floor is None and f.trailing_floor is None


def test_armed_floors_appear_and_the_nearest_is_the_highest() -> None:
    f = exit_floors(
        observe(
            current_price=Decimal("120"),
            peak_price=Decimal("125"),
            atr=Decimal("2"),
            atr_as_of=NOW.date(),
        ),
        h.config(),
        now=NOW,
    )
    assert f is not None
    assert f.trailing_floor == Decimal("118.75")
    assert f.volatility_floor == Decimal("119")
    assert f.nearest_rule == "volatility_stop" and f.nearest_floor == Decimal("119")


def test_the_roi_target_price_follows_the_decay_table() -> None:
    f = exit_floors(observe(), h.config(), now=NOW)  # weeks, 2 days held → 15%
    assert f is not None
    assert f.roi_target_price == Decimal("115")
    late = exit_floors(observe(opened_at=NOW - dt.timedelta(days=30)), h.config(), now=NOW)
    assert late is not None
    assert late.roi_target_price is None  # past the terminal row


def test_floors_agree_with_the_rule_that_would_fire() -> None:
    obs = observe(
        current_price=Decimal("118"),
        peak_price=Decimal("125"),
        atr=Decimal("2"),
        atr_as_of=NOW.date(),
    )
    f = exit_floors(obs, h.config(), now=NOW)
    signal = evaluate_exit(obs, h.config(), now=NOW)
    assert f is not None
    assert signal is not None and signal.rule_id == f.nearest_rule


@pytest.mark.parametrize(
    "overrides",
    [
        {"average_price": Decimal("0")},
        {"average_price": Decimal("-1")},
        {"current_price": Decimal("0")},
    ],
    ids=["zero-average-price", "negative-average-price", "zero-current-price"],
)
def test_an_unusable_observation_has_no_floors(overrides: dict[str, Any]) -> None:
    """A basis or price the rules cannot use yields no floors, not a divide."""
    obs = observe(peak_price=Decimal("120"), **overrides)
    assert exit_floors(obs, h.config(), now=NOW) is None
