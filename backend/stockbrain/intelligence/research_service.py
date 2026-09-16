"""Durable research orchestration using the existing PostgreSQL queue and budget.

A unique request identity and a conditional PENDING -> RUNNING transition protect
spend. A crashed paid run becomes TIMED_OUT, never automatically starts again:
the provider cannot tell us whether an interrupted call was billed. Intentional
reruns carry an operator-supplied UUID, so their redelivery is idempotent too.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument, Company, EventCompanyImpact
from stockbrain.db.models.research import LlmCall, ResearchRun, Thesis
from stockbrain.db.models.sources import Event, EventSourceLink, Source
from stockbrain.db.session import Database
from stockbrain.enums import EventStatus, JobType, ProviderStatus, ResearchStatus, ResolutionStatus
from stockbrain.errors import InstrumentResolutionError, ProviderError
from stockbrain.intelligence.memory import MemoryService
from stockbrain.intelligence.research import (
    PROMPT_VERSION,
    UPSTREAM_COMMIT,
    EvidenceDocument,
    MacroDataProvider,
    ProviderDegradation,
    ResearchBudgetBlockedError,
    ResearchDecision,
    ResearchEngine,
    ResearchMemory,
    ResearchPacket,
    ResearchValidationError,
    ResolvedCompany,
    SupplementalResearchProvider,
    safe_research_error,
)
from stockbrain.jobs.queue import JobQueue
from stockbrain.llm.budget import BudgetGuard, WorkPriority
from stockbrain.llm.telemetry import LlmCallRecord, LlmTelemetry
from stockbrain.observability.health import ProviderHealthRegistry, ProviderName


class ResearchService:
    def __init__(
        self,
        database: Database,
        engine: ResearchEngine,
        *,
        budget: BudgetGuard,
        health: ProviderHealthRegistry,
        models: dict[str, str],
        timeout_seconds: float = 600,
        max_tokens: int = 3000,
        evidence_chars: int = 12000,
        min_impact_materiality: float = 0.0,
        max_impacts_per_event: int = 0,
        macro: MacroDataProvider | None = None,
        supplemental: SupplementalResearchProvider | None = None,
        fundamentals: SupplementalResearchProvider | None = None,
        expectations: SupplementalResearchProvider | None = None,
        targets: SupplementalResearchProvider | None = None,
        macro_markets: MacroDataProvider | None = None,
        memory: MemoryService | None = None,
        memory_packet_enabled: bool = False,
    ) -> None:
        self.database = database
        self.engine = engine
        self.budget = budget
        self.health = health
        self.models = models
        self.timeout = timeout_seconds
        self.evidence_chars = evidence_chars
        # Selection policy, deliberately *not* part of ``config_version``: which
        # impacts are worth researching does not change what a research run is,
        # so tightening these must not invalidate and re-run existing work.
        self.min_impact_materiality = min_impact_materiality
        self.max_impacts_per_event = max_impacts_per_event
        self.macro = macro
        self.supplemental = supplemental
        self.fundamentals = fundamentals
        self.expectations = expectations
        self.targets = targets
        self.macro_markets = macro_markets
        self.memory = memory
        self.memory_packet_enabled = memory_packet_enabled
        self.queue = JobQueue()
        self.telemetry = LlmTelemetry()
        self.config = {
            "roles": models,
            "prompt_version": PROMPT_VERSION,
            "upstream": UPSTREAM_COMMIT,
            "max_tokens": max_tokens,
            # Part of the config version on purpose: a run that saw 1600
            # characters of an article and one that saw 12000 are not the same
            # analysis, and the second must not dedupe against the first.
            "evidence_chars": evidence_chars,
            "providers": [
                "alpaca" if supplemental else "no_market_provider",
                "fred" if macro else "no_macro_provider",
                "sec" if fundamentals else "no_fundamentals_provider",
                "finnhub" if expectations else "no_expectations_provider",
                "yfinance" if targets else "no_targets_provider",
                "polymarket" if macro_markets else "no_macro_markets_provider",
            ],
            "tools": ["read_research_context"],
            "debate_rounds": getattr(engine, "debate_rounds", 1),
            # A packet with memory is a different analysis from one without, so
            # the flag forks the config version.
            "memory": memory_packet_enabled,
        }
        self.config_version = hashlib.sha256(
            json.dumps(self.config, sort_keys=True).encode()
        ).hexdigest()

    async def packet(
        self,
        session: AsyncSession,
        impact_id: uuid.UUID,
        as_of: dt.datetime,
        previous_thesis_id: uuid.UUID | None = None,
    ) -> ResearchPacket:
        impact = await session.get(EventCompanyImpact, impact_id)
        if (
            impact is None
            or impact.resolution_status != ResolutionStatus.RESOLVED
            or impact.broker_instrument_id is None
            or impact.company_id is None
        ):
            raise InstrumentResolutionError("research requires a RESOLVED company and instrument")
        instrument = await session.get(BrokerInstrument, impact.broker_instrument_id)
        company = await session.get(Company, impact.company_id)
        event = await session.get(Event, impact.event_id)
        if (
            instrument is None
            or company is None
            or event is None
            or not instrument.is_active
            or not instrument.market_symbol
            or instrument.company_id != company.id
        ):
            raise InstrumentResolutionError("resolved research identity is no longer valid")
        if event.status not in {
            EventStatus.CLASSIFIED,
            EventStatus.CANDIDATE,
            EventStatus.RESEARCHING,
            EventStatus.RESEARCHED,
        }:
            raise ResearchValidationError("research requires a classified canonical event")
        sources = (
            await session.execute(
                sa.select(Source, EventSourceLink.relationship_type)
                .join(EventSourceLink, EventSourceLink.source_id == Source.id)
                .where(
                    EventSourceLink.event_id == event.id,
                    sa.func.coalesce(Source.published_at, Source.received_at) <= as_of,
                )
                .order_by(Source.received_at, Source.id)
                .limit(50)
            )
        ).all()
        previous: ResearchDecision | None = None
        if previous_thesis_id is not None:
            thesis = await session.get(Thesis, previous_thesis_id)
            old = await session.get(ResearchRun, thesis.research_run_id) if thesis else None
            if (
                old is None
                or old.company_id != company.id
                or old.broker_instrument_id != instrument.id
                or old.completed_at is None
                or old.completed_at > as_of
            ):
                raise ResearchValidationError("previous thesis is not valid for this listing/time")
            previous = ResearchDecision.model_validate(old.structured_decision)
        memory: ResearchMemory | None = None
        if self.memory_packet_enabled and self.memory is not None:
            memory = await self.memory.research_memory(
                session,
                company_id=company.id,
                broker_instrument_id=instrument.id,
                broker_ticker=instrument.broker_ticker,
                event_type=event.event_type,
                as_of=as_of,
            )
        try:
            return ResearchPacket(
                event_id=event.id,
                impact_id=impact.id,
                title=event.title,
                summary=event.summary or "",
                event_time=event.event_time or event.first_seen_at,
                as_of=as_of,
                company=ResolvedCompany(
                    company_id=company.id,
                    name=company.name,
                    broker_instrument_id=instrument.id,
                    broker_ticker=instrument.broker_ticker,
                    symbol=instrument.market_symbol,
                    exchange=instrument.exchange,
                    currency=instrument.currency,
                    isin=instrument.isin,
                ),
                evidence=tuple(
                    EvidenceDocument(
                        source_id=source.id,
                        publisher=source.source_name,
                        url=source.canonical_url,
                        published_at=source.published_at,
                        received_at=source.received_at,
                        text=(source.normalized_text or source.headline or "")[
                            : self.evidence_chars
                        ],
                        text_truncated=len(source.normalized_text or source.headline or "")
                        > self.evidence_chars,
                        relationship=relationship.value,
                    )
                    for source, relationship in sources
                ),
                impact_path=impact.impact_path,
                relationship=impact.relationship_type,
                classifier_rationale=impact.explanation,
                previous_thesis_id=previous_thesis_id,
                previous_thesis=previous,
                memory=memory,
            )
        except ValueError:
            raise ResearchValidationError(
                "invalid research packet or no evidence available as of analysis"
            ) from None

    async def request(
        self,
        session: AsyncSession,
        impact_id: uuid.UUID,
        *,
        rerun_id: uuid.UUID | None = None,
        as_of: dt.datetime | None = None,
        previous_thesis_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        packet = await self.packet(session, impact_id, as_of or utcnow(), previous_thesis_id)
        key = hashlib.sha256(
            f"{packet.event_id}:{packet.company.company_id}:"
            f"{self.config_version}:{rerun_id}:{previous_thesis_id}".encode()
        ).hexdigest()
        run_id = (
            await session.execute(
                insert(ResearchRun)
                .values(
                    id=uuid.uuid4(),
                    event_id=packet.event_id,
                    company_id=packet.company.company_id,
                    impact_id=impact_id,
                    broker_instrument_id=packet.company.broker_instrument_id,
                    dedupe_key=key,
                    config_version=self.config_version,
                    rerun_id=rerun_id,
                    as_of=packet.as_of,
                    status=ResearchStatus.PENDING,
                    trigger="POSITION_REASSESSMENT"
                    if previous_thesis_id
                    else "MANUAL"
                    if rerun_id
                    else "EVENT",
                    tradingagents_version=UPSTREAM_COMMIT,
                    quick_model=self.models["market"],
                    deep_model=self.models["manager"],
                    prompt_version=PROMPT_VERSION,
                    analyst_config=self.config,
                    research_packet=packet.model_dump(mode="json"),
                )
                .on_conflict_do_nothing(index_elements=[ResearchRun.dedupe_key])
                .returning(ResearchRun.id)
            )
        ).scalar_one_or_none()
        if run_id is None:
            existing = (
                await session.execute(sa.select(ResearchRun).where(ResearchRun.dedupe_key == key))
            ).scalar_one()
            run_id = existing.id
            if existing.status != ResearchStatus.PENDING:
                return run_id
        await self.queue.enqueue(
            session,
            JobType.RUN_RESEARCH,
            payload={"run_id": str(run_id)},
            dedupe_key=f"research:{run_id}",
            priority=40,
            max_attempts=1,
        )
        return run_id

    async def check_budget(self) -> None:
        self.budget.invalidate()
        decision = await self.budget.check(WorkPriority.OPTIONAL)
        if not decision.allowed:
            raise ResearchBudgetBlockedError(decision.reason or "LLM budget blocked")

    async def run(self, run_id: uuid.UUID, *, job_id: uuid.UUID | None = None) -> None:
        # Budget-blocked pending work is swept later; nothing was spent or claimed.
        try:
            await self.check_budget()
        except ResearchBudgetBlockedError:
            async with self.database.transaction() as session:
                await session.execute(
                    sa.update(ResearchRun)
                    .where(ResearchRun.id == run_id, ResearchRun.status == ResearchStatus.PENDING)
                    .values(
                        error_class="ResearchBudgetBlockedError",
                        error="Research deferred by LLM budget",
                    )
                )
            self.health.record(
                ProviderName.TRADINGAGENTS,
                ProviderStatus.DEGRADED,
                detail="Research deferred by LLM budget",
            )
            return
        lease = uuid.uuid4()
        async with self.database.transaction() as session:
            claimed = (
                await session.execute(
                    sa.update(ResearchRun)
                    .where(
                        ResearchRun.id == run_id,
                        ResearchRun.status == ResearchStatus.PENDING,
                        ResearchRun.config_version == self.config_version,
                    )
                    .values(
                        status=ResearchStatus.RUNNING,
                        started_at=sa.func.now(),
                        lease_token=lease,
                        lease_expires_at=sa.func.now() + dt.timedelta(seconds=self.timeout + 30),
                        error=None,
                        error_class=None,
                    )
                    .returning(ResearchRun.id)
                )
            ).scalar_one_or_none()
        if claimed is None:
            return

        async def record_call(record: LlmCallRecord) -> None:
            # Allowlist telemetry: no raw response, prompt or provider exception text.
            record.research_run_id = run_id
            record.job_id = job_id
            record.response_excerpt = None
            async with self.database.transaction() as session:
                row = await session.get(ResearchRun, run_id)
                record.event_id = row.event_id if row else None
                await self.telemetry.persist(session, record)
                await self._totals(session, run_id)
            self.budget.invalidate()

        async def check_owned_budget() -> None:
            async with self.database.session() as session:
                owned = await session.scalar(
                    sa.select(ResearchRun.id).where(
                        ResearchRun.id == run_id,
                        ResearchRun.status == ResearchStatus.RUNNING,
                        ResearchRun.lease_token == lease,
                        ResearchRun.lease_expires_at > sa.func.now(),
                    )
                )
            if owned is None:
                raise ResearchValidationError("research ownership expired")
            await self.check_budget()

        try:
            async with asyncio.timeout(self.timeout):
                async with self.database.session() as session:
                    row = await session.get(ResearchRun, run_id)
                    assert row is not None
                    packet = ResearchPacket.model_validate(row.research_packet)
                    # Recheck the resolver at execution; never refresh identity from a hint.
                    current = await self.packet(session, packet.impact_id, packet.as_of)
                    if current.company != packet.company:
                        raise InstrumentResolutionError(
                            "resolved listing changed after research enqueue"
                        )
                packet = await self._enrich(packet)
                async with self.database.transaction() as session:
                    await session.execute(
                        sa.update(ResearchRun)
                        .where(ResearchRun.id == run_id, ResearchRun.lease_token == lease)
                        .values(
                            research_packet=packet.model_dump(mode="json"),
                            provider_degradation={
                                "items": [item.model_dump() for item in packet.degradation]
                            },
                        )
                    )
                result = await self.engine.analyze(
                    packet, record_call=record_call, check_budget=check_owned_budget
                )
                async with self.database.transaction() as session:
                    row = (
                        await session.execute(
                            sa.select(ResearchRun).where(ResearchRun.id == run_id).with_for_update()
                        )
                    ).scalar_one()
                    if row.status != ResearchStatus.RUNNING or row.lease_token != lease:
                        raise ResearchValidationError("research ownership lost")
                    decision = result.decision
                    if not set(decision.evidence_ids) <= {
                        item.source_id for item in packet.evidence
                    }:
                        raise ResearchValidationError("unknown decision evidence")
                    row.structured_decision = decision.model_dump(mode="json")
                    row.raw_reports = dict(result.reports)
                    row.status = ResearchStatus.SUCCEEDED
                    row.completed_at = utcnow()
                    row.lease_token = None
                    row.lease_expires_at = None
                    previous = (
                        await session.get(Thesis, packet.previous_thesis_id)
                        if packet.previous_thesis_id
                        else None
                    )
                    thesis_id = uuid.uuid4()
                    session.add(
                        Thesis(
                            id=thesis_id,
                            research_run_id=run_id,
                            action=decision.action,
                            confidence=decision.confidence,
                            time_horizon=decision.horizon,
                            summary=decision.thesis,
                            bull_case=decision.bull_case,
                            bear_case=decision.bear_case,
                            catalysts={"items": list(decision.catalysts)},
                            risks={"items": list(decision.risks)},
                            invalidation_conditions={
                                "items": list(decision.invalidation_conditions)
                            },
                            evidence_source_ids={
                                "items": [str(item) for item in decision.evidence_ids]
                            },
                            supersedes_thesis_id=packet.previous_thesis_id,
                            original_thesis_id=(previous.original_thesis_id or previous.id)
                            if previous
                            else None,
                        )
                    )
                self.health.record(
                    ProviderName.TRADINGAGENTS,
                    ProviderStatus.DEGRADED if result.degradation else ProviderStatus.HEALTHY,
                    detail="Supplemental context degraded" if result.degradation else None,
                )
        except BaseException as exc:
            status = (
                ResearchStatus.CANCELLED
                if isinstance(exc, asyncio.CancelledError)
                else ResearchStatus.TIMED_OUT
                if isinstance(exc, TimeoutError)
                else ResearchStatus.FAILED
            )
            async with self.database.transaction() as session:
                await session.execute(
                    sa.update(ResearchRun)
                    .where(ResearchRun.id == run_id, ResearchRun.lease_token == lease)
                    .values(
                        status=status,
                        completed_at=sa.func.now(),
                        error_class=type(exc).__name__,
                        error=f"Research stopped: {type(exc).__name__}",
                        lease_token=None,
                        lease_expires_at=None,
                    )
                )
            self.health.record(
                ProviderName.TRADINGAGENTS, ProviderStatus.DEGRADED, detail=type(exc).__name__
            )
            if isinstance(exc, Exception):
                raise safe_research_error(exc) from None
            raise

    async def _totals(self, session: AsyncSession, run_id: uuid.UUID) -> None:
        rows = list(
            await session.scalars(sa.select(LlmCall).where(LlmCall.research_run_id == run_id))
        )
        await session.execute(
            sa.update(ResearchRun)
            .where(ResearchRun.id == run_id)
            .values(
                estimated_cost_usd=sum(
                    (row.estimated_cost_usd or Decimal(0) for row in rows), Decimal(0)
                ),
                token_usage={
                    "input_tokens": sum(row.input_tokens or 0 for row in rows),
                    "output_tokens": sum(row.output_tokens or 0 for row in rows),
                    "cache_hit_tokens": sum(row.cached_input_tokens or 0 for row in rows),
                    "cache_miss_tokens": sum(row.cache_miss_input_tokens or 0 for row in rows),
                    "calls": len(rows),
                },
            )
        )

    async def _enrich(self, packet: ResearchPacket) -> ResearchPacket:
        data = list(packet.market_context)
        degraded = list(packet.degradation)
        # Each entry is (name, configured provider, how to ask it for context).
        # Keeping the call as a thunk is what lets the macro provider take an
        # instant and the packet-scoped ones take a packet without the loop body
        # having to know which is which.
        sources: tuple[tuple[str, object | None, Callable[[], Awaitable[Any]]], ...] = (
            ("fred", self.macro, lambda: self.macro.context(packet.as_of)),  # type: ignore[union-attr]
            ("alpaca", self.supplemental, lambda: self.supplemental.context(packet)),  # type: ignore[union-attr]
            ("sec", self.fundamentals, lambda: self.fundamentals.context(packet)),  # type: ignore[union-attr]
            ("finnhub", self.expectations, lambda: self.expectations.context(packet)),  # type: ignore[union-attr]
            ("yfinance", self.targets, lambda: self.targets.context(packet)),  # type: ignore[union-attr]
            ("polymarket", self.macro_markets, lambda: self.macro_markets.context(packet.as_of)),  # type: ignore[union-attr]
        )
        for name, provider, fetch in sources:
            if provider is None:
                degraded.append(
                    ProviderDegradation(
                        provider=name,
                        error_class="NotConfigured",
                        detail="Supplemental research data is not configured",
                    )
                )
                continue
            try:
                context = await fetch()
                # Validate a replacement provider's output before adding it, so a
                # malformed/future-dated optional result degrades only that provider.
                ResearchPacket.model_validate(
                    {**packet.model_dump(), "market_context": tuple(data) + context}
                )
                data.extend(context)
                if name == "fred":
                    self.health.record(ProviderName.FRED, ProviderStatus.HEALTHY)
                # SEC health is deliberately not recorded from here. The common
                # failure is "this listing does not file with the SEC", which is
                # a property of the company, not an outage; reporting it as
                # DEGRADED would make the health panel blame a provider that is
                # answering correctly. Ingestion still probes SEC for real.
            except (ProviderError, ValueError) as exc:
                degraded.append(
                    ProviderDegradation(
                        provider=name,
                        error_class=type(exc).__name__,
                        detail="Supplemental research data unavailable",
                    )
                )
                if name == "fred":
                    self.health.record(
                        ProviderName.FRED, ProviderStatus.DEGRADED, detail=type(exc).__name__
                    )
        return ResearchPacket.model_validate(
            {**packet.model_dump(), "market_context": tuple(data), "degradation": tuple(degraded)}
        )

    async def sweep(self) -> None:
        async with self.database.transaction() as session:
            await session.execute(
                sa.update(ResearchRun)
                .where(
                    ResearchRun.status == ResearchStatus.RUNNING,
                    ResearchRun.lease_expires_at < sa.func.now(),
                )
                .values(
                    status=ResearchStatus.TIMED_OUT,
                    completed_at=sa.func.now(),
                    lease_token=None,
                    lease_expires_at=None,
                    error_class="StalledRun",
                    error=(
                        "Worker lease expired; explicit rerun required because spend may be unknown"
                    ),
                )
            )
            # A changed deployment never silently runs a pending old configuration as new.
            await session.execute(
                sa.update(ResearchRun)
                .where(
                    ResearchRun.status == ResearchStatus.PENDING,
                    ResearchRun.config_version != self.config_version,
                )
                .values(
                    status=ResearchStatus.CANCELLED,
                    completed_at=sa.func.now(),
                    error_class="ConfigurationChanged",
                    error="Pending research configuration changed; request an explicit rerun",
                )
            )
            pending = await session.scalars(
                sa.select(ResearchRun.id)
                .where(ResearchRun.status == ResearchStatus.PENDING)
                .limit(25)
            )
            for run_id in pending:
                await self.queue.enqueue(
                    session,
                    JobType.RUN_RESEARCH,
                    payload={"run_id": str(run_id)},
                    dedupe_key=f"research:{run_id}",
                    max_attempts=1,
                    priority=40,
                )

    async def enqueue_event(self, event_id: uuid.UUID | None = None) -> None:
        async with self.database.transaction() as session:
            ranked = sa.func.row_number().over(
                partition_by=EventCompanyImpact.event_id,
                order_by=(
                    EventCompanyImpact.materiality_score.desc(),
                    EventCompanyImpact.confidence.desc(),
                    EventCompanyImpact.id,
                ),
            )
            # Ranked before the cap is applied so "top N" means the N the
            # classifier thought this story actually bore on, not the N that
            # happened to be inserted first.  ``id`` breaks ties so a rerun
            # selects the same impacts rather than a fresh arbitrary subset.
            query = (
                sa.select(EventCompanyImpact.id)
                .add_columns(ranked.label("rank"))
                .join(Event, Event.id == EventCompanyImpact.event_id)
                .where(
                    EventCompanyImpact.resolution_status == ResolutionStatus.RESOLVED,
                    EventCompanyImpact.company_id.is_not(None),
                    Event.status == EventStatus.CANDIDATE,
                    EventCompanyImpact.materiality_score >= self.min_impact_materiality,
                )
            )
            if event_id is not None:
                query = query.where(Event.id == event_id)
            else:
                query = query.where(
                    ~sa.exists(
                        sa.select(ResearchRun.id).where(
                            ResearchRun.impact_id == EventCompanyImpact.id,
                            ResearchRun.config_version == self.config_version,
                        )
                    )
                )
            selected = query.subquery()
            capped = sa.select(selected.c.id)
            if self.max_impacts_per_event:
                capped = capped.where(selected.c.rank <= self.max_impacts_per_event)
            for impact_id in await session.scalars(capped.limit(25)):
                try:
                    await self.request(session, impact_id)
                except (InstrumentResolutionError, ResearchValidationError):
                    continue
