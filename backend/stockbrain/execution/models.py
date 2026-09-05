"""The vocabulary the execution layer speaks.

Everything here is frozen and broker-neutral.  An :class:`ExecutionCommand` is
built from a persisted, revalidated proposal row and from nothing else -- there
is no field on it that a chat message, an API request body or a model could
reach, which is what makes "Telegram cannot choose a quantity" a property of a
type rather than of a check.

The canonical DTOs (:class:`BrokerAcknowledgement`, :class:`BrokerOrderView`)
exist so reconciliation never sees a Trading 212 payload.  A second broker
becomes an adapter that produces these, not a second reconciliation engine.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from stockbrain.enums import Broker, ExecutionFailure, OrderSide, OrderType

__all__ = [
    "BrokerAcknowledgement",
    "BrokerOrderView",
    "CandidateSearch",
    "ExecutionCommand",
    "PreflightRefusal",
]


@dataclass(frozen=True, slots=True)
class ExecutionCommand:
    """Exactly what to transmit, derived only from persisted proposal state.

    Note what is *absent*: no free-text, no price, no account selector, no
    policy flag, no actor-supplied anything.  A market order needs a listing, a
    signed quantity and a session flag, and this carries those and the identity
    of the proposal they came from.
    """

    proposal_id: uuid.UUID
    broker: Broker
    broker_environment: str
    broker_ticker: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    """Unsigned.  The broker's sign convention is applied at the adapter
    boundary, so no caller can express "sell" by passing a negative number."""

    extended_hours: bool

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("an execution command must carry a positive quantity")
        if self.order_type is not OrderType.MARKET:
            # Phase 8 transmits market orders only. A limit order needs its own
            # price revalidation and its own rate limit (1 req/2s), and shipping
            # it untested alongside the first mutation this system has ever
            # performed would be two experiments at once.
            raise ValueError(f"{self.order_type.value} orders are not implemented")

    @property
    def signed_quantity(self) -> Decimal:
        """Trading 212's convention: positive buys, negative sells.

        Applied here, from the recorded :class:`~stockbrain.enums.OrderSide`, so
        the sign is a function of the proposal's side and can never be an input.
        """
        return self.quantity if self.side is OrderSide.BUY else -self.quantity

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": str(self.proposal_id),
            "broker": self.broker.value,
            "broker_environment": self.broker_environment,
            "broker_ticker": self.broker_ticker,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "quantity": str(self.quantity),
            "signed_quantity": str(self.signed_quantity),
            "extended_hours": self.extended_hours,
        }


@dataclass(frozen=True, slots=True)
class BrokerOrderView:
    """One order as the broker reports it, normalised.

    ``initiated_from`` is kept because it is the strongest evidence
    reconciliation has: an order StockBrain placed says ``API``, and an order
    the operator placed in the mobile app does not.
    """

    broker_order_id: str
    broker_ticker: str | None
    signed_quantity: Decimal | None
    filled_quantity: Decimal | None
    filled_value: Decimal | None
    status: str | None
    order_type: str | None
    side: str | None
    currency: str | None
    created_at: dt.datetime | None
    initiated_from: str | None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def placed_by_api(self) -> bool:
        return self.initiated_from == "API"

    @property
    def is_terminal(self) -> bool:
        return (self.status or "").upper() in {"FILLED", "CANCELLED", "REJECTED"}


@dataclass(frozen=True, slots=True)
class BrokerAcknowledgement:
    """A confirmed broker response to a submission."""

    order: BrokerOrderView
    http_status: int
    payload: dict[str, Any]
    rate_limit: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CandidateSearch:
    """What the broker's read-only endpoints said when asked about an attempt.

    ``pending_ok`` and ``history_ok`` are separate from the candidate list
    because "we looked and found nothing" and "we could not look" must never be
    the same answer: only the first can ever support concluding that no order
    was placed.
    """

    candidates: tuple[BrokerOrderView, ...] = ()
    pending_ok: bool = False
    history_ok: bool = False
    scanned: int = 0
    error_category: str | None = None

    @property
    def complete(self) -> bool:
        """Whether both read paths answered, so absence is evidence."""
        return self.pending_ok and self.history_ok


@dataclass(frozen=True, slots=True)
class PreflightRefusal:
    """Why a transmission did not happen, and whether the proposal survives.

    ``invalidates`` separates "the world moved and this trade is no longer the
    trade that was approved" from "we could not check right now".  The first
    must retire the proposal; the second must leave it alone, because failing a
    proposal every time a market-data provider blinks would make an outage
    destructive.
    """

    category: ExecutionFailure
    reasons: tuple[str, ...]
    invalidates: bool = False
    rule_ids: tuple[str, ...] = ()

    @property
    def detail(self) -> str:
        return "; ".join(self.reasons) or self.category.value
