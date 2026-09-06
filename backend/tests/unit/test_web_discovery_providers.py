"""Brave and Exa response parsing, error classification and canonical DTOs.

Each test asserts against the shape the provider's *current* documentation
specifies, verified 2026-09-05:

* Brave -- ``GET /res/v1/web/search``, ``X-Subscription-Token``, response
  ``{"web": {"results": [...]}, "news": {"results": [...]}}``; a result carries
  ``title``, ``url``, ``description``, ``page_age``, ``age`` and
  ``meta_url.hostname``
* Exa -- ``POST /search``, ``Authorization: Bearer``, response
  ``{"requestId", "results": [{"id", "title", "url", "publishedDate", "author",
  "text"?, "highlights"?, "summary"?}], "costDollars": {...}}``

The point of the canonical DTO is that nothing downstream can tell which of the
two produced a result, so the last section asserts exactly that.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from decimal import Decimal

import httpx
import pytest

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider, WebDiscoveryKind
from stockbrain.errors import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
)
from stockbrain.httpclient import ProviderHttpClient
from stockbrain.ingestion.brave import BraveSearchClient, refine_brave_error
from stockbrain.ingestion.exa import ExaSearchClient
from stockbrain.ingestion.web_search import WebSearchQuery, to_raw_document

ROUTINE = WebSearchQuery(query="grid transformer backlog", limit=10)
SEMANTIC = WebSearchQuery(
    query="who benefits from a transformer shortage",
    kind=WebDiscoveryKind.SEMANTIC,
    limit=10,
)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "web_auth_enabled": False,
        "brave_api_key": "brv-secret-key",
        "exa_api_key": "exa-secret-key",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _stub_client(provider: str, handler: object, **kwargs: object) -> ProviderHttpClient:
    """A real ``ProviderHttpClient`` over a mock transport.

    Deliberately not a mocked client: error classification, header parsing and
    the retry decision are the behaviour under test, and a mock of the client
    would assert only that the test's own mock was called.
    """
    return ProviderHttpClient(
        provider=provider,
        base_url="https://provider.invalid",
        client=httpx.AsyncClient(
            base_url="https://provider.invalid",
            transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        ),
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Brave
# ---------------------------------------------------------------------------
BRAVE_RESPONSE = {
    "type": "search",
    "query": {"original": "grid transformer backlog"},
    "web": {
        "results": [
            {
                "title": "Utility orders 400 transformers",
                "url": "https://reuters.com/a",
                "description": "A description from the web cluster.",
                "page_age": "2026-09-04T11:02:00",
                "age": "1 day ago",
                "meta_url": {"hostname": "reuters.com"},
            },
            {
                "title": "No URL here",
                "description": "dropped",
            },
        ]
    },
    "news": {
        "results": [
            {
                "title": "Breaking: interconnection queue doubles",
                "url": "https://cnbc.com/b",
                "description": "A description from the news cluster.",
                "age": "3 hours ago",
                "breaking": True,
                "meta_url": {"hostname": "cnbc.com"},
            }
        ]
    },
}


def test_brave_parses_the_web_and_news_clusters_into_one_canonical_list() -> None:
    """One request returns both, which is why the separate news endpoint --
    a second billable request -- is not used."""
    outcome = BraveSearchClient(_settings()).parse_response(BRAVE_RESPONSE, ROUTINE)

    assert [r.url for r in outcome.results] == ["https://reuters.com/a", "https://cnbc.com/b"]
    web, news = outcome.results
    assert web.provider is SourceProvider.BRAVE
    assert web.result_kind == "web"
    assert web.snippet == "A description from the web cluster."
    assert web.source_domain == "reuters.com"
    assert news.result_kind == "news"

    # One request, one billable unit, whatever came back.
    assert outcome.billed_units_reported == 1
    assert outcome.cost_usd_reported is None


def test_brave_parses_page_age_but_never_the_human_age_string() -> None:
    """``page_age`` is ISO-8601; ``age`` ("1 day ago") is a rendering.

    Turning a rendering back into an instant invents precision the provider did
    not offer, and that instant would reach the classifier as a fact.
    """
    outcome = BraveSearchClient(_settings()).parse_response(BRAVE_RESPONSE, ROUTINE)
    web, news = outcome.results
    assert web.published_at == dt.datetime(2026, 9, 4, 11, 2, tzinfo=dt.UTC)
    # The news result has only `age`, so it has no date at all rather than a
    # guessed one.
    assert news.published_at is None


def test_brave_counts_a_result_with_no_url_even_though_it_drops_it() -> None:
    """No URL means no provenance and no dedupe identity.

    The result is still counted: it was returned, and a count that only included
    the results StockBrain could use would understate what the provider did.
    """
    outcome = BraveSearchClient(_settings()).parse_response(BRAVE_RESPONSE, ROUTINE)
    assert len(outcome.results) == 2
    assert outcome.results_returned == 3


def test_brave_handles_an_empty_result_set_without_inventing_an_error() -> None:
    """A quiet day is a real answer, and is not a fault."""
    outcome = BraveSearchClient(_settings()).parse_response(
        {"type": "search", "web": {"results": []}}, ROUTINE
    )
    assert outcome.results == ()
    assert outcome.results_returned == 0


def test_brave_deduplicates_a_url_repeated_across_clusters() -> None:
    """The same article often appears in both ``web`` and ``news``.

    Deduplicated here rather than left to the ingestion layer, so one search
    does not report two results for one page -- but still *counted* twice,
    because the provider returned it twice.
    """
    outcome = BraveSearchClient(_settings()).parse_response(
        {
            "web": {"results": [{"url": "https://ft.com/x", "title": "A"}]},
            "news": {"results": [{"url": "https://ft.com/x", "title": "A"}]},
        },
        ROUTINE,
    )
    assert len(outcome.results) == 1
    assert outcome.results_returned == 2


def test_brave_rejects_a_response_with_neither_cluster() -> None:
    """A silent empty list would read as "no thematic news today"."""
    with pytest.raises(ProviderResponseError, match="no 'web' or 'news' block"):
        BraveSearchClient(_settings()).parse_response({"type": "search"}, ROUTINE)


def test_brave_rejects_a_malformed_cluster() -> None:
    with pytest.raises(ProviderResponseError):
        BraveSearchClient(_settings()).parse_response({"web": "not an object"}, ROUTINE)
    with pytest.raises(ProviderResponseError):
        BraveSearchClient(_settings()).parse_response({"web": {"results": {}}}, ROUTINE)


def test_brave_rejects_a_response_that_is_not_an_object() -> None:
    with pytest.raises(ProviderResponseError):
        BraveSearchClient(_settings()).parse_response(["nope"], ROUTINE)


async def test_brave_sends_the_subscription_token_header() -> None:
    """Documented as ``X-Subscription-Token`` -- not a bearer token.

    Asserted on the client the adapter actually built, because sending a bearer
    token here would authenticate nothing and the 401 would look like a bad key.
    """
    client = BraveSearchClient(_settings())
    headers = client.outbound_headers()
    await client.aclose()
    assert headers["x-subscription-token"] == "brv-secret-key"
    assert "authorization" not in headers
    # Brave documents that it wants this explicitly.
    assert headers["accept-encoding"] == "gzip"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (429, ProviderRateLimited),
        (503, ProviderUnavailable),
    ],
)
async def test_brave_classifies_its_error_statuses(status: int, expected: type) -> None:
    """Each one demands a different response from the caller, so each is a
    different exception rather than one "the provider failed"."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "x"})

    client = BraveSearchClient(_settings(), http=_stub_client("brave", handler, max_attempts=1))
    with pytest.raises(expected):
        await client.search(ROUTINE)
    await client.aclose()


