"""Classifying every way an order POST can end.

This is the most consequential classification in the system.  Trading 212's
order endpoint is documented as **not idempotent**, so the difference between
"the bytes never left" and "the bytes may have left" decides whether another
attempt is permitted at all -- and getting it wrong in the permissive direction
buys the same stock twice.

Every case here is driven through an ``httpx.MockTransport``, because
manufacturing a real broker failure would mean placing real duplicate orders to
observe them.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from stockbrain.broker.trading212_orders import (
    MARKET_ORDER_PATH,
    MARKET_ORDER_RATE_PER_MINUTE,
    Trading212OrderClient,
    _market_order_body,
)
from stockbrain.config import Settings
from stockbrain.enums import ExecutionFailure
from stockbrain.errors import (
    AmbiguousTransportFailure,
    BrokerRejection,
    DefinitePreSendFailure,
    ExecutionNotPermitted,
)

ACCEPTED = {
    "id": 730054321,
    "ticker": "AAPL_US_EQ",
    "quantity": 2,
    "filledQuantity": 0,
    "side": "BUY",
    "status": "NEW",
    "type": "MARKET",
    "strategy": "QUANTITY",
    "timeInForce": "DAY",
    "currency": "USD",
    "createdAt": "2026-09-05T12:00:00Z",
    "extendedHours": False,
    "initiatedFrom": "API",
    "instrument": {
        "ticker": "AAPL_US_EQ",
        "name": "Apple Inc.",
        "isin": "US0378331005",
        "currency": "USD",
    },
}


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "stockbrain_secret_key": "test-secret-key",
        "t212_api_key": "demo-key",
        "t212_api_secret": "demo-secret",
        "t212_env": "demo",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def client_for(handler: object, **setting_overrides: object) -> Trading212OrderClient:
    resolved = settings(**setting_overrides)
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return Trading212OrderClient(
        resolved,
        client=httpx.AsyncClient(
            base_url=resolved.t212_base_url,
            transport=transport,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        ),
    )


async def submit(client: Trading212OrderClient, **overrides: object) -> object:
    kwargs: dict[str, object] = {
        "broker_ticker": "AAPL_US_EQ",
        "signed_quantity": Decimal("2"),
        "extended_hours": False,
        "broker_environment": "demo",
    }
    kwargs.update(overrides)
    return await client.submit_market_order(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The request on the wire
# ---------------------------------------------------------------------------
def test_the_body_carries_the_quantity_as_an_exact_decimal_literal() -> None:
    """``json.dumps`` cannot serialise a ``Decimal`` and ``float()`` would lie.

    The body is rendered by hand so the number the broker reads is
    character-for-character the quantity that was authorized.
    """
    body = _market_order_body(
        broker_ticker="AAPL_US_EQ", signed_quantity=Decimal("0.10"), extended_hours=False
    )
    assert '"quantity": 0.10' in body
    parsed = json.loads(body)
    assert parsed == {"ticker": "AAPL_US_EQ", "quantity": 0.10, "extendedHours": False}
    # A quantity that a float could not represent exactly still round-trips as
    # the literal we wrote.
    awkward = _market_order_body(
        broker_ticker="X", signed_quantity=Decimal("-3.3000000000"), extended_hours=True
    )
    assert '"quantity": -3.3000000000' in awkward
    assert '"extendedHours": true' in awkward


def test_the_body_escapes_the_ticker_rather_than_interpolating_it() -> None:
    """A ticker comes from broker metadata, but it still goes through json."""
    body = _market_order_body(
        broker_ticker='A"B', signed_quantity=Decimal("1"), extended_hours=False
    )
    assert json.loads(body)["ticker"] == 'A"B'


async def test_a_successful_submission_is_parsed_into_a_canonical_order() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=ACCEPTED,
            headers={
                "x-ratelimit-limit": "50",
                "x-ratelimit-remaining": "49",
                "x-ratelimit-period": "60",
                "x-ratelimit-used": "1",
            },
        )

    client = client_for(handler)
    response = await submit(client)
    await client.aclose()

    assert seen["method"] == "POST"
    assert str(seen["url"]).endswith(MARKET_ORDER_PATH)
    assert seen["body"] == {"ticker": "AAPL_US_EQ", "quantity": 2, "extendedHours": False}
    assert response.order.id == 730054321  # type: ignore[attr-defined]
    assert response.order.side == "BUY"  # type: ignore[attr-defined]
    assert response.order.placed_by_api  # type: ignore[attr-defined]
    assert response.http_status == 200  # type: ignore[attr-defined]
    assert response.rate_limit is not None  # type: ignore[attr-defined]
    assert response.rate_limit.remaining == 49  # type: ignore[attr-defined]


async def test_a_sell_transmits_a_negative_quantity() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={**ACCEPTED, "side": "SELL", "quantity": -2})

    client = client_for(handler)
    await submit(client, signed_quantity=Decimal("-2"))
    await client.aclose()
    assert seen["body"] == {"ticker": "AAPL_US_EQ", "quantity": -2, "extendedHours": False}


# ---------------------------------------------------------------------------
# Definite refusals: a complete response is proof the broker decided
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "category"),
    [
        (400, ExecutionFailure.BROKER_REJECTED),
        (401, ExecutionFailure.BROKER_AUTH_REJECTED),
        (403, ExecutionFailure.BROKER_AUTH_REJECTED),
        (404, ExecutionFailure.BROKER_REJECTED),
        (422, ExecutionFailure.BROKER_REJECTED),
    ],
)
async def test_a_documented_refusal_is_definite(status: int, category: ExecutionFailure) -> None:
    """400 is "failed validation"; 403 is a missing ``orders:execute`` scope.

    Both are complete HTTP responses, which is proof the broker decided -- so no
    order exists and the proposal can fail cleanly rather than needing
    reconciliation.
    """
    client = client_for(lambda request: httpx.Response(status, json={"detail": "no"}))
    with pytest.raises(BrokerRejection) as excinfo:
        await submit(client)
    await client.aclose()
    assert excinfo.value.status == status
    assert excinfo.value.category == category.value


#: The demo API's refusal for a quantity with more than 4 decimal places,
#: captured verbatim today.  The reason has to survive the client.
QUANTITY_PRECISION_BODY = {
    "type": "/api-errors/quantity-precision-mismatch",
    "title": "Error while placing the order",
    "status": 400,
    "detail": "invalid quantity precision 4",
    "traceId": "0af7651916cd43dd8448eb211c80319c",
}


async def test_a_rejection_carries_the_brokers_reason() -> None:
    """HTTP 400 was already definitive; now it also says *why*."""
    client = client_for(lambda request: httpx.Response(400, json=QUANTITY_PRECISION_BODY))
    with pytest.raises(BrokerRejection) as excinfo:
        await submit(client)
    await client.aclose()
    assert excinfo.value.status == 400
    assert excinfo.value.detail == "invalid quantity precision 4"
    assert excinfo.value.payload is not None
    assert excinfo.value.payload["type"].endswith("quantity-precision-mismatch")


async def test_a_rejection_falls_back_to_the_title_then_the_raw_text() -> None:
    """A body that is not the documented shape must still yield something useful."""
    titled = client_for(lambda request: httpx.Response(400, json={"title": "Error while placing"}))
    with pytest.raises(BrokerRejection) as excinfo:
        await submit(titled)
    await titled.aclose()
    assert excinfo.value.detail == "Error while placing"

    raw = client_for(lambda request: httpx.Response(400, text="<html>bad request</html>"))
    with pytest.raises(BrokerRejection) as excinfo:
        await submit(raw)
    await raw.aclose()
    assert excinfo.value.detail == "<html>bad request</html>"
    assert excinfo.value.payload is None


# ---------------------------------------------------------------------------
# Ambiguity: a response that says nothing about the order
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504, 418])
async def test_a_status_that_proves_nothing_is_ambiguous(status: int) -> None:
    """408 and 429 are documented for this endpoint; 5xx is not documented at all.

    None of them says whether an order was created.  Trading 212 does not state
    whether its rate limiter runs before or after order acceptance, so a 429 on
    a mutation is treated as unknown rather than as a refusal -- the
    conservative direction, and the only one that cannot lose an order.
    """
    client = client_for(lambda request: httpx.Response(status, text="nope"))
    with pytest.raises(AmbiguousTransportFailure):
        await submit(client)
    await client.aclose()


async def test_an_accepted_order_with_an_unreadable_body_is_ambiguous() -> None:
    """The most dangerous shape there is.

    The broker accepted the order and StockBrain cannot read its id.  Treating
    it as a failure would release the reservation for a position that exists.
    """
    client = client_for(lambda request: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(AmbiguousTransportFailure, match="could not be parsed"):
        await submit(client)
    await client.aclose()


async def test_a_success_body_missing_the_order_id_is_ambiguous() -> None:
    client = client_for(lambda request: httpx.Response(200, json={"status": "NEW"}))
    with pytest.raises(AmbiguousTransportFailure, match="could not be parsed"):
        await submit(client)
    await client.aclose()


async def test_a_success_body_that_is_not_an_object_is_ambiguous() -> None:
    client = client_for(lambda request: httpx.Response(200, json=[1, 2, 3]))
    with pytest.raises(AmbiguousTransportFailure):
        await submit(client)
    await client.aclose()


# ---------------------------------------------------------------------------
# Transport failures: the classification that matters most
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "exception",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("dns timed out"),
        httpx.PoolTimeout("no connection available"),
        httpx.ProxyError("proxy refused"),
        httpx.LocalProtocolError("bad request construction"),
    ],
)
async def test_a_failure_before_the_request_line_is_definite(
    exception: Exception,
) -> None:
    """These cannot occur once a request line has been written.

    That proof is what permits another attempt, and it is the only thing that
    ever retracts ``sent_to_broker``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise exception

    client = client_for(handler)
    with pytest.raises(DefinitePreSendFailure):
        await submit(client)
    await client.aclose()


