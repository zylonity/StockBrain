"""Deterministic sizing: action semantics, rounding and the reference price.

Sizing is ordinary arithmetic, which is exactly why it deserves tests that state
the arithmetic's *direction*: down, never up, and against the side a market
order actually trades.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from stockbrain.enums import OrderSide, ThesisAction
from stockbrain.risk.models import SizingResult
from stockbrain.risk.sizing import ACTION_SIDES, size_trade
from tests import risk_helpers as h


def size(**kwargs: Any) -> SizingResult:
    defaults: dict[str, Any] = {
        "action": ThesisAction.BUY,
        "config": h.config(),
        "identity": h.identity(),
        "quote": h.quote(),
        "account": h.account(),
        "max_notional": Decimal("1000"),
        "size_factor": Decimal(1),
    }
    defaults.update(kwargs)
    return size_trade(**defaults)


# ---------------------------------------------------------------------------
# Action semantics
# ---------------------------------------------------------------------------
def test_the_action_to_side_map_is_exhaustive_and_explicit() -> None:
    assert set(ACTION_SIDES) == set(ThesisAction)
    assert ACTION_SIDES[ThesisAction.BUY] is OrderSide.BUY
    assert ACTION_SIDES[ThesisAction.SELL] is OrderSide.SELL
    assert ACTION_SIDES[ThesisAction.REDUCE] is OrderSide.SELL
    assert ACTION_SIDES[ThesisAction.HOLD] is None
    assert ACTION_SIDES[ThesisAction.NO_ACTION] is None


@pytest.mark.parametrize("action", [ThesisAction.HOLD, ThesisAction.NO_ACTION])
def test_hold_produces_no_side_and_no_quantity(action: ThesisAction) -> None:
    result = size(action=action)
    assert result.side is None
    assert result.quantity == 0
    assert not result.executable


# ---------------------------------------------------------------------------
# Reference price
# ---------------------------------------------------------------------------
def test_a_buy_is_sized_against_the_ask_not_the_mid() -> None:
    """Sizing a buy against the mid over-commits by half the spread, every time."""
    result = size(action=ThesisAction.BUY)
    assert result.reference_price == Decimal("200.05")
    assert any("ask" in reason for reason in result.reasons)


def test_a_sell_is_sized_against_the_bid() -> None:
    account = h.account(positions={"AAPL_US_EQ": h.position(quantity=Decimal("3"))})
    result = size(action=ThesisAction.SELL, account=account)
    assert result.reference_price == Decimal("199.95")


def test_no_quote_means_no_reference_price_and_no_order() -> None:
    result = size(quote=None)
    assert result.reference_price is None
    assert not result.executable


def test_a_non_positive_marketable_side_produces_no_order() -> None:
    result = size(quote=h.quote(bid=Decimal("10"), ask=Decimal(0)))
    assert not result.executable


# ---------------------------------------------------------------------------
# Rounding
# ---------------------------------------------------------------------------
def test_quantities_round_down_to_whole_shares_by_default() -> None:
    """Trading 212 documents no minimum quantity and no step, so none is invented.

    Rounding *down* can never breach a cap; rounding up breaches it by up to one
    share, and a cap breached "only a little" is not a cap.
    """
    result = size(max_notional=Decimal("1000"))
    assert result.quantity == Decimal("4")
    assert result.quantity * Decimal("200.05") <= Decimal("1000")


def test_fractional_sizing_is_available_but_off_by_default() -> None:
    fractional = size(config=h.config(allow_fractional_quantity=True), max_notional=Decimal("1000"))
    assert fractional.quantity > Decimal("4")
    assert fractional.quantity < Decimal("5")
    assert fractional.quantity * Decimal("200.05") <= Decimal("1000")


def test_quantity_rounds_down_to_the_instruments_known_precision() -> None:
    """Trading 212 rejects finer quantities: api-errors/quantity-precision-mismatch.

    The precision is per instrument (the broker publishes it nowhere), so the
    identity's learned value wins and the config default is only the fallback.
    """
    cfg = h.config(allow_fractional_quantity=True)
    known = size(
        config=cfg,
        identity=h.identity(quantity_precision=3),
        max_notional=Decimal("278.71404"),
    )
    assert known.quantity.as_tuple().exponent == -3
    unknown = size(config=cfg, identity=h.identity(), max_notional=Decimal("278.71404"))
    assert unknown.quantity.as_tuple().exponent == -2  # config default


def test_the_default_precision_is_in_the_policy_version() -> None:
    assert h.config().version != h.config(default_quantity_precision=3).version


def test_a_target_smaller_than_one_share_is_not_a_trade() -> None:
    result = size(max_notional=Decimal("150"))
    assert result.quantity == 0
    assert not result.executable
    assert any("less than one" in reason for reason in result.reasons)


def test_a_size_below_the_minimum_notional_is_refused() -> None:
    """A trade too small to be worth its own costs is not sized to "some shares"."""
    config = h.config(min_trade_notional=Decimal("500"))
    result = size(config=config, max_notional=Decimal("400"))
    assert not result.executable
    assert any("minimum trade notional" in r for r in result.reasons)


def test_no_notional_headroom_produces_no_order() -> None:
    result = size(max_notional=Decimal(0))
    assert not result.executable
    assert result.quantity == 0


# ---------------------------------------------------------------------------
# Size factors
# ---------------------------------------------------------------------------
def test_a_size_factor_only_ever_reduces() -> None:
    full = size(max_notional=Decimal("1000"), size_factor=Decimal(1))
    half = size(max_notional=Decimal("1000"), size_factor=Decimal("0.5"))
    assert half.quantity < full.quantity
    assert half.max_quantity == full.max_quantity


def test_the_quantity_never_exceeds_the_maximum_the_caps_permit() -> None:
    result = size(max_notional=Decimal("1000"), size_factor=Decimal(1))
    assert result.quantity <= result.max_quantity


# ---------------------------------------------------------------------------
# Reductions
# ---------------------------------------------------------------------------
def test_sell_closes_the_whole_available_position() -> None:
    account = h.account(
        positions={"AAPL_US_EQ": h.position(quantity=Decimal("12"), available=Decimal("12"))}
    )
    result = size(action=ThesisAction.SELL, account=account)
    assert result.quantity == Decimal("12")
    assert result.side is OrderSide.SELL


def test_reduce_takes_the_configured_fraction_rounded_down() -> None:
    account = h.account(positions={"AAPL_US_EQ": h.position(quantity=Decimal("7"))})
    result = size(action=ThesisAction.REDUCE, account=account)
    assert result.quantity == Decimal("3"), "half of 7, rounded down"


def test_reduce_refuses_to_become_a_full_exit_on_a_single_share() -> None:
    """A one-share holding cannot be halved in whole shares.

    Selling the single share would be a liquidation wearing a partial exit's
    name, so it is refused and the operator is told a SELL thesis is required.
    """
    account = h.account(positions={"AAPL_US_EQ": h.position(quantity=Decimal("1"))})
    result = size(action=ThesisAction.REDUCE, account=account)
    assert not result.executable
    assert any("full exit requires a SELL" in r for r in result.reasons)


def test_a_reduction_is_never_larger_than_the_holding() -> None:
    account = h.account(
        positions={"AAPL_US_EQ": h.position(quantity=Decimal("4"), available=Decimal("4"))}
    )
    for action in (ThesisAction.SELL, ThesisAction.REDUCE):
        result = size(action=action, account=account)
        assert result.quantity <= Decimal("4")


def test_selling_with_no_position_is_refused_because_shorting_is_disabled() -> None:
    result = size(action=ThesisAction.SELL, account=h.account())
    assert not result.executable
    assert result.quantity == 0


def test_a_reduction_ignores_the_exposure_cap_entirely() -> None:
    """Removing risk is not bounded by a limit on taking it."""
    account = h.account(positions={"AAPL_US_EQ": h.position(quantity=Decimal("8"))})
    result = size(action=ThesisAction.SELL, account=account, max_notional=Decimal(0))
    assert result.executable
    assert result.quantity == Decimal("8")


# ---------------------------------------------------------------------------
# No floats
# ---------------------------------------------------------------------------
def test_every_output_is_a_decimal() -> None:
    result = size()
    for value in (
        result.quantity,
        result.target_notional,
        result.max_quantity,
        result.max_notional,
        result.reference_price,
    ):
        assert isinstance(value, Decimal)
