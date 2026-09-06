"""The generic OpenAI-compatible LLM backend.

These tests exist to pin the three properties that make "any OpenAI-compatible
endpoint" safe rather than merely possible:

1. one provider's dialect never appears in another provider's request
2. a provider whose spend cannot be priced is refused at startup, because the
   budget guard sums estimates and an unpriced model estimates ``None``
3. selecting a provider selects exactly that provider, with no fallback

The DeepSeek regression suite in ``test_deepseek_client.py`` is unchanged and
still covers the DeepSeek dialect specifically.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from stockbrain.config import Settings
from stockbrain.errors import ProviderResponseError
from stockbrain.httpclient import ProviderHttpClient
from stockbrain.llm.base import ChatMessage, CompletionRequest, TokenUsage
from stockbrain.llm.deepseek import DeepSeekClient
from stockbrain.llm.factory import build_llm_client, build_pricing_table
from stockbrain.llm.openai_compat import (
    OpenAICompatibleClient,
    build_body,
    parse_completion,
    parse_usage,
)
from stockbrain.llm.profiles import DEEPSEEK, GENERIC, GENERIC_NO_JSON, META, OPENAI, profile_for

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(**overrides: object) -> CompletionRequest:
    base: dict[str, object] = {
        "messages": [
            ChatMessage("system", "Reply with json."),
            ChatMessage("user", "Classify this."),
        ],
        "model": "some-model",
        "json_schema": {"type": "object", "properties": {}},
        "json_schema_name": "Thing",
    }
    base.update(overrides)
    return CompletionRequest(**base)  # type: ignore[arg-type]


def _priced_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "llm_provider": "meta",
        "llm_api_key": "key-not-real",
        "llm_model": "muse-spark-1.3-contributor",
        "llm_input_usd_per_mtok": "0.10",
        "llm_cached_input_usd_per_mtok": "0.002",
        "llm_output_usd_per_mtok": "0.20",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Profile selection
# ---------------------------------------------------------------------------


def test_deepseek_remains_the_shipped_default() -> None:
    """Generalising the backend must not change what a normal install runs."""
    settings = Settings(app_env="test", deepseek_api_key="sk-test-not-real")
    assert settings.llm_provider == "deepseek"
    assert settings.llm_profile is DEEPSEEK
    assert settings.active_llm_quick_model == "deepseek-v4-flash"
    assert settings.active_llm_deep_model == "deepseek-v4-pro"
    assert settings.active_llm_base_url == "https://api.deepseek.com"


def test_an_unknown_provider_name_is_refused_not_defaulted() -> None:
    """A typo must not silently redirect every call onto another backend."""
    with pytest.raises(ValueError, match="unknown LLM provider"):
        profile_for("depseek")

    with pytest.raises(ValidationError, match="unknown LLM provider"):
        Settings(app_env="test", llm_provider="depseek", llm_api_key="k", llm_model="m")


def test_a_single_model_provider_uses_it_for_quick_and_deep_roles() -> None:
    """No Flash/Pro distinction is preserved where the provider has none."""
    settings = _priced_settings()
    assert settings.active_llm_quick_model == "muse-spark-1.3-contributor"
    assert settings.active_llm_deep_model == "muse-spark-1.3-contributor"


# ---------------------------------------------------------------------------
# Request construction: no dialect leaks between providers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", [META, OPENAI, GENERIC, GENERIC_NO_JSON])
def test_deepseek_only_fields_are_never_sent_to_other_providers(profile: Any) -> None:
    body = build_body(_request(), profile)
    assert "thinking" not in body
    assert "prompt_cache_hit_tokens" not in body
    assert "reasoning_content" not in body


def test_each_profile_uses_its_documented_output_token_field() -> None:
    assert "max_tokens" in build_body(_request(), DEEPSEEK)
    assert "max_completion_tokens" in build_body(_request(), META)
    # Exactly one spelling per request; sending both is how a 400 happens.
    meta_body = build_body(_request(), META)
    assert "max_tokens" not in meta_body


def test_structured_output_uses_each_dialect() -> None:
    assert build_body(_request(), DEEPSEEK)["response_format"] == {"type": "json_object"}
    assert build_body(_request(), META)["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "Thing", "schema": {"type": "object", "properties": {}}},
    }
    # An endpoint with no JSON mode gets no response_format at all.
    assert "response_format" not in build_body(_request(), GENERIC_NO_JSON)


def test_a_schema_provider_given_no_schema_refuses_rather_than_guessing() -> None:
    """Silently dropping the constraint would surface as a schema violation later."""
    with pytest.raises(ValueError, match="supplied no schema"):
        build_body(_request(json_schema=None), META)


def test_reasoning_is_expressed_in_each_providers_own_terms() -> None:
    # DeepSeek can switch reasoning off, and must be told to explicitly.
    assert build_body(_request(thinking=False), DEEPSEEK)["thinking"] == {"type": "disabled"}
    assert build_body(_request(thinking=True), DEEPSEEK)["thinking"] == {"type": "enabled"}

    # Muse Spark cannot: reasoning_effort "none" is a documented 400, so the
    # floor is the honest translation of thinking=False.
    assert build_body(_request(thinking=False), META)["reasoning_effort"] == "minimal"
    # thinking=True sends no effort at all rather than inventing a level.
    assert "reasoning_effort" not in build_body(_request(thinking=True), META)

    # A profile with no reasoning control sends nothing either way.
    assert "reasoning_effort" not in build_body(_request(thinking=False), GENERIC)


# ---------------------------------------------------------------------------
# Usage parsing and cache accounting
# ---------------------------------------------------------------------------


def test_openai_style_cached_tokens_are_a_subset_of_prompt_tokens() -> None:
    usage = parse_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "total_tokens": 1100,
            "prompt_tokens_details": {"cached_tokens": 800},
        },
        META,
    )
    assert usage.cache_hit_tokens == 800
    assert usage.cache_miss_tokens == 200
    assert usage.billable_cache_miss == 200


def test_a_provider_reporting_no_cache_bills_everything_uncached() -> None:
    """Guessing a hit rate would under-count spend; over-counting is the safe error."""
    usage = parse_usage(
        {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}, GENERIC
    )
    assert usage.cache_hit_tokens == 0
    assert usage.billable_cache_miss == 1000


def test_deepseek_cached_fields_are_ignored_for_other_providers() -> None:
    """A generic endpoint echoing DeepSeek's field names must not be believed."""
    usage = parse_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "total_tokens": 1100,
            "prompt_cache_hit_tokens": 900,
        },
        GENERIC,
    )
    assert usage.cache_hit_tokens == 0
    assert usage.billable_cache_miss == 1000


