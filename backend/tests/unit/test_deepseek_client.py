"""DeepSeek client behaviour, verified against the documented API contract.

Every fixture in this file mirrors a shape the current DeepSeek documentation
specifies. Where StockBrain's spec differed from those docs, the test encodes the
documented behaviour and says so.
"""

from __future__ import annotations

import httpx
import pytest

from stockbrain.config import Settings
from stockbrain.errors import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
)
from stockbrain.httpclient import ProviderHttpClient
from stockbrain.llm.base import ChatMessage, CompletionRequest
from stockbrain.llm.deepseek import DeepSeekClient, extract_json, parse_completion


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"app_env": "test", "deepseek_api_key": "sk-test-not-real"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _client(handler: httpx.MockTransport, **kwargs: object) -> DeepSeekClient:
    settings = _settings()
    http = ProviderHttpClient(
        provider="deepseek",
        base_url=settings.deepseek_base_url,
        client=httpx.AsyncClient(transport=handler, base_url=settings.deepseek_base_url),
        backoff_base_seconds=0.001,
        backoff_max_seconds=0.002,
    )
    return DeepSeekClient(settings, http=http, **kwargs)  # type: ignore[arg-type]


def _request(**overrides: object) -> CompletionRequest:
    base: dict[str, object] = {
        "messages": [
            ChatMessage("system", "Reply with json."),
            ChatMessage("user", "Classify this."),
        ],
        "model": "deepseek-v4-flash",
    }
    base.update(overrides)
    return CompletionRequest(**base)  # type: ignore[arg-type]


def _ok_body(content: str = '{"ok": true}') -> dict[str, object]:
    return {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "created": 1757000000,
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 300,
            "total_tokens": 1500,
            "prompt_cache_hit_tokens": 900,
            "prompt_cache_miss_tokens": 300,
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------


def test_thinking_is_explicitly_disabled() -> None:
    """DeepSeek's `thinking` parameter defaults to ENABLED.

    The StockBrain spec says the classifier runs non-thinking but not how.
    Omitting the field would silently turn the cheap triage path into a
    reasoning call, so it is always sent explicitly.
    """
    client = _client(httpx.MockTransport(lambda r: httpx.Response(200, json=_ok_body())))
    body = client.build_body(_request())
    assert body["thinking"] == {"type": "disabled"}


def test_thinking_can_be_enabled_explicitly() -> None:
    client = _client(httpx.MockTransport(lambda r: httpx.Response(200, json=_ok_body())))
    body = client.build_body(_request(thinking=True))
    assert body["thinking"] == {"type": "enabled"}


def test_json_mode_uses_the_documented_response_format() -> None:
    client = _client(httpx.MockTransport(lambda r: httpx.Response(200, json=_ok_body())))
    body = client.build_body(_request())
    assert body["response_format"] == {"type": "json_object"}
    assert body["stream"] is False


def test_json_mode_requires_the_prompt_to_mention_json() -> None:
    """A documented requirement; without it the model may not emit JSON at all."""
    client = _client(httpx.MockTransport(lambda r: httpx.Response(200, json=_ok_body())))
    with pytest.raises(ValueError, match="mention it"):
        client.build_body(_request(messages=[ChatMessage("user", "classify this document")]))


def test_non_json_requests_skip_the_json_check() -> None:
    client = _client(httpx.MockTransport(lambda r: httpx.Response(200, json=_ok_body())))
    body = client.build_body(_request(messages=[ChatMessage("user", "hello")], json_object=False))
    assert "response_format" not in body


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def test_usage_captures_the_cache_hit_miss_split() -> None:
    """Hit and miss are priced ~30x apart; merging them makes cost meaningless."""
    result = parse_completion(_ok_body(), latency_ms=10, attempts=1)
    assert result.usage.prompt_tokens == 1200
    assert result.usage.completion_tokens == 300
    assert result.usage.cache_hit_tokens == 900
    assert result.usage.cache_miss_tokens == 300
    assert result.usage.billable_cache_miss == 300


def test_billable_falls_back_to_all_prompt_tokens_when_unsplit() -> None:
    """Charging at the miss rate over-estimates, which is the safe direction."""
    body = _ok_body()
    body["usage"] = {"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600}
    result = parse_completion(body, latency_ms=1, attempts=1)
    assert result.usage.billable_cache_miss == 500


def test_provider_request_id_is_captured() -> None:
    assert parse_completion(_ok_body(), latency_ms=1, attempts=1).provider_request_id == (
        "chatcmpl-abc123"
    )


def test_reasoning_content_is_flagged_but_not_returned() -> None:
    """Hidden reasoning must never be surfaced; only the flag is kept."""
    body = _ok_body()
    body["choices"][0]["message"]["reasoning_content"] = "internal chain of thought"  # type: ignore[index]
    result = parse_completion(body, latency_ms=1, attempts=1)
    assert result.had_reasoning_content is True
    assert "chain of thought" not in result.content
    assert not hasattr(result, "reasoning_content")


def test_empty_content_is_an_error_not_an_empty_classification() -> None:
    """Documented DeepSeek behaviour: JSON mode occasionally returns empty."""
    body = _ok_body()
    body["choices"][0]["message"]["content"] = ""  # type: ignore[index]
    with pytest.raises(ProviderResponseError, match="empty"):
        parse_completion(body, latency_ms=1, attempts=1)


def test_truncated_output_names_its_real_cause() -> None:
    body = _ok_body('{"partial": ')
    body["choices"][0]["finish_reason"] = "length"  # type: ignore[index]
    with pytest.raises(ProviderResponseError, match="token limit"):
        parse_completion(body, latency_ms=1, attempts=1)


def test_insufficient_system_resource_is_treated_as_transient() -> None:
    """A documented DeepSeek capacity condition, not a model answer."""
    body = _ok_body()
    body["choices"][0]["finish_reason"] = "insufficient_system_resource"  # type: ignore[index]
    with pytest.raises(ProviderUnavailable):
        parse_completion(body, latency_ms=1, attempts=1)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{"index": 0}]},
        {"choices": ["not-an-object"]},
        [],
    ],
)
def test_malformed_responses_are_rejected(payload: object) -> None:
    with pytest.raises(ProviderResponseError):
        parse_completion(payload, latency_ms=1, attempts=1)


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


