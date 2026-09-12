"""Deterministic exits: the arithmetic, and which rule wins when several fire.

Every rule here is a ratio against a broker-supplied price, so the tests state
the *direction* of each threshold and the precedence between them.  Nothing in
this module touches a database, a quote provider or a broker.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from stockbrain.enums import ThesisAction, TimeHorizon
from stockbrain.risk.exits import (
    EXIT_PRECEDENCE,
    ExitObservation,
    evaluate_exit,
    roi_target_for,
)
from tests import risk_helpers as h

NOW = dt.datetime(2026, 9, 11, 15, 0, tzinfo=dt.UTC)


def observe(**overrides: Any) -> ExitObservation:
    defaults: dict[str, Any] = {
        "broker_ticker": "AAPL_US_EQ",
        "quantity": Decimal("10"),
        "quantity_available": Decimal("10"),
        "average_price": Decimal("100"),
        "current_price": Decimal("100"),
        "peak_price": Decimal("100"),
        "peak_observations": 10,
        "opened_at": NOW - dt.timedelta(minutes=30),
        "horizon": TimeHorizon.WEEKS,
        "thesis_superseded": False,
    }
    defaults.update(overrides)
    return ExitObservation(**defaults)


def test_a_flat_position_inside_its_horizon_produces_no_signal() -> None:
    assert evaluate_exit(observe(), h.config(), now=NOW) is None


def test_the_hard_stop_fires_below_the_configured_loss() -> None:
    signal = evaluate_exit(observe(current_price=Decimal("91")), h.config(), now=NOW)
    assert signal is not None
    assert signal.rule_id == "hard_stop"
    assert signal.action is ThesisAction.SELL


def test_the_hard_stop_holds_at_exactly_the_threshold() -> None:
    # 8% below 100 is 92.  The floor is breached *below* it, not at it.
    assert evaluate_exit(observe(current_price=Decimal("92")), h.config(), now=NOW) is None


def test_trailing_does_not_arm_before_the_position_has_run() -> None:
    # Up 6%, below the 10% arm level: a 5% pullback from the peak must not exit.
    signal = evaluate_exit(
        observe(current_price=Decimal("100.70"), peak_price=Decimal("106")),
        h.config(),
        now=NOW,
    )
    assert signal is None


def test_trailing_fires_once_armed_and_pulled_back() -> None:
    signal = evaluate_exit(
        observe(current_price=Decimal("114"), peak_price=Decimal("125")),
        h.config(),
        now=NOW,
    )
    assert signal is not None
    assert signal.rule_id == "trailing_stop"
    assert signal.action is ThesisAction.SELL


def test_trailing_ignores_a_peak_built_from_too_few_observations() -> None:
    signal = evaluate_exit(
        observe(current_price=Decimal("114"), peak_price=Decimal("125"), peak_observations=1),
        h.config(),
        now=NOW,
    )
    assert signal is None


def test_a_superseded_thesis_proposes_a_full_exit() -> None:
    signal = evaluate_exit(observe(thesis_superseded=True), h.config(), now=NOW)
    assert signal is not None
    assert signal.rule_id == "thesis_superseded"
    assert signal.action is ThesisAction.SELL


def test_the_roi_target_banks_half_rather_than_liquidating() -> None:
    signal = evaluate_exit(observe(current_price=Decimal("116")), h.config(), now=NOW)
    assert signal is not None
    assert signal.rule_id == "roi_target"
    assert signal.action is ThesisAction.REDUCE


def test_the_roi_target_decays_with_time_held() -> None:
    config = h.config()
    assert roi_target_for(TimeHorizon.WEEKS, 0, config) == Decimal("0.15")
    assert roi_target_for(TimeHorizon.WEEKS, 10080, config) == Decimal("0.08")
    assert roi_target_for(TimeHorizon.WEEKS, 40000, config) == Decimal("0")


def test_an_elapsed_horizon_exits_a_position_that_is_merely_flat() -> None:
    signal = evaluate_exit(
        observe(current_price=Decimal("100"), opened_at=NOW - dt.timedelta(days=30)),
        h.config(),
        now=NOW,
    )
    assert signal is not None
    assert signal.rule_id == "horizon_elapsed"
    assert signal.action is ThesisAction.SELL


def test_an_elapsed_horizon_exits_a_small_loser_too() -> None:
    # Past the terminal row and slightly underwater: the horizon exit fires
    # whatever the result, because the thesis has run out of time.  A loss past
    # the hard-stop floor is the hard stop's business and is caught first.
    signal = evaluate_exit(
        observe(current_price=Decimal("95"), opened_at=NOW - dt.timedelta(days=30)),
        h.config(),
        now=NOW,
    )
    assert signal is not None
    assert signal.rule_id == "horizon_elapsed"


def test_the_hard_stop_outranks_every_other_rule() -> None:
    signal = evaluate_exit(
        observe(
            current_price=Decimal("80"),
            peak_price=Decimal("130"),
            thesis_superseded=True,
            opened_at=NOW - dt.timedelta(days=60),
        ),
        h.config(),
        now=NOW,
    )
    assert signal is not None
    assert signal.rule_id == "hard_stop"


def test_precedence_is_declared_and_complete() -> None:
    assert EXIT_PRECEDENCE == (
        "hard_stop",
        "volatility_stop",
        "trailing_stop",
        "thesis_superseded",
        "roi_target",
        "horizon_elapsed",
    )


@pytest.mark.parametrize("price", [Decimal("0"), Decimal("-1")])
def test_a_non_positive_basis_produces_no_signal(price: Decimal) -> None:
    assert evaluate_exit(observe(average_price=price), h.config(), now=NOW) is None


def test_nothing_is_proposed_when_no_shares_are_available_to_trade() -> None:
    signal = evaluate_exit(
        observe(current_price=Decimal("80"), quantity_available=Decimal("0")),
        h.config(),
        now=NOW,
    )
    assert signal is None


def test_the_volatility_stop_fires_below_peak_minus_k_atr() -> None:
    # peak 110, ATR 2, k=3 → floor 104.  Price 103 is below it and above the -8% hard stop.
    signal = evaluate_exit(
        observe(
            current_price=Decimal("103"),
            peak_price=Decimal("110"),
            average_price=Decimal("101"),
            atr=Decimal("2"),
            atr_as_of=NOW.date(),
        ),
        h.config(),
        now=NOW,
    )
    assert signal is not None and signal.rule_id == "volatility_stop"
    assert signal.action is ThesisAction.SELL


def test_the_volatility_stop_holds_at_exactly_the_floor() -> None:
    signal = evaluate_exit(
        observe(
            current_price=Decimal("104"),
            peak_price=Decimal("110"),
            average_price=Decimal("101"),
            atr=Decimal("2"),
            atr_as_of=NOW.date(),
        ),
        h.config(),
        now=NOW,
    )
    assert signal is None


def test_a_stale_atr_is_ignored_and_the_flat_rules_stand() -> None:
    signal = evaluate_exit(
        observe(
            current_price=Decimal("103"),
            peak_price=Decimal("110"),
            average_price=Decimal("101"),
            atr=Decimal("2"),
            atr_as_of=NOW.date() - dt.timedelta(days=10),
        ),
        h.config(),
        now=NOW,
    )
    assert signal is None  # 103 vs cost 101 is +2%: no hard stop, trailing not armed


def test_a_missing_atr_skips_the_rule() -> None:
    assert (
        evaluate_exit(
            observe(
                current_price=Decimal("103"),
                peak_price=Decimal("110"),
                average_price=Decimal("101"),
            ),
            h.config(),
            now=NOW,
        )
        is None
    )


def test_the_hard_stop_still_outranks_the_volatility_stop() -> None:
    signal = evaluate_exit(
        observe(
            current_price=Decimal("80"),
            peak_price=Decimal("110"),
            atr=Decimal("2"),
            atr_as_of=NOW.date(),
        ),
        h.config(),
        now=NOW,
    )
    assert signal is not None and signal.rule_id == "hard_stop"


def test_a_volatile_name_gets_room_a_quiet_one_does_not() -> None:
    quiet = observe(
        current_price=Decimal("96"),
        peak_price=Decimal("100"),
        atr=Decimal("1"),
        atr_as_of=NOW.date(),
    )
    wild = observe(
        current_price=Decimal("96"),
        peak_price=Decimal("100"),
        atr=Decimal("3"),
        atr_as_of=NOW.date(),
    )
    assert evaluate_exit(quiet, h.config(), now=NOW) is not None  # floor 97
    assert evaluate_exit(wild, h.config(), now=NOW) is None  # floor 91


def test_the_volatility_stop_respects_min_peak_observations() -> None:
    signal = evaluate_exit(
        observe(
            current_price=Decimal("103"),
            peak_price=Decimal("110"),
            peak_observations=1,
            atr=Decimal("2"),
            atr_as_of=NOW.date(),
        ),
        h.config(),
        now=NOW,
    )
    assert signal is None