async def test_a_brave_failure_is_logged_as_a_class_name_not_a_body() -> None:
    """A provider error body can echo the request, and the request carries the
    credential -- so the ledger records the exception class, never the text."""
    source = (
        pathlib.Path(__file__).resolve().parents[2] / "stockbrain" / "jobs" / "handlers.py"
    ).read_text()
    block = source[source.index("async def handle_web_discovery_search") :]
    block = block[: block.index("async def handle_content_extract")]
    assert "error_category=type(exc).__name__" in block
    assert "error_category=str(exc)" not in block


async def test_brave_only_accepts_routine_queries() -> None:
    """It is a keyword index. Pointing second-order questions at it is not a
    cost mistake, it is a quality one -- and the type says so."""
    assert BraveSearchClient.supported_kinds == frozenset({WebDiscoveryKind.ROUTINE})


# ---------------------------------------------------------------------------
# Exa
# ---------------------------------------------------------------------------
EXA_RESPONSE = {
    "requestId": "b5947044c4b78efa9552a7c89b306d95",
    "results": [
        {
            "id": "https://example.com/supplier",
            "title": "Transformer supplier doubles capacity",
            "url": "https://example.com/supplier",
            "publishedDate": "2026-08-30T00:00:00.000Z",
            "author": "A Reporter",
            "score": 0.71,
            "highlights": ["capacity is sold out through 2028"],
        },
        {"title": "no url", "id": "x"},
    ],
    "costDollars": {"total": 0.007, "search": {"neural": 0.007}},
    "searchTime": 812.4,
}


