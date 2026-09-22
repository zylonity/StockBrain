"""The Responses-API dialect: profile selection, wire shapes, and the session.

``meta-go`` is the first profile whose endpoint does not speak chat-completions
at all (live-verified 2026-09-21: ``/chat/completions`` is a 503 for the Muse
Spark contributor model, ``/responses`` works). These tests pin the contract
that makes the swap invisible to callers: the same
:class:`~stockbrain.llm.base.CompletionRequest` produces either dialect's body,
the same :class:`~stockbrain.llm.base.CompletionResult` comes back, and neither
dialect's field spellings ever leak into the other's request.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import SecretStr

from stockbrain.config import Settings
from stockbrain.errors import ProviderResponseError
from stockbrain.httpclient import ProviderHttpClient
from stockbrain.intelligence.research import ResearchValidationError  # noqa: F401
from stockbrain.intelligence.research_transport import (
    ResearchTransport,
    _decode_responses,
    validate_usage,
)
from stockbrain.intelligence.tradingagents_adapter import CONTEXT_TOOL
from stockbrain.llm.base import ChatMessage, CompletionRequest
from stockbrain.llm.factory import build_llm_client
from stockbrain.llm.profiles import DEEPSEEK, META_GO
from stockbrain.llm.responses_api import (
    ResponsesApiClient,
    build_input,
    parse_response_payload,
    parse_responses_usage,
)
from stockbrain.llm.telemetry import LlmCallRecord
from tests.research_helpers import completion as _unused_completion  # noqa: F401


def _settings() -> Settings:
    """A meta-go install with its rate card supplied, as an operator would."""
    return Settings(
        app_env="test",
        llm_provider="meta-go",
        llm_api_key="go-key-not-real",
        llm_model="muse-spark-1.3-contributor",
        llm_input_usd_per_mtok="0.10",
        llm_cached_input_usd_per_mtok="0.002",
        llm_output_usd_per_mtok="0.20",
    )


def _stub_http() -> ProviderHttpClient:
    """A never-sent http wrapper: builders are pure, no request is performed."""
    return ProviderHttpClient(provider="meta-go", base_url="", client=httpx.AsyncClient())


def _responses_client() -> ResponsesApiClient:
    return ResponsesApiClient(META_GO, api_key="k", http=_stub_http())


def _request(**overrides: Any) -> CompletionRequest:
    base: dict[str, Any] = {
        "messages": [
            ChatMessage(role="system", content="Return JSON."),
            ChatMessage(role="user", content="Decide A or B."),
        ],
        "model": "muse-spark-1.3-contributor",
        "max_output_tokens": 3000,
        "json_object": True,
        "thinking": False,
        "purpose": "TEST",
        "json_schema": {
            "type": "object",
            "properties": {"answer": {"type": "string", "enum": ["A", "B"]}},
            "required": ["answer"],
        },
        "json_schema_name": "research_decision",
    }
    base.update(overrides)
    return CompletionRequest(**base)


def _responses_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "resp_test",
        "model": "muse-spark-1.3-contributor",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": '{"answer": "A"}'}],
            }
        ],
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 50,
            "total_tokens": 1050,
            "input_tokens_details": {"cached_tokens": 800},
            "output_tokens_details": {"reasoning_tokens": 20},
        },
    }
    body.update(overrides)
    return body


async def allowed(_: LlmCallRecord | None = None) -> None:
    pass


# ---------------------------------------------------------------------------
# Profile selection
# ---------------------------------------------------------------------------


def test_meta_go_declares_the_responses_contract() -> None:
    """Same Muse Spark quirks as ``meta``, a different wire protocol."""
    assert META_GO.api_style == "responses"
    assert META_GO.chat_path == "/responses"
    assert META_GO.max_output_tokens_field == "max_output_tokens"
    assert META_GO.structured_output == "json_schema"
    assert META_GO.minimum_reasoning_effort == "minimal"
    assert META_GO.documented_rpm == 100


def test_meta_go_is_resolvable_from_settings() -> None:
    settings = _settings()
    assert settings.llm_profile is META_GO
    # The profile carries the /v1 root; the chat_path is appended per request.
    assert settings.active_llm_base_url == "https://opencode.ai/zen/go/v1"


def test_factory_builds_the_responses_client_for_meta_go() -> None:
    settings = _settings()
    client = build_llm_client(settings)
    assert isinstance(client, ResponsesApiClient)
    # The provider answers a missing header with a hard 400 (MissingSessionID),
    # so the session rides on the client's default headers from the start.
    assert "x-opencode-session" in client._http.headers


# ---------------------------------------------------------------------------
# Request builders
# ---------------------------------------------------------------------------


def test_request_body_carries_no_chat_dialect_fields() -> None:
    body = _responses_client().build_body(_request())
    assert "messages" not in body
    assert "max_tokens" not in body and "max_completion_tokens" not in body
    assert "response_format" not in body and "thinking" not in body
    assert body["model"] == "muse-spark-1.3-contributor"
    assert body["max_output_tokens"] == 3000
    role_items = [item["role"] for item in body["input"] if "role" in item]
    assert role_items == ["system", "user"]


def test_reasoning_effort_lands_on_the_documented_floor_when_disabled() -> None:
    body = _responses_client().build_body(_request(thinking=False))
    assert body["reasoning"] == {"effort": "minimal"}


def test_structured_output_moves_to_text_format_and_enforces() -> None:
    body = _responses_client().build_body(_request())
    text = body["text"]["format"]
    assert text["type"] == "json_schema"
    assert text["strict"] is True
    # Strictness closes objects; the raw input schema did not.
    assert text["schema"]["additionalProperties"] is False
    assert text["schema"]["required"] == ["answer"]


def test_a_json_schema_request_without_a_schema_is_refused() -> None:
    with pytest.raises(ValueError, match="supplied no schema"):
        _responses_client().build_body(_request(json_schema=None))


def test_ai_messages_with_tool_calls_become_function_call_items() -> None:
    history = [
        AIMessage(
            content="Reading the packet.",
            tool_calls=[
                {
                    "name": "read_research_context",
                    "args": {},
                    "id": "call_abc",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content="the packet body", tool_call_id="call_abc"),
    ]
    items = build_input(history, META_GO)
    # Typed items carry a type; role turns carry a role.
    assert [item["type"] if "type" in item else item["role"] for item in items] == [
        "function_call",
        "assistant",
        "function_call_output",
    ]
    call_item = items[0]
    assert call_item["call_id"] == "call_abc" and call_item["name"] == "read_research_context"
    assert call_item["arguments"] == "{}"
    output = items[-1]
    assert output["type"] == "function_call_output" and output["output"] == "the packet body"


def test_chat_wrapped_tool_specs_are_flattened_for_the_wire() -> None:
    """Chat nests the callable under ``function``; Responses rejects the wrapper."""
    assert "function" in CONTEXT_TOOL  # the shipped spelling is the chat one
    body = _transport(_stub_http()).build_body(
        [HumanMessage(content="read")],
        model="muse-spark-1.3-contributor",
        thinking=False,
        tools=[CONTEXT_TOOL],
        json_schema=None,
    )
    tool = body["tools"][0]
    assert "function" not in tool  # the wrapper spelling must not travel
    assert tool["type"] == "function" and tool["name"] == "read_research_context"


def _transport(http: ProviderHttpClient | None = None) -> ResearchTransport:
    """A real transport; the stimulus reaches the wire only through `http`."""
    return ResearchTransport(
        SecretStr("key"),
        profile=META_GO,
        http=http or _stub_http(),
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def test_completed_payload_parses_to_the_shared_result() -> None:
    result = parse_response_payload(_responses_body(), META_GO, latency_ms=5, attempts=1)
    assert result.content == '{"answer": "A"}'
    assert result.finish_reason == "stop"
    assert result.usage.prompt_tokens == 1000
    assert result.usage.completion_tokens == 50
    assert result.usage.cache_hit_tokens == 800
    assert result.usage.cache_miss_tokens == 200
    assert result.usage.reasoning_tokens == 20
    validate_usage(result.usage)  # the split must account for every prompt token


def test_reasoning_items_become_a_flag_never_content() -> None:
    body = _responses_body(
        output=[
            {"type": "reasoning", "content": []},
            {"type": "message", "content": [{"type": "output_text", "text": '{"answer":"A"}'}]},
        ]
    )
    result = parse_response_payload(body, META_GO, latency_ms=5, attempts=1)
    assert result.had_reasoning_content is True
    assert result.content == '{"answer":"A"}'


def test_function_call_items_produce_a_tool_calls_finish() -> None:
    body = _responses_body(
        output=[
            {
                "type": "function_call",
                "call_id": "call_xyz",
                "name": "read_research_context",
                "arguments": "{}",
            }
        ]
    )
    result = parse_response_payload(body, META_GO, latency_ms=1, attempts=1)
    assert result.content == ""
    assert result.finish_reason == "tool_calls"
    # The chat-shaped decode (shared with the transport) carries the calls.
    _, _, _, _, _, calls = _decode_responses(body, "muse-spark-1.3-contributor", META_GO)
    assert calls[0]["name"] == "read_research_context"


def test_incomplete_at_the_cap_is_a_named_truncation_not_a_silent_empty() -> None:
    body = _responses_body(
        status="incomplete",
        incomplete_details={"reason": "max_output_tokens"},
    )
    with pytest.raises(ProviderResponseError, match="truncated"):
        parse_response_payload(body, META_GO, latency_ms=1, attempts=1)


def test_unreported_cache_split_bills_everything_at_the_miss_rate() -> None:
    usage = parse_responses_usage(
        {"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050}, META_GO
    )
    assert usage.cache_hit_tokens == 0
    assert usage.billable_cache_miss == 1000  # over-estimate: the safe direction


def test_malformed_arguments_are_refused_not_guessed() -> None:
    body = _responses_body(
        output=[
            {
                "type": "function_call",
                "call_id": "call_x",
                "name": "read_research_context",
                "arguments": "{not json",
            }
        ]
    )
    with pytest.raises((ProviderResponseError, ValueError)):
        parse_response_payload(body, META_GO, latency_ms=1, attempts=1)


def test_inconsistent_provider_totals_are_rejected() -> None:
    usage = parse_responses_usage(_responses_body()["usage"] | {"total_tokens": 1}, META_GO)
    with pytest.raises(ValueError, match="inconsistent token accounting"):
        validate_usage(usage)


# ---------------------------------------------------------------------------
# Research transport over the Responses dialect
# ---------------------------------------------------------------------------


async def test_transport_builds_a_responses_body_and_re_scopes_the_session() -> None:
    """One run, one session header; the chat dialect never sends that header."""
    injected_headers: list[dict[str, str]] = []
    decoded_bodies: list[dict[str, Any]] = []
    records: list[LlmCallRecord] = []

    async def record(row: LlmCallRecord) -> None:
        records.append(row)

    def handler(request: httpx.Request) -> httpx.Response:
        decoded_bodies.append(json.loads(request.content))
        injected_headers.append(dict(request.headers))
        assert request.url.path.endswith("/responses")
        return httpx.Response(200, json=_responses_body())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://opencode.ai/zen/go/v1"
    ) as http:
        transport = _transport(ProviderHttpClient(provider="meta-go", base_url="", client=http))
        response = await transport.complete(
            [HumanMessage(content="Decide.")],
            role="market",
            model="muse-spark-1.3-contributor",
            thinking=False,
            record_call=record,
            check_budget=allowed,
            session_id="session-for-this-run",
        )

    body = decoded_bodies[0]
    assert "messages" not in body and "input" in body
    assert injected_headers[0]["x-opencode-session"] == "session-for-this-run"
    assert records and records[-1].succeeded
    assert isinstance(response, AIMessage)
    assert response.content == '{"answer": "A"}'
    assert response.tool_calls == []


async def test_chat_profiles_never_send_a_session_header() -> None:
    """The inverse property: no field of one dialect travels on the other's wire."""
    headers: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(dict(request.headers))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_test",
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "{}"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.deepseek.com"
    ) as http:
        transport = ResearchTransport(
            SecretStr("k"),
            profile=DEEPSEEK,
            http=ProviderHttpClient(provider="deepseek", base_url="", client=http),
        )
        await transport.complete(
            [HumanMessage(content="hi")],
            role="market",
            model="deepseek-v4-flash",
            thinking=False,
            record_call=allowed,
            check_budget=allowed,
            session_id="sess-chattish",
        )
    assert "x-opencode-session" not in headers[0]


def test_decoded_tool_calls_reach_the_chat_shared_shape() -> None:
    payload = _responses_body(
        output=[
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Reading the packet."}],
            },
            {
                "type": "function_call",
                "call_id": "call_9",
                "name": "read_research_context",
                "arguments": "{}",
            },
        ]
    )
    _, message_view, finish, reported_model, had_reasoning, tool_calls = _decode_responses(
        payload, "muse-spark-1.3-contributor", META_GO
    )
    assert finish == "stop" and reported_model == "muse-spark-1.3-contributor"
    assert had_reasoning is False
    assert message_view["content"] == "Reading the packet."
    assert tool_calls == [
        {"name": "read_research_context", "args": {}, "id": "call_9", "type": "tool_call"}
    ]
