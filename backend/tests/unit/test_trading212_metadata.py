"""Trading 212 metadata client.

Read-only by construction: there is no order method on this client, and a test
below asserts that stays true.

Every payload here is the shape Trading 212's *current* documentation specifies
(2026-09-04).  Where StockBrain's own specification differed, the test encodes
the documented behaviour and says so.
"""

from __future__ import annotations

import base64
import datetime as dt

import httpx
import pytest

from stockbrain.broker.trading212_metadata import (
    EXCHANGES_PATH,
    INSTRUMENTS_PATH,
    Trading212MetadataClient,
)
from stockbrain.config import Settings
from stockbrain.errors import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
)

INSTRUMENT = {
    "addedOn": "2019-08-24T14:15:22Z",
    "currencyCode": "USD",
    "extendedHours": True,
    "isin": "US0378331005",
    "maxOpenQuantity": 55000.0,
    "name": "Apple Inc.",
    "shortName": "AAPL",
    "ticker": "AAPL_US_EQ",
    "type": "STOCK",
    "workingScheduleId": 74,
}

EXCHANGE = {
    "id": 331,
    "name": "NASDAQ",
    "workingSchedules": [
        {
            "id": 74,
            "timeEvents": [
                {"date": "2026-09-04T08:00:00Z", "type": "PRE_MARKET_OPEN"},
                {"date": "2026-09-04T13:30:00Z", "type": "OPEN"},
                {"date": "2026-09-04T20:00:00Z", "type": "CLOSE"},
            ],
        }
    ],
}

#: Documented rate-limit headers: limit, period, remaining, reset, used.
RATE_LIMIT_HEADERS = {
    "x-ratelimit-limit": "1",
    "x-ratelimit-period": "50",
    "x-ratelimit-remaining": "0",
    "x-ratelimit-reset": "1788550000",
    "x-ratelimit-used": "1",
}


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "t212_api_key": "key-abc",
        "t212_api_secret": "secret-xyz",
        "t212_env": "demo",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _client(handler: httpx.MockTransport, **overrides: object) -> Trading212MetadataClient:
    settings = _settings(**overrides)
    client = Trading212MetadataClient(
        settings,
        client=httpx.AsyncClient(
            transport=handler,
            base_url=settings.t212_base_url,
            headers=Trading212MetadataClient._auth_headers(settings),
        ),
    )
    # The documented limits are 1 request per 30-50 seconds. Honouring them in a
    # unit test would make the suite take minutes, so the buckets are replaced
    # with unrestricted ones; a separate test asserts the real rates.
    client._instruments_http._rate_limiter = None
    client._exchanges_http._rate_limiter = None
    return client


# ---------------------------------------------------------------------------
# Normal responses
# ---------------------------------------------------------------------------
async def test_instruments_parse_every_documented_field() -> None:
    """The documented response has no exchange and no minTradeQuantity.

    Both absences are load-bearing: the exchange is derived from the working
    schedule, and ``min_trade_quantity`` must stay unset rather than invented.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(INSTRUMENTS_PATH)
        return httpx.Response(200, json=[INSTRUMENT])

    client = _client(httpx.MockTransport(handler))
    instruments = await client.fetch_instruments()
    await client.aclose()

    assert len(instruments) == 1
    item = instruments[0]
    assert item.ticker == "AAPL_US_EQ"
    assert item.short_name == "AAPL"
    assert item.name == "Apple Inc."
    assert item.isin == "US0378331005"
    assert item.currency_code == "USD"
    assert item.type == "STOCK"
    assert item.extended_hours is True
    assert item.max_open_quantity == 55000.0
    assert item.working_schedule_id == 74
    assert item.added_on == dt.datetime(2019, 8, 24, 14, 15, 22, tzinfo=dt.UTC)
    # Absent from the current API; the spec assumed it existed.
    assert item.min_trade_quantity is None


async def test_exchanges_parse_schedules_and_time_events() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(EXCHANGES_PATH)
        return httpx.Response(200, json=[EXCHANGE])

    client = _client(httpx.MockTransport(handler))
    exchanges = await client.fetch_exchanges()
    await client.aclose()

    assert exchanges[0].id == 331
    assert exchanges[0].name == "NASDAQ"
    assert exchanges[0].working_schedules[0].id == 74
    assert [event.type for event in exchanges[0].working_schedules[0].time_events] == [
        "PRE_MARKET_OPEN",
        "OPEN",
        "CLOSE",
    ]


async def test_unknown_fields_do_not_break_a_sync() -> None:
    """A provider adding a field must not stop instrument resolution working."""
    payload = {**INSTRUMENT, "someBrandNewField": {"nested": True}}
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json=[payload])))
    instruments = await client.fetch_instruments()
    await client.aclose()
    assert instruments[0].ticker == "AAPL_US_EQ"


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------
async def test_auth_failure_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "unauthorised"})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(ProviderAuthError):
        await client.fetch_instruments()
    await client.aclose()
    assert calls == 1, "retrying a bad credential only burns the rate limit"


async def test_forbidden_is_an_auth_error() -> None:
    client = _client(httpx.MockTransport(lambda _: httpx.Response(403, json={})))
    with pytest.raises(ProviderAuthError):
        await client.fetch_exchanges()
    await client.aclose()


async def test_rate_limited_is_classified_and_carries_the_retry_hint() -> None:
    client = _client(
        httpx.MockTransport(lambda _: httpx.Response(429, headers={"retry-after": "50"}, json={}))
    )
    with pytest.raises(ProviderRateLimited) as excinfo:
        await client.fetch_instruments()
    await client.aclose()
    assert excinfo.value.retry_after_seconds == 50.0


async def test_server_error_is_transient_and_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={})

    client = _client(httpx.MockTransport(handler))
    client._instruments_http._backoff_base = 0.001
    client._instruments_http._backoff_max = 0.002
    with pytest.raises(ProviderUnavailable):
        await client.fetch_instruments()
    await client.aclose()
    assert calls == 2, "a GET is safe to repeat, bounded at two attempts"


async def test_request_timeout_408_is_transient() -> None:
    """408 is a documented status for both metadata endpoints."""
    client = _client(httpx.MockTransport(lambda _: httpx.Response(408, json={})))
    client._instruments_http._backoff_base = 0.001
    with pytest.raises(ProviderUnavailable):
        await client.fetch_instruments()
    await client.aclose()


async def test_a_json_object_where_an_array_is_documented_is_rejected() -> None:
    """Coercing an unexpected shape is how wrong data reaches resolution."""
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json={"items": []})))
    with pytest.raises(ProviderResponseError):
        await client.fetch_instruments()
    await client.aclose()


async def test_an_instrument_without_a_ticker_is_rejected() -> None:
    """The ticker is the broker's identity for the instrument; there is no fallback."""
    payload = {key: value for key, value in INSTRUMENT.items() if key != "ticker"}
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json=[payload])))
    with pytest.raises(ProviderResponseError):
        await client.fetch_instruments()
    await client.aclose()


