"""The one place in StockBrain that can change something at a broker.

Verified against Trading 212's current published documentation on 2026-09-05
(<https://docs.trading212.com/api.md> and the per-operation pages beneath it):

* ``POST /api/v0/equity/orders/market`` -- body ``{ticker, quantity,
  extendedHours}``, rate limit **50 req / 1m**, documented failure statuses
  **400 (failed validation), 401 (bad API key), 403 (scope ``orders:execute``
  missing), 408 (timed-out), 429 (limited)**.  No 5xx is documented.
* **Positive quantity buys, negative quantity sells.**  Trading 212 calls this
  "a core convention of the API".
* **The endpoint is explicitly documented as not idempotent**: "Sending the same
  request multiple times may result in duplicate orders."  There is no
  idempotency key, no client-supplied reference, and nothing in the request that
  would let the broker collapse a duplicate.
* ``GET /api/v0/equity/orders`` (pending, 1 req/5s), ``GET
  /api/v0/equity/orders/{id}`` (1 req/1s) and ``GET
  /api/v0/equity/history/orders`` (6 req/1m, cursor paginated, optional
  ``ticker`` filter) are the read-only endpoints reconciliation uses.
* Orders execute only in the primary account currency.

Three deliberate departures from the rest of the codebase:

1. **This client does not use** :class:`~stockbrain.httpclient.ProviderHttpClient`
   for the POST.  That class converts every transport failure into one
   ``ProviderUnavailable``, which erases the only distinction that matters here:
   whether the bytes left.  The order path therefore drives ``httpx`` directly
   so it can classify ``ConnectError`` (nothing sent) apart from ``ReadTimeout``
   (sent, answer unknown).
2. **The quantity is serialised from its ``Decimal`` without passing through a
   binary float.**  The JSON body is rendered by hand so the number on the wire
   is character-for-character the quantity that was authorized.
3. **There is no cancel, amend or modify method, and there must never be one.**
   Trading 212 documents ``DELETE /equity/orders/{id}``; StockBrain does not
   call it.  Cancellation has its own race with a fill, and a kill switch that
   cancelled would be making a trading decision of its own.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stockbrain.config import Settings
from stockbrain.enums import ExecutionFailure
from stockbrain.errors import (
    AmbiguousTransportFailure,
    BrokerRejection,
    DefinitePreSendFailure,
    ExecutionNotPermitted,
    ProviderResponseError,
)
from stockbrain.httpclient import ProviderHttpClient, RateLimitSnapshot, TokenBucket
from stockbrain.logging import get_logger

__all__ = [
    "HISTORY_ORDERS_PATH",
    "MARKET_ORDER_PATH",
    "MARKET_ORDER_RATE_PER_MINUTE",
    "ORDERS_PATH",
    "SubmissionResponse",
    "T212Order",
    "Trading212OrderClient",
]

log = get_logger(__name__)

MARKET_ORDER_PATH = "/equity/orders/market"
ORDERS_PATH = "/equity/orders"
HISTORY_ORDERS_PATH = "/equity/history/orders"

#: Documented limits.  The order bucket is set one request below the published
#: ceiling: the limiter is shared per *account* rather than per key or per IP,
#: so another client of the same account can consume part of the window.
MARKET_ORDER_RATE_PER_MINUTE = 50
_ORDER_BUCKET_RATE = (MARKET_ORDER_RATE_PER_MINUTE - 1) / 60.0
_PENDING_ORDERS_RATE = 1.0 / 5.0
_ORDER_BY_ID_RATE = 1.0
_HISTORY_RATE = 6.0 / 60.0

#: Trading 212's own account of who created an order.  StockBrain's orders read
#: ``API``; an order placed in the mobile app does not.
INITIATED_FROM_API = "API"

#: httpx failures that provably occur *before* a request line is written.  Every
#: other transport failure is ambiguous, because "the connection broke" and "the
#: connection broke after the broker read the order" are indistinguishable from
#: this side of the socket.
_PRE_SEND_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.LocalProtocolError,
    httpx.UnsupportedProtocol,
    httpx.InvalidURL,
)

#: Statuses the broker documents as a definitive refusal.  A complete HTTP
#: response is proof the broker decided, so no order exists.
_DEFINITE_REJECTION_STATUSES: dict[int, ExecutionFailure] = {
    400: ExecutionFailure.BROKER_REJECTED,
    401: ExecutionFailure.BROKER_AUTH_REJECTED,
    403: ExecutionFailure.BROKER_AUTH_REJECTED,
    404: ExecutionFailure.BROKER_REJECTED,
    422: ExecutionFailure.BROKER_REJECTED,
}

#: Statuses where a complete response arrived but says nothing about whether an
#: order was created.  408 is documented for this endpoint; 429 is documented
#: without saying whether the limiter runs before or after acceptance, so it is
#: treated as unknown rather than as a refusal.
_AMBIGUOUS_STATUSES: dict[int, ExecutionFailure] = {
    408: ExecutionFailure.BROKER_TIMEOUT,
    429: ExecutionFailure.BROKER_RATE_LIMITED,
}


class T212OrderInstrument(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    ticker: str | None = None
    name: str | None = None
    isin: str | None = None
    currency: str | None = None


class T212Order(BaseModel):
    """One order as Trading 212 reports it.

    ``id`` is documented ``int64``; it is kept as an ``int`` here and rendered
    to text at the persistence boundary, because a broker order id is an
    identifier rather than a number to do arithmetic on.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    ticker: str | None = None
    quantity: Decimal | None = None
    filled_quantity: Decimal | None = Field(default=None, alias="filledQuantity")
    filled_value: Decimal | None = Field(default=None, alias="filledValue")
    value: Decimal | None = None
    limit_price: Decimal | None = Field(default=None, alias="limitPrice")
    stop_price: Decimal | None = Field(default=None, alias="stopPrice")
    side: str | None = None
    status: str | None = None
    type: str | None = None
    strategy: str | None = None
    time_in_force: str | None = Field(default=None, alias="timeInForce")
    currency: str | None = None
    created_at: dt.datetime | None = Field(default=None, alias="createdAt")
    extended_hours: bool | None = Field(default=None, alias="extendedHours")
    initiated_from: str | None = Field(default=None, alias="initiatedFrom")
    instrument: T212OrderInstrument | None = None

    @property
    def broker_ticker(self) -> str | None:
        if self.ticker:
            return self.ticker
        return self.instrument.ticker if self.instrument else None

    @property
    def placed_by_api(self) -> bool:
        return self.initiated_from == INITIATED_FROM_API


