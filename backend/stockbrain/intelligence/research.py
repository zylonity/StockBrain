"""Stable, immutable research contracts. No upstream state or execution fields."""

from __future__ import annotations

import datetime as dt
import html
import json
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Annotated, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from stockbrain.enums import ThesisAction, TimeHorizon
from stockbrain.errors import (
    InstrumentResolutionError,
    ProviderAuthError,
    ProviderEntitlementError,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
    StockBrainError,
)
from stockbrain.llm.telemetry import LlmCallRecord

PROMPT_VERSION = "research-v3"
UPSTREAM_COMMIT = "2448d0a12576f9b2ddcd5980a0630833423d1e1b"
ROLES = ("market", "fundamentals", "sentiment", "bull", "bear", "manager", "trader")
DEEP_ROLES = frozenset({"bull", "bear", "manager", "trader"})


class ResearchError(StockBrainError):
    """Safe research error; provider bodies and model output never enter messages."""


class ResearchValidationError(ResearchError):
    pass


class ResearchToolError(ResearchError):
    pass


class ResearchBudgetBlockedError(ResearchError):
    pass


class FrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", allow_inf_nan=False, str_strip_whitespace=True
    )


Text = Annotated[str, Field(min_length=1, max_length=20000)]
Items = Annotated[
    tuple[Annotated[str, Field(min_length=1, max_length=2000)], ...], Field(max_length=30)
]