async def test_malformed_json_body_is_a_response_error() -> None:
    client = _client(
        httpx.MockTransport(lambda _: httpx.Response(200, content=b"not json", headers={}))
    )
    with pytest.raises(ProviderResponseError):
        await client.fetch_exchanges()
    await client.aclose()


# ---------------------------------------------------------------------------
# Rate-limit headers
# ---------------------------------------------------------------------------
async def test_rate_limit_headers_are_recorded() -> None:
    """Trading 212 documents limit/period/remaining/reset/used on every response."""
    client = _client(
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=[INSTRUMENT], headers=RATE_LIMIT_HEADERS)
        )
    )
    await client.fetch_instruments()
    snapshot = client.rate_limit_snapshot()
    await client.aclose()

    assert snapshot["limit"] == 1
    assert snapshot["remaining"] == 0
    assert snapshot["used"] == 1
    assert snapshot["reset"] == 1788550000
    assert snapshot["period"] == "50"


async def test_each_endpoint_gets_its_own_documented_rate_limit() -> None:
    """1 req/50s for instruments and 1 req/30s for exchanges are different budgets.

    A single shared bucket would either throttle the cheaper call or overrun the
    stricter one.
    """
    settings = _settings()
    client = Trading212MetadataClient(
        settings,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))
        ),
    )
    instruments_bucket = client._instruments_http._rate_limiter
    exchanges_bucket = client._exchanges_http._rate_limiter
    await client.aclose()

    assert instruments_bucket is not None
    assert exchanges_bucket is not None
    instruments_rate = instruments_bucket._rate
    exchanges_rate = exchanges_bucket._rate

    assert instruments_rate == pytest.approx(1 / 50)
    assert exchanges_rate == pytest.approx(1 / 30)


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
def test_auth_header_is_basic_key_and_secret() -> None:
    """Documented scheme: API key as username, API secret as password.

    The legacy API-key header scheme also exists in the docs; targeting it would
    be a silent downgrade, so it is never sent.
    """
    settings = _settings()
    headers = Trading212MetadataClient._auth_headers(settings)
    assert headers["Authorization"].startswith("Basic ")
    decoded = base64.b64decode(headers["Authorization"].split(" ", 1)[1]).decode()
    assert decoded == "key-abc:secret-xyz"
    assert "legacyApiKeyHeader" not in headers
    assert not any(key.lower().startswith("x-api") for key in headers)


async def test_error_messages_never_contain_the_credential() -> None:
    """A provider error reaches logs and the API; a secret must not ride along."""
    client = _client(httpx.MockTransport(lambda _: httpx.Response(401, text="denied")))
    with pytest.raises(ProviderAuthError) as excinfo:
        await client.fetch_instruments()
    await client.aclose()
    message = str(excinfo.value)
    assert "key-abc" not in message
    assert "secret-xyz" not in message


def test_the_metadata_client_exposes_no_mutation_method() -> None:
    """Phase 4 grants read access only. Order placement arrives in Phase 8."""
    surface = {name for name in dir(Trading212MetadataClient) if not name.startswith("_")}
    forbidden = {"place_order", "place_market_order", "cancel_order", "post", "delete"}
    assert not surface & forbidden
    assert surface == {
        "aclose",
        "fetch_exchanges",
        "fetch_instruments",
        "name",
        "rate_limit_snapshot",
    }