@pytest.mark.parametrize(
    "exception",
    [
        httpx.WriteTimeout("timed out writing"),
        httpx.WriteError("connection reset while writing"),
        httpx.ReadTimeout("timed out waiting for a response"),
        httpx.ReadError("connection reset"),
        httpx.RemoteProtocolError("server disconnected"),
        httpx.CloseError("close failed"),
    ],
)
async def test_a_failure_after_the_request_may_have_left_is_ambiguous(
    exception: Exception,
) -> None:
    """A read timeout is the canonical case: the order was fully sent.

    Every one of these leaves the outcome unknown, and unknown must never be
    read as "safe to send again".
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise exception

    client = client_for(handler)
    with pytest.raises(AmbiguousTransportFailure):
        await submit(client)
    await client.aclose()


# ---------------------------------------------------------------------------
# Refusals before the socket
# ---------------------------------------------------------------------------
async def test_an_environment_mismatch_refuses_before_opening_a_socket() -> None:
    """Belt and braces with the service check and the composite foreign key.

    A client is a thing that can be mis-wired; this is the last place to catch
    it, and it catches it without transmitting.
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=ACCEPTED)

    client = client_for(handler)
    with pytest.raises(ExecutionNotPermitted, match="environment"):
        await submit(client, broker_environment="live")
    await client.aclose()
    assert calls == 0


