"""Database-backed research spend ownership, recovery, provenance and read-only API."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument, Company, EventCompanyImpact
from stockbrain.db.models.research import LlmCall, ResearchRun, Thesis
from stockbrain.db.models.sources import Event, EventSourceLink, Source
from stockbrain.db.models.system import Job
from stockbrain.db.session import Database
from stockbrain.enums import (
    Broker,
    EventSourceRelationship,
    EventStatus,
    ImpactDirection,
    ResearchStatus,
    ResolutionStatus,
    SourceProvider,
)
from stockbrain.errors import (
    InstrumentResolutionError,
    ProviderAuthError,
    ProviderRateLimited,
    ProviderUnavailable,
)
from stockbrain.intelligence.research import (
    PROMPT_VERSION,
    ROLES,
    UPSTREAM_COMMIT,
    CheckBudget,
    RecordCall,
    ResearchPacket,
    ResearchResult,
    ResearchValidationError,
)
from stockbrain.intelligence.research_service import ResearchService
from stockbrain.jobs.handlers import register_ingestion_handlers
from stockbrain.jobs.registry import JobRegistry
from stockbrain.jobs.runner import JobRunner
from stockbrain.llm.base import TokenUsage
from stockbrain.llm.budget import BudgetGuard
from stockbrain.llm.telemetry import LlmCallRecord
from stockbrain.main import create_app
from stockbrain.observability.health import ProviderHealthRegistry
from tests.research_helpers import decision, packet

pytestmark = pytest.mark.integration


class Engine:
    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.error: Exception | None = None
        self.packet: ResearchPacket | None = None

    async def analyze(
        self, value: ResearchPacket, *, record_call: RecordCall, check_budget: CheckBudget
    ) -> ResearchResult:
        await check_budget()
        self.calls += 1
        self.packet = value
        self.entered.set()
        await record_call(
            LlmCallRecord(
                purpose="RESEARCH:market",
                provider="deepseek",
                model="deepseek-v4-flash",
                prompt_version=PROMPT_VERSION,
                succeeded=True,
                used=True,
                estimated_cost_usd=Decimal("0.001"),
                usage=TokenUsage(prompt_tokens=100, completion_tokens=10),
                provider_request_id="request-id",
                had_reasoning_content=True,
                response_excerpt="PRIVATE_REASONING_MUST_BE_REMOVED",
            )
        )
        if self.release:
            await self.release.wait()
        if self.error:
            raise self.error
        return ResearchResult(
            decision=decision(value),
            reports=(("market", "Public report"),),
            degradation=value.degradation,
        )


async def seed(db: Database) -> ResearchPacket:
    value = packet()
    async with db.transaction() as session:
        session.add(
            Company(id=value.company.company_id, name=value.company.name, isin=value.company.isin)
        )
        session.add(
            Event(
                id=value.event_id,
                title=value.title,
                title_hash="a" * 64,
                summary=value.summary,
                first_seen_at=value.event_time,
                event_time=value.event_time,
                status=EventStatus.CANDIDATE,
            )
        )
        await session.flush()
        session.add(
            BrokerInstrument(
                id=value.company.broker_instrument_id,
                company_id=value.company.company_id,
                broker=Broker.TRADING212,
                broker_ticker=value.company.broker_ticker,
                market_symbol="AAPL",
                name=value.company.name,
                exchange="NASDAQ",
                currency="USD",
                isin=value.company.isin,
                instrument_type="STOCK",
            )
        )
        for source in value.evidence:
            session.add(
                Source(
                    id=source.source_id,
                    provider=SourceProvider.MANUAL,
                    content_hash="b" * 64,
                    normalized_text=source.text,
                    source_name=source.publisher,
                    canonical_url=source.url,
                    received_at=source.received_at,
                    published_at=source.published_at,
                )
            )
        await session.flush()
        session.add(
            EventCompanyImpact(
                id=value.impact_id,
                event_id=value.event_id,
                company_id=value.company.company_id,
                company_name_hint="Apple",
                company_key="apple",
                ticker_hint="FAKE_LLM_HINT",
                direction=ImpactDirection.POSITIVE,
                materiality_score=0.8,
                confidence=0.8,
                impact_path=value.impact_path,
                relationship_type=value.relationship,
                explanation=value.classifier_rationale,
                broker_instrument_id=value.company.broker_instrument_id,
                resolution_status=ResolutionStatus.RESOLVED,
            )
        )
        session.add(
            EventSourceLink(
                event_id=value.event_id,
                source_id=value.evidence[0].source_id,
                relationship_type=EventSourceRelationship.PRIMARY,
            )
        )
    return value


def service(
    db: Database, engine: Engine, *, hard: Decimal = Decimal(10), soft: Decimal = Decimal(5)
) -> ResearchService:
    budget = BudgetGuard(
        db, daily_soft_usd=soft, daily_hard_usd=hard, monthly_soft_usd=soft, monthly_hard_usd=hard
    )
    return ResearchService(
        db,
        engine,
        budget=budget,
        health=ProviderHealthRegistry(),
        models=dict.fromkeys(ROLES, "deepseek-v4-flash"),
    )


async def request(
    db: Database,
    research: ResearchService,
    value: ResearchPacket,
    rerun_id: uuid.UUID | None = None,
) -> uuid.UUID:
    async with db.transaction() as session:
        return await research.request(
            session, value.impact_id, as_of=value.as_of, rerun_id=rerun_id
        )


@pytest.mark.parametrize(
    "status",
    [
        ResolutionStatus.AMBIGUOUS,
        ResolutionStatus.NOT_FOUND,
        ResolutionStatus.UNSUPPORTED,
        ResolutionStatus.PENDING,
    ],
)
async def test_unresolved_never_starts(clean_tables: Database, status: ResolutionStatus) -> None:
    value = await seed(clean_tables)
    engine = Engine()
    research = service(clean_tables, engine)
    async with clean_tables.transaction() as session:
        await session.execute(sa.update(EventCompanyImpact).values(resolution_status=status))
    with pytest.raises(InstrumentResolutionError):
        await request(clean_tables, research, value)
    assert engine.calls == 0


@pytest.mark.parametrize("status", [EventStatus.IRRELEVANT, EventStatus.CLASSIFICATION_FAILED])
async def test_unclassified_event_cannot_request_research(
    clean_tables: Database, status: EventStatus
) -> None:
    value = await seed(clean_tables)
    engine = Engine()
    research = service(clean_tables, engine)
    async with clean_tables.transaction() as session:
        await session.execute(sa.update(Event).values(status=status))
    with pytest.raises(ResearchValidationError):
        await request(clean_tables, research, value)
    assert engine.calls == 0


async def test_hint_cannot_bypass_missing_identity(clean_tables: Database) -> None:
    value = await seed(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(EventCompanyImpact).values(broker_instrument_id=None, ticker_hint="AAPL")
        )
    with pytest.raises(InstrumentResolutionError):
        await request(clean_tables, service(clean_tables, Engine()), value)


async def test_concurrent_enqueue_and_execution_spend_once(clean_tables: Database) -> None:
    value = await seed(clean_tables)
    engine = Engine()
    engine.release = asyncio.Event()
    research = service(clean_tables, engine)
    ids = await asyncio.gather(*(request(clean_tables, research, value) for _ in range(4)))
    assert len(set(ids)) == 1
    first = asyncio.create_task(research.run(ids[0]))
    await asyncio.wait_for(engine.entered.wait(), 5)
    await research.run(ids[0])
    assert engine.calls == 1
    engine.release.set()
    await first
    await research.run(ids[0])
    assert engine.calls == 1
    async with clean_tables.session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(ResearchRun)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(Job)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(Thesis)) == 1
        row = await session.get(ResearchRun, ids[0])
        assert row and row.status == ResearchStatus.SUCCEEDED
        assert row.tradingagents_version == UPSTREAM_COMMIT
        assert row.estimated_cost_usd == Decimal(".001")
        assert row.broker_instrument_id == value.company.broker_instrument_id
        assert row.token_usage and row.token_usage["input_tokens"] == 100
        thesis = await session.scalar(sa.select(Thesis))
        assert thesis and thesis.evidence_source_ids["items"] == [str(value.evidence[0].source_id)]
        call = await session.scalar(sa.select(LlmCall))
        assert call and call.research_run_id == ids[0] and call.response_excerpt is None
        assert "PRIVATE_REASONING" not in str(row.raw_reports) + str(row.research_packet)
    assert engine.packet and engine.packet.company.symbol == "AAPL"
    assert engine.packet.impact_path == "indirect"


async def test_explicit_rerun_is_a_new_idempotent_version(clean_tables: Database) -> None:
    value = await seed(clean_tables)
    research = service(clean_tables, Engine())
    first = await request(clean_tables, research, value)
    key = uuid.uuid4()
    second = await request(clean_tables, research, value, key)
    assert first != second
    assert second == await request(clean_tables, research, value, key)


@pytest.mark.parametrize("hard,soft", [(Decimal(0), Decimal(0)), (Decimal(10), Decimal(0))])
async def test_budget_defers_without_spend_and_resumes(
    clean_tables: Database, hard: Decimal, soft: Decimal
) -> None:
    value = await seed(clean_tables)
    engine = Engine()
    research = service(clean_tables, engine, hard=hard, soft=soft)
    run_id = await request(clean_tables, research, value)
    await research.run(run_id)
    assert engine.calls == 0
    async with clean_tables.session() as session:
        row = await session.get(ResearchRun, run_id)
        assert (
            row
            and row.status == ResearchStatus.PENDING
            and row.error_class == "ResearchBudgetBlockedError"
        )
    await service(clean_tables, engine).run(run_id)
    assert engine.calls == 1


async def test_resolution_rechecked_after_enqueue(clean_tables: Database) -> None:
    value = await seed(clean_tables)
    engine = Engine()
    research = service(clean_tables, engine)
    run_id = await request(clean_tables, research, value)
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(EventCompanyImpact).values(resolution_status=ResolutionStatus.AMBIGUOUS)
        )
    with pytest.raises(InstrumentResolutionError):
        await research.run(run_id)
    assert engine.calls == 0
    async with clean_tables.session() as session:
        row = await session.get(ResearchRun, run_id)
        assert (
            row
            and row.status == ResearchStatus.FAILED
            and row.error_class == "InstrumentResolutionError"
        )


async def test_failed_paid_run_is_not_automatically_replayed(clean_tables: Database) -> None:
    value = await seed(clean_tables)
    engine = Engine()
    engine.error = ProviderAuthError("unsafe provider body SECRET")
    research = service(clean_tables, engine)
    run_id = await request(clean_tables, research, value)
    with pytest.raises(ProviderAuthError):
        await research.run(run_id)
    await research.run(run_id)
    assert engine.calls == 1
    async with clean_tables.session() as session:
        row = await session.get(ResearchRun, run_id)
        assert row and row.error_class == "ProviderAuthError" and "SECRET" not in str(row.error)
        assert row.estimated_cost_usd == Decimal(".001")


async def test_rate_limited_run_returns_to_pending_until_the_final_attempt(
    clean_tables: Database,
) -> None:
    value = await seed(clean_tables)
    engine = Engine()
    engine.error = ProviderRateLimited("429")
    research = service(clean_tables, engine)
    run_id = await request(clean_tables, research, value)
    with pytest.raises(ProviderRateLimited):
        await research.run(run_id, final_attempt=False)
    async with clean_tables.session() as session:
        row = await session.get(ResearchRun, run_id)
        assert row and row.status == ResearchStatus.PENDING
    engine.error = None
    await research.run(run_id)
    assert engine.calls == 2
    async with clean_tables.session() as session:
        row = await session.get(ResearchRun, run_id)
        assert row and row.status == ResearchStatus.SUCCEEDED


async def test_rate_limited_final_attempt_fails_the_run(clean_tables: Database) -> None:
    value = await seed(clean_tables)
    engine = Engine()
    engine.error = ProviderRateLimited("429")
    research = service(clean_tables, engine)
    run_id = await request(clean_tables, research, value)
    with pytest.raises(ProviderRateLimited):
        await research.run(run_id, final_attempt=True)
    async with clean_tables.session() as session:
        row = await session.get(ResearchRun, run_id)
        assert row and row.status == ResearchStatus.FAILED


async def test_stalled_run_terminal_recovery_does_not_repeat_spend(clean_tables: Database) -> None:
    value = await seed(clean_tables)
    engine = Engine()
    research = service(clean_tables, engine)
    run_id = await request(clean_tables, research, value)
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(ResearchRun).values(
                status=ResearchStatus.RUNNING,
                lease_token=uuid.uuid4(),
                lease_expires_at=utcnow() - dt.timedelta(minutes=1),
            )
        )
    await research.sweep()
    await research.run(run_id)
    assert engine.calls == 0
    async with clean_tables.session() as session:
        row = await session.get(ResearchRun, run_id)
        assert row and row.status == ResearchStatus.TIMED_OUT and row.error_class == "StalledRun"


async def test_cancellation_persists_and_invalidates_worker(clean_tables: Database) -> None:
    value = await seed(clean_tables)
    engine = Engine()
    engine.release = asyncio.Event()
    research = service(clean_tables, engine)
    run_id = await request(clean_tables, research, value)
    task = asyncio.create_task(research.run(run_id))
    await asyncio.wait_for(engine.entered.wait(), 5)
    # Wait until telemetry's short transaction is done, so the cancellation
    # exercises the in-flight research path instead of a driver cancellation.
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with clean_tables.session() as session:
        row = await session.get(ResearchRun, run_id)
        assert row and row.status == ResearchStatus.CANCELLED and row.lease_token is None
    await research.run(run_id)
    assert engine.calls == 1


@pytest.mark.parametrize("error", [ProviderUnavailable("FRED transient"), ValueError("bad data")])
async def test_supplemental_failure_degrades_successful_research(
    clean_tables: Database, error: Exception
) -> None:
    value = await seed(clean_tables)
    engine = Engine()
    research = service(clean_tables, engine)

    class Macro:
        async def context(self, as_of: dt.datetime) -> tuple[Any, ...]:
            raise error

    research.macro = Macro()
    run_id = await request(clean_tables, research, value)
    await research.run(run_id)
    assert engine.packet and engine.packet.degradation[0].error_class == type(error).__name__
    async with clean_tables.session() as session:
        row = await session.get(ResearchRun, run_id)
        assert row and row.status == ResearchStatus.SUCCEEDED and row.provider_degradation


async def test_reassessment_retains_previous_thesis(clean_tables: Database) -> None:
    value = await seed(clean_tables)
    engine = Engine()
    research = service(clean_tables, engine)
    first = await request(clean_tables, research, value)
    await research.run(first)
    async with clean_tables.transaction() as session:
        thesis = await session.scalar(sa.select(Thesis))
        assert thesis
        second = await research.request(session, value.impact_id, previous_thesis_id=thesis.id)
    await research.run(second)
    assert (
        engine.packet
        and engine.packet.previous_thesis
        and engine.packet.previous_thesis_id == thesis.id
    )
    async with clean_tables.session() as session:
        new_thesis = await session.scalar(sa.select(Thesis).where(Thesis.research_run_id == second))
        assert new_thesis and new_thesis.supersedes_thesis_id == thesis.id
        assert new_thesis.original_thesis_id == thesis.id


async def test_job_pipeline_and_research_inspection(clean_tables: Database) -> None:
    value = await seed(clean_tables)
    engine = Engine()
    research = service(clean_tables, engine)
    await research.enqueue_event(value.event_id)
    from types import SimpleNamespace

    registry = JobRegistry()
    register_ingestion_handlers(registry, classifier_available=False, research_available=True)
    runner = JobRunner(clean_tables, registry, services=SimpleNamespace(research=research))
    assert await runner._run_one("research-test-worker")
    assert engine.calls == 1
    app = create_app(
        Settings(
            app_env="test",
            stockbrain_secret_key="test",
            discovery_enabled=False,
            web_auth_enabled=False,
        )
    )
    app.state.database = clean_tables
    app.state.services = None
    app.state.health_registry = ProviderHealthRegistry()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        response = await http.get("/api/v1/research", params={"event_id": str(value.event_id)})
        assert response.status_code == 200
        item = response.json()[0]
        detail = await http.get("/api/v1/research/" + item["id"])
        assert detail.status_code == 200
        data = detail.json()
        assert data["decision"]["action"] == "HOLD"
        assert data["calls"][0]["purpose"] == "RESEARCH:market"
        assert data["thesis_id"]
        assert "PRIVATE_REASONING" not in detail.text and "reasoning_content" not in detail.text
        health = await http.get("/api/v1/research/health")
        assert health.status_code == 200
        assert set(health.json()["provider_health"]) == {
            "tradingagents",
            "fred",
            "alpaca_market_data",
        }
        assert (await http.get("/api/v1/research/" + str(uuid.uuid4()))).status_code == 404
