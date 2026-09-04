"""End-to-end classification flow against a real database.

Covers the properties a job queue with at-least-once delivery demands:
idempotency, visible failure states, and correct behaviour when the same job is
redelivered.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.models.companies import EventCompanyImpact
from stockbrain.db.models.research import LlmCall
from stockbrain.db.models.sources import Event, EventSourceLink
from stockbrain.db.models.system import AuditLog
from stockbrain.db.session import Database
from stockbrain.enums import EventStatus, ImpactDirection, SourceCategory, SourceProvider
from stockbrain.errors import ProviderAuthError, ProviderResponseError, ProviderUnavailable
from stockbrain.ingestion.base import RawSourceDocument
from stockbrain.ingestion.service import IngestionService
from stockbrain.intelligence.classifier import EventClassifier
from stockbrain.intelligence.service import ClassificationService
from stockbrain.llm.base import CompletionResult, TokenUsage
from stockbrain.llm.budget import BudgetGuard
from stockbrain.llm.telemetry import LlmTelemetry

pytestmark = pytest.mark.integration


CLASSIFICATION = {
    "relevant_to_public_equities": True,
    "event_type": "CONTRACT_AWARD",
    "canonical_title": "Vertiv wins hyperscaler cooling contract",
    "summary": "Vertiv named as supplier for a large data centre build-out.",
    "event_time": "2026-09-04T12:30:00Z",
    "novelty": 0.8,
    "importance": 0.75,
    "confidence": 0.85,
    "needs_corroboration": False,
    "topics": ["ai_infrastructure", "data_centres"],
    "rationale": "The article names Vertiv as the contract winner.",
    "companies": [
        {
            "company_name": "Vertiv Holdings Co.",
            "ticker_hint": "VRT",
            "exchange_hint": "NYSE",
            "relationship": "Named contract winner",
            "impact_path": "direct",
            "direction": "positive",
            "materiality": 0.7,
            "confidence": 0.8,
        },
        {
            "company_name": "NVIDIA Corporation",
            "ticker_hint": "NVDA",
            "exchange_hint": "NASDAQ",
            "relationship": "Accelerators drive the cooling demand",
            "impact_path": "indirect",
            "direction": "positive",
            "materiality": 0.3,
            "confidence": 0.6,
        },
    ],
}


class ScriptedProvider:
    """Returns queued responses or raises queued errors. No other capability."""

    name = "scripted"

    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.calls: list[Any] = []

    async def complete(self, request: Any) -> CompletionResult:
        self.calls.append(request)
        item = self._responses.pop(0) if self._responses else self._responses_default()
        if isinstance(item, BaseException):
            raise item
        return CompletionResult(
            content=item if isinstance(item, str) else json.dumps(item),
            model="deepseek-v4-flash",
            usage=TokenUsage(
                prompt_tokens=1500,
                completion_tokens=400,
                total_tokens=1900,
                cache_hit_tokens=1000,
                cache_miss_tokens=500,
            ),
            finish_reason="stop",
            provider_request_id=f"chatcmpl-{len(self.calls)}",
            latency_ms=42,
            started_at=dt.datetime.now(dt.UTC),
            completed_at=dt.datetime.now(dt.UTC),
        )

    @staticmethod
    def _responses_default() -> str:
        return json.dumps(CLASSIFICATION)

    async def aclose(self) -> None:
        return None


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "test",
        "log_level": "CRITICAL",
        "semantic_dedupe_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


def _service(
    database: Database,
    provider: ScriptedProvider,
    *,
    settings: Settings | None = None,
    deduplicator: Any = None,
    budget: BudgetGuard | None = None,
) -> ClassificationService:
    return ClassificationService(
        database,
        settings or _settings(),
        classifier=EventClassifier(provider, model="deepseek-v4-flash"),
        deduplicator=deduplicator,
        budget=budget,
        telemetry=LlmTelemetry(),
    )


async def _ingest(
    database: Database,
    *,
    item_id: str = "a-1",
    headline: str = "Vertiv wins big cooling contract",
    body: str = "<p>Vertiv has been selected as the supplier.</p>",
    url: str = "https://benzinga.com/vertiv",
    provider: SourceProvider = SourceProvider.ALPACA,
    is_distinct_event: bool = False,
) -> uuid.UUID:
    result = await IngestionService(database).ingest(
        RawSourceDocument(
            provider=provider,
            provider_item_id=item_id,
            url=url,
            source_name="Benzinga",
            source_category=SourceCategory.NEWSWIRE,
            headline=headline,
            published_at=dt.datetime(2026, 9, 4, 12, 0, tzinfo=dt.UTC),
            body=body,
            symbols=["VRT"],
            is_distinct_event=is_distinct_event,
        )
    )
    assert result.event_id is not None
    return result.event_id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_classification_writes_event_state_and_impacts(clean_tables: Database) -> None:
    event_id = await _ingest(clean_tables)
    service = _service(clean_tables, ScriptedProvider(CLASSIFICATION))

    result = await service.classify_event(event_id)

    assert result.status is EventStatus.CANDIDATE
    assert result.company_count == 2

    async with clean_tables.session() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        assert event.status is EventStatus.CANDIDATE
        assert event.event_type == "CONTRACT_AWARD"
        assert event.summary is not None and "Vertiv" in event.summary
        assert event.importance_score == 0.75
        assert event.novelty_score == 0.8
        assert event.confidence_score == 0.85
        assert event.relevant_to_public_equities is True
        assert event.needs_corroboration is False
        assert event.topics == ["ai_infrastructure", "data_centres"]
        assert event.classifier_model == "deepseek-v4-flash"
        assert event.classifier_prompt_version == "event_classifier/v1"
        assert event.classified_at is not None
        assert event.classifier_error is None
        assert event.candidate_score is not None and event.candidate_score > 0
        # The full validated result is retained for audit.
        assert event.classifier_output is not None
        assert event.classifier_output["rationale"].startswith("The article")

        impacts = list(
            (
                await session.execute(
                    sa.select(EventCompanyImpact)
                    .where(EventCompanyImpact.event_id == event_id)
                    .order_by(EventCompanyImpact.materiality_score.desc())
                )
            ).scalars()
        )
    assert [impact.company_key for impact in impacts] == ["vertiv", "nvidia"]
    assert impacts[0].ticker_hint == "VRT"
    assert impacts[0].direction is ImpactDirection.POSITIVE
    assert impacts[0].impact_path == "direct"
    assert impacts[1].impact_path == "indirect"
    assert impacts[0].company_id is None, "resolution to a real instrument is a later phase"


async def test_low_scores_classify_without_promoting_to_candidate(
    clean_tables: Database,
) -> None:
    """Thresholds gate where compute is spent; they are not a trading signal."""
    event_id = await _ingest(clean_tables)
    weak = {**CLASSIFICATION, "importance": 0.2, "confidence": 0.9}
    service = _service(clean_tables, ScriptedProvider(weak))

    assert (await service.classify_event(event_id)).status is EventStatus.CLASSIFIED


async def test_irrelevant_events_are_marked_irrelevant_with_no_impacts(
    clean_tables: Database,
) -> None:
    event_id = await _ingest(clean_tables)
    irrelevant = {**CLASSIFICATION, "relevant_to_public_equities": False}
    service = _service(clean_tables, ScriptedProvider(irrelevant))

    result = await service.classify_event(event_id)
    assert result.status is EventStatus.IRRELEVANT
    assert result.company_count == 0

    async with clean_tables.session() as session:
        count = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(EventCompanyImpact)
                .where(EventCompanyImpact.event_id == event_id)
            )
        ).scalar_one()
    assert count == 0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_running_the_same_job_twice_does_not_duplicate_impacts(
    clean_tables: Database,
) -> None:
    """At-least-once delivery means this will happen in production."""
    event_id = await _ingest(clean_tables)
    provider = ScriptedProvider(CLASSIFICATION, CLASSIFICATION)
    service = _service(clean_tables, provider)

    first = await service.classify_event(event_id)
    second = await service.classify_event(event_id)

    assert first.skipped is False
    assert second.skipped is True, "an already-classified event must short-circuit"
    assert second.reason is not None and "already in status" in second.reason
    # The second run never reached the model.
    assert len(provider.calls) == 1

    async with clean_tables.session() as session:
        count = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(EventCompanyImpact)
                .where(EventCompanyImpact.event_id == event_id)
            )
        ).scalar_one()
    assert count == 2


async def test_reclassification_upserts_rather_than_duplicating(
    clean_tables: Database,
) -> None:
    """Even bypassing the status guard, the unique constraint holds."""
    event_id = await _ingest(clean_tables)
    service = _service(clean_tables, ScriptedProvider(CLASSIFICATION))
    await service.classify_event(event_id)

    updated = {
        **CLASSIFICATION,
        "companies": [
            {**CLASSIFICATION["companies"][0], "materiality": 0.95, "direction": "negative"},  # type: ignore[index]
        ],
    }
    # Force a second application of a classification for the same event.
    await service._apply_classification(
        event_id, EventClassifier.parse(json.dumps(updated)), "event_classifier/v1"
    )

    async with clean_tables.session() as session:
        impacts = list(
            (
                await session.execute(
                    sa.select(EventCompanyImpact).where(EventCompanyImpact.event_id == event_id)
                )
            ).scalars()
        )
    vertiv = [impact for impact in impacts if impact.company_key == "vertiv"]
    assert len(vertiv) == 1, "the same company must not gain a second row"
    assert vertiv[0].materiality_score == 0.95
    assert vertiv[0].direction is ImpactDirection.NEGATIVE


async def test_duplicate_company_names_in_one_response_collapse(
    clean_tables: Database,
) -> None:
    """A model listing the same company twice must not violate the constraint."""
    event_id = await _ingest(clean_tables)
    duplicated = {
        **CLASSIFICATION,
        "companies": [
            CLASSIFICATION["companies"][0],  # type: ignore[index]
            {**CLASSIFICATION["companies"][0], "company_name": "VERTIV HOLDINGS"},  # type: ignore[index]
            {**CLASSIFICATION["companies"][0], "company_name": "Vertiv Holdings"},  # type: ignore[index]
        ],
    }
    result = await _service(clean_tables, ScriptedProvider(duplicated)).classify_event(event_id)
    assert result.company_count == 1


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


async def test_transient_failure_leaves_the_event_retryable(clean_tables: Database) -> None:
    event_id = await _ingest(clean_tables)
    service = _service(clean_tables, ScriptedProvider(ProviderUnavailable("deepseek: 503")))

    with pytest.raises(ProviderUnavailable):
        await service.classify_event(event_id, attempt=1, is_final_attempt=False)

    async with clean_tables.session() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        # Back to NEW so the job's own retry re-enters the normal path.
        assert event.status is EventStatus.NEW
        assert event.classifier_error is None


async def test_final_failure_becomes_a_visible_failed_state(clean_tables: Database) -> None:
    """A permanently unusable response must not vanish into a stalled row."""
    event_id = await _ingest(clean_tables)
    service = _service(
        clean_tables, ScriptedProvider(ProviderResponseError("classifier output failed schema"))
    )

    with pytest.raises(ProviderResponseError):
        await service.classify_event(event_id, attempt=3, is_final_attempt=True)

    async with clean_tables.session() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        assert event.status is EventStatus.CLASSIFICATION_FAILED
        assert event.classifier_error is not None
        assert "ProviderResponseError" in event.classifier_error

        audit = list(
            (
                await session.execute(
                    sa.select(AuditLog).where(AuditLog.action == "CLASSIFICATION_FAILED")
                )
            ).scalars()
        )
    assert len(audit) == 1
    assert audit[0].details["error_class"] == "ProviderResponseError"


async def test_auth_failure_is_recorded_with_its_error_class(clean_tables: Database) -> None:
    event_id = await _ingest(clean_tables)
    service = _service(clean_tables, ScriptedProvider(ProviderAuthError("deepseek: rejected")))

    with pytest.raises(ProviderAuthError):
        await service.classify_event(event_id, attempt=1, is_final_attempt=True)

    async with clean_tables.session() as session:
        call = (await session.execute(sa.select(LlmCall))).scalar_one()
    assert call.succeeded is False
    assert call.error_class == "ProviderAuthError"
    assert call.used is False


async def test_a_missing_event_is_skipped_not_fatal(clean_tables: Database) -> None:
    service = _service(clean_tables, ScriptedProvider(CLASSIFICATION))
    result = await service.classify_event(uuid.uuid4())
    assert result.skipped is True


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


async def test_telemetry_records_the_full_call(clean_tables: Database) -> None:
    event_id = await _ingest(clean_tables)
    job_id = uuid.uuid4()
    await _service(clean_tables, ScriptedProvider(CLASSIFICATION)).classify_event(
        event_id, job_id=None
    )

    async with clean_tables.session() as session:
        call = (await session.execute(sa.select(LlmCall))).scalar_one()

    assert call.provider == "deepseek"
    assert call.model == "deepseek-v4-flash"
    assert call.purpose == "CLASSIFY_EVENT"
    assert call.prompt_version == "event_classifier/v1"
    assert call.thinking_enabled is False
    assert call.event_id == event_id
    assert call.succeeded is True
    assert call.used is True
    assert call.input_tokens == 1500
    assert call.output_tokens == 400
    assert call.cached_input_tokens == 1000
    assert call.cache_miss_input_tokens == 500
    assert call.provider_request_id == "chatcmpl-1"
    assert call.finish_reason == "stop"
    assert call.latency_ms == 42
    assert call.started_at is not None and call.completed_at is not None
    assert call.estimated_cost_usd is not None and call.estimated_cost_usd > 0
    assert call.retry_count == 0
    assert job_id is not None  # sanity for the local name


async def test_telemetry_never_stores_a_prompt_or_credential(
    clean_tables: Database,
) -> None:
    event_id = await _ingest(clean_tables)
    await _service(clean_tables, ScriptedProvider(CLASSIFICATION)).classify_event(event_id)

    async with clean_tables.session() as session:
        call = (await session.execute(sa.select(LlmCall))).scalar_one()

    stored = json.dumps(
        {
            "excerpt": call.response_excerpt,
            "error": call.error,
            "request_id": call.provider_request_id,
        }
    )
    for forbidden in ("Authorization", "Bearer ", "sk-", "api_key", "untrusted_document"):
        assert forbidden not in stored


async def test_spend_aggregation_matches_recorded_calls(clean_tables: Database) -> None:
    telemetry = LlmTelemetry()
    for index in range(3):
        event_id = await _ingest(
            clean_tables,
            item_id=f"a-{index}",
            headline=f"Story {index}",
            url=f"https://benzinga.com/{index}",
            body=f"<p>Body {index}</p>",
        )
        await _service(clean_tables, ScriptedProvider(CLASSIFICATION)).classify_event(event_id)

    async with clean_tables.session() as session:
        calls = list((await session.execute(sa.select(LlmCall))).scalars())
        total = await telemetry.spend_since(
            session, dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
        )

    assert len(calls) == 3
    assert total == sum(call.estimated_cost_usd for call in calls if call.estimated_cost_usd)


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


async def test_hard_budget_stops_classification_without_losing_the_event(
    clean_tables: Database,
) -> None:
    """Ingested data must survive a budget stop; only the analysis pauses."""
    from decimal import Decimal

    event_id = await _ingest(clean_tables)
    provider = ScriptedProvider(CLASSIFICATION)
    guard = BudgetGuard(
        clean_tables,
        daily_soft_usd=Decimal("0.00"),
        daily_hard_usd=Decimal("0.00"),
        monthly_soft_usd=Decimal("1000"),
        monthly_hard_usd=Decimal("1000"),
        cache_seconds=0.0,
    )
    service = _service(clean_tables, provider, budget=guard)

    result = await service.classify_event(event_id)

    assert result.skipped is True
    assert result.status is EventStatus.NEW, "the event waits rather than failing"
    assert provider.calls == [], "no model call may be made past the hard limit"

    async with clean_tables.session() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        # Source and event data are untouched: nothing is lost.
        links = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(EventSourceLink)
                .where(EventSourceLink.event_id == event_id)
            )
        ).scalar_one()
    assert links == 1


# ---------------------------------------------------------------------------
# Job-level behaviour
# ---------------------------------------------------------------------------


async def test_a_redelivered_classify_job_runs_safely_through_the_runner(
    clean_tables: Database,
) -> None:
    """At-least-once delivery: the same job body may arrive twice."""
    import asyncio

    from stockbrain.db.models.system import Job
    from stockbrain.enums import JobStatus, JobType
    from stockbrain.jobs.handlers import handle_classify_event
    from stockbrain.jobs.queue import JobQueue
    from stockbrain.jobs.registry import JobRegistry
    from stockbrain.jobs.runner import JobRunner
    from stockbrain.observability.health import ProviderHealthRegistry

    event_id = await _ingest(clean_tables)
    provider = ScriptedProvider(CLASSIFICATION, CLASSIFICATION)

    class _Services:
        classification = _service(clean_tables, provider)
        health = ProviderHealthRegistry()

    registry = JobRegistry()
    registry.register(JobType.CLASSIFY_EVENT.value, handle_classify_event)
    runner = JobRunner(
        clean_tables, registry, services=_Services(), concurrency=2, poll_interval_seconds=0.05
    )

    queue = JobQueue()
    async with clean_tables.transaction() as session:
        await queue.enqueue(session, JobType.CLASSIFY_EVENT, payload={"event_id": str(event_id)})
    # A second job for the same event, as a redelivery would produce. No dedupe
    # key, so the queue itself offers no protection -- the service must.
    async with clean_tables.transaction() as session:
        await queue.enqueue(session, JobType.CLASSIFY_EVENT, payload={"event_id": str(event_id)})

    await runner.start()
    try:
        for _ in range(200):
            async with clean_tables.session() as session:
                statuses = [job.status for job in (await session.execute(sa.select(Job))).scalars()]
            if statuses and all(status is JobStatus.SUCCEEDED for status in statuses):
                break
            await asyncio.sleep(0.05)
    finally:
        await runner.stop()

    async with clean_tables.session() as session:
        jobs = list((await session.execute(sa.select(Job))).scalars())
        impacts = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(EventCompanyImpact)
                .where(EventCompanyImpact.event_id == event_id)
            )
        ).scalar_one()
        event = await session.get(Event, event_id)

    assert len(jobs) == 2
    assert all(job.status is JobStatus.SUCCEEDED for job in jobs), "both jobs complete cleanly"
    assert impacts == 2, "the second delivery must not duplicate impact rows"
    assert event is not None and event.status is EventStatus.CANDIDATE
    assert len(provider.calls) == 1, "the model is consulted once, not once per delivery"


async def test_the_queue_dedupe_key_also_suppresses_a_second_pending_job(
    clean_tables: Database,
) -> None:
    """Defence in depth: the queue prevents the duplicate before the service sees it."""
    from stockbrain.db.models.system import Job
    from stockbrain.enums import JobType
    from stockbrain.jobs.queue import JobQueue

    event_id = await _ingest(clean_tables)
    queue = JobQueue()
    async with clean_tables.transaction() as session:
        first = await queue.enqueue(
            session,
            JobType.CLASSIFY_EVENT,
            payload={"event_id": str(event_id)},
            dedupe_key=f"classify:{event_id}",
        )
        second = await queue.enqueue(
            session,
            JobType.CLASSIFY_EVENT,
            payload={"event_id": str(event_id)},
            dedupe_key=f"classify:{event_id}",
        )
    assert first is not None and second is None

    async with clean_tables.session() as session:
        count = (await session.execute(sa.select(sa.func.count()).select_from(Job))).scalar_one()
    assert count == 1


async def test_an_event_left_classifying_by_a_crash_is_released_then_reclassified(
    clean_tables: Database,
) -> None:
    """A worker dying mid-classification must not strand the event forever.

    While it is CLASSIFYING it is deliberately *not* re-claimable -- that is what
    stops two workers spending two model calls on one event. A stall reaper
    returns it to NEW after a timeout, mirroring the job queue's own reaper.
    """
    event_id = await _ingest(clean_tables)
    service = _service(clean_tables, ScriptedProvider(CLASSIFICATION))

    # Simulate the crash: status was advanced, then the process died.
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(Event)
            .where(Event.id == event_id)
            .values(
                status=EventStatus.CLASSIFYING,
                updated_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
            )
        )

    # A fresh attempt is refused while the claim still looks live.
    assert (await service.classify_event(event_id)).skipped is True

    assert await service.release_stalled(timeout_seconds=60) == 1

    result = await service.classify_event(event_id)
    assert result.skipped is False
    assert result.status is EventStatus.CANDIDATE


async def test_a_live_classification_claim_is_not_stolen(clean_tables: Database) -> None:
    """The reaper must only release genuinely abandoned claims."""
    event_id = await _ingest(clean_tables)
    service = _service(clean_tables, ScriptedProvider(CLASSIFICATION))
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(Event).where(Event.id == event_id).values(status=EventStatus.CLASSIFYING)
        )
    assert await service.release_stalled(timeout_seconds=900) == 0


async def test_concurrent_workers_classify_an_event_once(clean_tables: Database) -> None:
    """Regression: both workers read NEW, and each spent a model call."""
    import asyncio

    event_id = await _ingest(clean_tables)
    provider = ScriptedProvider(CLASSIFICATION, CLASSIFICATION, CLASSIFICATION, CLASSIFICATION)
    service = _service(clean_tables, provider)

    results = await asyncio.gather(*(service.classify_event(event_id) for _ in range(4)))

    performed = [result for result in results if not result.skipped]
    assert len(performed) == 1, "exactly one worker may classify"
    assert len(provider.calls) == 1, "the model must be consulted once"
