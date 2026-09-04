"""Provider payload parsing.

Each test asserts against the shape the provider's *current* documentation
specifies. Where StockBrain's own spec differed from those docs, the test
encodes the documented behaviour and says so.
"""

from __future__ import annotations

import datetime as dt

import pytest

from stockbrain.config import Settings
from stockbrain.enums import SourceCategory, SourceProvider
from stockbrain.errors import (
    ProviderAuthError,
    ProviderEntitlementError,
    ProviderResponseError,
    ProviderUnavailable,
)
from stockbrain.httpclient import RateLimitSnapshot, TokenBucket
from stockbrain.ingestion.alpaca_news import AlpacaNewsClient, parse_news_item
from stockbrain.ingestion.base import DiscoveryQuerySpec
from stockbrain.ingestion.firecrawl import FirecrawlClient
from stockbrain.ingestion.sec_edgar import SecEdgarClient, normalize_cik, parse_recent_filings

# ---------------------------------------------------------------------------
# Alpaca
# ---------------------------------------------------------------------------

ALPACA_ARTICLE = {
    "T": "n",
    "id": 39987654,
    "headline": "Vertiv Wins Data Centre Cooling Contract",
    "summary": "Summary sentence.",
    "author": "Benzinga Newsdesk",
    "created_at": "2026-09-04T12:30:00Z",
    "updated_at": "2026-09-04T12:35:00Z",
    "content": "<p>Full article <b>body</b>.</p>",
    "url": "https://www.benzinga.com/news/26/09/39987654/vertiv?utm_campaign=feed",
    "symbols": ["vrt", "  nvda "],
    "source": "benzinga",
}


def test_alpaca_article_maps_every_documented_field() -> None:
    document = parse_news_item(ALPACA_ARTICLE)

    assert document.provider is SourceProvider.ALPACA
    assert document.provider_item_id == "39987654"
    assert document.headline == "Vertiv Wins Data Centre Cooling Contract"
    assert document.author == "Benzinga Newsdesk"
    assert document.body == "<p>Full article <b>body</b>.</p>"
    assert document.published_at == dt.datetime(2026, 9, 4, 12, 30, tzinfo=dt.UTC)
    assert document.updated_at_source == dt.datetime(2026, 9, 4, 12, 35, tzinfo=dt.UTC)
    # `symbols` and `source` are in the current docs but absent from the
    # StockBrain spec's field list; both are captured.
    assert document.symbols == ["VRT", "NVDA"]
    assert document.source_name == "benzinga"
    assert document.source_category is SourceCategory.NEWSWIRE
    assert document.raw_payload == ALPACA_ARTICLE


def test_alpaca_falls_back_to_summary_when_content_is_absent() -> None:
    payload = {**ALPACA_ARTICLE}
    del payload["content"]
    assert parse_news_item(payload).body == "Summary sentence."


def test_alpaca_naive_timestamps_are_treated_as_utc() -> None:
    payload = {**ALPACA_ARTICLE, "created_at": "2026-09-04T12:30:00"}
    published = parse_news_item(payload).published_at
    assert published is not None
    assert published.tzinfo is not None


def test_alpaca_rejects_a_payload_missing_its_identifier() -> None:
    payload = {**ALPACA_ARTICLE}
    del payload["id"]
    with pytest.raises(ProviderResponseError, match="documented schema"):
        parse_news_item(payload)


def test_alpaca_ignores_unknown_fields_rather_than_failing() -> None:
    document = parse_news_item({**ALPACA_ARTICLE, "some_new_field": {"a": 1}})
    assert document.provider_item_id == "39987654"


def test_alpaca_decodes_both_single_and_array_frames() -> None:
    """Alpaca sends arrays of messages; one frame may carry several."""
    assert AlpacaNewsClient._decode_frame('{"T":"success"}') == [{"T": "success"}]
    assert AlpacaNewsClient._decode_frame('[{"T":"a"},{"T":"b"}]') == [{"T": "a"}, {"T": "b"}]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (402, ProviderAuthError),  # auth failed
        (404, ProviderAuthError),  # auth timeout
        (409, ProviderEntitlementError),  # insufficient subscription
        (410, ProviderEntitlementError),
        (500, ProviderUnavailable),
    ],
)
def test_alpaca_stream_errors_are_classified(code: int, expected: type[Exception]) -> None:
    """Credential and entitlement failures must not be retried forever."""
    with pytest.raises(expected):
        AlpacaNewsClient._raise_for_stream_error({"T": "error", "code": code, "msg": "x"})


# ---------------------------------------------------------------------------
# Firecrawl
# ---------------------------------------------------------------------------


def _firecrawl() -> FirecrawlClient:
    return FirecrawlClient(Settings(app_env="test", firecrawl_api_key="fc-test"))