def test_exa_parses_a_semantic_result_into_the_same_canonical_shape() -> None:
    outcome = ExaSearchClient(_settings()).parse_response(EXA_RESPONSE, SEMANTIC)

    assert len(outcome.results) == 1
    result = outcome.results[0]
    assert result.provider is SourceProvider.EXA
    assert result.result_kind == "semantic"
    assert result.url == "https://example.com/supplier"
    assert result.published_at == dt.datetime(2026, 8, 30, tzinfo=dt.UTC)
    assert result.score == 0.71
    # Exa's `id` is the handle its /contents endpoint takes. Kept as provenance;
    # StockBrain does not call that endpoint.
    assert result.provider_result_id == "https://example.com/supplier"
    # A highlight is the passage Exa judged relevant to *this* query, which is a
    # better triage input than the first paragraph of the page.
    assert result.snippet == "capacity is sold out through 2028"
    assert result.source_domain == "example.com"


def test_exa_records_the_providers_own_price_for_the_call() -> None:
    """``costDollars`` is what the ledger reconciles against, so a call that
    cost more than the published model predicted is charged at what it cost."""
    outcome = ExaSearchClient(_settings()).parse_response(EXA_RESPONSE, SEMANTIC)
    assert outcome.cost_usd_reported == Decimal("0.007")
    assert outcome.billed_units_reported == 1
    assert outcome.results_returned == 2


def test_exa_tolerates_a_missing_cost_block() -> None:
    """The documentation calls it an estimate, not an invoice.

    Absent means "no provider figure", which the ledger handles by charging the
    reservation -- never by treating the call as free.
    """
    outcome = ExaSearchClient(_settings()).parse_response(
        {"results": [{"url": "https://a.example", "title": "t"}]}, SEMANTIC
    )
    assert outcome.cost_usd_reported is None


def test_exa_handles_an_empty_result_set() -> None:
    outcome = ExaSearchClient(_settings()).parse_response({"results": []}, SEMANTIC)
    assert outcome.results == ()
    assert outcome.results_returned == 0


def test_exa_rejects_a_response_without_results() -> None:
    with pytest.raises(ProviderResponseError, match="no 'results'"):
        ExaSearchClient(_settings()).parse_response({"requestId": "x"}, SEMANTIC)
    with pytest.raises(ProviderResponseError, match="not a list"):
        ExaSearchClient(_settings()).parse_response({"results": {}}, SEMANTIC)


def test_exa_rejects_a_response_that_is_not_an_object() -> None:
    with pytest.raises(ProviderResponseError):
        ExaSearchClient(_settings()).parse_response("nope", SEMANTIC)


def test_exa_deduplicates_a_repeated_url() -> None:
    outcome = ExaSearchClient(_settings()).parse_response(
        {
            "results": [
                {"url": "https://a.example/x", "title": "A"},
                {"url": "https://a.example/x", "title": "A again"},
            ]
        },
        SEMANTIC,
    )
    assert len(outcome.results) == 1
    assert outcome.results_returned == 2


def test_exa_start_published_date_is_derived_from_the_freshness_window() -> None:
    """Exa takes an ISO instant where Brave takes ``pw``.  Same idea, and the
    stored query knows about neither."""
    now = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
    body = ExaSearchClient(_settings()).build_request(
        WebSearchQuery(query="q", kind=WebDiscoveryKind.SEMANTIC, limit=5, freshness_days=30),
        now=now,
    )
    assert body["startPublishedDate"].startswith("2026-08-06")


async def test_exa_sends_a_bearer_token() -> None:
    """Exa documents ``Authorization: Bearer`` -- the opposite of Brave."""
    client = ExaSearchClient(_settings())
    headers = client.outbound_headers()
    await client.aclose()
    assert headers["authorization"] == "Bearer exa-secret-key"
    assert headers["content-type"] == "application/json"


def test_neither_key_renders_when_a_settings_object_is_interpolated() -> None:
    """``SecretStr`` is the whole reason an accidental log line is harmless."""
    settings = _settings()
    assert "brv-secret-key" not in str(settings)
    assert "exa-secret-key" not in str(settings)
    assert str(settings.brave_api_key) == "**********"
    assert str(settings.exa_api_key) == "**********"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ProviderAuthError),
        (429, ProviderRateLimited),
        (500, ProviderUnavailable),
    ],
)
async def test_exa_classifies_its_error_statuses(status: int, expected: type) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "x"})

    client = ExaSearchClient(_settings(), http=_stub_client("exa", handler))
    with pytest.raises(expected):
        await client.search(SEMANTIC)
    await client.aclose()