def test_more_cached_than_prompt_tokens_cannot_produce_negative_billing() -> None:
    usage = parse_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
            "prompt_tokens_details": {"cached_tokens": 500},
        },
        META,
    )
    assert usage.cache_hit_tokens == 100
    assert usage.billable_cache_miss == 0


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _ok_body(model: str = "some-model") -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "model": model,
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


def test_truncation_names_its_real_cause_for_any_provider() -> None:
    body = _ok_body()
    body["choices"][0]["finish_reason"] = "length"
    with pytest.raises(ProviderResponseError, match="token limit"):
        parse_completion(body, GENERIC, latency_ms=1, attempts=1)


def test_reasoning_content_is_only_looked_for_where_a_provider_reports_it() -> None:
    """No reasoning field is fabricated for providers that do not return one."""
    body = _ok_body()
    body["choices"][0]["message"]["reasoning_content"] = "hidden chain of thought"

    assert parse_completion(body, DEEPSEEK, latency_ms=1, attempts=1).had_reasoning_content
    # META declares no reasoning_content field, so the flag stays false.
    assert not parse_completion(body, META, latency_ms=1, attempts=1).had_reasoning_content


def test_hidden_reasoning_is_never_carried_into_the_result_content() -> None:
    body = _ok_body()
    body["choices"][0]["message"]["reasoning_content"] = "secret reasoning"
    result = parse_completion(body, DEEPSEEK, latency_ms=1, attempts=1)
    assert "secret reasoning" not in result.content
    assert result.content == "{}"


# ---------------------------------------------------------------------------
# Pricing and the budget gate
# ---------------------------------------------------------------------------


def test_configured_rates_price_a_provider_the_codebase_has_never_heard_of() -> None:
    settings = Settings(
        app_env="test",
        llm_provider="generic",
        llm_api_key="k",
        llm_base_url="https://llm.internal/v1",
        llm_model="some-local-model",
        llm_input_usd_per_mtok="3.00",
        llm_cached_input_usd_per_mtok="0.30",
        llm_output_usd_per_mtok="6.00",
    )
    pricing = build_pricing_table(settings)
    cost = pricing.estimate(
        "some-local-model",
        TokenUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000),
    )
    # No cache reported, so all input bills uncached: 3.00 + 6.00.
    assert cost == Decimal("9.000000")


