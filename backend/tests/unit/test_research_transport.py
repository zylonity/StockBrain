"""Actual pinned DeepSeek serializer + assistant/tool/assistant protocol tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any

import httpx
import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import SecretStr

from stockbrain.errors import (
    ProviderAuthError,
    ProviderEntitlementError,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
)
from stockbrain.httpclient import ProviderHttpClient
from stockbrain.intelligence.research import (
    DEEP_ROLES,
    ROLES,
    ResearchToolError,
    ResearchValidationError,
)
from stockbrain.intelligence.research_transport import ResearchTransport, validate_usage
from stockbrain.intelligence.tradingagents_adapter import (
    CONTEXT_TOOL,
    SYSTEM_POLICY,
    TradingAgentsResearchEngine,
    debate_sequence,
)
from stockbrain.llm.openai_compat import parse_usage
from stockbrain.llm.profiles import DEEPSEEK
from stockbrain.llm.telemetry import LlmCallRecord
from tests.research_helpers import completion, decision, packet


async def allowed() -> None:
    pass


@pytest.mark.parametrize("hits", [0, 60, 100])
def test_partial_cache_split_accounts_for_all_prompt_tokens(hits: int) -> None:
    usage = parse_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_cache_hit_tokens": hits,
            "completion_tokens_details": {"reasoning_tokens": 10},
        },
        DEEPSEEK,
    )
    assert usage.cache_miss_tokens == 100 - hits
    assert usage.billable_cache_miss == 100 - hits
    assert usage.reasoning_tokens == 10
    validate_usage(usage)


@pytest.mark.parametrize(
    "extra",
    [
        {"prompt_cache_hit_tokens": 101},
        {"prompt_cache_miss_tokens": 2},
        {"total_tokens": 1},
        {"completion_tokens_details": {"reasoning_tokens": 21}},
    ],
)
def test_inconsistent_provider_accounting_rejected(extra: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        validate_usage(parse_usage({**completion()["usage"], **extra}, DEEPSEEK))


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "deepseek-v4-pro"])
@pytest.mark.parametrize("first_uses_tool", [True, False])
async def test_reasoning_preserved_for_transport_but_never_telemetry(
    model: str, first_uses_tool: bool
) -> None:
    requests: list[dict[str, Any]] = []
    records: list[LlmCallRecord] = []

    async def record(row: LlmCallRecord) -> None:
        records.append(row)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=completion(
                model=model,
                tool="read_research_context" if first_uses_tool and len(requests) == 1 else None,
            ),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.deepseek.com"
    ) as http:
        transport = ResearchTransport(
            SecretStr("key"), http=ProviderHttpClient(provider="deepseek", base_url="", client=http)
        )
        first = await transport.complete(
            [HumanMessage(content="Read context")],
            role="market",
            model=model,
            thinking=True,
            record_call=record,
            check_budget=allowed,
            tools=[CONTEXT_TOOL],
        )
        await transport.complete(
            [
                HumanMessage(content="Read context"),
                first,
                ToolMessage(content="Known data", tool_call_id="call-1")
                if first_uses_tool
                else HumanMessage(content="Continue with the available tool."),
            ],
            role="market",
            model=model,
            thinking=True,
            record_call=record,
            check_budget=allowed,
            tools=[CONTEXT_TOOL],
        )
    assert requests[1]["messages"][1]["reasoning_content"] == "PRIVATE_TRANSPORT_REASONING"
    if first_uses_tool:
        assert requests[1]["messages"][2]["tool_call_id"] == "call-1"
    assert "tool_choice" not in requests[1]
    assert "PRIVATE_TRANSPORT_REASONING" not in str([asdict(row) for row in records])
    assert records[0].had_reasoning_content
    assert records[0].usage and records[0].usage.cache_hit_tokens == 60
    assert records[0].estimated_cost_usd and records[0].estimated_cost_usd > 0
    assert records[0].provider_request_id == "req-research-1"


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, ProviderAuthError),
        (402, ProviderEntitlementError),
        (429, ProviderRateLimited),
        (503, ProviderUnavailable),
        (400, ProviderResponseError),
    ],
)
async def test_errors_have_no_automatic_http_retry(status: int, expected: type[Exception]) -> None:
    calls = 0
    rows: list[LlmCallRecord] = []

    async def record(row: LlmCallRecord) -> None:
        rows.append(row)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": "provider secret body"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.deepseek.com"
    ) as http:
        client = ResearchTransport(
            SecretStr("key"), http=ProviderHttpClient(provider="deepseek", base_url="", client=http)
        )
        with pytest.raises(expected):
            await client.complete(
                [HumanMessage(content="data")],
                role="market",
                model="deepseek-v4-flash",
                thinking=False,
                record_call=record,
                check_budget=allowed,
            )
    assert calls == 1
    assert "provider secret body" not in str(rows)


@pytest.mark.parametrize(
    "failure", ["length", "insufficient_system_resource", "empty", "malformed", "bad_tools"]
)
async def test_malformed_truncation_and_capacity(failure: str) -> None:
    rows: list[LlmCallRecord] = []

    async def record(row: LlmCallRecord) -> None:
        rows.append(row)

    payload = completion(finish=failure)
    if failure == "empty":
        payload = completion(content="")
    if failure == "malformed":
        payload = {"choices": []}
    if failure == "bad_tools":
        payload = completion(tool="read_research_context")
        payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{broken"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
        base_url="https://api.deepseek.com",
    ) as http:
        client = ResearchTransport(
            SecretStr("key"), http=ProviderHttpClient(provider="deepseek", base_url="", client=http)
        )
        with pytest.raises((ProviderResponseError, ProviderUnavailable, ResearchValidationError)):
            await client.complete(
                [HumanMessage(content="data")],
                role="market",
                model="deepseek-v4-flash",
                thinking=False,
                record_call=record,
                check_budget=allowed,
            )
    assert len(rows) == (2 if failure == "insufficient_system_resource" else 1)
    assert all(not row.succeeded for row in rows)


async def test_actual_upstream_graph_role_routing_and_no_rediscovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = packet()
    requests: list[dict[str, Any]] = []
    records: list[LlmCallRecord] = []

    async def record(row: LlmCallRecord) -> None:
        records.append(row)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        output = (
            decision(value).model_dump_json()
            if "response_format" in body
            else "Public evidence-based summary."
        )
        return httpx.Response(200, json=completion(output, model=body["model"]))

    # An accidental upstream provider call must fail this offline test immediately.
    import requests as requests_library

    def denied(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("unapproved upstream network call")

    monkeypatch.setattr(requests_library.sessions.Session, "request", denied)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.deepseek.com"
    ) as http:
        client = ResearchTransport(
            SecretStr("key"), http=ProviderHttpClient(provider="deepseek", base_url="", client=http)
        )
        engine = TradingAgentsResearchEngine(client)
        result = await engine.analyze(value, record_call=record, check_budget=allowed)
    # Derived from the engine's own debate length rather than pinned to a
    # literal, so adding a rebuttal round does not silently stop checking the
    # routing of every node that actually runs.
    expected_roles = [role for _, role in debate_sequence(engine.debate_rounds)]
    assert len(requests) == len(expected_roles)
    assert result.decision.action == "HOLD"
    assert {role for role, _ in result.reports} == set(ROLES) - {"trader"}
    for role, body in zip(expected_roles, requests, strict=True):
        assert body["model"] == ("deepseek-v4-pro" if role in DEEP_ROLES else "deepseek-v4-flash")
        assert body["thinking"]["type"] == ("enabled" if role in DEEP_ROLES else "disabled")
        assert body["messages"][0]["content"].startswith(SYSTEM_POLICY)
        assert str(value.event_id) in body["messages"][1]["content"]
        assert value.title not in body["messages"][0]["content"]
        assert [tool["function"]["name"] for tool in body.get("tools", [])] in [
            [],
            ["read_research_context"],
        ]
    assert "PRIVATE_TRANSPORT_REASONING" not in result.model_dump_json()


async def test_graph_rejects_broker_tool_call() -> None:
    value = packet()

    async def record(row: LlmCallRecord) -> None:
        pass

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=completion(tool="place_order"))
        ),
        base_url="https://api.deepseek.com",
    ) as http:
        client = ResearchTransport(
            SecretStr("key"), http=ProviderHttpClient(provider="deepseek", base_url="", client=http)
        )
        with pytest.raises(ResearchToolError):
            await TradingAgentsResearchEngine(client).analyze(
                value, record_call=record, check_budget=allowed
            )


async def test_cancelled_upstream_thread_cannot_continue_provider_calls() -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    calls = 0
    records: list[LlmCallRecord] = []

    async def record(row: LlmCallRecord) -> None:
        records.append(row)

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("cancelled request cannot complete")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.deepseek.com"
    ) as http:
        transport = ResearchTransport(
            SecretStr("key"), http=ProviderHttpClient(provider="deepseek", base_url="", client=http)
        )
        task = asyncio.create_task(
            TradingAgentsResearchEngine(transport).analyze(
                packet(), record_call=record, check_budget=allowed
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(cancelled.wait(), timeout=5)
    assert calls == 1
    assert len(records) == 1 and not records[0].succeeded