async def test_an_exa_search_is_never_retried() -> None:
    """Exa publishes no "failed requests are not billed" guarantee, so the
    conservative reading applies: a retry is a second call at $0.007."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    client = ExaSearchClient(_settings(), http=_stub_client("exa", handler, max_attempts=3))
    with pytest.raises(ProviderUnavailable):
        await client.search(SEMANTIC)
    await client.aclose()
    assert calls == 1


def test_exa_only_accepts_semantic_queries() -> None:
    """Not a second Brave. Running every routine query through both would
    double the bill to rediscover the same articles."""
    assert ExaSearchClient.supported_kinds == frozenset({WebDiscoveryKind.SEMANTIC})


# ---------------------------------------------------------------------------
# The canonical DTO -- nothing downstream may know which provider answered
# ---------------------------------------------------------------------------
def test_both_providers_produce_the_same_document_shape() -> None:
    """The whole point of the abstraction.

    The only field that differs is the provenance, which is the one field that
    *should*.
    """
    brave = BraveSearchClient(_settings()).parse_response(BRAVE_RESPONSE, ROUTINE).results[0]
    exa = ExaSearchClient(_settings()).parse_response(EXA_RESPONSE, SEMANTIC).results[0]

    brave_doc = to_raw_document(brave, discovery_query=ROUTINE.query, kind=WebDiscoveryKind.ROUTINE)
    exa_doc = to_raw_document(exa, discovery_query=SEMANTIC.query, kind=WebDiscoveryKind.SEMANTIC)

    assert set(brave_doc.model_dump()) == set(exa_doc.model_dump())
    assert brave_doc.provider is SourceProvider.BRAVE
    assert exa_doc.provider is SourceProvider.EXA
    assert brave_doc.metadata["discovery_kind"] == "ROUTINE"
    assert exa_doc.metadata["discovery_kind"] == "SEMANTIC"


def test_a_document_body_is_the_snippet_and_only_the_snippet() -> None:
    """A search result is not an article.

    What is stored is enough to deduplicate against, enough for the source
    category rules and enough for the cheap classifier to triage. Fetching the
    article before any of that has run is spending on something nothing has yet
    judged worth reading.
    """
    brave = BraveSearchClient(_settings()).parse_response(BRAVE_RESPONSE, ROUTINE).results[0]
    document = to_raw_document(brave, discovery_query=ROUTINE.query, kind=WebDiscoveryKind.ROUTINE)
    assert document.body == "A description from the web cluster."
    assert len(document.body) < 200


def test_a_document_carries_the_provider_payload_for_audit_only() -> None:
    """Kept so a classification can be re-examined against exactly what
    arrived, and never indexed into by downstream code."""
    exa = ExaSearchClient(_settings()).parse_response(EXA_RESPONSE, SEMANTIC).results[0]
    document = to_raw_document(exa, discovery_query=SEMANTIC.query, kind=WebDiscoveryKind.SEMANTIC)
    assert document.raw_payload["id"] == "https://example.com/supplier"
    assert document.metadata["discovery_provider"] == "EXA"


def test_the_source_category_is_assigned_from_the_url_not_the_provider() -> None:
    """A Reuters article is a newswire whichever backend surfaced it."""
    brave = BraveSearchClient(_settings()).parse_response(BRAVE_RESPONSE, ROUTINE).results[0]
    document = to_raw_document(brave, discovery_query=ROUTINE.query, kind=WebDiscoveryKind.ROUTINE)
    assert document.source_category.value == "NEWSWIRE"


# ---------------------------------------------------------------------------
# Credential probes: proving a key without buying a search
# ---------------------------------------------------------------------------
# Both bodies are what the live APIs actually returned on 2026-09-06, captured
# by sending a deliberately invalid request with a valid and an invalid key.
# They are here rather than paraphrased because the whole point of the probe is
# that it reads a provider-specific body, and a paraphrase would let the real
# shape drift away from the parser without a test noticing.
BRAVE_REJECTED_TOKEN_BODY = {
    "type": "ErrorResponse",
    "error": {
        "code": "SUBSCRIPTION_TOKEN_INVALID",
        "detail": "The provided subscription token is invalid.",
        "meta": {"component": "authentication"},
        "status": 422,
    },
}

BRAVE_MISSING_QUERY_BODY = {
    "type": "ErrorResponse",
    "error": {
        "id": "ac767880-a9c9-4b26-99e1-716fd0249fa8",
        "status": 422,
        "detail": "Unable to validate request parameter(s)",
        "meta": {"errors": [{"type": "missing", "loc": ["query", "q"], "msg": "Field required"}]},
    },
}

EXA_REJECTED_KEY_BODY = {
    "requestId": "2e2745fb5066969f5ed51841d014efbd",
    "error": "Invalid API key",
    "tag": "INVALID_API_KEY",
}

EXA_REJECTED_BODY_BODY = {
    "requestId": "548d6b15d79e6a60f71400e8d66667ed",
    "error": "Invalid request body | Validation error: Invalid input: expected string, "
    'received undefined at "query"',
    "tag": "INVALID_REQUEST_BODY",
}


def _brave_probe_client(handler: object) -> BraveSearchClient:
    """A Brave client whose transport is stubbed but whose *refinement is real*.

    ``refine_error`` has to be passed explicitly because injecting ``http``
    bypasses the constructor that normally wires it, and the refinement is
    precisely what these tests are about.
    """
    return BraveSearchClient(
        _settings(),
        http=_stub_client("brave", handler, refine_error=refine_brave_error, max_attempts=1),
    )


async def test_brave_reports_a_rejected_token_as_an_auth_error_despite_the_422() -> None:
    """Brave answers 422 -- not 401 -- for a bad subscription token.

    Without refinement the shared classifier calls that a malformed request, so
    an operator with an expired key is told "HTTP 422" and goes looking for a
    bug in the caller.
    """
    client = _brave_probe_client(
        lambda request: httpx.Response(422, json=BRAVE_REJECTED_TOKEN_BODY)
    )
    try:
        with pytest.raises(ProviderAuthError):
            await client.verify_credentials()
    finally:
        await client.aclose()


async def test_brave_reads_a_422_about_the_query_as_an_accepted_token() -> None:
    """The other half of the same status code: parameters refused, token fine."""
    client = _brave_probe_client(lambda request: httpx.Response(422, json=BRAVE_MISSING_QUERY_BODY))
    try:
        await client.verify_credentials()  # returns; a raise here is the failure
    finally:
        await client.aclose()


async def test_brave_refinement_leaves_other_statuses_alone() -> None:
    """A hook that narrows 422 must not start reclassifying everything else."""
    client = _brave_probe_client(lambda request: httpx.Response(503, text="upstream"))
    try:
        with pytest.raises(ProviderUnavailable):
            await client.verify_credentials()
    finally:
        await client.aclose()


async def test_the_brave_credential_probe_never_asks_for_results() -> None:
    """The safety property the probe rests on.

    Brave bills successful requests, so a probe that could succeed would be a
    probe that spends.  Sending no ``q`` makes success impossible rather than
    merely unlikely.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(422, json=BRAVE_MISSING_QUERY_BODY)

    client = _brave_probe_client(handler)
    try:
        await client.verify_credentials()
    finally:
        await client.aclose()

    assert len(seen) == 1, "a credential probe must make exactly one request"
    assert "q" not in httpx.QueryParams(seen[0].url.query)
    assert seen[0].method == "GET"