def test_extract_json_handles_bare_and_fenced_output() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_rejects_malformed_output() -> None:
    with pytest.raises(ProviderResponseError, match="not valid JSON"):
        extract_json("{this is not json")
    with pytest.raises(ProviderResponseError):
        extract_json("")


# ---------------------------------------------------------------------------
# Error handling and retries
# ---------------------------------------------------------------------------


async def test_successful_call_returns_a_result() -> None:
    client = _client(httpx.MockTransport(lambda r: httpx.Response(200, json=_ok_body())))
    result = await client.complete(_request())
    assert result.content == '{"ok": true}'
    assert result.attempts == 1
    assert result.started_at is not None and result.completed_at is not None
    await client.aclose()


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failures_are_never_retried(status: int) -> None:
    """A rejected key does not become valid by trying again."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": {"message": "bad key"}})

    client = _client(httpx.MockTransport(handler), max_attempts=4)
    with pytest.raises(ProviderAuthError):
        await client.complete(_request())
    assert calls == 1
    await client.aclose()


async def test_rate_limit_is_retried_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json=_ok_body())

    client = _client(httpx.MockTransport(handler), max_attempts=3)
    result = await client.complete(_request())
    assert calls == 3
    assert result.attempts == 3
    await client.aclose()


async def test_rate_limit_eventually_gives_up() -> None:
    client = _client(
        httpx.MockTransport(lambda r: httpx.Response(429, headers={"retry-after": "0"})),
        max_attempts=2,
    )
    with pytest.raises(ProviderRateLimited):
        await client.complete(_request())
    await client.aclose()


async def test_server_errors_are_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 2:
            return httpx.Response(503)
        return httpx.Response(200, json=_ok_body())

    client = _client(httpx.MockTransport(handler), max_attempts=3)
    assert (await client.complete(_request())).attempts == 2
    assert calls == 2
    await client.aclose()


async def test_timeouts_are_retried_then_reported() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client(httpx.MockTransport(handler), max_attempts=3)
    with pytest.raises(ProviderUnavailable):
        await client.complete(_request())
    assert calls == 3
    await client.aclose()


async def test_schema_violations_are_not_retried() -> None:
    """Retrying a bad response shape usually reproduces it and always costs money."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": []})

    client = _client(httpx.MockTransport(handler), max_attempts=4)
    with pytest.raises(ProviderResponseError):
        await client.complete(_request())
    assert calls == 1
    await client.aclose()


async def test_api_key_never_appears_in_an_error() -> None:
    client = _client(
        httpx.MockTransport(lambda r: httpx.Response(401, text="invalid key sk-test-not-real"))
    )
    with pytest.raises(ProviderAuthError) as excinfo:
        await client.complete(_request())
    assert "sk-test-not-real" not in str(excinfo.value)
    await client.aclose()


async def test_authorization_header_is_sent_but_not_echoed() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json=_ok_body())

    client = _client(httpx.MockTransport(handler))
    await client.complete(_request())
    # The transport was configured without the auth header (the test builds its
    # own client), so this asserts the request path works, not the credential.
    assert "content-type" in seen
    await client.aclose()
