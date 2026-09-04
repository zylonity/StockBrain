"""Layer 4 deduplication: semantic event matching.

This is the only deduplication layer that costs money, so it runs last and only
against candidates that a deterministic pre-filter already considers plausible.
Asking a model to compare a new article against every recent event would be both
expensive and worse: most pairs are obviously unrelated and a cheap filter says
so for free.

The bias is towards **keeping events separate**. A duplicate event is visible and
correctable; a wrong merge silently destroys the distinction between two real
occurrences and nobody notices. So a merge requires a confidence threshold, and
anything the model is unsure about stays separate.

Documents flagged ``is_distinct_event`` -- regulatory filings -- never reach this
layer at all. Two filings of the same form by the same company read almost
identically and would be merged by any similarity measure, but they are always
different filings.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.base import utcnow
from stockbrain.db.models.sources import Event, EventSourceLink, Source
from stockbrain.enums import EventStatus
from stockbrain.errors import ProviderResponseError
from stockbrain.intelligence.prompts import PromptTemplate, load_prompt, sanitize_untrusted
from stockbrain.intelligence.schemas import (
    DedupeVerdict,
    SemanticDedupeResult,
    example_dedupe_payload,
)
from stockbrain.llm.base import ChatMessage, CompletionRequest, CompletionResult, LlmProvider
from stockbrain.llm.deepseek import extract_json
from stockbrain.logging import get_logger

__all__ = [
    "DEFAULT_CANDIDATE_WINDOW",
    "DEFAULT_MERGE_CONFIDENCE",
    "CandidateEvent",
    "SemanticDedupeOutcome",
    "SemanticDeduplicator",
    "select_candidates",
]

log = get_logger(__name__)

DEDUPE_PURPOSE = "DEDUPE_EVENT"

#: How far back to look for candidate events. Wide enough for syndication lag and
#: developing stories, narrow enough that a recurring headline months later is a
#: new event.
DEFAULT_CANDIDATE_WINDOW = dt.timedelta(hours=48)

#: Below this confidence, a proposed merge is ignored and the events stay apart.
DEFAULT_MERGE_CONFIDENCE = 0.7

#: Never ask the model about more than this many candidates in one call.
MAX_CANDIDATES = 6

#: Body excerpt per candidate. Enough to judge sameness, small enough that six
#: candidates plus the new document stay comfortably in context.
CANDIDATE_EXCERPT_CHARS = 700
NEW_DOCUMENT_EXCERPT_CHARS = 4000

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "and",
        "or",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "after",
        "before",
        "over",
        "under",
        "new",
        "said",
        "says",
        "will",
        "would",
        "could",
        "may",
        "might",
        "up",
        "down",
        "more",
        "most",
        "less",
        "than",
        "into",
        "out",
        "about",
    ]
)


@dataclass(slots=True)
class CandidateEvent:
    """A tracked event offered to the model for comparison."""

    event_id: uuid.UUID
    title: str
    summary: str | None
    first_seen_at: dt.datetime
    excerpt: str | None = None
    overlap: float = 0.0


@dataclass(slots=True)
class SemanticDedupeOutcome:
    verdicts: list[DedupeVerdict]
    merge_target: uuid.UUID | None
    merge_relation: str | None
    result: CompletionResult | None
    prompt_version: str | None
    candidates_considered: int = 0
    skipped_reason: str | None = None
    candidate_ids: dict[str, uuid.UUID] = field(default_factory=dict)


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    words = "".join(char if char.isalnum() else " " for char in text.lower()).split()
    return {word for word in words if len(word) > 2 and word not in _STOPWORDS}


def _overlap(left: set[str], right: set[str]) -> float:
    """Jaccard overlap. Cheap, deterministic, and good enough to pre-filter."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


async def select_candidates(
    session: AsyncSession,
    *,
    title: str | None,
    body: str | None,
    window: dt.timedelta = DEFAULT_CANDIDATE_WINDOW,
    min_overlap: float = 0.08,
    limit: int = MAX_CANDIDATES,
    now: dt.datetime | None = None,
    exclude_event_id: uuid.UUID | None = None,
) -> list[CandidateEvent]:
    """Deterministically shortlist events worth asking a model about.

    Recent, non-archived events are scored by simple token overlap against the
    new document. Only those above a floor are returned, so a model call happens
    only when there is something plausible to compare -- and does not happen at
    all when there is not.
    """
    cutoff = (now or utcnow()) - window
    stmt = (
        sa.select(Event)
        .where(
            Event.first_seen_at >= cutoff,
            Event.status.notin_([EventStatus.ARCHIVED, EventStatus.IRRELEVANT]),
        )
        .order_by(Event.first_seen_at.desc())
        # Bounded scan: even a busy window cannot turn this into a table sweep.
        .limit(200)
    )
    if exclude_event_id is not None:
        stmt = stmt.where(Event.id != exclude_event_id)

    events = list((await session.execute(stmt)).scalars())
    if not events:
        return []

    needle = _tokens(title) | _tokens((body or "")[:2000])
    scored: list[CandidateEvent] = []
    for event in events:
        overlap = _overlap(needle, _tokens(event.title) | _tokens(event.summary))
        if overlap < min_overlap:
            continue
        scored.append(
            CandidateEvent(
                event_id=event.id,
                title=event.title,
                summary=event.summary,
                first_seen_at=event.first_seen_at,
                overlap=overlap,
            )
        )

    scored.sort(key=lambda candidate: candidate.overlap, reverse=True)
    return scored[:limit]


