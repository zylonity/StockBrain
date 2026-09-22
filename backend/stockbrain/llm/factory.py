"""Construction of the configured LLM backend.

One function, one decision. Keeping provider selection here rather than in
``services.py`` means the service container asks for "the LLM" and never learns
which endpoint answered -- which is what lets the classifier, the deduplicator
and the research engine stay provider-agnostic.

There is deliberately **no fallback between providers**. If the configured
backend fails, the call fails and the failure is visible. An automatic switch to
a second provider would hide an outage, silently change the cost profile of
whatever ran next, and make the ``llm_calls`` audit trail ambiguous about which
model actually produced a stored classification. This mirrors the rule the
discovery layer already enforces for paid search providers.
"""

from __future__ import annotations

from stockbrain.config import Settings
from stockbrain.llm.deepseek import DeepSeekClient
from stockbrain.llm.openai_compat import OpenAICompatibleClient
from stockbrain.llm.pricing import PricingTable
from stockbrain.llm.responses_api import ResponsesApiClient

__all__ = ["build_llm_client", "build_pricing_table"]


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


def build_pricing_table(settings: Settings) -> PricingTable:
    """Pricing for the configured provider, built-ins plus configured rates."""
    return PricingTable.from_settings(settings)
