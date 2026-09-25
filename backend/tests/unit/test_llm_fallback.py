"""The optional fallback LLM backend.

Only "the primary cannot answer right now" falls through; every other fault
stays visible, and a fallback answer is attributed to the fallback.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from stockbrain.config import Settings
from stockbrain.errors import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
)
from stockbrain.llm.base import ChatMessage, CompletionRequest, CompletionResult
from stockbrain.llm.factory import build_llm_client_with_fallback, build_pricing_table
from stockbrain.llm.fallback import FallbackLlmClient, FallbackResearchTransport
from stockbrain.llm.openai_compat import OpenAICompatibleClient

pytestmark = pytest.mark.anyio


class _Fake:
    name = "fake"

    def __init__(self, outcome: Exception | str) -> None:
        self.outcome = outcome
        self.requests: list[Any] = []

    async def complete(self, request: Any = None, *args: Any, **kwargs: Any) -> Any:
        self.requests.append(kwargs.get("model") or request.model)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return CompletionResult(content=self.outcome, model=kwargs.get("model") or request.model)

    async def aclose(self) -> None:
        return None


def _client(primary: _Fake, fallback: _Fake) -> FallbackLlmClient:
    return FallbackLlmClient(
        primary,
        fallback,
        fallback_provider="openai",
        primary_deep_model="p-deep",
        fallback_model="f-quick",
        fallback_deep_model="f-deep",
    )


def _request(model: str = "p-quick") -> CompletionRequest:
    return CompletionRequest(messages=[ChatMessage("user", "json please")], model=model)


@pytest.mark.parametrize("error", [ProviderRateLimited("429"), ProviderUnavailable("503")])
async def test_capacity_failures_fall_through_and_are_attributed(error: Exception) -> None:
    fallback = _Fake("ok")
    result = await _client(_Fake(error), fallback).complete(_request())
    assert result.content == "ok"
    assert result.provider == "openai"
    assert fallback.requests == ["f-quick"]


async def test_a_deep_request_uses_the_fallback_deep_model() -> None:
    fallback = _Fake("ok")
    await _client(_Fake(ProviderRateLimited("429")), fallback).complete(_request("p-deep"))
    assert fallback.requests == ["f-deep"]


async def test_a_primary_answer_is_not_attributed_to_the_fallback() -> None:
    fallback = _Fake("never")
    result = await _client(_Fake("primary"), fallback).complete(_request())
    assert result.provider is None
    assert fallback.requests == []


@pytest.mark.parametrize("error", [ProviderAuthError("401"), ProviderResponseError("junk")])
async def test_real_faults_do_not_fall_through(error: Exception) -> None:
    fallback = _Fake("never")
    with pytest.raises(type(error)):
        await _client(_Fake(error), fallback).complete(_request())
    assert fallback.requests == []


async def test_research_transport_falls_through_per_role() -> None:
    fallback = _Fake("unused")

    async def _noop(*_: Any) -> None:
        return None

    transport = FallbackResearchTransport(
        _Fake(ProviderRateLimited("429")),  # type: ignore[arg-type]
        fallback,  # type: ignore[arg-type]
        fallback_provider="openai",
        primary_deep_model="p-deep",
        fallback_model="f-quick",
        fallback_deep_model="f-deep",
    )
    await transport.complete(
        [], role="bull", model="p-quick", thinking=False, record_call=_noop, check_budget=_noop
    )
    assert fallback.requests == ["f-quick"]


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "llm_provider": "meta",
        "llm_api_key": "key-not-real",
        "llm_model": "muse-spark-1.3-contributor",
        "llm_input_usd_per_mtok": "0.10",
        "llm_output_usd_per_mtok": "0.20",
        "llm_fallback_provider": "openai",
        "llm_fallback_api_key": "fallback-not-real",
        "llm_fallback_model": "gpt-fallback",
        "llm_fallback_input_usd_per_mtok": "0.40",
        "llm_fallback_output_usd_per_mtok": "1.60",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_no_fallback_configured_builds_the_plain_client() -> None:
    settings = _settings(llm_fallback_provider="")
    assert isinstance(build_llm_client_with_fallback(settings), OpenAICompatibleClient)


def test_a_configured_fallback_wraps_the_primary_and_is_priced() -> None:
    settings = _settings()
    assert isinstance(build_llm_client_with_fallback(settings), FallbackLlmClient)
    assert build_pricing_table(settings).rates_for("gpt-fallback") is not None


def test_an_unpriced_fallback_is_refused() -> None:
    with pytest.raises(ValidationError, match="fallback LLM model"):
        _settings(llm_fallback_input_usd_per_mtok="")


def test_a_fallback_without_a_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="LLM_FALLBACK_API_KEY"):
        _settings(llm_fallback_api_key="")
