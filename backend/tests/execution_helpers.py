"""Doubles for the broker execution surface.

Destructive and error-path testing must never be done by making a real broker
fail, so the seam is the four-operation
:class:`~stockbrain.execution.base.BrokerExecutionProvider`.  :class:`FakeProvider`
implements it and can be told to answer, to refuse, to time out, or to accept an
order and then be unreadable -- every shape the real client can produce.

:func:`order_view` builds a canonical broker order the way the Trading 212
adapter would, so reconciliation tests exercise the same matching rules the real
adapter feeds.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from stockbrain.db.base import utcnow
from stockbrain.enums import Broker
from stockbrain.execution.models import (
    BrokerAcknowledgement,
    BrokerOrderView,
    CandidateSearch,
    ExecutionCommand,
)

__all__ = ["FakeProvider", "acknowledgement_for", "order_view"]


def order_view(
    *,
    broker_order_id: str = "500100",
    broker_ticker: str = "AAPL_US_EQ",
    signed_quantity: Decimal | None = Decimal("2"),
    status: str = "NEW",
    order_type: str = "MARKET",
    initiated_from: str = "API",
    created_at: dt.datetime | None = None,
    filled_quantity: Decimal | None = None,
    filled_value: Decimal | None = None,
    side: str = "BUY",
    currency: str = "USD",
) -> BrokerOrderView:
    return BrokerOrderView(
        broker_order_id=broker_order_id,
        broker_ticker=broker_ticker,
        signed_quantity=signed_quantity,
        filled_quantity=filled_quantity,
        filled_value=filled_value,
        status=status,
        order_type=order_type,
        side=side,
        currency=currency,
        created_at=created_at or utcnow(),
        initiated_from=initiated_from,
        raw={"id": int(broker_order_id), "status": status},
    )


@dataclass
class FakeProvider:
    """A broker that does exactly what a test tells it to.

    ``submitted`` is the count that matters in nearly every test here: the
    invariant under examination is almost always "how many times did we
    transmit", and the answer must be at most one.
    """

    broker: Broker = Broker.TRADING212
    _environment: str = "demo"

    #: Raised from ``submit`` if set, instead of answering.
    error: Exception | None = None
    #: The acknowledgement ``submit`` returns when it does answer.
    acknowledgement: BrokerAcknowledgement | None = None
    #: ``False`` makes the local rate limiter deny -- a pre-send condition.
    slot_available: bool = True

    #: What ``find_candidates`` reports.
    search: CandidateSearch = field(default_factory=CandidateSearch)
    #: What ``fetch_order`` returns, keyed by broker order id.
    orders: dict[str, BrokerOrderView] = field(default_factory=dict)
    fetch_error: Exception | None = None

    submitted: int = 0
    commands: list[ExecutionCommand] = field(default_factory=list)
    slot_requests: int = 0
    searches: int = 0

    @property
    def environment(self) -> str:
        return self._environment

    def set_environment(self, environment: str) -> None:
        self._environment = environment

    async def reserve_slot(self) -> bool:
        self.slot_requests += 1
        return self.slot_available

    async def submit(self, command: ExecutionCommand) -> BrokerAcknowledgement:
        # Recorded before any raise, so a test can prove a transmission was
        # attempted even when it failed.
        self.submitted += 1
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        if self.acknowledgement is not None:
            return self.acknowledgement
        return BrokerAcknowledgement(
            order=order_view(signed_quantity=command.signed_quantity),
            http_status=200,
            payload={"id": 500100, "status": "NEW"},
            rate_limit={"limit": 50, "remaining": 49, "period": "60"},
        )

    async def fetch_order(self, broker_order_id: str) -> BrokerOrderView | None:
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.orders.get(broker_order_id)

    async def find_candidates(
        self, *, broker_ticker: str, since: dt.datetime, until: dt.datetime
    ) -> CandidateSearch:
        self.searches += 1
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.search


def acknowledgement_for(
    order: BrokerOrderView, *, http_status: int = 200, payload: dict[str, Any] | None = None
) -> BrokerAcknowledgement:
    return BrokerAcknowledgement(
        order=order,
        http_status=http_status,
        payload=payload or {"id": int(order.broker_order_id), "status": order.status},
        rate_limit={},
    )