async def test_exa_reports_a_rejected_key_as_an_auth_error() -> None:
    """Exa, unlike Brave, does answer 401 -- so no refinement is needed."""
    client = ExaSearchClient(
        _settings(),
        http=_stub_client("exa", lambda request: httpx.Response(401, json=EXA_REJECTED_KEY_BODY)),
    )
    try:
        with pytest.raises(ProviderAuthError):
            await client.verify_credentials()
    finally:
        await client.aclose()


async def test_exa_reads_a_400_about_the_body_as_an_accepted_key() -> None:
    client = ExaSearchClient(
        _settings(),
        http=_stub_client("exa", lambda request: httpx.Response(400, json=EXA_REJECTED_BODY_BODY)),
    )
    try:
        await client.verify_credentials()  # returns
    finally:
        await client.aclose()


async def test_the_exa_credential_probe_sends_no_query_and_is_never_retried() -> None:
    """One attempt, empty body.

    ``max_attempts=3`` is passed deliberately: the probe must make one request
    even when the transport would allow more, because a retried POST against a
    metered provider is the shape this codebase refuses everywhere else.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(400, json=EXA_REJECTED_BODY_BODY)

    client = ExaSearchClient(_settings(), http=_stub_client("exa", handler, max_attempts=3))
    try:
        await client.verify_credentials()
    finally:
        await client.aclose()

    assert len(seen) == 1, "a credential probe must never be retried"
    assert seen[0].method == "POST"
    assert json.loads(seen[0].content) == {}