def test_firecrawl_sources_are_objects_not_strings() -> None:
    """Current docs specify ``[{"type": "web"}]``.

    StockBrain's spec showed ``["web", "news"]``, which the API does not accept.
    """
    body = _firecrawl().build_request(DiscoveryQuerySpec(query="q", limit=5))
    assert body["sources"] == [{"type": "web"}, {"type": "news"}]


def test_firecrawl_query_is_truncated_to_the_documented_maximum() -> None:
    body = _firecrawl().build_request(DiscoveryQuerySpec(query="x" * 900))
    assert len(body["query"]) == 500


def test_firecrawl_limit_is_clamped_to_the_documented_range() -> None:
    client = _firecrawl()
    assert client.build_request(DiscoveryQuerySpec(query="q", limit=999))["limit"] == 100
    assert client.build_request(DiscoveryQuerySpec(query="q", limit=0))["limit"] == 1


def test_firecrawl_parses_web_and_news_result_shapes_separately() -> None:
    """Web results carry ``description``; news results carry ``snippet``/``date``."""
    client = _firecrawl()
    spec = DiscoveryQuerySpec(query="datacentre power")
    documents = client.parse_response(
        {
            "success": True,
            "creditsUsed": 3,
            "data": {
                "web": [
                    {
                        "url": "https://reuters.com/a",
                        "title": "Web title",
                        "description": "Web description",
                        "markdown": "# Scraped body",
                    }
                ],
                "news": [
                    {
                        "url": "https://cnbc.com/b",
                        "title": "News title",
                        "snippet": "News snippet",
                        "date": "2026-09-01T00:00:00Z",
                    }
                ],
            },
        },
        spec,
    )

    assert len(documents) == 2
    web, news = documents
    assert web.body == "# Scraped body"
    assert web.metadata["firecrawl_source_type"] == "web"
    assert news.body == "News snippet"
    assert news.published_at == dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    assert client.last_credits_used == 3


def test_firecrawl_drops_results_without_a_url() -> None:
    """No URL means no provenance and no dedupe identity."""
    documents = _firecrawl().parse_response(
        {"success": True, "data": {"web": [{"title": "no url"}]}},
        DiscoveryQuerySpec(query="q"),
    )
    assert documents == []


def test_firecrawl_reports_an_explicit_failure() -> None:
    with pytest.raises(ProviderResponseError):
        _firecrawl().parse_response(
            {"success": False, "warning": "quota"}, DiscoveryQuerySpec(query="q")
        )


def test_firecrawl_rejects_a_response_without_data() -> None:
    with pytest.raises(ProviderResponseError):
        _firecrawl().parse_response({"success": True}, DiscoveryQuerySpec(query="q"))


# ---------------------------------------------------------------------------
# SEC EDGAR
# ---------------------------------------------------------------------------

SEC_SUBMISSIONS = {
    "name": "APPLE INC",
    "tickers": ["AAPL"],
    "sic": "3571",
    "sicDescription": "Electronic Computers",
    "exchanges": ["Nasdaq"],
    "filings": {
        "recent": {
            "accessionNumber": ["0000320193-26-000010", "0000320193-26-000011"],
            "form": ["8-K", "SD"],
            "filingDate": ["2026-09-03", "2026-09-02"],
            "acceptanceDateTime": ["2026-09-03T16:31:00.000Z", "2026-09-02T09:00:00.000Z"],
            "primaryDocument": ["aapl-8k.htm", "aapl-sd.htm"],
            "primaryDocDescription": ["Results of Operations", "Specialised Disclosure"],
            "items": ["2.02,7.01", ""],
        }
    },
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("320193", "0000320193"), (320193, "0000320193"), ("CIK0000320193", "0000320193")],
)
def test_cik_is_padded_to_ten_digits(raw: str | int, expected: str) -> None:
    assert normalize_cik(raw) == expected


def test_cik_without_digits_is_rejected() -> None:
    with pytest.raises(ValueError, match="not a CIK"):
        normalize_cik("not-a-cik")


def test_columnar_filings_are_flattened_into_aligned_rows() -> None:
    rows = list(parse_recent_filings(SEC_SUBMISSIONS))
    assert len(rows) == 2
    assert rows[0]["form"] == "8-K"
    assert rows[0]["accessionNumber"] == "0000320193-26-000010"
    assert rows[0]["items"] == "2.02,7.01"


def test_truncated_columns_do_not_produce_misaligned_rows() -> None:
    """A short column must shorten the output, never shift fields between filings."""
    payload = {
        "filings": {
            "recent": {
                "accessionNumber": ["a-1", "a-2", "a-3"],
                "form": ["8-K", "10-Q"],
                "filingDate": ["2026-09-03", "2026-09-02", "2026-09-01"],
            }
        }
    }
    rows = list(parse_recent_filings(payload))
    assert len(rows) == 2
    assert rows[1]["accessionNumber"] == "a-2"
    assert rows[1]["form"] == "10-Q"


