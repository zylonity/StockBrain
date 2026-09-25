"""Construction of the configured LLM backend.

One function, one decision. Keeping provider selection here rather than in
``services.py`` means the service container asks for "the LLM" and never learns
which endpoint answered -- which is what lets the classifier, the deduplicator
and the research engine stay provider-agnostic.

A fallback provider is optional (``LLM_FALLBACK_*``). When configured, it is
used only when the primary rate-limits us or is unavailable, every call it
answers is recorded under its own provider and model, and its spend counts
against the same caps -- see :mod:`stockbrain.llm.fallback`.
"""

from __future__ import annotations

from stockbrain.config import Settings
from stockbrain.llm.deepseek import DeepSeekClient
from stockbrain.llm.fallback import FallbackLlmClient
from stockbrain.llm.openai_compat import OpenAICompatibleClient
from stockbrain.llm.pricing import PricingTable
from stockbrain.llm.responses_api import ResponsesApiClient

__all__ = ["build_llm_client", "build_llm_client_with_fallback", "build_pricing_table"]


def build_llm_client(settings: Settings) -> OpenAICompatibleClient:
    """Build the client for the configured provider.

    DeepSeek keeps its own subclass so its settings-shaped constructor and its
    regression suite continue to exercise the DeepSeek dialect directly. Every
    other chat-completions provider is the generic client plus a profile; a
    provider whose profile declares a different wire protocol gets that
    protocol's client (``meta-go``: the Responses API, with its required
    ``x-opencode-session`` header).
    """
    if settings.llm_is_deepseek:
        return DeepSeekClient(settings, max_attempts=settings.active_llm_max_attempts)

    if settings.llm_profile.api_style == "responses":
        return ResponsesApiClient(
            settings.llm_profile,
            api_key=settings.active_llm_api_key.get_secret_value(),
            base_url=settings.active_llm_base_url,
            timeout_seconds=settings.active_llm_timeout_seconds,
            max_attempts=settings.active_llm_max_attempts,
        )

    return OpenAICompatibleClient(
        settings.llm_profile,
        api_key=settings.active_llm_api_key.get_secret_value(),
        base_url=settings.active_llm_base_url,
        timeout_seconds=settings.active_llm_timeout_seconds,
        max_attempts=settings.active_llm_max_attempts,
    )


def build_fallback_llm_client(settings: Settings) -> OpenAICompatibleClient:
    """Build the client for the configured fallback provider."""
    profile = settings.llm_fallback_profile
    cls = ResponsesApiClient if profile.api_style == "responses" else OpenAICompatibleClient
    return cls(
        profile,
        api_key=settings.llm_fallback_api_key.get_secret_value(),
        base_url=settings.active_llm_fallback_base_url,
        timeout_seconds=settings.active_llm_timeout_seconds,
        max_attempts=settings.active_llm_max_attempts,
    )


def build_llm_client_with_fallback(
    settings: Settings,
) -> OpenAICompatibleClient | FallbackLlmClient:
    """The primary client, wrapped with the fallback when one is configured."""
    primary = build_llm_client(settings)
    if not settings.llm_fallback_enabled:
        return primary
    return FallbackLlmClient(
        primary,
        build_fallback_llm_client(settings),
        fallback_provider=settings.llm_fallback_profile.name,
        primary_deep_model=settings.active_llm_deep_model,
        fallback_model=settings.llm_fallback_model,
        fallback_deep_model=settings.active_llm_fallback_deep_model,
    )


def build_pricing_table(settings: Settings) -> PricingTable:
    """Pricing for the configured provider, built-ins plus configured rates."""
    return PricingTable.from_settings(settings)
