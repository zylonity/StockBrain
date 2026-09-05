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
        # These tests exercise the read API, not the login flow.
        web_auth_enabled=False,
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


# ---------------------------------------------------------------------------
# Classification surface
# ---------------------------------------------------------------------------


async def test_event_detail_exposes_classification_and_impacts(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    from tests.integration.test_classification import (
        CLASSIFICATION,
        ScriptedProvider,
        _service,
    )

    result = await IngestionService(clean_tables).ingest(
        _doc(item_id="a", headline="Vertiv wins contract", url="https://benzinga.com/a")
    )
    assert result.event_id is not None
    await _service(clean_tables, ScriptedProvider(CLASSIFICATION)).classify_event(result.event_id)

    body = (await client.get(f"/api/v1/events/{result.event_id}")).json()
    event = body["event"]

    assert event["status"] == "CANDIDATE"
    assert event["event_type"] == "CONTRACT_AWARD"
    assert event["relevant_to_public_equities"] is True
    assert event["needs_corroboration"] is False
    assert event["importance_score"] == 0.75
    assert event["novelty_score"] == 0.8
    assert event["confidence_score"] == 0.85
    assert event["candidate_score"] is not None
    assert event["topics"] == ["ai_infrastructure", "data_centres"]
    assert event["classifier_model"] == "deepseek-v4-flash"
    assert event["classifier_prompt_version"] == "event_classifier/v1"
    assert event["classifier_error"] is None
    assert event["company_count"] == 2

    assert body["rationale"].startswith("The article names Vertiv")

    companies = {company["company_name_hint"]: company for company in body["companies"]}
    vertiv = companies["Vertiv Holdings Co."]
    assert vertiv["ticker_hint"] == "VRT"
    assert vertiv["direction"] == "POSITIVE"
    assert vertiv["impact_path"] == "direct"
    assert vertiv["materiality_score"] == 0.7
    assert vertiv["resolved_company_id"] is None
    assert companies["NVIDIA Corporation"]["impact_path"] == "indirect"

    usage = body["llm_usage"]
    assert usage["calls"] == 1
    assert usage["input_tokens"] == 1500
    assert usage["cached_input_tokens"] == 1000
    assert float(usage["estimated_cost_usd"]) > 0

    call = body["llm_calls"][0]
    assert call["purpose"] == "CLASSIFY_EVENT"
    assert call["thinking_enabled"] is False
    assert call["succeeded"] is True and call["used"] is True
    assert call["provider_request_id"] == "chatcmpl-1"


async def test_hidden_reasoning_text_is_never_exposed(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    """Only the structured rationale is surfaced; chain-of-thought never is.

    The provider returns `reasoning_content` alongside the answer. StockBrain
    records that it was present and discards the text at the client boundary, so
    there is nowhere for it to leak from afterwards.
    """
    import json as _json

    from stockbrain.llm.deepseek import parse_completion
    from tests.integration.test_classification import (
        CLASSIFICATION,
        ScriptedProvider,
        _service,
    )

    secret_reasoning = "STEP 1: I should first consider the hidden chain of thought."
    parsed = parse_completion(
        {
            "id": "chatcmpl-x",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": _json.dumps(CLASSIFICATION),
                        "reasoning_content": secret_reasoning,
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
        latency_ms=1,
        attempts=1,
    )
    # The client keeps the flag and drops the text.
    assert parsed.had_reasoning_content is True
    assert secret_reasoning not in parsed.content

    result = await IngestionService(clean_tables).ingest(
        _doc(item_id="a", headline="Vertiv wins contract", url="https://benzinga.com/a")
    )
    assert result.event_id is not None
    await _service(clean_tables, ScriptedProvider(CLASSIFICATION)).classify_event(result.event_id)

    response = await client.get(f"/api/v1/events/{result.event_id}")
    payload = response.json()

    assert secret_reasoning not in response.text
    assert "chain of thought" not in response.text.lower()
    # Only the boolean flag is exposed, never the reasoning text.
    assert isinstance(payload["llm_calls"][0]["had_reasoning_content"], bool)
    assert payload["rationale"] == CLASSIFICATION["rationale"]


async def test_classification_failure_is_visible_in_the_api(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    from stockbrain.errors import ProviderResponseError
    from tests.integration.test_classification import ScriptedProvider, _service

    result = await IngestionService(clean_tables).ingest(
        _doc(item_id="a", headline="Broken", url="https://benzinga.com/a")
    )
    assert result.event_id is not None
    service = _service(clean_tables, ScriptedProvider(ProviderResponseError("bad schema")))
    with pytest.raises(ProviderResponseError):
        await service.classify_event(result.event_id, attempt=3, is_final_attempt=True)

    body = (await client.get(f"/api/v1/events/{result.event_id}")).json()
    assert body["event"]["status"] == "CLASSIFICATION_FAILED"
    assert "ProviderResponseError" in body["event"]["classifier_error"]

    failed_call = body["llm_calls"][0]
    assert failed_call["succeeded"] is False
    assert failed_call["error_class"] == "ProviderResponseError"


async def test_discovery_status_reports_classifier_and_budget(
    client: httpx.AsyncClient,
) -> None:
    """With no DeepSeek key the classifier is reported inactive, not broken."""
    body = (await client.get("/api/v1/discovery/status")).json()
    assert body["classifier_active"] is False
    assert body["classifier_model"] is None
    assert body["budget"] is None
