"""Retry policy of the shared HTTP client.

The rule this file protects: **nothing is retried unless the caller explicitly
says the request is safe to repeat.** A generic retry wrapper around a
non-idempotent broker POST is the failure mode this whole architecture is built
to avoid, so the default must be "do not retry" and it must be tested.
"""

from __future__ import annotations

import httpx
import pytest

from stockbrain.errors import (
    ProviderAuthError,
    ProviderEntitlementError,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
)
from stockbrain.httpclient import ProviderHttpClient


def _client(handler: httpx.MockTransport, **kwargs: object) -> ProviderHttpClient:
    return ProviderHttpClient(
        provider="test",
        base_url="https://provider.test",
        client=httpx.AsyncClient(transport=handler, base_url="https://provider.test"),
        backoff_base_seconds=0.001,
        backoff_max_seconds=0.002,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_a_post_is_not_retried_by_default() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": "unavailable"})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(ProviderUnavailable):
        await client.request_json("POST", "/orders")
    assert calls == 1, "a mutation must never be repeated implicitly"
    await client.aclose()


async def test_a_post_transport_failure_is_not_retried_either() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(ProviderUnavailable):
        await client.request_json("POST", "/orders")
    assert calls == 1
    await client.aclose()


async def test_a_get_is_retried_up_to_the_budget() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    client = _client(httpx.MockTransport(handler), max_attempts=3)
    assert await client.get_json("/data") == {"ok": True}
    assert calls == 3
    await client.aclose()


async def test_retries_are_bounded() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    client = _client(httpx.MockTransport(handler), max_attempts=3)
    with pytest.raises(ProviderUnavailable):
        await client.get_json("/data")
    assert calls == 3
    await client.aclose()


async def test_an_explicitly_retry_safe_post_is_retried() -> None:
    """Firecrawl search has no side effect beyond credits, so it opts in."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 2:
            return httpx.Response(502)
        return httpx.Response(200, json={"success": True})

    client = _client(httpx.MockTransport(handler), max_attempts=3)
    assert await client.request_json("POST", "/v2/search", retry_safe=True) == {"success": True}
    assert calls == 2
    await client.aclose()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (402, ProviderEntitlementError),
        (429, ProviderRateLimited),
        (500, ProviderUnavailable),
        (503, ProviderUnavailable),
        (400, ProviderResponseError),
        (404, ProviderResponseError),
    ],
)
async def test_status_codes_are_classified(status: int, expected: type[Exception]) -> None:
    client = _client(httpx.MockTransport(lambda request: httpx.Response(status)))
    with pytest.raises(expected):
        await client.request_json("GET", "/x")
    await client.aclose()


async def test_authentication_failures_are_never_retried() -> None:
    """Retrying a bad credential burns rate limit and, for SEC, earns an IP block."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403)

    client = _client(httpx.MockTransport(handler), max_attempts=5)
    with pytest.raises(ProviderAuthError):
        await client.get_json("/x")
    assert calls == 1
    await client.aclose()


async def test_rate_limit_headers_are_captured_from_a_successful_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True},
            headers={"x-ratelimit-limit": "50", "x-ratelimit-remaining": "49"},
        )

    client = _client(httpx.MockTransport(handler))
    await client.get_json("/x")
    assert client.last_rate_limit is not None
    assert client.last_rate_limit.remaining == 49
    await client.aclose()


async def test_a_non_json_body_is_reported_not_guessed() -> None:
    client = _client(
        httpx.MockTransport(lambda request: httpx.Response(200, text="<html>nope</html>"))
    )
    with pytest.raises(ProviderResponseError, match="not valid JSON"):
        await client.get_json("/x")
    await client.aclose()


async def test_error_bodies_do_not_leak_into_retry_exhaustion_messages() -> None:
    client = _client(
        httpx.MockTransport(
            lambda request: httpx.Response(500, text="secret-token-abc in error body")
        ),
        max_attempts=2,
    )
    with pytest.raises(ProviderUnavailable) as excinfo:
        await client.get_json("/x")
    assert "secret-token-abc" not in str(excinfo.value)
    await client.aclose()
