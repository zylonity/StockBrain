"""Exit floors: the prices the rules would act on, from the same predicates.

``exit_floors`` exists so an operator can see, for each open position, where the
hard stop, the volatility floor, the trailing floor and the ROI target sit --
computed from the very arming conditions ``evaluate_exit`` uses.  The nearest
floor is the first level a falling price reaches, named by the rule that owns
it.  If several floors are breached within one tick the engine still acts in its
declared precedence (``EXIT_PRECEDENCE``), so the label names the level, not
necessarily the rule that fires.
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


def test_the_nearest_floor_is_the_volatility_floor_when_it_sits_highest() -> None:
    """With the price between the two armed floors, only the higher one is breached.

    peak 125, ATR 1 gives a volatility floor at 122 and a trailing floor at
    118.75; at 120 the engine fires ``volatility_stop`` and the nearest label
    names the same level.
    """
    obs = observe(
        current_price=Decimal("120"),
        peak_price=Decimal("125"),
        atr=Decimal("1"),
        atr_as_of=NOW.date(),
    )
    f = exit_floors(obs, h.config(), now=NOW)
    signal = evaluate_exit(obs, h.config(), now=NOW)
    assert f is not None
    assert f.volatility_floor == Decimal("122")
    assert f.trailing_floor == Decimal("118.75")
    assert signal is not None
    assert signal.rule_id == "volatility_stop" == f.nearest_rule


def test_the_nearest_floor_is_the_trailing_floor_when_it_sits_highest() -> None:
    """The nearest label names the level, not necessarily the rule that fires.

    peak 125, ATR 4 gives a volatility floor at 113 and a trailing floor at
    118.75; at 116 the volatility floor is untouched and the engine fires
    ``trailing_stop``, the rule the nearest label names.  Had the price been
    under both, ``EXIT_PRECEDENCE`` would fire ``volatility_stop`` while the
    label still named the higher trailing level.
    """
    obs = observe(
        current_price=Decimal("116"),
        peak_price=Decimal("125"),
        atr=Decimal("4"),
        atr_as_of=NOW.date(),
    )
    f = exit_floors(obs, h.config(), now=NOW)
    signal = evaluate_exit(obs, h.config(), now=NOW)
    assert f is not None
    assert f.volatility_floor == Decimal("113")
    assert f.trailing_floor == Decimal("118.75")
    assert f.nearest_rule == "trailing_stop"
    assert signal is not None and signal.rule_id == "trailing_stop"


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


def test_no_tradable_shares_has_no_floors() -> None:
    """The sweep will never act on a position with nothing available to sell."""
    assert exit_floors(observe(quantity_available=Decimal("0")), h.config(), now=NOW) is None