async def attach_candidate_excerpts(
    session: AsyncSession, candidates: list[CandidateEvent]
) -> None:
    """Load one representative source excerpt per candidate, in a single query."""
    if not candidates:
        return
    ids = [candidate.event_id for candidate in candidates]
    stmt = (
        sa.select(EventSourceLink.event_id, Source.normalized_text)
        .join(Source, Source.id == EventSourceLink.source_id)
        .where(EventSourceLink.event_id.in_(ids))
        .order_by(EventSourceLink.event_id, Source.received_at.asc())
    )
    seen: dict[uuid.UUID, str] = {}
    for event_id, text in (await session.execute(stmt)).all():
        if event_id not in seen and text:
            seen[event_id] = text[:CANDIDATE_EXCERPT_CHARS]
    for candidate in candidates:
        candidate.excerpt = seen.get(candidate.event_id)


class SemanticDeduplicator:
    """Asks the model how a new document relates to shortlisted events."""

    def __init__(
        self,
        provider: LlmProvider,
        *,
        model: str,
        prompt_name: str = "event_dedupe",
        prompt_version: str = "v1",
        merge_confidence: float = DEFAULT_MERGE_CONFIDENCE,
        max_output_tokens: int = 1200,
        timeout_seconds: float | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._prompt_name = prompt_name
        self._prompt_version = prompt_version
        self._merge_confidence = merge_confidence
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds

    @property
    def prompt(self) -> PromptTemplate:
        return load_prompt(self._prompt_name, self._prompt_version)

    @property
    def prompt_version(self) -> str:
        return self.prompt.identifier

    def build_messages(
        self,
        *,
        title: str | None,
        body: str | None,
        published_at: dt.datetime | None,
        source_name: str | None,
        candidates: list[CandidateEvent],
        as_of: dt.datetime,
    ) -> tuple[list[ChatMessage], dict[str, uuid.UUID]]:
        """Render the comparison prompt.

        Candidates are addressed by a short opaque index rather than by UUID:
        the model never needs a database identifier, and a short label leaves
        less room for it to invent one that happens to look real.
        """
        index_map: dict[str, uuid.UUID] = {}
        blocks: list[str] = []
        for position, candidate in enumerate(candidates, start=1):
            key = str(position)
            index_map[key] = candidate.event_id
            blocks.append(
                "<candidate_event>\n"
                f"id: {key}\n"
                f"first seen: {candidate.first_seen_at.astimezone(dt.UTC).isoformat()}\n"
                f"title: {sanitize_untrusted(candidate.title, limit=400)}\n"
                f"summary: {sanitize_untrusted(candidate.summary or '(none)', limit=600)}\n"
                "excerpt: "
                + sanitize_untrusted(candidate.excerpt or "(none)", limit=CANDIDATE_EXCERPT_CHARS)
                + "\n"
                "</candidate_event>"
            )

        system, user = self.prompt.render(
            {
                "SCHEMA_EXAMPLE": json.dumps(example_dedupe_payload(), indent=2),
                "AS_OF": as_of.astimezone(dt.UTC).isoformat(),
                "NEW_TITLE": sanitize_untrusted(title or "(no title)", limit=400),
                "NEW_PUBLISHED_AT": (
                    published_at.astimezone(dt.UTC).isoformat() if published_at else "(unknown)"
                ),
                "NEW_SOURCE_NAME": sanitize_untrusted(source_name or "(unknown)", limit=120),
                "NEW_BODY": sanitize_untrusted(body, limit=NEW_DOCUMENT_EXCERPT_CHARS),
                "CANDIDATES": "\n\n".join(blocks),
            }
        )
        return [ChatMessage("system", system), ChatMessage("user", user)], index_map

    async def compare(
        self,
        *,
        title: str | None,
        body: str | None,
        published_at: dt.datetime | None,
        source_name: str | None,
        candidates: list[CandidateEvent],
        as_of: dt.datetime | None = None,
    ) -> SemanticDedupeOutcome:
        """Compare a document against shortlisted candidates."""
        if not candidates:
            return SemanticDedupeOutcome(
                verdicts=[],
                merge_target=None,
                merge_relation=None,
                result=None,
                prompt_version=None,
                candidates_considered=0,
                skipped_reason="no plausible candidates after deterministic filtering",
            )

        moment = as_of or utcnow()
        messages, index_map = self.build_messages(
            title=title,
            body=body,
            published_at=published_at,
            source_name=source_name,
            candidates=candidates,
            as_of=moment,
        )
        result = await self._provider.complete(
            CompletionRequest(
                messages=messages,
                model=self._model,
                max_output_tokens=self._max_output_tokens,
                temperature=0.0,
                json_object=True,
                thinking=False,
                timeout_seconds=self._timeout_seconds,
                purpose=DEDUPE_PURPOSE,
            )
        )

        parsed = self.parse(result.content)
        # Drop verdicts referring to ids we did not offer: a hallucinated id must
        # never be able to attach a source to an arbitrary event.
        verdicts = [verdict for verdict in parsed.verdicts if verdict.candidate_id in index_map]
        best = SemanticDedupeResult(verdicts=verdicts).best_merge_target(self._merge_confidence)

        return SemanticDedupeOutcome(
            verdicts=verdicts,
            merge_target=index_map[best.candidate_id] if best else None,
            merge_relation=best.relation if best else None,
            result=result,
            prompt_version=self.prompt_version,
            candidates_considered=len(candidates),
            candidate_ids=index_map,
        )

    @staticmethod
    def parse(content: str) -> SemanticDedupeResult:
        raw = extract_json(content)
        if not isinstance(raw, dict):
            raise ProviderResponseError(
                f"dedupe returned {type(raw).__name__}, expected a JSON object"
            )
        try:
            return SemanticDedupeResult.model_validate(raw)
        except ValidationError as exc:
            fields = sorted({".".join(str(part) for part in err["loc"]) for err in exc.errors()})
            raise ProviderResponseError(
                f"dedupe output failed schema validation on: {', '.join(fields[:10])}"
            ) from exc
