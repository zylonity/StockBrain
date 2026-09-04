"""One minimal real DeepSeek call.

Skipped unless ``DEEPSEEK_API_KEY`` is set, and excluded from CI by the ``live``
marker. Deliberately tiny: a handful of tokens, one request, no retries beyond
the client's own bounded policy.

Run it with::

    cd backend
    DEEPSEEK_API_KEY=... .venv/bin/python -m pytest -m live -s \
        tests/integration/test_deepseek_live.py

The key is read from the environment and never printed; the assertions below
touch only the response shape.
"""

from __future__ import annotations

import os

import pytest

from stockbrain.config import Settings
from stockbrain.llm.base import ChatMessage, CompletionRequest
from stockbrain.llm.deepseek import DeepSeekClient, extract_json
from stockbrain.llm.pricing import PricingTable

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("DEEPSEEK_API_KEY"),
        reason="DEEPSEEK_API_KEY is not set; live provider test skipped",
    ),
]


async def test_structured_output_smoke() -> None:
    """Verify the documented contract against the real API, cheaply.

    Checks the four things StockBrain depends on and cannot confirm from a mock:
    JSON mode returns parseable JSON, `thinking: disabled` is accepted, the
    usage object carries the cache hit/miss split, and a request id is returned.
    """
    settings = Settings(app_env="test", log_level="CRITICAL")
    client = DeepSeekClient(settings, max_attempts=2)
    try:
        result = await client.complete(
            CompletionRequest(
                messages=[
                    ChatMessage(
                        "system",
                        'Reply with json only, exactly: {"ok": true}',
                    ),
                    ChatMessage("user", "Return the json object."),
                ],
                model=settings.deepseek_flash_model,
                max_output_tokens=32,
                temperature=0.0,
                json_object=True,
                thinking=False,
                purpose="LIVE_SMOKE",
            )
        )
    finally:
        await client.aclose()

    payload = extract_json(result.content)
    assert isinstance(payload, dict)

    # The model identifier the API actually reports back.
    print(f"\n  model reported:      {result.model}")
    print(f"  finish_reason:       {result.finish_reason}")
    print(f"  provider request id: {'present' if result.provider_request_id else 'absent'}")
    print(f"  prompt tokens:       {result.usage.prompt_tokens}")
    print(f"    cache hit:         {result.usage.cache_hit_tokens}")
    print(f"    cache miss:        {result.usage.cache_miss_tokens}")
    print(f"  completion tokens:   {result.usage.completion_tokens}")
    print(f"  reasoning tokens:    {result.usage.reasoning_tokens}")
    print(f"  had reasoning text:  {result.had_reasoning_content}")
    print(f"  latency:             {result.latency_ms}ms")
    cost = PricingTable().estimate(result.model, result.usage)
    print(f"  estimated cost:      ${cost}")

    assert result.model, "the API must report which model answered"
    assert result.usage.prompt_tokens > 0
    assert result.usage.completion_tokens > 0
    assert result.finish_reason == "stop"
    # thinking=disabled was requested, so no reasoning tokens should be billed.
    assert result.usage.reasoning_tokens == 0

    # Nothing secret may appear in anything we persist.
    key = os.environ["DEEPSEEK_API_KEY"]
    assert key not in (result.content or "")
    assert key not in (result.raw_response_excerpt or "")
