"""Validated schemas for classifier output.

Nothing here trusts the model. JSON that parses is not JSON that is correct: a
model can return `materiality: 5`, `direction: "up"`, an empty company name or a
thousand companies, and every one of those must be rejected or clamped *here*
rather than reaching the database.

The scores are ranking features, not calibrated probabilities. `confidence: 0.9`
means the model ranked this above a 0.8; it does not mean 90%.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

__all__ = [
    "MAX_COMPANIES",
    "ClassifiedEvent",
    "CompanyImpact",
    "DedupeRelation",
    "DedupeVerdict",
    "SemanticDedupeResult",
]

#: Upper bound on companies accepted from one classification. A model that
#: returns hundreds has misunderstood the task; truncating keeps one bad response
#: from writing an unbounded number of rows.
MAX_COMPANIES = 25

#: Upper bound on topics, for the same reason.
MAX_TOPICS = 12

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
_LongText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]
_Score = Annotated[float, Field(ge=0.0, le=1.0)]


class CompanyImpact(BaseModel):
    """One company the model believes an event affects.

    ``ticker_hint`` is a *hint*. It is never sufficient to place an order: the
    instrument resolution boundary (phase 4) must match a real broker instrument,
    preferably by ISIN, before any quantity is computed.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    company_name: _Text
    # Deliberately unconstrained at the Field level so that an implausible value
    # is nulled by the validators below rather than failing the whole
    # classification. Losing a hint is recoverable; losing the company impact
    # record because the model wrote prose in one field is not.
    ticker_hint: str | None = None
    exchange_hint: str | None = None

    relationship: _Text = Field(
        description="How the company relates to the event, in the model's words."
    )
    impact_path: Literal["direct", "indirect", "unknown"] = "unknown"
    """Direct: the event names or is about this company. Indirect: the company is
    affected through a supply chain, customer, sector or commodity linkage.

    Kept separate from materiality because a large indirect effect and a small
    direct one are different things, and downstream sizing should be able to tell
    them apart."""

    direction: Literal["positive", "negative", "mixed", "unknown"] = "unknown"
    materiality: _Score
    confidence: _Score

    @field_validator("ticker_hint", "exchange_hint", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """Models return "", "null", "N/A" and "unknown" for absent values."""
        if value is None:
            return None
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned.lower() in ("", "null", "none", "n/a", "na", "unknown", "-"):
                return None
            return cleaned
        # A non-string (number, list, dict) is not a hint; treat it as absent.
        return None

    @field_validator("exchange_hint")
    @classmethod
    def _bound_exchange(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value[:64] or None

    @field_validator("ticker_hint")
    @classmethod
    def _plausible_ticker(cls, value: str | None) -> str | None:
        """Reject anything that is obviously not a ticker.

        This does not resolve the ticker -- it only stops sentences and invented
        prose from being stored in a field the resolver will later read.
        """
        if value is None:
            return None
        candidate = value.strip().upper()
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-: ")
        if len(candidate) > 32 or not candidate or set(candidate) - allowed:
            return None
        return candidate

    @field_validator("direction", mode="before")
    @classmethod
    def _normalise_direction(cls, value: object) -> object:
        if isinstance(value, str):
            lowered = value.strip().lower()
            aliases = {
                "up": "positive",
                "bullish": "positive",
                "down": "negative",
                "bearish": "negative",
                "neutral": "mixed",
                "both": "mixed",
                "": "unknown",
            }
            return aliases.get(lowered, lowered)
        return value


class ClassifiedEvent(BaseModel):
    """The full validated classifier result.

    ``rationale`` is the model's structured justification, which is part of the
    requested schema and is safe to show. Any ``reasoning_content`` the provider
    returns is deliberately not part of this model and is never surfaced.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    relevant_to_public_equities: bool
    event_type: _Text = "OTHER"
    canonical_title: _Text
    summary: _LongText = ""
    event_time: dt.datetime | None = None

    novelty: _Score = 0.0
    importance: _Score = 0.0
    confidence: _Score = 0.0

    companies: list[CompanyImpact] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    needs_corroboration: bool = True
    rationale: _LongText = ""

    @field_validator("event_time", mode="before")
    @classmethod
    def _drop_unparseable_time(cls, value: object) -> object:
        """A wrong timestamp is worse than none: staleness checks depend on it."""
        if value is None or isinstance(value, dt.datetime):
            return value
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned or cleaned.lower() in ("null", "none", "unknown", "n/a"):
                return None
            return cleaned
        return None

    @field_validator("event_time")
    @classmethod
    def _as_utc(cls, value: dt.datetime | None) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)

    @field_validator("topics", mode="before")
    @classmethod
    def _clean_topics(cls, value: object) -> object:
        if not isinstance(value, list):
            return []
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            topic = item.strip().lower()[:64]
            if topic and topic not in cleaned:
                cleaned.append(topic)
        return cleaned[:MAX_TOPICS]

    @field_validator("companies", mode="before")
    @classmethod
    def _bound_companies(cls, value: object) -> object:
        if not isinstance(value, list):
            return []
        return value[:MAX_COMPANIES]

    @model_validator(mode="after")
    def _irrelevant_events_carry_no_companies(self) -> ClassifiedEvent:
        """An event the model called irrelevant must not also assert impacts.

        Letting both through would allow an "irrelevant" event to seed company
        impact rows that later stages might act on.
        """
        if not self.relevant_to_public_equities and self.companies:
            object.__setattr__(self, "companies", [])
        return self

    @property
    def max_materiality(self) -> float:
        return max((company.materiality for company in self.companies), default=0.0)


DedupeRelation = Literal["SAME_EVENT", "UPDATE_TO_EVENT", "RELATED_DIFFERENT_EVENT", "UNRELATED"]


class DedupeVerdict(BaseModel):
    """The model's judgement about one candidate event."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    candidate_id: str
    relation: DedupeRelation = "UNRELATED"
    confidence: _Score = 0.0
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] = ""

    @field_validator("relation", mode="before")
    @classmethod
    def _normalise_relation(cls, value: object) -> object:
        """Anything unrecognised becomes UNRELATED: the safe direction is to keep
        events separate, because a wrong merge is far harder to notice than a
        duplicate."""
        if isinstance(value, str):
            candidate = value.strip().upper().replace(" ", "_").replace("-", "_")
            if candidate in (
                "SAME_EVENT",
                "UPDATE_TO_EVENT",
                "RELATED_DIFFERENT_EVENT",
                "UNRELATED",
            ):
                return candidate
            return "UNRELATED"
        return "UNRELATED"


class SemanticDedupeResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdicts: list[DedupeVerdict] = Field(default_factory=list)

    @field_validator("verdicts", mode="before")
    @classmethod
    def _bound(cls, value: object) -> object:
        if not isinstance(value, list):
            return []
        return value[:MAX_COMPANIES]

    def best_merge_target(self, minimum_confidence: float) -> DedupeVerdict | None:
        """The highest-confidence SAME_EVENT/UPDATE verdict above the threshold.

        Merging is the destructive-ish direction, so it requires clearing a
        configured confidence bar; everything else stays separate.
        """
        merges = [
            verdict
            for verdict in self.verdicts
            if verdict.relation in ("SAME_EVENT", "UPDATE_TO_EVENT")
            and verdict.confidence >= minimum_confidence
        ]
        if not merges:
            return None
        return max(merges, key=lambda verdict: verdict.confidence)


def example_classifier_payload() -> dict[str, Any]:
    """A schema example for the prompt.

    DeepSeek's JSON mode documentation recommends showing the desired shape, so
    the prompt embeds this rather than describing it in prose.
    """
    return {
        "relevant_to_public_equities": True,
        "event_type": "CONTRACT_AWARD",
        "canonical_title": "Example Corp wins data centre cooling contract",
        "summary": "One or two sentences describing what happened.",
        "event_time": "2026-09-04T12:30:00Z",
        "novelty": 0.7,
        "importance": 0.65,
        "confidence": 0.8,
        "needs_corroboration": False,
        "topics": ["ai_infrastructure", "data_centres"],
        "rationale": "Why this classification, referring only to the evidence.",
        "companies": [
            {
                "company_name": "Example Corp",
                "ticker_hint": "EXMP",
                "exchange_hint": "NASDAQ",
                "relationship": "Named as the contract winner.",
                "impact_path": "direct",
                "direction": "positive",
                "materiality": 0.6,
                "confidence": 0.75,
            }
        ],
    }


def example_dedupe_payload() -> dict[str, Any]:
    return {
        "verdicts": [
            {
                "candidate_id": "1",
                "relation": "SAME_EVENT",
                "confidence": 0.85,
                "reason": "Both describe the same contract award on the same day.",
            }
        ]
    }
