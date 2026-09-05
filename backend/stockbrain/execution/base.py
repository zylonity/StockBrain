"""The broker execution interface, and the Trading 212 adapter for it.

The interface is deliberately four operations wide and no wider.  There is no
"call this endpoint" escape hatch, no generic request method and no cancel: a
caller can reserve a rate-limit slot, submit one command, look one order up, and
search for orders that might match an attempt.  Anything a future broker needs
beyond that is a change to this file, reviewed as such.

Cancellation is absent on purpose.  Trading 212 documents
``DELETE /equity/orders/{id}``, and Phase 8 does not call it: cancelling races a
fill, its failure modes are a second unknown on top of the first, and a kill
switch that cancelled would be making a trading decision rather than stopping
one.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable

from stockbrain.broker.trading212_orders import T212Order, Trading212OrderClient
from stockbrain.enums import Broker
from stockbrain.errors import ProviderError
from stockbrain.execution.models import (
    BrokerAcknowledgement,
    BrokerOrderView,
    CandidateSearch,
    ExecutionCommand,
)
from stockbrain.logging import get_logger

__all__ = ["BrokerExecutionProvider", "Trading212ExecutionProvider"]

log = get_logger(__name__)


@runtime_checkable
class BrokerExecutionProvider(Protocol):
    """What the execution service is allowed to ask a broker to do."""

    broker: Broker

    @property
    def environment(self) -> str:
        """The environment this provider's credentials and base URL point at."""
        ...

    async def reserve_slot(self) -> bool:
        """Take a local rate-limit token without waiting.

        Must be called before the transaction that records a send, so that a
        denial is provably a pre-send condition.
        """
        ...

    async def submit(self, command: ExecutionCommand) -> BrokerAcknowledgement:
        """Transmit exactly once.  Never retried by the caller or the callee."""
        ...

    async def fetch_order(self, broker_order_id: str) -> BrokerOrderView | None:
        """Look one order up by broker id, or ``None`` if the broker has none."""
        ...

    async def find_candidates(
        self, *, broker_ticker: str, since: dt.datetime, until: dt.datetime
    ) -> CandidateSearch:
        """Every order that could plausibly be the one an attempt created."""
        ...


class Trading212ExecutionProvider:
    """Adapts :class:`~stockbrain.broker.trading212_orders.Trading212OrderClient`.

    The adapter's whole job is to convert: a StockBrain command in, a canonical
    DTO out.  No decision is made here -- not whether to send, not whether a
    candidate matches, not what a failure means.  Those belong to the execution
    service and the reconciler, which are broker-neutral because of this file.
    """

    broker = Broker.TRADING212

    def __init__(self, client: Trading212OrderClient) -> None:
        self._client = client

    @property
    def environment(self) -> str:
        return self._client.environment

    async def reserve_slot(self) -> bool:
        return await self._client.reserve_order_slot()

    async def submit(self, command: ExecutionCommand) -> BrokerAcknowledgement:
        response = await self._client.submit_market_order(
            broker_ticker=command.broker_ticker,
            signed_quantity=command.signed_quantity,
            extended_hours=command.extended_hours,
            broker_environment=command.broker_environment,
        )
        return BrokerAcknowledgement(
            order=_view(response.order),
            http_status=response.http_status,
            payload=response.payload,
            rate_limit=response.rate_limit.as_dict() if response.rate_limit else {},
        )

    async def fetch_order(self, broker_order_id: str) -> BrokerOrderView | None:
        order = await self._client.fetch_order(broker_order_id)
        return _view(order) if order is not None else None

    async def find_candidates(
        self, *, broker_ticker: str, since: dt.datetime, until: dt.datetime
    ) -> CandidateSearch:
        """Read both the pending list and the history, and report which answered.

        Both are consulted because an order can be in either: still working, or
        already filled.  Their individual success is reported separately,
        because only a search where *both* answered can support the conclusion
        that no order exists.
        """
        pending_ok = False
        history_ok = False
        error_category: str | None = None
        found: dict[str, BrokerOrderView] = {}

        try:
            for order in await self._client.fetch_pending_orders():
                view = _view(order)
                found[view.broker_order_id] = view
            pending_ok = True
        except ProviderError as exc:
            error_category = type(exc).__name__
            log.warning("reconciliation_pending_read_failed", error_category=error_category)

        try:
            for order in await self._client.fetch_order_history(broker_ticker=broker_ticker):
                view = _view(order)
                found.setdefault(view.broker_order_id, view)
            history_ok = True
        except ProviderError as exc:
            error_category = type(exc).__name__
            log.warning("reconciliation_history_read_failed", error_category=error_category)

        scanned = len(found)
        candidates = tuple(
            view
            for view in found.values()
            if view.broker_ticker == broker_ticker
            and view.created_at is not None
            and since <= view.created_at <= until
        )
        return CandidateSearch(
            candidates=candidates,
            pending_ok=pending_ok,
            history_ok=history_ok,
            scanned=scanned,
            error_category=error_category,
        )


def _view(order: T212Order) -> BrokerOrderView:
    """Trading 212's order shape, normalised.

    ``quantity`` arrives signed, exactly as it was sent, so the sign survives
    into matching -- a buy for 3 and a sell for 3 on the same listing in the
    same minute are different orders and must never match each other.
    """
    return BrokerOrderView(
        broker_order_id=str(order.id),
        broker_ticker=order.broker_ticker,
        signed_quantity=order.quantity,
        filled_quantity=order.filled_quantity,
        filled_value=order.filled_value,
        status=order.status,
        order_type=order.type,
        side=order.side,
        currency=order.currency,
        created_at=order.created_at,
        initiated_from=order.initiated_from,
        raw=order.model_dump(mode="json", by_alias=True),
    )
