"""Trading 212 account and position reads.

Read-only by construction: there is no order method on this client, and a test
below asserts that stays true.

Every payload here is the shape Trading 212's *current* documentation specifies
(verified 2026-09-05 against <https://docs.trading212.com/api.md>). Two things
changed since the original specification was written and both are encoded here:
open positions moved from ``/equity/portfolio`` to ``/equity/positions``, and
the payload is nested under ``instrument`` and ``walletImpact`` rather than
flat.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import httpx
import pytest

from stockbrain.broker.trading212_account import (
    ACCOUNT_SUMMARY_PATH,
    POSITIONS_PATH,
    POSITIONS_RATE_PERIOD_SECONDS,
    SUMMARY_RATE_PERIOD_SECONDS,
    Trading212AccountClient,
)
from stockbrain.config import Settings
from stockbrain.errors import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
)

SUMMARY = {
    "cash": {"availableToTrade": 1234.56, "inPies": 10.0, "reservedForOrders": 99.5},
    "currency": "GBP",
    "id": 4242,
    "investments": {
        "currentValue": 8000.25,
        "realizedProfitLoss": 120.0,
        "totalCost": 7500.0,
        "unrealizedProfitLoss": 500.25,
    },
    "totalValue": 9234.81,
}

POSITION = {
    "averagePricePaid": 180.5,
    "createdAt": "2026-01-05T09:30:00Z",
    "currentPrice": 200.25,
    "instrument": {
        "currency": "USD",
        "isin": "US0378331005",
        "name": "Apple Inc.",
        "ticker": "AAPL_US_EQ",
    },
    "quantity": 12.5,
    "quantityAvailableForTrading": 10.0,
    "quantityInPies": 2.5,
    "walletImpact": {
        "currency": "GBP",
        "currentValue": 1975.0,
        "fxImpact": -12.5,
        "totalCost": 1800.0,
        "unrealizedProfitLoss": 175.0,
    },
}

RATE_LIMIT_HEADERS = {
    "x-ratelimit-limit": "1",
    "x-ratelimit-period": "5",
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


def _client(handler: httpx.MockTransport, **overrides: object) -> Trading212AccountClient:
    settings = _settings(**overrides)
    client = Trading212AccountClient(
        settings,
        client=httpx.AsyncClient(
            transport=handler,
            base_url=settings.t212_base_url,
            headers=Trading212AccountClient._auth_headers(settings),
        ),
    )
    # Honouring 1 req/5s in a unit test would make the suite take minutes; a
    # separate test asserts the real documented rates.
    client._summary_http._rate_limiter = None
    client._positions_http._rate_limiter = None
    return client


# ---------------------------------------------------------------------------
# Documented contract
# ---------------------------------------------------------------------------
async def test_the_account_summary_parses_every_documented_field() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(ACCOUNT_SUMMARY_PATH)
        return httpx.Response(200, json=SUMMARY, headers=RATE_LIMIT_HEADERS)

    client = _client(httpx.MockTransport(handler))
    summary = await client.fetch_account_summary()
    await client.aclose()

    assert summary.id == 4242
    assert summary.currency == "GBP"
    assert summary.total_value == Decimal("9234.81")
    assert summary.cash.available_to_trade == Decimal("1234.56")
    assert summary.cash.in_pies == Decimal("10.0")
    assert summary.cash.reserved_for_orders == Decimal("99.5")
    assert summary.investments.current_value == Decimal("8000.25")
    assert summary.investments.unrealized_profit_loss == Decimal("500.25")
    assert summary.investments.realized_profit_loss == Decimal("120.0")
    assert summary.investments.total_cost == Decimal("7500.0")


async def test_every_money_value_is_a_decimal_not_a_float() -> None:
    """A JSON number reaching a balance as a binary float is how a cash buffer
    becomes approximately a cash buffer."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SUMMARY)

    client = _client(httpx.MockTransport(handler))
    summary = await client.fetch_account_summary()
    await client.aclose()
    for value in (
        summary.total_value,
        summary.cash.available_to_trade,
        summary.investments.current_value,
    ):
        assert isinstance(value, Decimal)


