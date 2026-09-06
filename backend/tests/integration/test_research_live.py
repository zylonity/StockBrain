"""Opt-in protocol checks: tiny DeepSeek tool flow and one FRED series request."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import SecretStr

from stockbrain.config import Settings
from stockbrain.db.base import utcnow
from stockbrain.intelligence.research_data import FredMacroProvider
from stockbrain.intelligence.research_transport import ResearchTransport
from stockbrain.llm.factory import build_pricing_table
from stockbrain.llm.telemetry import LlmCallRecord

pytestmark = pytest.mark.live


def credential(name: str) -> str:
    path = Path(__file__).resolve().parents[3] / ".env"
    value = os.environ.get(name) or dotenv_values(path).get(name) or ""
    if not value:
        pytest.skip(f"{name} absent from environment and root .env")
    return value


async def test_deepseek_flash_live_tool_continuity() -> None:
    transport = ResearchTransport(SecretStr(credential("DEEPSEEK_API_KEY")), max_tokens=1500)
    records: list[LlmCallRecord] = []

    async def record(row: LlmCallRecord) -> None:
        records.append(row)

    async def allowed() -> None:
        pass

    tool = {
        "type": "function",
        "function": {
            "name": "read_test_value",
            "description": "Read the secret-free test integer; no external access.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }
    messages: list[BaseMessage] = [
        SystemMessage(
            content=(
                "This is a protocol test. Use read_test_value to obtain the integer, "
                "then reply with just that integer. Never guess."
            )
        ),
        HumanMessage(content="What is the test integer? Call read_test_value first."),
    ]
    try:
        first = await transport.complete(
            messages,
            role="compatibility_test",
            model="deepseek-v4-flash",
            thinking=True,
            record_call=record,
            check_budget=allowed,
            tools=[tool],
        )
        assert first.tool_calls and first.tool_calls[0]["name"] == "read_test_value"
        assert first.additional_kwargs.get("reasoning_content") is not None
        messages.append(first)
        for call in first.tool_calls:
            assert call["name"] == "read_test_value" and call["args"] == {}
            messages.append(ToolMessage(content="731", tool_call_id=call["id"]))
        final = await transport.complete(
            messages,
            role="compatibility_test",
            model="deepseek-v4-flash",
            thinking=True,
            record_call=record,
            check_budget=allowed,
            tools=[tool],
        )
        assert "731" in str(final.content) and not final.tool_calls
        assert all(row.response_excerpt is None for row in records)
        print(
            "DeepSeek Flash pinned TradingAgents continuity: PASS; calls=",
            len(records),
            "tokens=",
            sum(row.usage.total_tokens for row in records if row.usage),
            "cost_usd=",
            sum(row.estimated_cost_usd or 0 for row in records),
            "request_ids_present=",
            all(bool(row.provider_request_id) for row in records),
            "finish_reasons=",
            [row.finish_reason for row in records],
        )
    finally:
        await transport.aclose()


async def test_muse_spark_live_tool_continuity() -> None:
    """The same TradingAgents protocol against a schema-based, always-reasoning API.

    Proves the four things a provider swap can break in the research graph and
    that no mock can confirm: the tool array is accepted, the pinned upstream
    serializer parses the reply, the assistant/tool/assistant sequence
    round-trips, and no reasoning field is invented for a provider that does
    not return one.
    """
    settings = Settings(_env_file=str(Path(__file__).resolve().parents[3] / ".env"))
    if not settings.active_llm_api_key.get_secret_value() or settings.llm_is_deepseek:
        pytest.skip("no non-DeepSeek LLM provider configured")

    model = settings.active_llm_quick_model
    transport = ResearchTransport(
        settings.active_llm_api_key,
        profile=settings.llm_profile,
        base_url=settings.active_llm_base_url,
        max_tokens=1500,
        pricing=build_pricing_table(settings),
    )
    records: list[LlmCallRecord] = []

    async def record(row: LlmCallRecord) -> None:
        records.append(row)

    async def allowed() -> None:
        pass

    tool = {
        "type": "function",
        "function": {
            "name": "read_test_value",
            "description": "Read the secret-free test integer; no external access.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }
    messages: list[BaseMessage] = [
        SystemMessage(
            content=(
                "This is a protocol test. Use read_test_value to obtain the integer, "
                "then reply with just that integer. Never guess."
            )
        ),
        HumanMessage(content="What is the test integer? Call read_test_value first."),
    ]
    try:
        first = await transport.complete(
            messages,
            role="compatibility_test",
            model=model,
            thinking=False,
            record_call=record,
            check_budget=allowed,
            tools=[tool],
        )
        assert first.tool_calls and first.tool_calls[0]["name"] == "read_test_value"
        # No reasoning field is fabricated for a provider that does not return one.
        assert first.additional_kwargs.get("reasoning_content") is None
        messages.append(first)
        for call in first.tool_calls:
            assert call["name"] == "read_test_value" and call["args"] == {}
            messages.append(ToolMessage(content="731", tool_call_id=call["id"]))
        final = await transport.complete(
            messages,
            role="compatibility_test",
            model=model,
            thinking=False,
            record_call=record,
            check_budget=allowed,
            tools=[tool],
        )
        assert "731" in str(final.content) and not final.tool_calls
        # Hidden reasoning is never persisted, on any provider.
        assert all(row.response_excerpt is None for row in records)
        assert all(row.had_reasoning_content is False for row in records)
        print(
            f"{model} pinned TradingAgents continuity: PASS; calls=",
            len(records),
            "tokens=",
            sum(row.usage.total_tokens for row in records if row.usage),
            "reasoning_tokens=",
            sum(row.usage.reasoning_tokens for row in records if row.usage),
            "cost_usd=",
            sum(row.estimated_cost_usd or 0 for row in records),
            "request_ids_present=",
            all(bool(row.provider_request_id) for row in records),
            "finish_reasons=",
            [row.finish_reason for row in records],
        )
    finally:
        await transport.aclose()


async def test_fred_live_one_series() -> None:
    client = FredMacroProvider(SecretStr(credential("FRED_API_KEY")), series=("DFF",))
    try:
        rows = await client.context(utcnow())
        assert rows and rows[0].kind == "DFF"
        print("FRED DFF bounded observations: PASS")
    finally:
        await client.aclose()
