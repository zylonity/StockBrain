"""The request fingerprint: an audit hash, explicitly not an idempotency key.

Trading 212's order endpoint accepts no client-supplied reference and is
documented as non-idempotent.  Nothing about this hash makes a resend safe, and
the tests here pin down both what it *does* guarantee (determinism, coverage of
the trade's identity, no secrets) and what it does not.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from stockbrain.enums import Broker, OrderSide, OrderType
from stockbrain.execution.fingerprint import (
    FINGERPRINT_VERSION,
    fingerprint_for,
    fingerprint_payload,
)
from stockbrain.execution.models import ExecutionCommand

PROPOSAL = uuid.UUID("cccccccc-0000-0000-0000-000000000001")
OTHER = uuid.UUID("cccccccc-0000-0000-0000-000000000002")


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


def test_the_same_command_always_hashes_the_same() -> None:
    """Deterministic across processes: sorted keys, fixed separators, no floats."""
    assert fingerprint_for(command()) == fingerprint_for(command())
    assert len(fingerprint_for(command())) == 64


def test_a_known_command_has_a_pinned_fingerprint() -> None:
    """A change to how the body is built changes this value.

    Pinned deliberately: the fingerprint is stored on every attempt, and a
    silent change to the scheme would make historical rows incomparable without
    anybody noticing.
    """
    assert fingerprint_for(command()) == (
        "6fb1fa95743fafad498f5004aadd17f5e35f916bf0a0377f67833ce5957b75d4"
    )


def test_every_field_of_the_trade_changes_the_fingerprint() -> None:
    """Nothing identifying is left out of the hash."""
    baseline = fingerprint_for(command())
    for overrides in (
        {"proposal_id": OTHER},
        {"broker_environment": "live"},
        {"broker_ticker": "MSFT_US_EQ"},
        {"side": OrderSide.SELL},
        {"quantity": Decimal("4")},
        {"extended_hours": True},
    ):
        assert fingerprint_for(command(**overrides)) != baseline, overrides


def test_the_fingerprint_covers_the_signed_quantity_not_just_the_size() -> None:
    """A buy for 3 and a sell for 3 must never fingerprint the same."""
    buy = fingerprint_payload(command(side=OrderSide.BUY))
    sell = fingerprint_payload(command(side=OrderSide.SELL))
    assert buy["signed_quantity"] == "3"
    assert sell["signed_quantity"] == "-3"


def test_the_payload_uses_decimal_strings_rather_than_floats() -> None:
    """A float would make the same trade hash differently on another machine."""
    payload = fingerprint_payload(command(quantity=Decimal("0.10")))
    assert payload["quantity"] == "0.10"
    assert isinstance(payload["quantity"], str)
    assert all(not isinstance(value, float) for value in payload.values())


def test_the_payload_carries_no_credential_and_no_clock() -> None:
    """Safe to log and to return over the API.

    No timestamp and no attempt number either: two attempts at the same trade
    *should* fingerprint the same, because that collision is the thing an
    operator needs to be able to see.
    """
    payload = fingerprint_payload(command())
    rendered = repr(payload).lower()
    for forbidden in ("secret", "token", "authorization", "password", "basic ", "api_key"):
        assert forbidden not in rendered
    for absent in ("timestamp", "created_at", "attempt", "now", "sent_at"):
        assert absent not in payload


def test_the_version_is_part_of_the_hash() -> None:
    """A stored fingerprint stays interpretable against the scheme that made it."""
    assert fingerprint_payload(command())["version"] == FINGERPRINT_VERSION


def test_the_fingerprint_is_not_an_idempotency_key() -> None:
    """Asserted as documentation, because the mistake it prevents is expensive.

    Trading 212 has no idempotency mechanism. Two identical commands produce the
    same fingerprint *and*, if both were sent, two real orders -- which is why
    nothing in the codebase may treat a matching fingerprint as permission to
    resend.
    """
    from stockbrain.execution import fingerprint as module

    doc = module.__doc__ or ""
    assert "not an idempotency key" in doc.lower()
    assert fingerprint_for(command()) == fingerprint_for(command())
