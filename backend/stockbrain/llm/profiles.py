"""Declarative differences between OpenAI-compatible chat-completions endpoints.

"OpenAI-compatible" is a family resemblance, not a specification. Every provider
in this file speaks ``POST /chat/completions`` with a bearer token and returns
``choices[0].message.content``, and every one of them disagrees with the others
about at least one field that StockBrain actually depends on:

* the name of the output-token cap (``max_tokens`` vs ``max_completion_tokens``)
* how to ask for JSON (``json_object`` vs ``json_schema``, or not at all)
* how to turn reasoning *off* -- and whether that is even permitted
* where cached prompt tokens are reported, which decides whether a cost estimate
  is right or wrong by an order of magnitude

Encoding those as data rather than as ``if provider == ...`` ladders inside the
client is the whole point: adding an endpoint means adding a profile, and the
request builder and response parser stay single-path and testable.

A profile describes the **API contract**. It deliberately carries no prices --
rates come from configuration, because a hard spend limit must never depend on a
constant that was accurate when this file was written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "PROFILES",
    "ApiStyle",
    "CacheUsageStyle",
    "ProviderProfile",
    "ReasoningStyle",
    "StructuredOutputStyle",
    "profile_for",
]

#: How the endpoint expresses "return JSON".
#:
#: ``json_object`` -- ``response_format={"type": "json_object"}`` (DeepSeek, OpenAI).
#: ``json_schema`` -- ``response_format={"type": "json_schema", ...}``; the caller
#: must supply a schema, so a profile using it needs a schema to send.
#: ``none`` -- no native JSON mode; the prompt asks and the parser validates.
StructuredOutputStyle = Literal["json_object", "json_schema", "none"]

#: How the endpoint expresses "do not reason on this call".
#:
#: ``deepseek_thinking`` -- ``thinking={"type": "enabled"|"disabled"}``.
#: ``reasoning_effort`` -- an effort knob with no off switch; the profile's
#: ``minimum_reasoning_effort`` is the closest thing to disabled.
#: ``none`` -- the endpoint has no reasoning control; send nothing.
ReasoningStyle = Literal["deepseek_thinking", "reasoning_effort", "none"]

#: The wire protocol the endpoint speaks, not just its field spellings.
#:
#: ``chat_completions`` -- ``POST /chat/completions`` with a ``messages`` list
#: and ``choices[0].message`` back. What all profiles below assumed before
#: OpenCode Go's Muse Spark endpoint: that one is served *only* through the
#: Responses API (a ``chat/completions`` POST returns 503 "Endpoint is
#: unavailable" for the contributor model, verified live 2026-09-21), so a
#: profile that cannot be expressed as fields on a chat body needs this knob.
#: ``responses`` -- ``POST`` to :attr:`chat_path` (``/responses``) with an
#: ``input`` list and an ``output`` item list back; see
#: :mod:`stockbrain.llm.responses_api` for the builders that speak it.
ApiStyle = Literal["chat_completions", "responses"]

#: Where cached prompt tokens appear in ``usage``.
#:
#: ``deepseek_split`` -- ``prompt_cache_hit_tokens`` + ``prompt_cache_miss_tokens``,
#: which partition ``prompt_tokens``.
#: ``openai_details`` -- ``prompt_tokens_details.cached_tokens``, a *subset* of
#: ``prompt_tokens``; the miss count is the remainder.
#: ``none`` -- not reported. Everything is billed at the uncached rate, which
#: over-estimates. Guessing a hit rate here would under-estimate, and a budget
#: that under-counts is the one failure mode worth engineering against.
CacheUsageStyle = Literal["deepseek_split", "openai_details", "none"]


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """The API contract of one OpenAI-compatible endpoint."""

    name: str

    #: Default endpoint. Configuration may override it; a profile whose
    #: ``base_url`` is empty (``generic``) requires the operator to supply one.
    base_url: str = ""

    chat_path: str = "/chat/completions"

    #: Which wire protocol ``chat_path`` serves. Chat-completions for every
    #: shipped profile except ``meta-go``; see :data:`ApiStyle`.
    api_style: ApiStyle = "chat_completions"

    #: Catalogue endpoint used to validate a credential without spending tokens.
    #: ``None`` means the endpoint has none and startup must not probe it.
    models_path: str | None = "/models"

    max_output_tokens_field: str = "max_tokens"

    structured_output: StructuredOutputStyle = "json_object"

    #: DeepSeek documents that JSON mode requires the word "json" in the prompt
    #: and otherwise returns prose. Checked before sending, because the failure
    #: is a confusing model reply rather than an API error.
    requires_json_keyword: bool = False

    #: Whether a ``json_schema`` endpoint must be asked to *enforce* the schema.
    #:
    #: Meta documents ``strict`` as defaulting to ``false``, which makes the
    #: schema a hint. Observed live on 2026-09-06, an unenforced request
    #: returned corrupted key escaping -- ``"confidence\": 0.95`` parses as a
    #: key named ``confidence"``, so the real field fell back to its default and
    #: a duplicate event silently failed to merge. Wrong-but-parseable is the
    #: worst failure mode available here, so the schema is enforced.
    strict_structured_output: bool = False

    reasoning: ReasoningStyle = "none"

    #: Lowest ``reasoning_effort`` the endpoint accepts, for profiles that cannot
    #: switch reasoning off. ``None`` when the knob does not exist.
    minimum_reasoning_effort: str | None = None

    cache_usage: CacheUsageStyle = "none"

    #: Response field carrying hidden reasoning. Detected so the *fact* can be
    #: recorded; the text itself is dropped and never persisted or displayed.
    reasoning_content_field: str | None = None

    supports_temperature: bool = True

    #: ``finish_reason`` values that mean "the server could not serve this",
    #: not "the model answered". Retried exactly like a 5xx.
    retryable_finish_reasons: frozenset[str] = frozenset()

    #: Prices vary by time of day (DeepSeek). Flat elsewhere. Pricing itself
    #: lives in configuration; this only says whether the clock is an input.
    time_of_day_pricing: bool = False

    #: Documented requests-per-minute ceiling, for operator-facing docs only.
    #: Never enforced client-side -- the server is the authority, and a local
    #: limiter that disagrees with it just fails differently.
    documented_rpm: int | None = None

    notes: tuple[str, ...] = field(default_factory=tuple)


#: DeepSeek. StockBrain's original and still-default backend; every quirk here is
#: verified against its documentation (2026-09-04) and covered by the existing
#: DeepSeek regression tests.
DEEPSEEK = ProviderProfile(
    name="deepseek",
    base_url="https://api.deepseek.com",
    max_output_tokens_field="max_tokens",
    structured_output="json_object",
    requires_json_keyword=True,
    reasoning="deepseek_thinking",
    cache_usage="deepseek_split",
    reasoning_content_field="reasoning_content",
    retryable_finish_reasons=frozenset({"insufficient_system_resource"}),
    time_of_day_pricing=True,
    notes=(
        "thinking defaults to enabled and must be disabled explicitly",
        "off-peak rates are half of peak, 01:00-04:00 and 06:00-10:00 UTC weekdays",
    ),
)

#: Meta Model API. Verified against dev.meta.ai/docs on 2026-09-06; see
#: docs/sources.md for the per-field citation. Included because it is a worked
#: example of an endpoint that is OpenAI-*shaped* while disagreeing with DeepSeek
#: on four of the five fields this module exists to abstract.
META = ProviderProfile(
    name="meta",
    base_url="https://api.meta.ai/v1",
    # Documented as the current field; `max_tokens` is a deprecated alias.
    max_output_tokens_field="max_completion_tokens",
    # Only `json_schema` is documented. `json_object` is not, so it is not used.
    structured_output="json_schema",
    requires_json_keyword=False,
    # Verified necessary, not merely advisable: see the field's own comment.
    strict_structured_output=True,
    # Muse Spark always reasons internally: `reasoning_effort: "none"` is a 400.
    # "minimal" is the floor, so a classifier call cannot be made non-reasoning.
    reasoning="reasoning_effort",
    minimum_reasoning_effort="minimal",
    cache_usage="openai_details",
    reasoning_content_field=None,
    retryable_finish_reasons=frozenset(),
    time_of_day_pricing=False,
    documented_rpm=100,  # contributor tier; standard tier is 3,000
    notes=(
        "reasoning cannot be disabled; minimal is the floor",
        "json_schema strict defaults to false and must be requested explicitly",
        "prompt caching is automatic and reported as prompt_tokens_details.cached_tokens",
        "the -contributor model tier trains on prompts and completions",
    ),
)

#: OpenCode Go's Muse Spark contributor endpoint. The *prices* are Meta's
#: contributor list card forwarded unchanged (OpenCode documents that it passes
#: provider rates through; the subscription buys ~6x usage, it does not change
#: the rate card), so operator pricing stays ``0.10 / 0.002 / 0.20``.
#:
#: Same Muse Spark quirks as the direct ``meta`` profile, but served only
#: through the Responses API -- verified live 2026-09-21: ``/chat/completions``
#: returns 503 "Endpoint is unavailable" for this model, ``/responses`` answers.
#: Two differences beyond field spellings, both verified live the same day:
#:
#: * credential probe must carry ``x-opencode-session`` (a hard 400 without it:
#:   "MissingSessionID ... cannot be routed efficiently"), so the client sets a
#:   stable per-process session id, and the research transport re-scopes it per
#:   research run. It is a routing/cache-affinity hint, not a credential.
#: * the workspace's privacy settings must allow "paid endpoints that train on
#:   request data" -- this is the contributor tier, whose entire discount is
#:   Meta's right to train on prompts and completions. Denied there, every call
#:   is a 400 that no client-side retry can fix.
#:
#: Requires ``LLM_API_KEY`` (a Go key) and, if overridden, an ``LLM_BASE_URL``
#: that is the ``/v1`` root -- the client appends :attr:`chat_path` itself.
META_GO = ProviderProfile(
    name="meta-go",
    base_url="https://opencode.ai/zen/go/v1",
    chat_path="/responses",
    api_style="responses",
    # Responses-spelling field, not chat's ``max_completion_tokens``.
    max_output_tokens_field="max_output_tokens",
    structured_output="json_schema",
    requires_json_keyword=False,
    # Same live-observed rule as ``meta``: an unenforced schema is only a hint
    # and produced corrupted key escaping on the direct endpoint. Tighter is the
    # only safe reading here.
    strict_structured_output=True,
    reasoning="reasoning_effort",
    # Muse Spark cannot switch reasoning off ("none"), on the Responses API
    # spelled ``{"reasoning": {"effort": ...}}``; the delta from ``meta`` is the
    # builder's job, not a second reasoning style.
    minimum_reasoning_effort="minimal",
    # Same economics and cache mechanic as the direct ``meta`` profile; the
    # Responses usage dialect's field names are mapped by the responses parser.
    cache_usage="openai_details",
    reasoning_content_field=None,
    retryable_finish_reasons=frozenset(),
    time_of_day_pricing=False,
    documented_rpm=100,  # contributor tier, same as direct
    notes=(
        "served only through the Responses API; chat/completions is a 503",
        "requires x-opencode-session on every call (stability, not security)",
        "reasoning cannot be disabled; minimal is the floor",
        "workspace privacy must allow endpoints that train on request data",
        "the -contributor model tier trains on prompts and completions",
    ),
)

#: OpenAI itself, and the many gateways that mirror it exactly.
OPENAI = ProviderProfile(
    name="openai",
    base_url="https://api.openai.com/v1",
    max_output_tokens_field="max_completion_tokens",
    structured_output="json_object",
    reasoning="none",
    cache_usage="openai_details",
    time_of_day_pricing=False,
)

#: Anything else that speaks the dialect. The operator supplies the base URL and
#: the model, and gets the most conservative reading of every optional feature:
#: no reasoning field, no cache accounting (so everything bills at the uncached
#: rate), and JSON asked for the way almost every endpoint spells it.
#:
#: This is the profile that makes "any OpenAI-compatible endpoint" true rather
#: than aspirational. It is deliberately the least clever one.
GENERIC = ProviderProfile(
    name="generic",
    base_url="",
    max_output_tokens_field="max_tokens",
    structured_output="json_object",
    reasoning="none",
    cache_usage="none",
    time_of_day_pricing=False,
    notes=("cached tokens are not assumed; all prompt tokens bill at the uncached rate",),
)

#: A generic endpoint that has no JSON mode at all. Same as ``generic`` except
#: the request carries no ``response_format``; the prompt asks for JSON and the
#: existing schema validation rejects anything else. Local llama.cpp and vLLM
#: builds frequently land here.
GENERIC_NO_JSON = ProviderProfile(
    name="generic-no-json",
    base_url="",
    max_output_tokens_field="max_tokens",
    structured_output="none",
    reasoning="none",
    cache_usage="none",
    time_of_day_pricing=False,
)


PROFILES: dict[str, ProviderProfile] = {
    profile.name: profile for profile in (DEEPSEEK, META, META_GO, OPENAI, GENERIC, GENERIC_NO_JSON)
}


def profile_for(name: str) -> ProviderProfile:
    """Look up a profile by name.

    Raises ``ValueError`` for an unknown name rather than falling back to
    ``generic``: a typo in ``LLM_PROVIDER`` must not silently redirect traffic
    onto a backend with different pricing and different capabilities. This
    mirrors the discovery layer, where an unknown provider resolves to "do not
    run" rather than to the configured default.
    """
    try:
        return PROFILES[name.strip().lower()]
    except KeyError:
        known = ", ".join(sorted(PROFILES))
        raise ValueError(f"unknown LLM provider {name!r}; known profiles: {known}") from None
