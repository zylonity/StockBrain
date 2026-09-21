"""Classification orchestration.

Turns one ``CLASSIFY_EVENT`` job into a classified event, affected-company
impact rows, and a full telemetry record -- or into a visible failed state.

Idempotency is layered, because a job queue with at-least-once delivery will
eventually run the same job twice:

* the event's own status short-circuits a re-run of an already-classified event;
* company impacts are upserted on ``(event_id, company_key)``, which
  ``uq_event_company_impacts_event_id_company_key`` enforces, so a duplicate run
  updates rows rather than adding them;
* every LLM attempt is recorded separately, with exactly one marked ``used``.

Failure is layered too. A transient provider failure leaves the event in
``CLASSIFYING`` and lets the job's own retry policy handle it. Only on the final
attempt does the event become ``CLASSIFICATION_FAILED`` with the reason stored,
so a permanently unparseable response becomes something an operator can see
rather than a row that quietly stops moving.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.config import Settings
from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import EventCompanyImpact
from stockbrain.db.models.sources import Event, EventSourceLink, Source
from stockbrain.db.models.system import AuditLog
from stockbrain.db.session import Database
from stockbrain.enums import (
    ActorType,
    EventSourceRelationship,
    EventStatus,
    ImpactDirection,
)
from stockbrain.errors import ProviderError, ProviderResponseError
from stockbrain.instruments.normalize import normalize_ticker
from stockbrain.intelligence.classifier import ClassificationInput, EventClassifier
from stockbrain.intelligence.normalize import company_key
from stockbrain.intelligence.schemas import ClassifiedEvent
from stockbrain.intelligence.semantic_dedupe import (
    SemanticDeduplicator,
    attach_candidate_excerpts,
    select_candidates,
)
from stockbrain.llm.budget import BudgetGuard, WorkPriority
from stockbrain.llm.telemetry import LlmTelemetry
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = ["ClassificationResult", "ClassificationService"]

log = get_logger(__name__)

#: How long an event may sit in CLASSIFYING before it is assumed abandoned.
#: Mirrors the job queue's own reclaim timeout: a worker that dies mid-call must
#: not strand its event forever.
STALE_CLASSIFYING_SECONDS = 900

_DIRECTIONS: dict[str, ImpactDirection] = {
    "positive": ImpactDirection.POSITIVE,
    "negative": ImpactDirection.NEGATIVE,
    "mixed": ImpactDirection.MIXED,
    "unknown": ImpactDirection.UNKNOWN,
}


@dataclass(slots=True)
class ClassificationResult:
    event_id: uuid.UUID
    status: EventStatus
    skipped: bool = False
    reason: str | None = None
    merged_into: uuid.UUID | None = None
    company_count: int = 0


class ClassificationService:
    """Runs the classify-and-deduplicate flow for one event."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        classifier: EventClassifier,
        deduplicator: SemanticDeduplicator | None = None,
        budget: BudgetGuard | None = None,
        telemetry: LlmTelemetry | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._classifier = classifier
        self._deduplicator = deduplicator
        self._budget = budget
        self._telemetry = telemetry or LlmTelemetry()

    # ------------------------------------------------------------------
    async def classify_event(
        self,
        event_id: uuid.UUID,
        *,
        job_id: uuid.UUID | None = None,
        attempt: int = 1,
        is_final_attempt: bool = False,
    ) -> ClassificationResult:
        """Classify one event. Safe to call repeatedly for the same event."""
        async with self._database.session() as session:
            event = await session.get(Event, event_id)
            if event is None:
                return ClassificationResult(
                    event_id=event_id,
                    status=EventStatus.ARCHIVED,
                    skipped=True,
                    reason="event no longer exists",
                )
            if event.status is not EventStatus.NEW:
                # Already classified, already being classified by another worker,
                # or archived by a merge. Nothing to do, nothing to duplicate.
                return ClassificationResult(
                    event_id=event_id,
                    status=event.status,
                    skipped=True,
                    reason=f"event already in status {event.status.value}",
                )

        if self._budget is not None:
            decision = await self._budget.check(WorkPriority.ESSENTIAL)
            if not decision.allowed:
                # Deliberately leaves the event in NEW: no data is lost, and it
                # will be picked up once budget is available again.
                log.warning("classification_skipped_budget", event_id=str(event_id))
                return ClassificationResult(
                    event_id=event_id,
                    status=EventStatus.NEW,
                    skipped=True,
                    reason=decision.reason or "LLM budget exhausted",
                )

        # Compare-and-swap, not check-then-act. Reading the status and then
        # updating it lets two workers that both saw NEW each spend a model call
        # on the same event; a conditional UPDATE lets exactly one win.
        async with self._database.session() as session:
            if not await self._claim(session, event_id):
                return ClassificationResult(
                    event_id=event_id,
                    status=EventStatus.CLASSIFYING,
                    skipped=True,
                    reason="another worker is already classifying this event",
                )
            claimed_event = await session.get(Event, event_id)
            assert claimed_event is not None
            source = await self._primary_source(session, event_id)
            payload = self._build_input(claimed_event, source)

        started = utcnow()
        try:
            outcome = await self._classifier.classify(payload, as_of=started)
        except (ProviderError, ProviderResponseError) as exc:
            await self._record_failure(
                exc,
                event_id=event_id,
                job_id=job_id,
                attempt=attempt,
                started_at=started,
                is_final_attempt=is_final_attempt,
            )
            raise

        async with self._database.transaction() as session:
            record = self._telemetry.from_result(
                outcome.result,
                purpose="CLASSIFY_EVENT",
                provider="deepseek",
                model=self._classifier.model,
                prompt_version=outcome.prompt_version,
                thinking_enabled=False,
                event_id=event_id,
                job_id=job_id,
            )
            await self._telemetry.persist(session, record)
        if self._budget is not None:
            self._budget.invalidate()

        merged_into = await self._maybe_merge(event_id, payload, outcome.classification)
        if merged_into is not None:
            return ClassificationResult(
                event_id=event_id,
                status=EventStatus.ARCHIVED,
                merged_into=merged_into,
                reason="semantically merged into an existing event",
            )

        return await self._apply_classification(
            event_id, outcome.classification, outcome.prompt_version
        )

    # ------------------------------------------------------------------
    @staticmethod
    async def _primary_source(session: AsyncSession, event_id: uuid.UUID) -> Source | None:
        stmt = (
            sa.select(Source)
            .join(EventSourceLink, EventSourceLink.source_id == Source.id)
            .where(EventSourceLink.event_id == event_id)
            .order_by(
                # The PRIMARY link is the document the event was created from.
                sa.case((EventSourceLink.relationship_type == "PRIMARY", 0), else_=1),
                Source.received_at.asc(),
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _build_input(event: Event, source: Source | None) -> ClassificationInput:
        metadata = (source.provider_metadata if source else {}) or {}
        raw_symbols = metadata.get("symbols")
        symbols = [str(item) for item in raw_symbols] if isinstance(raw_symbols, list) else []
        return ClassificationInput(
            headline=(source.headline if source else None) or event.title,
            body=(source.normalized_text if source else None),
            provider=source.provider.value if source else "MANUAL",
            source_name=source.source_name if source else None,
            source_category=source.source_category if source else None,
            url=source.canonical_url if source else None,
            published_at=(source.published_at if source else None) or event.event_time,
            symbol_hints=symbols[:20],
            exchange_hint=_optional_str(metadata.get("exchange_hint")),
            isin=_optional_str(metadata.get("isin")),
            event_id=event.id,
        )

    @staticmethod
    async def _claim(session: AsyncSession, event_id: uuid.UUID) -> bool:
        """Atomically move NEW -> CLASSIFYING. True when this caller won."""
        result = await session.execute(
            sa.update(Event)
            .where(Event.id == event_id, Event.status == EventStatus.NEW)
            .values(status=EventStatus.CLASSIFYING, updated_at=utcnow())
            .returning(Event.id)
        )
        won = result.scalar_one_or_none() is not None
        await session.commit()
        return won

    async def _set_status(self, event_id: uuid.UUID, status: EventStatus) -> None:
        async with self._database.transaction() as session:
            await session.execute(
                sa.update(Event)
                .where(Event.id == event_id)
                .values(status=status, updated_at=utcnow())
            )

    async def release_stalled(self, *, timeout_seconds: int = STALE_CLASSIFYING_SECONDS) -> int:
        """Return events abandoned mid-classification to NEW.

        A worker that dies after claiming an event leaves it in CLASSIFYING
        forever. This is the event-level equivalent of the job queue's reaper.
        """
        cutoff = utcnow() - dt.timedelta(seconds=timeout_seconds)
        async with self._database.transaction() as session:
            result = await session.execute(
                sa.update(Event)
                .where(Event.status == EventStatus.CLASSIFYING, Event.updated_at < cutoff)
                .values(status=EventStatus.NEW, updated_at=utcnow())
                .returning(Event.id)
            )
            released = list(result.scalars())
        if released:
            log.warning("classification_stalled_released", count=len(released))
        return len(released)

    async def _record_failure(
        self,
        error: BaseException,
        *,
        event_id: uuid.UUID,
        job_id: uuid.UUID | None,
        attempt: int,
        started_at: dt.datetime,
        is_final_attempt: bool,
    ) -> None:
        """Persist the failed attempt, and fail the event only when out of retries."""
        async with self._database.transaction() as session:
            record = self._telemetry.from_failure(
                error,
                purpose="CLASSIFY_EVENT",
                provider="deepseek",
                model=self._classifier.model,
                prompt_version=self._classifier.prompt_version,
                thinking_enabled=False,
                event_id=event_id,
                job_id=job_id,
                started_at=started_at,
                attempt=attempt,
            )
            await self._telemetry.persist(session, record)

            if is_final_attempt:
                await session.execute(
                    sa.update(Event)
                    .where(Event.id == event_id)
                    .values(
                        status=EventStatus.CLASSIFICATION_FAILED,
                        classifier_error=f"{type(error).__name__}: {error}"[:2000],
                        updated_at=utcnow(),
                    )
                )
                session.add(
                    AuditLog(
                        actor_type=ActorType.SYSTEM,
                        actor_id="classifier",
                        action="CLASSIFICATION_FAILED",
                        entity_type="event",
                        entity_id=event_id,
                        details={"error_class": type(error).__name__, "attempt": attempt},
                    )
                )
            else:
                # Back to NEW so the retry re-enters the normal path rather than
                # finding the event stuck in CLASSIFYING.
                await session.execute(
                    sa.update(Event)
                    .where(Event.id == event_id)
                    .values(status=EventStatus.NEW, updated_at=utcnow())
                )
        METRICS.inc("stockbrain_classifier_failures_total", labels={"kind": "attempt"})

    # ------------------------------------------------------------------
    async def _maybe_merge(
        self,
        event_id: uuid.UUID,
        payload: ClassificationInput,
        classification: ClassifiedEvent,
    ) -> uuid.UUID | None:
        """Run layer-4 deduplication, and merge if the model is confident enough."""
        if self._deduplicator is None or not self._settings.semantic_dedupe_enabled:
            return None

        async with self._database.session() as session:
            source = await self._primary_source(session, event_id)
            if source is not None and self._is_distinct_artifact(source):
                # A regulatory filing is its own event. Two filings of the same
                # form read almost identically, and merging them would destroy a
                # real distinction.
                return None
            candidates = await select_candidates(
                session,
                title=classification.canonical_title or payload.headline,
                body=payload.body,
                exclude_event_id=event_id,
            )
            await attach_candidate_excerpts(session, candidates)

        if not candidates:
            return None

        if self._budget is not None:
            decision = await self._budget.check(WorkPriority.OPTIONAL)
            if not decision.allowed:
                # Deduplication is quality, not correctness: skipping it costs a
                # possible duplicate event, which is visible and fixable.
                log.info("semantic_dedupe_skipped_budget", event_id=str(event_id))
                return None

        started = utcnow()
        try:
            outcome = await self._deduplicator.compare(
                title=classification.canonical_title or payload.headline,
                body=payload.body,
                published_at=payload.published_at,
                source_name=payload.source_name,
                candidates=candidates,
                as_of=started,
            )
        except (ProviderError, ProviderResponseError) as exc:
            # Never fail a classification because deduplication failed. The
            # deterministic layers already ran; the cost is a possible duplicate.
            async with self._database.transaction() as session:
                await self._telemetry.persist(
                    session,
                    self._telemetry.from_failure(
                        exc,
                        purpose="DEDUPE_EVENT",
                        provider="deepseek",
                        model=self._classifier.model,
                        prompt_version=self._deduplicator.prompt_version,
                        thinking_enabled=False,
                        event_id=event_id,
                        started_at=started,
                    ),
                )
            log.warning(
                "semantic_dedupe_failed", event_id=str(event_id), error_type=type(exc).__name__
            )
            return None

        if outcome.result is not None:
            async with self._database.transaction() as session:
                await self._telemetry.persist(
                    session,
                    self._telemetry.from_result(
                        outcome.result,
                        purpose="DEDUPE_EVENT",
                        provider="deepseek",
                        model=self._classifier.model,
                        prompt_version=outcome.prompt_version,
                        thinking_enabled=False,
                        event_id=event_id,
                    ),
                )
            if self._budget is not None:
                self._budget.invalidate()

        if outcome.merge_target is None:
            return None
        return await self._merge_into(
            event_id, outcome.merge_target, outcome.merge_relation or "SAME_EVENT", classification
        )

    @staticmethod
    def _is_distinct_artifact(source: Source) -> bool:
        metadata = source.provider_metadata or {}
        payload = metadata.get("raw_payload")
        # SEC filings carry an accession number; the ingestion layer also flags
        # them, and either signal is enough to keep them un-mergeable.
        return bool(
            metadata.get("is_distinct_event")
            or (isinstance(payload, dict) and payload.get("accessionNumber"))
            or source.provider.value == "SEC"
        )

    async def _merge_into(
        self,
        event_id: uuid.UUID,
        target_id: uuid.UUID,
        relation: str,
        classification: ClassifiedEvent,
    ) -> uuid.UUID | None:
        """Fold one event into another, preserving all provenance.

        The source links move to the target; the merged event is archived rather
        than deleted, with a pointer to where it went. Nothing is erased.
        """
        link_relationship = (
            EventSourceRelationship.UPDATE
            if relation == "UPDATE_TO_EVENT"
            else EventSourceRelationship.CORROBORATING
        )

        async with self._database.transaction() as session:
            target = await session.get(Event, target_id)
            event = await session.get(Event, event_id)
            if target is None or event is None:
                return None
            if target.status is EventStatus.ARCHIVED or target.merged_into_event_id is not None:
                # Do not chain merges into an already-merged event.
                return None

            moved = (
                await session.execute(
                    sa.select(EventSourceLink).where(EventSourceLink.event_id == event_id)
                )
            ).scalars()
            moved_ids: list[str] = []
            for link in moved:
                existing = await session.get(EventSourceLink, (target_id, link.source_id))
                if existing is None:
                    session.add(
                        EventSourceLink(
                            event_id=target_id,
                            source_id=link.source_id,
                            relationship_type=link_relationship,
                        )
                    )
                    moved_ids.append(str(link.source_id))
                await session.delete(link)

            previous = {
                "summary": target.summary,
                "event_time": target.event_time.isoformat() if target.event_time else None,
            }
            if relation == "UPDATE_TO_EVENT":
                # An update carries new information; the prior values are kept in
                # the audit record, and every source of the original remains.
                if classification.summary:
                    target.summary = classification.summary
                if classification.event_time and (
                    target.event_time is None or classification.event_time > target.event_time
                ):
                    target.event_time = classification.event_time
            target.updated_at = utcnow()

            event.status = EventStatus.ARCHIVED
            event.merged_into_event_id = target_id
            event.updated_at = utcnow()

            session.add(
                AuditLog(
                    actor_type=ActorType.LLM,
                    actor_id=self._classifier.model,
                    action="EVENT_MERGED",
                    entity_type="event",
                    entity_id=target_id,
                    details={
                        "merged_event_id": str(event_id),
                        "relation": relation,
                        "moved_source_ids": moved_ids,
                        "previous_target_values": previous,
                    },
                )
            )

        METRICS.inc("stockbrain_events_deduped_total", labels={"layer": relation})
        log.info(
            "event_merged",
            event_id=str(event_id),
            target_event_id=str(target_id),
            relation=relation,
        )
        return target_id

    # ------------------------------------------------------------------
    async def _apply_classification(
        self,
        event_id: uuid.UUID,
        classification: ClassifiedEvent,
        prompt_version: str,
    ) -> ClassificationResult:
        """Write the classification and its company impacts, idempotently."""
        status = self._status_for(classification)

        async with self._database.transaction() as session:
            event = await session.get(Event, event_id)
            if event is None:  # pragma: no cover - deleted mid-flight
                return ClassificationResult(
                    event_id=event_id, status=EventStatus.ARCHIVED, skipped=True
                )

            event.status = status
            event.event_type = classification.event_type
            event.summary = classification.summary or event.summary
            event.importance_score = classification.importance
            event.novelty_score = classification.novelty
            event.confidence_score = classification.confidence
            event.relevant_to_public_equities = classification.relevant_to_public_equities
            event.needs_corroboration = classification.needs_corroboration
            event.topics = list(classification.topics)
            event.classifier_model = self._classifier.model
            event.classifier_prompt_version = prompt_version
            event.classifier_output = classification.model_dump(mode="json")
            event.classified_at = utcnow()
            event.classifier_error = None
            if classification.event_time and event.event_time is None:
                event.event_time = classification.event_time
            event.candidate_score = self._candidate_score(classification)
            event.updated_at = utcnow()

            source = await self._primary_source(session, event_id)
            source_metadata = (source.provider_metadata if source else {}) or {}
            written = await self._upsert_impacts(
                session, event_id, classification, source_metadata=source_metadata
            )

            session.add(
                AuditLog(
                    actor_type=ActorType.LLM,
                    actor_id=self._classifier.model,
                    action="EVENT_CLASSIFIED",
                    entity_type="event",
                    entity_id=event_id,
                    details={
                        "status": status.value,
                        "prompt_version": prompt_version,
                        "importance": classification.importance,
                        "confidence": classification.confidence,
                        "relevant": classification.relevant_to_public_equities,
                        "company_count": written,
                    },
                )
            )

        METRICS.inc("stockbrain_events_classified_total", labels={"status": status.value})
        log.info(
            "event_classified",
            event_id=str(event_id),
            status=status.value,
            importance=classification.importance,
            companies=written,
        )
        return ClassificationResult(event_id=event_id, status=status, company_count=written)

    async def _upsert_impacts(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
        classification: ClassifiedEvent,
        *,
        source_metadata: dict[str, Any] | None = None,
    ) -> int:
        """Insert or update one impact row per company.

        Upsert on ``(event_id, company_key)`` rather than delete-then-insert, so
        a re-run neither duplicates rows nor briefly empties them, and so any
        resolution already attached to a row is preserved.
        """
        if not classification.companies:
            return 0

        metadata = source_metadata or {}
        raw_symbols = metadata.get("symbols")
        document_symbol = (
            normalize_ticker(str(raw_symbols[0]))
            if isinstance(raw_symbols, list) and len(raw_symbols) == 1
            else ""
        )
        document_exchange = str(metadata.get("exchange_hint") or "")

        rows: list[dict[str, object]] = []
        seen: set[str] = set()
        for company in classification.companies:
            key = company_key(company.company_name)
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "event_id": event_id,
                    "company_key": key,
                    "company_name_hint": company.company_name,
                    "ticker_hint": company.ticker_hint,
                    "exchange_hint": _impact_exchange_hint(
                        company.ticker_hint,
                        company.exchange_hint,
                        document_symbol=document_symbol,
                        document_exchange=document_exchange,
                    ),
                    "direction": _DIRECTIONS.get(company.direction, ImpactDirection.UNKNOWN),
                    "relationship_type": company.relationship,
                    "impact_path": company.impact_path,
                    "materiality_score": company.materiality,
                    "confidence": company.confidence,
                    "explanation": company.relationship,
                }
            )

        if not rows:
            return 0

        statement = pg_insert(EventCompanyImpact).values(rows)
        await session.execute(
            statement.on_conflict_do_update(
                constraint="uq_event_company_impacts_event_id_company_key",
                set_={
                    "company_name_hint": statement.excluded.company_name_hint,
                    "ticker_hint": statement.excluded.ticker_hint,
                    "exchange_hint": statement.excluded.exchange_hint,
                    "direction": statement.excluded.direction,
                    "relationship_type": statement.excluded.relationship_type,
                    "impact_path": statement.excluded.impact_path,
                    "materiality_score": statement.excluded.materiality_score,
                    "confidence": statement.excluded.confidence,
                    "explanation": statement.excluded.explanation,
                    "updated_at": utcnow(),
                },
            )
        )
        return len(rows)

    def _status_for(self, classification: ClassifiedEvent) -> EventStatus:
        """Decide whether the event is worth deeper analysis.

        This only gates where compute is spent. It is not a trading signal, and
        the thresholds are configuration.
        """
        if not classification.relevant_to_public_equities:
            return EventStatus.IRRELEVANT
        promotes = (
            classification.importance >= self._settings.classifier_min_importance
            and classification.confidence >= self._settings.classifier_min_confidence
            and classification.max_materiality >= self._settings.classifier_min_materiality
        )
        return EventStatus.CANDIDATE if promotes else EventStatus.CLASSIFIED

    @staticmethod
    def _candidate_score(classification: ClassifiedEvent) -> float:
        """Deterministic pre-research ranking (spec section 36).

        Weighted heuristic over the classifier's own features. Decides only
        whether to spend deep-analysis compute; it is not a trading signal.
        """
        return round(
            0.30 * classification.importance
            + 0.20 * classification.max_materiality
            + 0.15 * classification.confidence
            + 0.10 * classification.novelty,
            4,
        )


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _impact_exchange_hint(
    ticker_hint: str | None,
    exchange_hint: str | None,
    *,
    document_symbol: str,
    document_exchange: str,
) -> str | None:
    """Use the model's exchange when it gave one, else the feed's.

    The feed knows the venue; the model may not repeat it.  The backfill happens
    only when the document carried exactly one symbol and the impact's
    normalised ticker is that symbol.
    """
    if exchange_hint is not None:
        return exchange_hint
    if not document_symbol or not document_exchange:
        return None
    if normalize_ticker(ticker_hint) != document_symbol:
        return None
    return document_exchange