class ResearchDecision(FrozenModel):
    action: ThesisAction
    confidence: float = Field(
        ge=0,
        le=1,
        strict=True,
        description=(
            "Strength of the evidential case for this action on this horizon. Not the"
            " probability of a price move, not an expected return, and not a position size."
            " Use the full range: the top for a decisive, corroborated case, the bottom for"
            " a marginal one. A downstream risk engine both blocks and shrinks positions on"
            " this number, so hedging it toward the middle quietly discards a call you hold."
        ),
    )
    horizon: TimeHorizon
    thesis: Text
    bull_case: Text
    bear_case: Text
    catalysts: Items
    risks: Items
    invalidation_conditions: Items
    evidence_ids: tuple[uuid.UUID, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def public_analysis_only(self) -> ResearchDecision:
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("duplicate evidence ids")
        for value in (
            self.thesis,
            self.bull_case,
            self.bear_case,
            *self.catalysts,
            *self.risks,
            *self.invalidation_conditions,
        ):
            public_text(value)
        return self


class EvidenceDocument(FrozenModel):
    source_id: uuid.UUID
    publisher: str | None
    url: str | None
    published_at: AwareDatetime | None
    received_at: AwareDatetime
    text: str = Field(max_length=20000)
    relationship: str
    text_truncated: bool = False


class ResolvedCompany(FrozenModel):
    company_id: uuid.UUID
    name: str
    broker_instrument_id: uuid.UUID
    broker_ticker: str
    symbol: str = Field(min_length=1)
    exchange: str | None
    currency: str | None
    isin: str | None
    resolution_status: Literal["RESOLVED"] = "RESOLVED"


class ResearchDatum(FrozenModel):
    """Bounded normalized provider result, never an uncontrolled provider object."""

    provider: str
    kind: str
    as_of: AwareDatetime
    text: str = Field(max_length=40000)
    research_only: bool = True


class ProviderDegradation(FrozenModel):
    provider: str
    error_class: str
    detail: str


class CalibrationRecord(BaseModel):
    """Pydantic mirror of ``stockbrain.risk.models.CalibrationBucket`` (spec §6)."""

    model_config = ConfigDict(frozen=True)

    key: str
    samples: int
    correct: int
    hit_rate: Decimal
    mean_alpha: Decimal
    latest_graded_at: AwareDatetime


class StandingThesis(BaseModel):
    """The company's latest published thesis, shown to research, never linked."""

    model_config = ConfigDict(frozen=True)

    thesis_id: uuid.UUID
    published_at: AwareDatetime
    action: ThesisAction
    confidence: float
    horizon: TimeHorizon
    thesis: str = Field(max_length=600)
    invalidation_conditions: tuple[str, ...] = ()
    age_hours: float


class PositionMemory(BaseModel):
    model_config = ConfigDict(frozen=True)

    quantity: Decimal
    average_price: Decimal | None
    current_price: Decimal | None
    unrealised_pct: Decimal | None
    opened_at: AwareDatetime | None
    synced_at: AwareDatetime


class ResearchMemory(BaseModel):
    """This system's own prior state.  Context, never evidence (spec §7.1)."""

    model_config = ConfigDict(frozen=True)

    standing_thesis: StandingThesis | None = None
    position: PositionMemory | None = None
    company_record: CalibrationRecord | None = None
    event_type_record: tuple[CalibrationRecord, ...] = ()


class ResearchPacket(FrozenModel):
    event_id: uuid.UUID
    impact_id: uuid.UUID
    title: str
    summary: str
    event_time: AwareDatetime
    as_of: AwareDatetime
    company: ResolvedCompany
    evidence: tuple[EvidenceDocument, ...] = Field(min_length=1, max_length=50)
    impact_path: str
    relationship: str | None
    classifier_rationale: str | None
    market_context: tuple[ResearchDatum, ...] = ()
    degradation: tuple[ProviderDegradation, ...] = ()
    previous_thesis_id: uuid.UUID | None = None
    previous_thesis: ResearchDecision | None = None
    memory: ResearchMemory | None = None

    @model_validator(mode="after")
    def temporal_boundary(self) -> ResearchPacket:
        if self.event_time > self.as_of:
            raise ValueError("event is after analysis time")
        if len({item.source_id for item in self.evidence}) != len(self.evidence):
            raise ValueError("duplicate evidence ids")
        if any((item.published_at or item.received_at) > self.as_of for item in self.evidence):
            raise ValueError("evidence is after analysis time")
        if any(item.as_of > self.as_of for item in self.market_context):
            raise ValueError("market context is after analysis time")
        return self

    def fenced(self) -> str:
        return fence(self.model_dump_json())


def fence(value: str) -> str:
    # Escaping prevents a source from closing the fence or introducing prompt tags.
    #
    # ``quote=False`` on purpose. Only ``&``, ``<`` and ``>`` can form a tag or a
    # closing fence, and those stay escaped; ``"`` and ``'`` are dangerous only
    # inside an HTML *attribute*, which no fenced payload is ever interpolated
    # into. Escaping them cost 6 characters per quote in a JSON dump -- roughly a
    # 1.9x inflation of the whole packet -- which was spent on every role call.
    return "<untrusted_document>\n" + html.escape(value, quote=False) + "\n</untrusted_document>"


def public_text(value: object) -> str:
    """Accept prose only; reject separately encoded reasoning even inside content."""
    if not isinstance(value, str) or not value.strip():
        raise ResearchValidationError("research returned empty or non-text content")
    lowered = value.lower()
    if any(
        marker in lowered
        for marker in ("<think", "<reasoning", '"reasoning_content"', '"chain_of_thought"')
    ):
        raise ResearchValidationError("research returned a hidden-reasoning payload")
    return value[:20000]


def normalize_decision(content: str, packet: ResearchPacket) -> ResearchDecision:
    try:
        data = json.loads(public_text(content))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        # The upstream trader's quantity/order fields have no representation here.
        decision = ResearchDecision.model_validate(
            {key: value for key, value in data.items() if key in ResearchDecision.model_fields}
        )
        if not set(decision.evidence_ids) <= {item.source_id for item in packet.evidence}:
            raise ValueError("unknown evidence")
    except (ValueError, TypeError):
        raise ResearchValidationError("invalid research decision or evidence references") from None
    return decision


class ResearchResult(FrozenModel):
    decision: ResearchDecision
    reports: tuple[tuple[str, str], ...]
    degradation: tuple[ProviderDegradation, ...] = ()

    @model_validator(mode="after")
    def safe_reports(self) -> ResearchResult:
        for role, report in self.reports:
            if role not in ROLES:
                raise ValueError("unknown research report role")
            public_text(report)
        return self


def safe_research_error(error: Exception) -> StockBrainError:
    """Preserve actionable taxonomy without leaking provider bodies into job errors."""
    for kind in (
        ProviderAuthError,
        ProviderEntitlementError,
        ProviderRateLimited,
        ProviderUnavailable,
        ProviderResponseError,
        InstrumentResolutionError,
        ResearchValidationError,
        ResearchToolError,
        ResearchBudgetBlockedError,
    ):
        if isinstance(error, kind):
            return kind(f"Research failed: {type(error).__name__}")
    return ResearchError(f"Research failed: {type(error).__name__}")


RecordCall = Callable[[LlmCallRecord], Awaitable[None]]
CheckBudget = Callable[[], Awaitable[None]]


class ResearchEngine(Protocol):
    async def analyze(
        self, packet: ResearchPacket, *, record_call: RecordCall, check_budget: CheckBudget
    ) -> ResearchResult: ...


class MacroDataProvider(Protocol):
    async def context(self, as_of: dt.datetime) -> tuple[ResearchDatum, ...]: ...


class SupplementalResearchProvider(Protocol):
    async def context(self, packet: ResearchPacket) -> tuple[ResearchDatum, ...]: ...