async def test_positions_parse_the_nested_instrument_and_wallet_impact() -> None:
    """The current payload is nested; the older flat shape is gone.

    A client written from the old spec would look for ``ticker`` and ``ppl`` at
    the top level and find neither.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(POSITIONS_PATH)
        return httpx.Response(200, json=[POSITION])

    client = _client(httpx.MockTransport(handler))
    positions = await client.fetch_positions()
    await client.aclose()

    assert len(positions) == 1
    item = positions[0]
    assert item.broker_ticker == "AAPL_US_EQ"
    assert item.instrument.isin == "US0378331005"
    assert item.instrument.currency == "USD"
    assert item.quantity == Decimal("12.5")
    assert item.average_price_paid == Decimal("180.5")
    assert item.current_price == Decimal("200.25")
    assert item.created_at == dt.datetime(2026, 1, 5, 9, 30, tzinfo=dt.UTC)
    assert item.wallet_impact.currency == "GBP"
    assert item.wallet_impact.current_value == Decimal("1975.0")
    assert item.wallet_impact.fx_impact == Decimal("-12.5")


async def test_quantity_available_for_trading_is_kept_separate_from_the_total() -> None:
    """Shares inside a pie are owned but not individually tradable.

    Sizing a reduction against ``quantity`` would produce an order the broker
    refuses, so the two numbers stay distinct all the way to the risk engine.
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[POSITION])

    client = _client(httpx.MockTransport(handler))
    position = (await client.fetch_positions())[0]
    await client.aclose()
    assert position.quantity == Decimal("12.5")
    assert position.quantity_available_for_trading == Decimal("10.0")
    assert position.quantity_in_pies == Decimal("2.5")


async def test_an_unknown_field_does_not_break_a_read() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{**POSITION, "somethingNew": 1}])

    client = _client(httpx.MockTransport(handler))
    assert len(await client.fetch_positions()) == 1
    await client.aclose()


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (408, ProviderUnavailable),
        (429, ProviderRateLimited),
        (500, ProviderUnavailable),
    ],
)
async def test_documented_failure_statuses_are_classified(
    status: int, error: type[Exception]
) -> None:
    """401, 403, 408 and 429 are all documented for both endpoints."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(error):
        await client.fetch_account_summary()
    await client.aclose()


async def test_a_summary_that_is_not_an_object_is_rejected() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(ProviderResponseError, match="expected a JSON object"):
        await client.fetch_account_summary()
    await client.aclose()


async def test_positions_that_are_not_an_array_are_rejected() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"positions": []})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(ProviderResponseError, match="expected a JSON array"):
        await client.fetch_positions()
    await client.aclose()


async def test_a_summary_missing_the_currency_is_rejected_rather_than_defaulted() -> None:
    """A snapshot without a currency cannot size anything.

    Defaulting it would produce a plausible-looking account state built on a
    field that was never sent.
    """
    payload = {key: value for key, value in SUMMARY.items() if key != "currency"}

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(ProviderResponseError, match="documented schema"):
        await client.fetch_account_summary()
    await client.aclose()


async def test_a_validation_error_never_quotes_the_provider_payload() -> None:
    """The message reaches logs and the API; provider data must not ride along."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[{"instrument": {"ticker": "X"}, "quantity": "not-a-number"}]
        )

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(ProviderResponseError) as excinfo:
        await client.fetch_positions()
    await client.aclose()
    assert "not-a-number" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Rate limits and credentials
# ---------------------------------------------------------------------------
def test_the_documented_per_endpoint_rates_are_recorded() -> None:
    """1 req/5s for the summary, 1 req/1s for positions -- five times apart.

    A single shared bucket would either throttle positions to the summary's
    cadence or overrun the summary's limit.
    """
    assert SUMMARY_RATE_PERIOD_SECONDS == 5.0
    assert POSITIONS_RATE_PERIOD_SECONDS == 1.0

    client = Trading212AccountClient(_settings())
    assert client._summary_http._rate_limiter is not None
    assert client._positions_http._rate_limiter is not None
    assert client._summary_http._rate_limiter is not client._positions_http._rate_limiter


async def test_rate_limit_headers_are_captured_for_health_reporting() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SUMMARY, headers=RATE_LIMIT_HEADERS)

    client = _client(httpx.MockTransport(handler))
    await client.fetch_account_summary()
    snapshot = client.rate_limit_snapshot()
    await client.aclose()
    assert snapshot["limit"] == 1
    assert snapshot["remaining"] == 0
    assert snapshot["period"] == "5"


def test_the_basic_credential_is_built_once_and_never_logged() -> None:
    import base64

    settings = _settings()
    headers = Trading212AccountClient._auth_headers(settings)
    expected = base64.b64encode(b"key-abc:secret-xyz").decode("ascii")
    assert headers["Authorization"] == f"Basic {expected}"
    assert "key-abc" not in repr(settings)
    assert "secret-xyz" not in repr(settings)


# ---------------------------------------------------------------------------
# Read-only, permanently
# ---------------------------------------------------------------------------
def test_the_client_has_no_mutation_method() -> None:
    """Broker writes belong to Phase 8, behind the four-gate live check."""
    surface = {name for name in dir(Trading212AccountClient) if not name.startswith("_")}
    assert surface == {
        "aclose",
        "fetch_account_summary",
        "fetch_positions",
        "name",
        "rate_limit_snapshot",
    }


def test_the_module_names_no_order_endpoint() -> None:
    import inspect

    from stockbrain.broker import trading212_account

    source = inspect.getsource(trading212_account)
    assert "/equity/orders" not in source
    assert 'request_json("POST"' not in source
    assert 'request_json("DELETE"' not in source