def test_muse_contributor_pricing_matches_the_published_rates() -> None:
    """Verified against dev.meta.ai/docs/pricing-rate-limits on 2026-09-06."""
    settings = _priced_settings()
    pricing = build_pricing_table(settings)
    cost = pricing.estimate(
        "muse-spark-1.3-contributor",
        TokenUsage(
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
            cache_hit_tokens=1_000_000,
            cache_miss_tokens=0,
        ),
    )
    # All input cached: 0.002 + 0.20 output.
    assert cost == Decimal("0.202000")


def test_a_provider_whose_spend_cannot_be_priced_is_refused_at_startup() -> None:
    """An unpriced model estimates None, which would exempt it from the caps."""
    with pytest.raises(ValidationError, match="no price is configured"):
        Settings(
            app_env="test",
            llm_provider="meta",
            llm_api_key="k",
            llm_model="muse-spark-1.3-contributor",
        )


def test_generalising_the_backend_did_not_raise_the_spend_caps() -> None:
    settings = Settings(app_env="test")
    assert settings.llm_daily_soft_usd == Decimal("2.00")
    assert settings.llm_daily_hard_usd == Decimal("5.00")
    assert settings.llm_monthly_soft_usd == Decimal("30.00")
    assert settings.llm_monthly_hard_usd == Decimal("75.00")


def test_deepseek_keeps_its_time_of_day_pricing_and_others_do_not() -> None:
    assert DEEPSEEK.time_of_day_pricing is True
    assert META.time_of_day_pricing is False
    assert GENERIC.time_of_day_pricing is False


# ---------------------------------------------------------------------------
# Selection, and the absence of fallback
# ---------------------------------------------------------------------------


def test_the_factory_builds_exactly_the_configured_provider() -> None:
    deepseek = build_llm_client(Settings(app_env="test", deepseek_api_key="sk-test-not-real"))
    assert isinstance(deepseek, DeepSeekClient)
    assert deepseek.profile is DEEPSEEK

    muse = build_llm_client(_priced_settings())
    assert isinstance(muse, OpenAICompatibleClient)
    assert not isinstance(muse, DeepSeekClient)
    assert muse.profile is META


async def test_a_provider_failure_is_visible_and_never_retried_on_another_backend() -> None:
    """No silent failover: the configured provider fails, and the call fails."""
    attempted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempted.append(str(request.url))
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    http = ProviderHttpClient(
        provider="meta",
        base_url="https://api.meta.ai/v1",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.meta.ai/v1"
        ),
        backoff_base_seconds=0.001,
        backoff_max_seconds=0.002,
    )
    client = OpenAICompatibleClient(META, api_key="k", http=http, max_attempts=2)
    with pytest.raises(Exception) as excinfo:
        await client.complete(_request(model="muse-spark-1.3-contributor"))

    assert "deepseek" not in str(excinfo.value).lower()
    # Every attempt went to the configured host and nowhere else.
    assert attempted and all("api.meta.ai" in url for url in attempted)
    await client.aclose()


async def test_the_api_key_never_appears_in_an_error_for_any_provider() -> None:
    http = ProviderHttpClient(
        provider="generic",
        base_url="https://llm.internal/v1",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(401, text="bad key sk-generic-not-real")
            ),
            base_url="https://llm.internal/v1",
        ),
        backoff_base_seconds=0.001,
        backoff_max_seconds=0.002,
    )
    client = OpenAICompatibleClient(
        GENERIC, api_key="sk-generic-not-real", base_url="https://llm.internal/v1", http=http
    )
    with pytest.raises(Exception) as excinfo:
        await client.complete(_request())
    assert "sk-generic-not-real" not in str(excinfo.value)
    await client.aclose()


def test_a_profile_with_no_endpoint_requires_one_to_be_configured() -> None:
    with pytest.raises(ValueError, match="no default base URL"):
        OpenAICompatibleClient(GENERIC, api_key="k")


def test_a_blank_rate_variable_means_unset_rather_than_unparseable() -> None:
    """`.env.example` ships these declared and empty; a dotenv supplies "".

    Without this the shipped example refuses to load, and the error names
    decimal parsing rather than the missing configuration it represents.
    """
    settings = Settings(
        app_env="test",
        deepseek_api_key="sk-test-not-real",
        llm_input_usd_per_mtok="",
        llm_cached_input_usd_per_mtok="   ",
        llm_output_usd_per_mtok="",
    )
    assert settings.llm_input_usd_per_mtok is None
    assert settings.llm_model_rates() == {}
    # DeepSeek's built-in rates still price the default models.
    assert build_pricing_table(settings).rates_for("deepseek-v4-flash") is not None