async def test_a_zero_quantity_order_is_refused_before_transmission() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=ACCEPTED)

    client = client_for(handler)
    with pytest.raises(DefinitePreSendFailure, match="zero-quantity"):
        await submit(client, signed_quantity=Decimal("0"))
    await client.aclose()
    assert calls == 0


# ---------------------------------------------------------------------------
# Rate limiting and environment
# ---------------------------------------------------------------------------
async def test_the_local_order_bucket_denies_rather_than_waits() -> None:
    """A denial must be provably pre-send, so it cannot block inside the send.

    The bucket is sized one request below the documented 50/minute because
    Trading 212 applies limits *per account*, not per key or per IP, so another
    client of the same account can consume part of the window.
    """
    client = client_for(lambda request: httpx.Response(200, json=ACCEPTED))
    assert MARKET_ORDER_RATE_PER_MINUTE == 50
    assert await client.reserve_order_slot() is True
    # Burst of one: the next request in the same instant is denied, not queued.
    assert await client.reserve_order_slot() is False
    await client.aclose()


def test_the_client_reports_the_environment_its_base_url_points_at() -> None:
    demo = client_for(lambda request: httpx.Response(200), t212_env="demo")
    assert demo.environment == "demo"


def test_the_authorization_header_is_basic_and_never_rendered() -> None:
    """Key as username, secret as password, exactly as documented."""
    resolved = settings(t212_api_key="the-key-value", t212_api_secret="the-secret-value")
    headers = Trading212OrderClient._auth_headers(resolved)
    assert headers["Authorization"].startswith("Basic ")
    assert "the-key-value" not in headers["Authorization"]
    assert "the-secret-value" not in headers["Authorization"]
    import base64

    decoded = base64.b64decode(headers["Authorization"].split(" ", 1)[1]).decode()
    assert decoded == "the-key-value:the-secret-value"


async def test_redirects_are_not_followed_on_the_order_client() -> None:
    """A redirect on a POST would resend the body to an unauthenticated host."""
    client = client_for(
        lambda request: httpx.Response(307, headers={"location": "https://evil.example/x"})
    )
    with pytest.raises(AmbiguousTransportFailure):
        await submit(client)
    await client.aclose()
