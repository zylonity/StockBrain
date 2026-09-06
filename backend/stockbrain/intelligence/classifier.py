"""Event classification via DeepSeek V4 Flash.

The classifier turns one retrieved document into a validated
:class:`ClassifiedEvent`. It has exactly one capability: send text, receive text.
It cannot reach the broker, the filesystem, or any network endpoint other than
the configured model provider, because the provider interface it holds
(:class:`~stockbrain.llm.base.LlmProvider`) exposes nothing else.

Model output is never trusted because it parsed. Every response is validated
against the Pydantic schema, and a response that parses but violates the schema
is a failure, not a partial success.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass

from pydantic import ValidationError

from stockbrain.db.base import utcnow
from stockbrain.errors import ProviderError, ProviderResponseError
from stockbrain.intelligence.prompts import PromptTemplate, load_prompt, sanitize_untrusted
from stockbrain.intelligence.schemas import ClassifiedEvent, example_classifier_payload
from stockbrain.llm.base import ChatMessage, CompletionRequest, CompletionResult, LlmProvider
from stockbrain.llm.openai_compat import extract_json
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = ["ClassificationInput", "ClassificationOutcome", "EventClassifier"]

log = get_logger(__name__)

CLASSIFIER_PURPOSE = "CLASSIFY_EVENT"

#: Output budget. Large enough for the full schema with 25 companies, bounded so
#: a runaway response cannot cost unboundedly. Truncation surfaces as an explicit
#: error rather than as invalid JSON.
MAX_OUTPUT_TOKENS = 3000


@dataclass(slots=True)
class ClassificationInput:
    """Everything the classifier is given about one document.

    Deliberately narrow: metadata StockBrain established during retrieval, plus
    the document body. No credentials, no configuration, no portfolio positions
    beyond what a caller explicitly passes.
    """

    headline: str | None
    body: str | None
    provider: str
    source_name: str | None = None
    source_category: str | None = None
    url: str | None = None
    published_at: dt.datetime | None = None
    symbol_hints: list[str] | None = None
    event_id: uuid.UUID | None = None


@dataclass(slots=True)
class ClassificationOutcome:
    """A validated classification plus the call that produced it."""

    classification: ClassifiedEvent
    result: CompletionResult
    prompt_version: str


class EventClassifier:
    """Classifies one document at a time."""

    def __init__(
        self,
        provider: LlmProvider,
        *,
        model: str,
        prompt_name: str = "event_classifier",
        prompt_version: str = "v1",
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
        timeout_seconds: float | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._prompt_name = prompt_name
        self._prompt_version = prompt_version
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds

    @property
    def prompt(self) -> PromptTemplate:
        return load_prompt(self._prompt_name, self._prompt_version)

    @property
    def prompt_version(self) -> str:
        return self.prompt.identifier

    @property
    def model(self) -> str:
        return self._model

    def build_messages(
        self, payload: ClassificationInput, *, as_of: dt.datetime
    ) -> list[ChatMessage]:
        """Render the prompt.

        The document body is sanitised and fenced by the template; the values
        substituted here are never re-scanned for placeholders, so document text
        cannot introduce prompt structure.
        """
        template = self.prompt
        hints = ", ".join(payload.symbol_hints or []) or "(none)"
        system, user = template.render(
            {
                "SCHEMA_EXAMPLE": json.dumps(example_classifier_payload(), indent=2),
                "AS_OF": as_of.astimezone(dt.UTC).isoformat(),
                "PROVIDER": payload.provider,
                "SOURCE_NAME": payload.source_name or "(unknown)",
                "SOURCE_CATEGORY": payload.source_category or "UNKNOWN",
                "URL": payload.url or "(none)",
                "PUBLISHED_AT": (
                    payload.published_at.astimezone(dt.UTC).isoformat()
                    if payload.published_at
                    else "(unknown)"
                ),
                "SYMBOL_HINTS": hints,
                "DOCUMENT": sanitize_untrusted(
                    f"title: {payload.headline or '(no title)'}\n\n{payload.body or ''}"
                ),
            }
        )
        return [ChatMessage("system", system), ChatMessage("user", user)]

    async def classify(
        self, payload: ClassificationInput, *, as_of: dt.datetime | None = None
    ) -> ClassificationOutcome:
        """Classify one document.

        Raises :class:`ProviderResponseError` when the model returns something
        that is not a valid :class:`ClassifiedEvent`, and the provider's own
        errors otherwise. Both become a visible failed state upstream; neither is
        silently swallowed into a default classification.
        """
        moment = as_of or utcnow()
        request = CompletionRequest(
            messages=self.build_messages(payload, as_of=moment),
            model=self._model,
            max_output_tokens=self._max_output_tokens,
            temperature=0.0,
            json_object=True,
            # Derived from the same model that validates the reply, so a
            # schema-based endpoint is constrained by exactly what the parser
            # will accept and the two cannot drift apart. Endpoints whose JSON
            # mode is schemaless ignore it and rely on the prompt.
            json_schema=ClassifiedEvent.model_json_schema(),
            json_schema_name="ClassifiedEvent",
            # Non-thinking wherever the provider allows it: this is the cheap
            # high-volume triage path, and at least one supported API defaults
            # reasoning to enabled.
            thinking=False,
            timeout_seconds=self._timeout_seconds,
            purpose=CLASSIFIER_PURPOSE,
        )

        METRICS.inc("stockbrain_classifier_calls_total", labels={"model": self._model})
        try:
            result = await self._provider.complete(request)
        except ProviderError:
            METRICS.inc("stockbrain_classifier_failures_total", labels={"kind": "provider"})
            raise

        classification = self.parse(result.content)
        return ClassificationOutcome(
            classification=classification,
            result=result,
            prompt_version=self.prompt_version,
        )

    @staticmethod
    def parse(content: str) -> ClassifiedEvent:
        """Decode and validate model output.

        Two distinct failures, both fatal for this attempt:
        the text was not JSON, or the JSON did not satisfy the schema. Neither is
        coerced into a usable answer.
        """
        raw = extract_json(content)
        if not isinstance(raw, dict):
            METRICS.inc("stockbrain_classifier_failures_total", labels={"kind": "shape"})
            raise ProviderResponseError(
                f"classifier returned {type(raw).__name__}, expected a JSON object"
            )
        try:
            return ClassifiedEvent.model_validate(raw)
        except ValidationError as exc:
            METRICS.inc("stockbrain_classifier_failures_total", labels={"kind": "schema"})
            # The error names the offending fields but not their values, so a
            # malicious document cannot echo itself into the logs at will.
            fields = sorted({".".join(str(part) for part in err["loc"]) for err in exc.errors()})
            raise ProviderResponseError(
                f"classifier output failed schema validation on: {', '.join(fields[:10])}"
            ) from exc