class SubmissionResponse:
    """A confirmed broker acknowledgement plus the audit trail around it."""

    __slots__ = ("http_status", "order", "payload", "rate_limit")

    def __init__(
        self,
        *,
        order: T212Order,
        http_status: int,
        payload: dict[str, Any],
        rate_limit: RateLimitSnapshot | None,
    ) -> None:
        self.order = order
        self.http_status = http_status
        self.payload = payload
        self.rate_limit = rate_limit


class Trading212OrderClient:
    """Market-order transmission and the read-only lookups reconciliation needs."""

    name = "trading212_orders"

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._environment = settings.t212_env
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.t212_base_url,
            headers=self._auth_headers(settings),
            timeout=httpx.Timeout(settings.t212_order_timeout_seconds),
            # A redirect on a POST would resend the body to a host this client
            # did not authenticate against.
            follow_redirects=False,
        )
        self._order_bucket = TokenBucket(rate_per_second=_ORDER_BUCKET_RATE, burst=1)
        self._pending_http = ProviderHttpClient(
            provider="trading212",
            base_url=settings.t212_base_url,
            client=self._client,
            rate_limiter=TokenBucket(rate_per_second=_PENDING_ORDERS_RATE),
        )
        self._by_id_http = ProviderHttpClient(
            provider="trading212",
            base_url=settings.t212_base_url,
            client=self._client,
            rate_limiter=TokenBucket(rate_per_second=_ORDER_BY_ID_RATE),
        )
        self._history_http = ProviderHttpClient(
            provider="trading212",
            base_url=settings.t212_base_url,
            client=self._client,
            rate_limiter=TokenBucket(rate_per_second=_HISTORY_RATE),
        )
        self.last_rate_limit: RateLimitSnapshot | None = None

    @property
    def environment(self) -> str:
        """The environment this client's base URL actually points at.

        Derived from configuration once, at construction, and compared against
        the *proposal's* recorded environment before every send.  A client and a
        proposal that disagree is a refusal, not a coercion.
        """
        return self._environment.value

    @staticmethod
    def _auth_headers(settings: Settings) -> dict[str, str]:
        """The documented Basic header: API key as username, secret as password.

        Built here and nowhere else, never logged, and never interpolated into
        an exception message or a persisted payload.
        """
        pair = (
            f"{settings.t212_api_key.get_secret_value()}:"
            f"{settings.t212_api_secret.get_secret_value()}"
        )
        encoded = base64.b64encode(pair.encode("utf-8")).decode("ascii")
        return {
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # The mutation
    # ------------------------------------------------------------------
    async def reserve_order_slot(self) -> bool:
        """Take a token from the local order bucket, without waiting.

        Called *before* the transaction that records ``sent_to_broker``, so a
        limiter denial is provably a pre-send condition and can be deferred
        safely.  Blocking here instead would move the wait inside the window
        where a crash means "unknown".
        """
        return await self._order_bucket.try_acquire()

    async def submit_market_order(
        self,
        *,
        broker_ticker: str,
        signed_quantity: Decimal,
        extended_hours: bool,
        broker_environment: str,
    ) -> SubmissionResponse:
        """Send exactly one market order.  Never retried, by anything, ever.

        Raises :class:`~stockbrain.errors.DefinitePreSendFailure` only when the
        bytes provably never left, :class:`BrokerRejection` when the broker
        answered and refused, and
        :class:`~stockbrain.errors.AmbiguousTransportFailure` in every other
        case -- including a 2xx whose body cannot be parsed, which means an
        order exists whose id is unknown.
        """
        if broker_environment != self.environment:
            # Belt and braces with the service-level check and the composite
            # foreign key. A client is a thing that can be mis-wired; this is
            # the last place to catch it before a socket is opened.
            raise ExecutionNotPermitted(
                f"refusing to transmit: the proposal is bound to the "
                f"{broker_environment!r} environment and this client is "
                f"{self.environment!r}"
            )
        if signed_quantity == 0:
            raise DefinitePreSendFailure("refusing to transmit a zero-quantity order")

        body = _market_order_body(
            broker_ticker=broker_ticker,
            signed_quantity=signed_quantity,
            extended_hours=extended_hours,
        )

        try:
            response = await self._client.post(MARKET_ORDER_PATH, content=body.encode("utf-8"))
        except _PRE_SEND_EXCEPTIONS as exc:
            raise DefinitePreSendFailure(
                f"trading212: the order was not transmitted ({type(exc).__name__})"
            ) from exc
        except httpx.HTTPError as exc:
            # WriteTimeout, WriteError, ReadTimeout, ReadError, RemoteProtocolError,
            # CloseError: the request line may already be on the wire.
            raise AmbiguousTransportFailure(
                f"trading212: the order may have been transmitted ({type(exc).__name__})"
            ) from exc

        self.last_rate_limit = RateLimitSnapshot.from_headers(response.headers)
        status = response.status_code

        if status in _DEFINITE_REJECTION_STATUSES:
            raise BrokerRejection(
                f"trading212: the broker refused the order (HTTP {status})",
                status=status,
                category=_DEFINITE_REJECTION_STATUSES[status].value,
            )
        if not response.is_success:
            category = _AMBIGUOUS_STATUSES.get(status, ExecutionFailure.UNEXPECTED_STATUS)
            raise AmbiguousTransportFailure(
                f"trading212: HTTP {status} says nothing about whether the order "
                f"was created ({category.value})"
            )

        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("expected a JSON object")
            order = T212Order.model_validate(payload)
        except (ValueError, ValidationError) as exc:
            # The most dangerous shape there is: the broker accepted the order
            # and StockBrain cannot read its id. Reconciliation must find it.
            raise AmbiguousTransportFailure(
                "trading212: the order was accepted but the response could not be "
                f"parsed ({type(exc).__name__}); the order id is unknown"
            ) from exc

        return SubmissionResponse(
            order=order,
            http_status=status,
            payload=payload,
            rate_limit=self.last_rate_limit,
        )

    # ------------------------------------------------------------------
    # Read-only lookups for reconciliation
    # ------------------------------------------------------------------
    async def fetch_pending_orders(self) -> list[T212Order]:
        payload = await self._pending_http.get_json(ORDERS_PATH, max_attempts=2)
        self.last_rate_limit = self._pending_http.last_rate_limit
        if not isinstance(payload, list):
            raise ProviderResponseError(
                f"trading212: {ORDERS_PATH} returned {type(payload).__name__}, expected an array"
            )
        return [_parse_order(item) for item in payload if isinstance(item, dict)]

    async def fetch_order(self, broker_order_id: str) -> T212Order | None:
        """Look one order up by id.  ``None`` when the broker has no such order.

        A pending-order lookup 404s once the order is filled or cancelled, which
        is not an error here: it means "look in the history instead".
        """
        from stockbrain.errors import ProviderResponseError as _Missing

        try:
            payload = await self._by_id_http.get_json(
                f"{ORDERS_PATH}/{broker_order_id}", max_attempts=2
            )
        except _Missing:
            return None
        self.last_rate_limit = self._by_id_http.last_rate_limit
        if not isinstance(payload, dict):
            return None
        return _parse_order(payload)

    async def fetch_order_history(
        self, *, broker_ticker: str | None = None, limit: int = 50
    ) -> list[T212Order]:
        """Recent historical orders, optionally filtered to one instrument.

        One page only.  The window reconciliation cares about is minutes wide,
        the page holds the documented maximum of 50 items, and walking
        ``nextPagePath`` to the beginning of time would spend a 6-per-minute
        budget to answer a question the first page already answers.
        """
        params: dict[str, Any] = {"limit": min(max(limit, 1), 50)}
        if broker_ticker:
            params["ticker"] = broker_ticker
        payload = await self._history_http.get_json(
            HISTORY_ORDERS_PATH, params=params, max_attempts=2
        )
        self.last_rate_limit = self._history_http.last_rate_limit
        if not isinstance(payload, dict):
            raise ProviderResponseError(
                f"trading212: {HISTORY_ORDERS_PATH} returned "
                f"{type(payload).__name__}, expected an object"
            )
        items = payload.get("items")
        if not isinstance(items, list):
            raise ProviderResponseError(
                f"trading212: {HISTORY_ORDERS_PATH} returned no 'items' array"
            )
        orders: list[T212Order] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            # The history wraps each order in `{fill: ..., order: ...}`.
            raw = item.get("order") if isinstance(item.get("order"), dict) else item
            if isinstance(raw, dict):
                orders.append(_parse_order(raw))
        return orders

    def rate_limit_snapshot(self) -> dict[str, Any]:
        snapshot = self.last_rate_limit
        return snapshot.as_dict() if snapshot is not None else {}


def _parse_order(payload: dict[str, Any]) -> T212Order:
    try:
        return T212Order.model_validate(payload)
    except ValidationError as exc:
        raise ProviderResponseError(
            "trading212: an order payload did not match the documented schema "
            f"({exc.error_count()} validation error(s))"
        ) from exc


def _market_order_body(
    *, broker_ticker: str, signed_quantity: Decimal, extended_hours: bool
) -> str:
    """Render the request body with the quantity as an exact decimal literal.

    ``json.dumps`` cannot serialise a ``Decimal`` and ``float(quantity)`` would
    put a binary approximation of an authorized share count on the wire.  The
    quantity is therefore formatted with ``format(q, 'f')`` -- a plain,
    non-scientific decimal that is also a valid JSON number -- and spliced in.
    The result is byte-identical to what the fingerprint hashes.
    """
    ticker = json.dumps(broker_ticker)
    quantity = format(signed_quantity, "f")
    hours = "true" if extended_hours else "false"
    return f'{{"ticker": {ticker}, "quantity": {quantity}, "extendedHours": {hours}}}'