def test_missing_filings_structure_yields_nothing() -> None:
    assert list(parse_recent_filings({})) == []
    assert list(parse_recent_filings({"filings": {}})) == []


async def test_sec_filings_become_documents_with_the_accession_as_identity() -> None:
    client = SecEdgarClient(Settings(app_env="test", sec_contact_email="me@example.com"))
    try:
        documents = [
            client._filing_document(row, "0000320193", "APPLE INC", ["AAPL"], SEC_SUBMISSIONS)
            for row in parse_recent_filings(SEC_SUBMISSIONS)
        ]
    finally:
        await client.aclose()

    filing = documents[0]
    assert filing.provider is SourceProvider.SEC
    # The accession number is EDGAR's globally unique filing identifier.
    assert filing.provider_item_id == "0000320193-26-000010"
    assert filing.source_category is SourceCategory.REGULATOR
    assert filing.url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000010/aapl-8k.htm"
    )
    assert filing.published_at == dt.datetime(2026, 9, 3, 16, 31, tzinfo=dt.UTC)
    assert filing.symbols == ["AAPL"]
    assert filing.metadata["form"] == "8-K"
    assert filing.metadata["items"] == "2.02,7.01"
    assert "8-K" in (filing.headline or "")


def test_sec_user_agent_carries_a_contact() -> None:
    """EDGAR returns 403 without a descriptive User-Agent."""
    agent = SecEdgarClient.build_user_agent(
        Settings(app_env="test", sec_contact_email="ops@example.com")
    )
    assert "StockBrain" in agent
    assert "ops@example.com" in agent


# ---------------------------------------------------------------------------
# HTTP core
# ---------------------------------------------------------------------------


def test_rate_limit_headers_are_parsed_case_insensitively() -> None:
    snapshot = RateLimitSnapshot.from_headers(
        {
            "X-RateLimit-Limit": "100",
            "x-ratelimit-remaining": "97",
            "X-Ratelimit-Reset": "1757000000",
            "x-ratelimit-period": "1m0s",
            "content-type": "application/json",
        }
    )
    assert snapshot.limit == 100
    assert snapshot.remaining == 97
    assert snapshot.reset == 1757000000
    assert snapshot.period == "1m0s"
    assert "content-type" not in snapshot.raw


def test_rate_limit_parsing_tolerates_garbage() -> None:
    snapshot = RateLimitSnapshot.from_headers({"x-ratelimit-limit": "unlimited"})
    assert snapshot.limit is None


async def test_token_bucket_limits_throughput() -> None:
    import time

    bucket = TokenBucket(rate_per_second=50.0, burst=1)
    started = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    # 1 burst token then 4 refills at 50/s = at least ~80ms.
    assert time.monotonic() - started >= 0.05


def test_token_bucket_rejects_a_nonsensical_rate() -> None:
    with pytest.raises(ValueError, match="positive"):
        TokenBucket(rate_per_second=0)


async def test_sec_filings_are_marked_as_their_own_events() -> None:
    """Templated filing titles must not be grouped; each accession is an event."""
    client = SecEdgarClient(Settings(app_env="test", sec_contact_email="me@example.com"))
    try:
        documents = [
            client._filing_document(row, "0000320193", "APPLE INC", ["AAPL"], SEC_SUBMISSIONS)
            for row in parse_recent_filings(SEC_SUBMISSIONS)
        ]
    finally:
        await client.aclose()

    assert all(document.is_distinct_event for document in documents)


async def test_sec_headlines_distinguish_filings_of_the_same_form() -> None:
    """Two Form 4s on different dates must not render identically in a list."""
    payload = {
        "name": "APPLE INC",
        "filings": {
            "recent": {
                "accessionNumber": ["a-1", "a-2"],
                "form": ["4", "4"],
                "filingDate": ["2026-09-03", "2026-09-01"],
                "primaryDocument": ["x.htm", "y.htm"],
                "primaryDocDescription": ["FORM 4", "FORM 4"],
                "items": ["", ""],
            }
        },
    }
    client = SecEdgarClient(Settings(app_env="test", sec_contact_email="me@example.com"))
    try:
        headlines = [
            client._filing_document(row, "0000320193", "APPLE INC", [], payload).headline
            for row in parse_recent_filings(payload)
        ]
    finally:
        await client.aclose()

    assert headlines[0] != headlines[1]
    assert "2026-09-03" in (headlines[0] or "")
    assert "Form 4" in (headlines[0] or "")


async def test_sec_8k_headline_carries_the_item_numbers() -> None:
    """8-K item numbers say what the filing is actually about."""
    client = SecEdgarClient(Settings(app_env="test", sec_contact_email="me@example.com"))
    try:
        rows = list(parse_recent_filings(SEC_SUBMISSIONS))
        headline = client._filing_document(
            rows[0], "0000320193", "APPLE INC", [], SEC_SUBMISSIONS
        ).headline
    finally:
        await client.aclose()
    assert "2.02,7.01" in (headline or "")
