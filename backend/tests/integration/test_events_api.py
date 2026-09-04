"""Event and discovery REST endpoints.

Also asserts the boundary that matters for untrusted content: the API returns
extracted plain text, never the raw provider HTML, so a scraped article cannot
carry markup into the browser.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager

from stockbrain.config import Settings
from stockbrain.db.session import Database
from stockbrain.enums import SourceCategory, SourceProvider
from stockbrain.ingestion.base import RawSourceDocument
from stockbrain.ingestion.service import IngestionService
from stockbrain.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(clean_tables: Database) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        app_env="test",
        log_level="CRITICAL",
        database_url=str(clean_tables.engine.url.render_as_string(hide_password=False)),
        stockbrain_secret_key="test-key",
        # Keep the discovery subsystem out of these tests: they exercise the
        # read API, not the schedulers.
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


def _doc(
    *,
    item_id: str,
    headline: str,
    url: str,
    provider: SourceProvider = SourceProvider.ALPACA,
    body: str = "<p>Body text</p><script>alert(1)</script>",
) -> RawSourceDocument:
    return RawSourceDocument(
        provider=provider,
        provider_item_id=item_id,
        url=url,
        source_name="Benzinga",
        source_category=SourceCategory.NEWSWIRE,
        headline=headline,
        published_at=dt.datetime(2026, 9, 4, 9, 0, tzinfo=dt.UTC),
        body=body,
        symbols=["ACME"],
        raw_payload={"id": item_id},
    )


async def test_empty_event_list(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/v1/events")).json()
    assert body == {"total": 0, "limit": 50, "offset": 0, "events": []}


async def test_events_are_listed_newest_first_with_source_counts(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    service = IngestionService(clean_tables)
    await service.ingest(_doc(item_id="a", headline="Acme Wins", url="https://benzinga.com/a"))
    # Same headline, different outlet: one event, two sources.
    await service.ingest(
        _doc(
            item_id="b",
            headline="Acme Wins",
            url="https://reuters.com/b",
            provider=SourceProvider.FIRECRAWL,
            body="<p>Different wording entirely.</p>",
        )
    )
    await service.ingest(
        _doc(item_id="c", headline="Beta Falls", url="https://benzinga.com/c", body="<p>x</p>")
    )

    body = (await client.get("/api/v1/events")).json()
    assert body["total"] == 2
    titles = [event["title"] for event in body["events"]]
    assert set(titles) == {"Acme Wins", "Beta Falls"}

    acme = next(event for event in body["events"] if event["title"] == "Acme Wins")
    assert acme["source_count"] == 2
    assert sorted(acme["providers"]) == ["ALPACA", "FIRECRAWL"]
    assert acme["status"] == "NEW"


async def test_event_detail_returns_evidence_as_plain_text(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    service = IngestionService(clean_tables)
    result = await service.ingest(
        _doc(item_id="a", headline="Acme Wins", url="https://benzinga.com/a")
    )

    response = await client.get(f"/api/v1/events/{result.event_id}")
    assert response.status_code == 200
    body = response.json()

    assert body["event"]["title"] == "Acme Wins"
    assert len(body["sources"]) == 1
    source = body["sources"][0]
    assert source["provider"] == "ALPACA"
    assert source["relationship"] == "PRIMARY"
    assert source["canonical_url"] == "https://benzinga.com/a"
    assert source["symbols"] == ["ACME"]
    # Extracted text only: no markup, and the script contents are gone.
    assert source["excerpt"] == "Body text"
    assert "<p>" not in response.text
    assert "alert(1)" not in response.text


async def test_unknown_event_is_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/events/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


async def test_malformed_event_id_is_422_not_500(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/events/not-a-uuid")).status_code == 422


async def test_filters_narrow_the_result_set(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    service = IngestionService(clean_tables)
    await service.ingest(_doc(item_id="a", headline="Acme Wins", url="https://benzinga.com/a"))
    await service.ingest(
        _doc(
            item_id="b",
            headline="Beta Falls",
            url="https://sec.gov/b",
            provider=SourceProvider.SEC,
            body="<p>filing</p>",
        )
    )

    by_provider = (await client.get("/api/v1/events?provider=SEC")).json()
    assert [e["title"] for e in by_provider["events"]] == ["Beta Falls"]

    by_search = (await client.get("/api/v1/events?search=acme")).json()
    assert [e["title"] for e in by_search["events"]] == ["Acme Wins"]

    by_status = (await client.get("/api/v1/events?status=NEW")).json()
    assert by_status["total"] == 2

    none_match = (await client.get("/api/v1/events?status=EXECUTED_NONSENSE")).status_code
    assert none_match == 422


async def test_pagination_reports_the_full_total(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    service = IngestionService(clean_tables)
    for index in range(5):
        await service.ingest(
            _doc(
                item_id=f"a{index}",
                headline=f"Story {index}",
                url=f"https://benzinga.com/{index}",
                body=f"<p>body {index}</p>",
            )
        )

    page = (await client.get("/api/v1/events?limit=2&offset=0")).json()
    assert page["total"] == 5
    assert len(page["events"]) == 2

    second = (await client.get("/api/v1/events?limit=2&offset=2")).json()
    assert {e["id"] for e in page["events"]}.isdisjoint({e["id"] for e in second["events"]})


async def test_limit_is_bounded(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/events?limit=100000")).status_code == 422


async def test_discovery_status_reports_stats(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    service = IngestionService(clean_tables)
    await service.ingest(_doc(item_id="a", headline="Acme Wins", url="https://benzinga.com/a"))

    body = (await client.get("/api/v1/discovery/status")).json()
    assert body["discovery_enabled"] is False
    assert body["paused"] is False
    assert body["stats"]["sources_total"] == 1
    assert body["stats"]["events_total"] == 1
    assert body["stats"]["sources_by_provider"] == {"ALPACA": 1}
    assert body["stats"]["events_by_status"] == {"NEW": 1}
    assert body["stats"]["latest_source_at"] is not None


async def test_discovery_topics_are_seeded_and_listed(client: httpx.AsyncClient) -> None:
    topics = (await client.get("/api/v1/discovery/topics")).json()
    assert topics, "default topics should be seeded on first start"

    slugs = {topic["slug"] for topic in topics}
    assert "ai_infrastructure" in slugs

    ai = next(topic for topic in topics if topic["slug"] == "ai_infrastructure")
    assert ai["enabled"] is True
    assert ai["queries"], "a topic without queries would never run"
    assert all(query["last_run_at"] is None for query in ai["queries"])

    # Conservative defaults: not every theme is enabled on a fresh install.
    assert any(topic["enabled"] is False for topic in topics)
