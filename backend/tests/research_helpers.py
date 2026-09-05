"""Deterministic research fixtures; no live provider access."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from stockbrain.intelligence.research import (
    EvidenceDocument,
    ResearchDecision,
    ResearchPacket,
    ResolvedCompany,
)


def packet() -> ResearchPacket:
    moment = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
    return ResearchPacket(
        event_id=uuid.uuid4(),
        impact_id=uuid.uuid4(),
        title="Issuer expands capacity",
        summary="A new plant may benefit its supplier.",
        event_time=moment - dt.timedelta(hours=1),
        as_of=moment,
        company=ResolvedCompany(
            company_id=uuid.uuid4(),
            name="Apple Inc.",
            broker_instrument_id=uuid.uuid4(),
            broker_ticker="AAPL_US_EQ",
            symbol="AAPL",
            exchange="NASDAQ",
            currency="USD",
            isin="US0378331005",
        ),
        evidence=(
            EvidenceDocument(
                source_id=uuid.uuid4(),
                publisher="Issuer",
                url="https://example.com/filing",
                published_at=moment - dt.timedelta(hours=1),
                received_at=moment - dt.timedelta(minutes=50),
                text="New plant announced.",
                relationship="PRIMARY",
            ),
        ),
        impact_path="indirect",
        relationship="Supplier impact",
        classifier_rationale="Additional capacity could increase component demand.",
    )


def decision(value: ResearchPacket) -> ResearchDecision:
    return ResearchDecision.model_validate(
        {
            "action": "HOLD",
            "confidence": 0.6,
            "horizon": "weeks",
            "thesis": "Await corroboration.",
            "bull_case": "Demand improves.",
            "bear_case": "Demand uncertain.",
            "catalysts": ["Next filing"],
            "risks": ["Weak demand"],
            "invalidation_conditions": ["Contract cancelled"],
            "evidence_ids": [str(value.evidence[0].source_id)],
        }
    )


def completion(
    content: str = "Public research summary.",
    *,
    model: str = "deepseek-v4-flash",
    reasoning: str | None = "PRIVATE_TRANSPORT_REASONING",
    tool: str | None = None,
    finish: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "reasoning_content": reasoning,
    }
    if tool:
        message["tool_calls"] = [
            {"id": "call-1", "type": "function", "function": {"name": tool, "arguments": "{}"}}
        ]
    return {
        "id": "req-research-1",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish or ("tool_calls" if tool else "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_cache_hit_tokens": 60,
            "prompt_cache_miss_tokens": 40,
        },
    }
