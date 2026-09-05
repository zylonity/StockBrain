"""Instrument-resolution and market-data REST endpoints.

The point of these endpoints is that a refusal is inspectable: an AMBIGUOUS
mapping nobody can look at is indistinguishable from a bug.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager

from stockbrain.config import Settings
from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument, Company, EventCompanyImpact
from stockbrain.db.models.sources import Event
from stockbrain.db.session import Database
from stockbrain.enums import AliasType, Broker, EventStatus, ImpactDirection
from stockbrain.instruments.aliases import AliasSpec, upsert_alias
from stockbrain.instruments.service import ResolutionService
from stockbrain.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(clean_tables: Database) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        app_env="test",
        log_level="CRITICAL",
        database_url=str(clean_tables.engine.url.render_as_string(hide_password=False)),
        stockbrain_secret_key="test-key",
        discovery_enabled=False,
        alpaca_news_enabled=False,
        firecrawl_enabled=False,
        sec_enabled=False,
    )
    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def _seed(database: Database) -> None:
    """Two Alphabet listings (ambiguous) and one Apple listing (resolvable)."""
    async with database.transaction() as session:
        session.add_all(
            [
                BrokerInstrument(
                    broker=Broker.TRADING212,
                    broker_ticker="AAPL_US_EQ",
                    name="Apple Inc.",
                    short_name="AAPL",
                    isin="US0378331005",
                    currency="USD",
                    instrument_type="STOCK",
                    exchange="NASDAQ",
                    market_symbol="AAPL",
                    market_code="US",
                    name_key="apple",
                    last_refreshed_at=utcnow(),
                    last_seen_at=utcnow(),
                ),
                BrokerInstrument(
                    broker=Broker.TRADING212,
                    broker_ticker="GOOGL_US_EQ",
                    name="Alphabet Inc.",
                    short_name="GOOGL",
                    isin="US02079K3059",
                    currency="USD",
                    instrument_type="STOCK",
                    exchange="NASDAQ",
                    market_symbol="GOOGL",
                    market_code="US",
                    name_key="alphabet",
                    last_refreshed_at=utcnow(),
                    last_seen_at=utcnow(),
                ),
                BrokerInstrument(
                    broker=Broker.TRADING212,
                    broker_ticker="GOOG_US_EQ",
                    name="Alphabet Inc.",
                    short_name="GOOG",
                    isin="US02079K1079",
                    currency="USD",
                    instrument_type="STOCK",
                    exchange="NASDAQ",
                    market_symbol="GOOG",
                    market_code="US",
                    name_key="alphabet",
                    last_refreshed_at=utcnow(),
                    last_seen_at=utcnow(),
                ),
            ]
        )

    async with database.transaction() as session:
        event = Event(
            title="Alphabet and Apple in the news",
            status=EventStatus.CLASSIFIED,
            first_seen_at=utcnow(),
            title_hash="b" * 64,
        )
        session.add(event)
        await session.flush()
        session.add_all(
            [
                EventCompanyImpact(
                    event_id=event.id,
                    company_name_hint="Apple Inc.",
                    company_key="apple",
                    ticker_hint="AAPL",
                    direction=ImpactDirection.POSITIVE,
                    impact_path="direct",
                    materiality_score=0.9,
                    confidence=0.9,
                ),
                EventCompanyImpact(
                    event_id=event.id,
                    company_name_hint="Alphabet",
                    company_key="alphabet",
                    direction=ImpactDirection.MIXED,
                    impact_path="direct",
                    materiality_score=0.6,
                    confidence=0.7,
                ),
            ]
        )
        event_id = event.id

    await ResolutionService(database).resolve_event(event_id)


async def test_resolutions_expose_the_hint_and_the_verified_instrument(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    await _seed(clean_tables)
    response = await client.get("/api/v1/instruments/resolutions")
    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 2
    assert body["counts_by_status"]["RESOLVED"] == 1
    assert body["counts_by_status"]["AMBIGUOUS"] == 1

    resolved = next(item for item in body["items"] if item["status"] == "RESOLVED")
    # The model's hint and the executable identity are shown side by side.
    assert resolved["model_ticker_hint"] == "AAPL"
    assert resolved["broker_ticker"] == "AAPL_US_EQ"
    assert resolved["market_symbol"] == "AAPL"
    assert resolved["exchange"] == "NASDAQ"
    assert resolved["currency"] == "USD"
    assert resolved["isin"] == "US0378331005"
    assert resolved["confidence"] is not None
    assert resolved["method"]


async def test_an_ambiguous_mapping_lists_its_alternatives(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    await _seed(clean_tables)
    response = await client.get("/api/v1/instruments/resolutions?resolution_status=AMBIGUOUS")
    body = response.json()

    assert body["total"] == 1
    item = body["items"][0]
    assert item["broker_instrument_id"] is None
    assert {alt["broker_ticker"] for alt in item["alternatives"]} == {
        "GOOGL_US_EQ",
        "GOOG_US_EQ",
    }
    assert item["notes"]


async def test_a_single_resolution_can_be_fetched(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    await _seed(clean_tables)
    listing = (await client.get("/api/v1/instruments/resolutions")).json()
    impact_id = listing["items"][0]["impact_id"]

    response = await client.get(f"/api/v1/instruments/resolutions/{impact_id}")
    assert response.status_code == 200
    assert response.json()["impact_id"] == impact_id

    missing = await client.get(
        "/api/v1/instruments/resolutions/00000000-0000-0000-0000-000000000000"
    )
    assert missing.status_code == 404


async def test_instruments_can_be_browsed_and_searched(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    await _seed(clean_tables)
    response = await client.get("/api/v1/instruments?search=alphabet")
    tickers = {row["broker_ticker"] for row in response.json()}
    assert tickers == {"GOOGL_US_EQ", "GOOG_US_EQ"}

    by_isin = await client.get("/api/v1/instruments?isin=US0378331005")
    assert [row["broker_ticker"] for row in by_isin.json()] == ["AAPL_US_EQ"]


async def test_sync_status_reports_coverage(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    await _seed(clean_tables)
    body = (await client.get("/api/v1/instruments/sync-status")).json()
    assert body["broker"] == "TRADING212"
    assert body["instruments_total"] == 3
    assert body["instruments_active"] == 3
    assert body["with_isin"] == 3
    assert body["with_exchange"] == 3
    # No credentials in this test environment, so the client is not constructed.
    assert body["configured"] is False


async def test_aliases_are_inspectable(client: httpx.AsyncClient, clean_tables: Database) -> None:
    async with clean_tables.transaction() as session:
        company = Company(
            name="Meta Platforms Inc.", name_key="meta platforms", isin="US30303M1027"
        )
        session.add(company)
        await session.flush()
        await upsert_alias(
            session,
            company,
            AliasSpec(alias="Facebook", alias_type=AliasType.HISTORICAL, notes="renamed 2021"),
        )

    body = (await client.get("/api/v1/aliases")).json()
    assert len(body) == 1
    assert body[0]["alias"] == "Facebook"
    assert body[0]["alias_type"] == "HISTORICAL"
    assert body[0]["company_name"] == "Meta Platforms Inc."
    assert body[0]["is_authoritative"] is True
    assert body[0]["notes"] == "renamed 2021"


async def test_market_data_health_reports_disabled_without_credentials(
    client: httpx.AsyncClient,
) -> None:
    """An unconfigured provider is "not set up", not "broken"."""
    body = (await client.get("/api/v1/market-data/health")).json()
    assert body["configured"] is False
    assert body["state"] == "DISABLED"
    assert body["realtime_pricing_usable"] is False
    assert body["max_quote_age_seconds"] == 15.0
    assert body["probe_quote_stale"] is False
    assert body["blockers"]


async def test_a_quote_request_without_a_provider_is_503(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/market-data/quote/AAPL")
    assert response.status_code == 503


async def test_price_reaction_without_a_provider_is_503(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    await _seed(clean_tables)
    listing = (await client.get("/api/v1/instruments/resolutions")).json()
    event_id = listing["items"][0]["event_id"]
    response = await client.get(f"/api/v1/events/{event_id}/price-reaction")
    assert response.status_code == 503


async def test_the_event_detail_shows_resolution_state(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    await _seed(clean_tables)
    listing = (await client.get("/api/v1/instruments/resolutions")).json()
    event_id = listing["items"][0]["event_id"]

    body = (await client.get(f"/api/v1/events/{event_id}")).json()
    statuses = {company["resolution_status"] for company in body["companies"]}
    assert statuses == {"RESOLVED", "AMBIGUOUS"}

    resolved = next(c for c in body["companies"] if c["resolution_status"] == "RESOLVED")
    assert resolved["broker_ticker"] == "AAPL_US_EQ"
    assert resolved["resolved_market_symbol"] == "AAPL"

    ambiguous = next(c for c in body["companies"] if c["resolution_status"] == "AMBIGUOUS")
    assert ambiguous["broker_ticker"] is None
    assert len(ambiguous["resolution_alternatives"]) == 2
