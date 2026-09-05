"""The execution command, and what it structurally cannot carry.

An :class:`~stockbrain.execution.models.ExecutionCommand` is the only thing the
broker adapter accepts.  Its shape is the safety argument: there is no field on
it for a price, an account, a policy flag, a free-text anything, or a signed
quantity -- so "Telegram cannot choose a side" and "the frontend cannot
substitute an execution field" are properties of a type rather than of a check
somebody remembered to write.
"""

from __future__ import annotations

import uuid
from dataclasses import fields
from decimal import Decimal

import pytest

from stockbrain.enums import Broker, ExecutionFailure, OrderSide, OrderType
from stockbrain.execution.models import (
    CandidateSearch,
    ExecutionCommand,
    PreflightRefusal,
)
from tests.execution_helpers import order_view

PROPOSAL = uuid.UUID("cccccccc-0000-0000-0000-000000000001")


def command(**overrides: object) -> ExecutionCommand:
    base: dict[str, object] = {
        "proposal_id": PROPOSAL,
        "broker": Broker.TRADING212,
        "broker_environment": "demo",
        "broker_ticker": "AAPL_US_EQ",
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "quantity": Decimal("3"),
        "extended_hours": False,
    }
    base.update(overrides)
    return ExecutionCommand(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------
def test_the_command_carries_only_trade_identity() -> None:
    """Every field is read off a persisted proposal.  Nothing else fits.

    A field for a price, an account or a policy would be a field a caller could
    fill in, and the caller of the execution layer is a job handler acting on
    somebody else's authorization.
    """
    names = {field.name for field in fields(ExecutionCommand)}
    assert names == {
        "proposal_id",
        "broker",
        "broker_environment",
        "broker_ticker",
        "side",
        "order_type",
        "quantity",
        "extended_hours",
    }
    for forbidden in (
        "price",
        "limit_price",
        "reference_price",
        "account",
        "account_id",
        "actor",
        "reason",
        "signed_quantity",
        "notional",
        "authorization_source",
        "execution_policy",
    ):
        assert forbidden not in names


def test_the_command_is_frozen() -> None:
    """Nothing between the snapshot and the socket may edit the trade."""
    order = command()
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError
        order.quantity = Decimal("99")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The sign convention
# ---------------------------------------------------------------------------
def test_positive_buys_and_negative_sells() -> None:
    """Trading 212 calls this "a core convention of the API".

    Derived from the recorded :class:`OrderSide`, so the sign is a *function* of
    the proposal's side. A caller cannot express "sell" by handing over a
    negative number, because the quantity it hands over must be positive.
    """
    assert command(side=OrderSide.BUY).signed_quantity == Decimal("3")
    assert command(side=OrderSide.SELL).signed_quantity == Decimal("-3")


def test_a_negative_or_zero_quantity_is_refused_at_construction() -> None:
    """A zero-quantity order is not a trade, and a negative one is a side."""
    for quantity in (Decimal("0"), Decimal("-1")):
        with pytest.raises(ValueError, match="positive quantity"):
            command(quantity=quantity)


def test_the_signed_quantity_keeps_decimal_exactness() -> None:
    """Never a binary float: 0.1 shares must transmit as 0.1."""
    signed = command(quantity=Decimal("0.1"), side=OrderSide.SELL).signed_quantity
    assert signed == Decimal("-0.1")
    assert str(signed) == "-0.1"


# ---------------------------------------------------------------------------
# Order types
# ---------------------------------------------------------------------------
def test_only_market_orders_are_implemented() -> None:
    """A limit order needs its own price revalidation and its own rate limit.

    Shipping it alongside the first broker mutation this system has ever
    performed would be two experiments at once, so the type refuses rather than
    silently sending a market order instead.
    """
    for order_type in (OrderType.LIMIT, OrderType.STOP, OrderType.STOP_LIMIT):
        with pytest.raises(ValueError, match="not implemented"):
            command(order_type=order_type)


# ---------------------------------------------------------------------------
# Candidate search and refusals
# ---------------------------------------------------------------------------
def test_a_search_is_only_complete_when_both_read_paths_answered() -> None:
    """ "We looked and found nothing" and "we could not look" are not the same.

    Only the first can support concluding that no order was placed, so the two
    flags are separate fields rather than one boolean.
    """
    assert not CandidateSearch().complete
    assert not CandidateSearch(pending_ok=True).complete
    assert not CandidateSearch(history_ok=True).complete
    assert CandidateSearch(pending_ok=True, history_ok=True).complete


def test_a_refusal_says_whether_the_proposal_survives_it() -> None:
    """A kill switch must not silently destroy every proposal it stops."""
    transient = PreflightRefusal(
        category=ExecutionFailure.PREFLIGHT_REFUSED,
        reasons=("the quote provider is unavailable",),
        invalidates=False,
    )
    assert not transient.invalidates
    assert "quote provider" in transient.detail

    fatal = PreflightRefusal(
        category=ExecutionFailure.PREFLIGHT_REFUSED,
        reasons=("the reference price drifted past the limit",),
        invalidates=True,
    )
    assert fatal.invalidates


def test_an_order_view_reports_api_provenance_and_terminality() -> None:
    """``initiatedFrom`` is the strongest evidence reconciliation has."""
    ours = order_view(initiated_from="API", status="FILLED")
    theirs = order_view(initiated_from="IOS", status="NEW")
    assert ours.placed_by_api and ours.is_terminal
    assert not theirs.placed_by_api and not theirs.is_terminal
