"""Fallback between two LLM backends.

The primary backend answers every call it can. Only when it rate-limits us or
is unavailable *after its own retries* is the same request sent to the
fallback. Nothing else falls through: an authentication error, a malformed
response or a validation failure is a real fault and stays visible.

Every call the fallback answers is attributed to the fallback provider and
model in ``llm_calls``, so the audit trail always says which model produced a
stored result, and its spend counts against the same budget caps.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from stockbrain.errors import ProviderRateLimited, ProviderUnavailable
from stockbrain.llm.base import CompletionRequest, CompletionResult, LlmProvider
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

if TYPE_CHECKING:
    from langchain_core.messages import AIMessage, BaseMessage

    from stockbrain.intelligence.research import CheckBudget, RecordCall
    from stockbrain.intelligence.research_transport import ResearchTransport

__all__ = ["FALLBACK_ERRORS", "FallbackLlmClient", "FallbackResearchTransport"]

log = get_logger(__name__)

#: Failures that mean "the primary cannot answer right now", not "the request
#: is wrong". Only these are sent to the fallback.
FALLBACK_ERRORS: tuple[type[Exception], ...] = (ProviderRateLimited, ProviderUnavailable)


def _count(provider: str, purpose: str) -> None:
    METRICS.inc("stockbrain_llm_fallback_total", labels={"provider": provider, "purpose": purpose})


def _fallback_model(
    model: str, *, primary_deep: str, fallback_quick: str, fallback_deep: str
) -> str:
    """Map a primary model name onto the fallback's equivalent depth."""
    if model == primary_deep and primary_deep and fallback_deep:
        return fallback_deep
    return fallback_quick


class FallbackLlmClient:
    """An :class:`LlmProvider` that retries on a second backend when the first is down."""

    def __init__(
        self,
        primary: LlmProvider,
        fallback: LlmProvider,
        *,
        fallback_provider: str,
        primary_deep_model: str,
        fallback_model: str,
        fallback_deep_model: str,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = getattr(primary, "name", "primary")
        self.fallback_provider = fallback_provider
        self._primary_deep = primary_deep_model
        self._fallback_quick = fallback_model
        self._fallback_deep = fallback_deep_model

    def __getattr__(self, item: str) -> Any:
        # Anything beyond the protocol (profile, verify_credentials, ...) is the primary's.
        return getattr(self.primary, item)

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        try:
            return await self.primary.complete(request)
        except FALLBACK_ERRORS as exc:
            model = _fallback_model(
                request.model,
                primary_deep=self._primary_deep,
                fallback_quick=self._fallback_quick,
                fallback_deep=self._fallback_deep,
            )
            log.warning(
                "llm_fallback_used",
                purpose=request.purpose,
                primary_error=type(exc).__name__,
                fallback_provider=self.fallback_provider,
                fallback_model=model,
            )
            _count(self.fallback_provider, request.purpose)
            result = await self.fallback.complete(dataclasses.replace(request, model=model))
            result.provider = self.fallback_provider
            return result

    async def aclose(self) -> None:
        await self.primary.aclose()
        await self.fallback.aclose()


class FallbackResearchTransport:
    """The research transport equivalent of :class:`FallbackLlmClient`.

    Each research role is one call, so a fallback switch happens per role: a
    run can mix providers, and ``llm_calls`` records which answered each role.
    """

    def __init__(
        self,
        primary: ResearchTransport,
        fallback: ResearchTransport,
        *,
        fallback_provider: str,
        primary_deep_model: str,
        fallback_model: str,
        fallback_deep_model: str,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.fallback_provider = fallback_provider
        self._primary_deep = primary_deep_model
        self._fallback_quick = fallback_model
        self._fallback_deep = fallback_deep_model

    def __getattr__(self, item: str) -> Any:
        return getattr(self.primary, item)

    async def complete(
        self,
        messages: list[BaseMessage],
        *,
        role: str,
        model: str,
        thinking: bool,
        record_call: RecordCall,
        check_budget: CheckBudget,
        **kwargs: Any,
    ) -> AIMessage:
        try:
            return await self.primary.complete(
                messages,
                role=role,
                model=model,
                thinking=thinking,
                record_call=record_call,
                check_budget=check_budget,
                **kwargs,
            )
        except FALLBACK_ERRORS as exc:
            fallback_model = _fallback_model(
                model,
                primary_deep=self._primary_deep,
                fallback_quick=self._fallback_quick,
                fallback_deep=self._fallback_deep,
            )
            log.warning(
                "llm_fallback_used",
                purpose=f"RESEARCH:{role}",
                primary_error=type(exc).__name__,
                fallback_provider=self.fallback_provider,
                fallback_model=fallback_model,
            )
            _count(self.fallback_provider, f"RESEARCH:{role}")
            # The fallback transport records its calls under its own profile name.
            return await self.fallback.complete(
                messages,
                role=role,
                model=fallback_model,
                thinking=thinking,
                record_call=record_call,
                check_budget=check_budget,
                **kwargs,
            )

    async def aclose(self) -> None:
        await self.primary.aclose()
        await self.fallback.aclose()
