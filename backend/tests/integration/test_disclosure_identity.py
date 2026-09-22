"""Provider-known identity reaches the classifier and the resolver."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument, EventCompanyImpact
from stockbrain.db.session import Database
from stockbrain.enums import (
    Broker,
    ResolutionMethod,
    ResolutionStatus,
    SourceCategory,
    SourceProvider,
)
from stockbrain.ingestion.base import RawSourceDocument
from stockbrain.ingestion.service import IngestionService
from stockbrain.instruments.normalize import instrument_name_key
from stockbrain.instruments.service import ResolutionService
from stockbrain.intelligence.classifier import ClassificationInput, EventClassifier
from stockbrain.intelligence.service import ClassificationService
from stockbrain.llm.base import CompletionResult, TokenUsage
from stockbrain.llm.telemetry import LlmTelemetry

pytestmark = pytest.mark.integration


CLASSIFICATION: dict[str, Any] = {
    "relevant_to_public_equities": True,
    "event_type": "REGULATORY",
    "canonical_title": "Barclays updates the market",
    "summary": "Barclays published a regulatory update.",
    "event_time": "2026-09-21T11:46:00Z",
    "novelty": 0.6,
    "importance": 0.7,
    "confidence": 0.8,
    "needs_corroboration": False,
    "topics": ["regulatory"],
    "rationale": "The release names both companies.",
    "companies": [
        {
            "company_name": "Barclays PLC",
            "ticker_hint": "BARC",
            "exchange_hint": None,
            "relationship": "Issuer",
            "impact_path": "direct",
            "direction": "unknown",
            "materiality": 0.5,
            "confidence": 0.7,
        },
        {
            "company_name": "HSBC Holdings plc",
            "ticker_hint": "HSBA",
            "exchange_hint": None,
            "relationship": "Peer",
            "impact_path": "indirect",
            "direction": "unknown",
            "materiality": 0.3,
            "confidence": 0.5,
        },
    ],
}


class ScriptedProvider:
    name = "scripted"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def complete(self, request: Any) -> CompletionResult:
        return CompletionResult(
            content=json.dumps(self._payload),
            model="deepseek-v4-flash",
            usage=TokenUsage(
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                cache_hit_tokens=0,
                cache_miss_tokens=100,
            ),
            finish_reason="stop",
            provider_request_id="req-1",
            latency_ms=10,
            started_at=dt.datetime.now(dt.UTC),
            completed_at=dt.datetime.now(dt.UTC),
        )

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


def _service(database: Database, payload: dict[str, Any]) -> ClassificationService:
    return ClassificationService(
        database,
        _settings(),
        classifier=EventClassifier(ScriptedProvider(payload), model="deepseek-v4-flash"),
        telemetry=LlmTelemetry(),
    )


async def _ingest_disclosure(
    database: Database,
    *,
    provider: SourceProvider,
    metadata: dict[str, Any],
    headline: str,
) -> uuid.UUID:
    result = await IngestionService(database).ingest(
        RawSourceDocument(
            provider=provider,
            provider_item_id="9782378",
            url="https://www.investegate.co.uk/announcement/rns/barclays--barc/x/9782378",
            source_name="Investegate",
            source_category=SourceCategory.REGULATOR,
            headline=headline,
            published_at=dt.datetime(2026, 9, 21, 11, 46, tzinfo=dt.UTC),
            symbols=list(metadata.get("symbols") or []),
            is_distinct_event=True,
            metadata=metadata,
        )
    )
    assert result.event_id is not None
    return result.event_id


async def test_classifier_sees_exchange_and_isin_template_values() -> None:
    classifier = EventClassifier(ScriptedProvider(CLASSIFICATION), model="deepseek-v4-flash")
    messages = classifier.build_messages(
        ClassificationInput(
            headline="Barclays update",
            body="body",
            provider="INVESTEGATE",
            symbol_hints=["BARC"],
            exchange_hint="London Stock Exchange",
            isin="GB0031348658",
        ),
        as_of=dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.UTC),
    )
    assert "exchange: London Stock Exchange" in messages[1].content
    assert "isin: GB0031348658" in messages[1].content


async def test_a_matching_impact_gets_the_exchange_hint_and_a_peer_does_not(
    clean_tables: Database,
) -> None:
    event_id = await _ingest_disclosure(
        clean_tables,
        provider=SourceProvider.INVESTEGATE,
        metadata={
            "language": "en",
            "exchange_hint": "London Stock Exchange",
            "exchange_hints": ["London Stock Exchange", "London Stock Exchange AIM"],
            "company_name": "Barclays",
            "symbols": ["BARC"],
        },
        headline="Barclays: Interim Results",
    )
    service = _service(clean_tables, CLASSIFICATION)

    await service.classify_event(event_id)

    async with clean_tables.session() as session:
        impacts = {
            impact.company_key: impact
            for impact in (
                await session.execute(
                    sa.select(EventCompanyImpact).where(EventCompanyImpact.event_id == event_id)
                )
            ).scalars()
        }
    assert impacts["barclays"].exchange_hint == "London Stock Exchange"
    assert impacts["hsbc"].exchange_hint is None


async def test_the_source_isin_reaches_resolution_for_a_matching_impact(
    clean_tables: Database,
) -> None:
    """The instrument's name does not match the hint, so only the ISIN can resolve it."""
    async with clean_tables.transaction() as session:
        session.add(
            BrokerInstrument(
                broker=Broker.TRADING212,
                broker_ticker="SPIEp_EQ",
                name="Société Parisienne",
                short_name="SPIE",
                isin="FR0012757854",
                currency="EUR",
                instrument_type="STOCK",
                exchange="Euronext Paris",
                market_symbol="SPIE",
                name_key=instrument_name_key("Société Parisienne"),
                is_active=True,
                last_refreshed_at=utcnow(),
                last_seen_at=utcnow(),
            )
        )

    event_id = await _ingest_disclosure(
        clean_tables,
        provider=SourceProvider.EQS,
        metadata={
            "language": "en",
            "isin": "FR0012757854",
            "exchange_hint": None,
            "company_name": "SPIE SA",
            "symbols": [],
        },
        headline="SPIE: Final Terms",
    )
    payload = {
        **CLASSIFICATION,
        "companies": [
            {
                "company_name": "SPIE SA",
                "ticker_hint": None,
                "exchange_hint": None,
                "relationship": "Issuer",
                "impact_path": "direct",
                "direction": "unknown",
                "materiality": 0.5,
                "confidence": 0.7,
            }
        ],
    }
    service = _service(clean_tables, payload)
    await service.classify_event(event_id)

    async with clean_tables.session() as session:
        impact_id = (
            await session.execute(
                sa.select(EventCompanyImpact.id).where(EventCompanyImpact.event_id == event_id)
            )
        ).scalar_one()

    outcome = await ResolutionService(clean_tables).resolve_impact(impact_id)
    assert outcome is not None
    assert outcome.status is ResolutionStatus.RESOLVED
    assert outcome.method is ResolutionMethod.ISIN_EXACT
