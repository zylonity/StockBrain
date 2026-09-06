"""Per-provider cost models, request shapes and cadence floors.

These are the numbers the whole cost control rests on, so they are asserted
against each provider's *published* billing model rather than against the
implementation.  Verified 2026-09-05:

* Brave -- ``$5 / 1,000 requests``; ``count`` is per request and does **not**
  multiply the price; only successful requests are billed
  (<https://brave.com/search/api/>, ``/documentation/guides/rate-limiting``)
* Exa -- ``$7 / 1,000 requests`` for up to 10 results, ``$1 / 1,000`` per result
  above 10, contents ``$1 / 1,000 pages per content type``
  (<https://exa.ai/docs/reference/pricing>)
* Firecrawl -- search 2 credits per 10 results rounded up per 10, scrape 1
  credit per page, charged even when the target errors
  (<https://docs.firecrawl.dev/billing>)

The Firecrawl search estimator is still exercised here even though nothing
schedules a search any more: it is what priced the historical ledger rows, and
the incident it reconstructs is the reason every other assertion in this file
exists.
"""

from __future__ import annotations

import pytest

from stockbrain.config import Settings
from stockbrain.enums import WebDiscoveryKind
from stockbrain.ingestion.brave import BraveSearchClient, brave_freshness_token, trim_brave_query
from stockbrain.ingestion.exa import EXA_INCLUDED_RESULTS, ExaSearchClient
from stockbrain.ingestion.provider_budget import (
    estimate_firecrawl_scrape_credits,
    estimate_firecrawl_search_credits,
)
from stockbrain.ingestion.web_search import WebSearchQuery

#: Verified prices, in dollars per request.  Named here rather than inlined so a
#: price change is one edit and shows up in a diff as a price change.
BRAVE_USD_PER_REQUEST = 0.005
EXA_USD_PER_REQUEST = 0.007


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "web_auth_enabled": False,
        "brave_api_key": "brv-test",
        "exa_api_key": "exa-test",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The Firecrawl cost model -- kept because it is the incident's arithmetic
# ---------------------------------------------------------------------------
def test_one_ten_result_block_costs_two_credits() -> None:
    """The base unit of Firecrawl's published model."""
    assert estimate_firecrawl_search_credits(result_limit=10, source_count=1) == 2
    assert estimate_firecrawl_search_credits(result_limit=5, source_count=2) == 2


def test_the_firecrawl_limit_is_per_source_so_two_sources_double_the_cost() -> None:
    """The subtlety that made Phase 2's cost estimate wrong by 2x before
    ``scrapeOptions`` even entered into it.

    ``limit=10`` with ``web`` and ``news`` is twenty billed results, not ten.
    Brave's ``count`` deliberately does *not* work this way, which is asserted
    separately below.
    """
    assert estimate_firecrawl_search_credits(result_limit=10, source_count=2) == 4
    assert estimate_firecrawl_search_credits(result_limit=10, source_count=3) == 6


def test_a_partial_block_rounds_up() -> None:
    """Eleven results is four credits, per the documentation's own example.

    Rounding down would let the cap be crossed by a whole block.
    """
    assert estimate_firecrawl_search_credits(result_limit=11, source_count=1) == 4
    assert estimate_firecrawl_search_credits(result_limit=1, source_count=1) == 2


def test_a_search_never_estimates_zero() -> None:
    """A request that was processed was billed, whatever it returned."""
    assert estimate_firecrawl_search_credits(result_limit=0, source_count=0) == 2


def test_a_scrape_is_one_credit_per_page() -> None:
    assert estimate_firecrawl_scrape_credits() == 1
    assert estimate_firecrawl_scrape_credits(pages=1) == 1
    assert estimate_firecrawl_scrape_credits(pages=7) == 7


def test_the_phase_2_configuration_is_reconstructed_as_24_credits() -> None:
    """The incident, priced.

    Nine enabled queries on 20- and 30-minute intervals is 21 searches an hour.
    Each one asked for ``limit=10`` across two sources -- twenty billed results,
    4 credits -- *and* carried ``scrapeOptions``, adding one credit for every
    one of those twenty pages. 24 credits a search, 504 credits an hour, roughly
    12,000 a day against a 1,000-credit monthly allowance.
    """
    search = estimate_firecrawl_search_credits(result_limit=10, source_count=2)
    scrapes = estimate_firecrawl_scrape_credits(pages=20)
    assert search == 4
    assert scrapes == 20
    assert search + scrapes == 24
    assert 21 * (search + scrapes) == 504


# ---------------------------------------------------------------------------
# The new cost model, from the defaults an operator actually gets
# ---------------------------------------------------------------------------
def test_the_default_brave_budget_stays_inside_the_free_monthly_credit() -> None:
    """$5 of monthly credit buys about 1,000 requests at $5 per 1,000.

    The cap is set well under that rather than at it: the point of a margin is
    that a manual search, a re-run or an estimate being wrong does not turn into
    a bill.
    """
    settings = _settings()
    assert settings.brave_max_searches_per_day == 12
    assert settings.brave_max_searches_per_month == 320
    monthly_cost = settings.brave_max_searches_per_month * BRAVE_USD_PER_REQUEST
    assert monthly_cost == pytest.approx(1.60)
    assert monthly_cost < 5.00

    # The monthly cap is the binding one, which is the whole reason it exists:
    # a daily cap alone cannot protect a monthly allowance.
    assert settings.brave_max_searches_per_day * 31 > settings.brave_max_searches_per_month


def test_the_default_exa_budget_stays_far_inside_the_free_monthly_credit() -> None:
    """$10 a month of free credit at $7 per 1,000 requests.

    A semantic search costs 1.4x a Brave one and answers a question whose answer
    moves over weeks, so the cap is a handful a day rather than a handful an
    hour.
    """
    settings = _settings()
    assert settings.exa_max_searches_per_day == 3
    monthly_cost = settings.exa_max_searches_per_month * EXA_USD_PER_REQUEST
    assert monthly_cost == pytest.approx(0.49)
    assert monthly_cost < 10.00
    assert settings.exa_max_searches_per_day * 31 > settings.exa_max_searches_per_month


def test_the_combined_default_ceiling_is_under_three_dollars_a_month() -> None:
    """Every metered search this deployment can make, priced at the cap.

    Firecrawl is absent from the sum on purpose: it bills credits against a
    monthly allowance rather than dollars per call, so adding a dollar figure
    for it would be inventing one.
    """
    settings = _settings()
    ceiling = (
        settings.brave_max_searches_per_month * BRAVE_USD_PER_REQUEST
        + settings.exa_max_searches_per_month * EXA_USD_PER_REQUEST
    )
    assert ceiling == pytest.approx(2.09)
    # And both free allowances together are $15 a month.
    assert ceiling < 15.00


def test_the_default_firecrawl_fallback_budget_fits_a_1000_credit_allowance() -> None:
    """One credit per fallback page, and the fallback is rare by construction."""
    settings = _settings()
    daily = settings.firecrawl_max_scrapes_per_day * estimate_firecrawl_scrape_credits()
    assert daily <= settings.firecrawl_daily_credit_cap
    # The daily ceiling taken every day for a long month exceeds the monthly
    # ceiling -- which is the constraint a daily cap alone cannot hold.
    assert settings.firecrawl_daily_credit_cap * 31 > settings.firecrawl_monthly_credit_cap
    assert settings.firecrawl_monthly_credit_cap <= 1000


# ---------------------------------------------------------------------------
# Request shapes
# ---------------------------------------------------------------------------
def test_a_brave_search_never_fetches_pages() -> None:
    """Brave has no scrape option at all, and the request must not grow one.

    This is the assertion that replaces Phase 2's most expensive line. The
    equivalent trap on Exa is ``contents``, asserted next.
    """
    client = BraveSearchClient(_settings())
    params = client.build_params(WebSearchQuery(query="datacentre power", limit=10))
    assert "scrapeOptions" not in params
    assert params["count"] == 10
    assert params["result_filter"] == "web,news"


def test_brave_count_is_per_request_not_per_source() -> None:
    """One request is one billable request however many clusters come back.

    ``result_filter=web,news`` returns both, which is why the separate
    ``/res/v1/news/search`` endpoint -- a second billable request -- is not
    used.
    """
    client = BraveSearchClient(_settings())
    both = client.build_params(WebSearchQuery(query="q", limit=10))
    assert both["result_filter"] == "web,news"
    assert both["count"] == 10
    # Documented maximum 20, and it applies to web results only.
    assert client.build_params(WebSearchQuery(query="q", limit=99))["count"] == 20


def test_a_brave_query_is_trimmed_to_the_documented_maximum() -> None:
    """400 characters and 50 words.

    Trimmed rather than rejected: a query one word too long should return
    slightly less, not nothing.
    """
    assert len(trim_brave_query("word " * 200).split()) == 50
    assert len(trim_brave_query("x" * 900)) <= 400


def test_brave_freshness_rounds_up_to_the_next_documented_bucket() -> None:
    """``pd`` / ``pw`` / ``pm`` / ``py`` are the only accepted tokens.

    Eight days rounds to ``pm``, not ``pw``: a superset is discarded for free by
    the classifier, and a subset silently drops the eighth day.
    """
    assert brave_freshness_token(1) == "pd"
    assert brave_freshness_token(7) == "pw"
    assert brave_freshness_token(8) == "pm"
    assert brave_freshness_token(31) == "pm"
    assert brave_freshness_token(365) == "py"
    assert brave_freshness_token(None) is None


def test_an_exa_search_asks_for_no_contents_by_default() -> None:
    """Exa's ``contents`` is Firecrawl's ``scrapeOptions`` under another name.

    Off by default and never set from a schedule: extraction happens after
    triage, locally, and is free.
    """
    client = ExaSearchClient(_settings())
    body = client.build_request(
        WebSearchQuery(query="who benefits", kind=WebDiscoveryKind.SEMANTIC, limit=10)
    )
    assert "contents" not in body
    assert body["type"] == "auto"
    assert body["numResults"] == EXA_INCLUDED_RESULTS


def test_exa_contents_are_available_but_must_be_asked_for() -> None:
    """Kept as an explicit opt-in rather than removed.

    A one-off diagnostic search may legitimately want text; what must not happen
    is a *scheduler* asking for it without saying so. Only plain text is ever
    requested -- ``summary`` is an extra $1/1k pages of LLM output for a job
    DeepSeek already does under StockBrain's own budget.
    """
    client = ExaSearchClient(_settings(exa_fetch_contents=True))
    body = client.build_request(WebSearchQuery(query="q", kind=WebDiscoveryKind.SEMANTIC, limit=5))
    assert body["contents"] == {"text": True}
    assert "summary" not in str(body)


def test_an_exa_deep_mode_is_never_the_default() -> None:
    """``deep`` and ``deep-reasoning`` are $12-15 per 1,000: a research tool,
    not a discovery sweep."""
    assert _settings().exa_search_type == "auto"


def test_a_query_longer_than_the_tightest_provider_limit_is_refused() -> None:
    """400 characters is Brave's ceiling and therefore the shared model's.

    Caught where the query is constructed rather than at the point of spending
    money, so a query too long for one backend cannot be stored, pass
    validation, and then fail on a billable call.
    """
    with pytest.raises(ValueError, match="at most 400"):
        WebSearchQuery(query="x" * 401)


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------
def test_a_query_cannot_ask_for_a_faster_cadence_than_its_floor() -> None:
    """A restored backup or a hand-written UPDATE cannot reintroduce 20 minutes.

    The floor is applied where the interval is *used*, not where it is edited,
    so there is no path that bypasses it.
    """
    from stockbrain.jobs.handlers import effective_query_interval_minutes

    settings = _settings()
    assert settings.web_discovery_min_query_interval_minutes == 360
    routine = WebDiscoveryKind.ROUTINE
    # The Phase 2 cadences, all clamped.
    assert effective_query_interval_minutes(20, routine, settings) == 360
    assert effective_query_interval_minutes(30, routine, settings) == 360
    assert effective_query_interval_minutes(60, routine, settings) == 360
    # A slower request is honoured: the floor is a floor, not a target.
    assert effective_query_interval_minutes(1440, routine, settings) == 1440


def test_the_semantic_floor_is_higher_than_the_routine_one() -> None:
    """Ten times the price for an answer that moves over weeks.

    Running a semantic query on the routine cadence is the single most
    expensive misconfiguration available here, so the floor is a property of the
    kind rather than of the row.
    """
    from stockbrain.jobs.handlers import effective_query_interval_minutes

    settings = _settings()
    assert (
        settings.web_discovery_min_semantic_interval_minutes
        > settings.web_discovery_min_query_interval_minutes
    )
    assert effective_query_interval_minutes(60, WebDiscoveryKind.SEMANTIC, settings) == 1440


# ---------------------------------------------------------------------------
# Configuration posture
# ---------------------------------------------------------------------------
def test_a_provider_without_a_key_is_disabled_and_says_why() -> None:
    """Missing credentials disable one provider and nothing else.

    "Available" is defined as "no blockers remain", so a panel can never show a
    blocker beside a green light.
    """
    settings = Settings(app_env="test", web_auth_enabled=False)
    assert not settings.brave_available
    assert any("BRAVE_API_KEY is not set" in b for b in settings.brave_blockers)
    assert not settings.exa_available
    assert any("EXA_API_KEY is not set" in b for b in settings.exa_blockers)
    # And local extraction, which needs no credential, keeps working.
    assert settings.content_extraction_available


def test_firecrawl_is_disabled_by_default_and_needs_two_switches() -> None:
    """The one provider that can spend real money with no human in the loop.

    "The credential exists" and "spend it on this page" stay two decisions, so
    a key landing in ``.env`` does not by itself enable paid fetching.
    """
    settings = _settings(firecrawl_api_key="fc-test")
    assert settings.firecrawl_enabled is False
    assert not settings.firecrawl_available
    assert any("FIRECRAWL_ENABLED is false" in b for b in settings.firecrawl_blockers)

    with_switch = _settings(firecrawl_api_key="fc-test", firecrawl_enabled=True)
    assert not with_switch.firecrawl_available
    assert any(
        "FIRECRAWL_FALLBACK_EXTRACTION_ENABLED is false" in b
        for b in with_switch.firecrawl_blockers
    )

    both = _settings(
        firecrawl_api_key="fc-test",
        firecrawl_enabled=True,
        firecrawl_fallback_extraction_enabled=True,
    )
    assert both.firecrawl_available


def test_the_paid_fallback_cannot_become_the_primary_extractor() -> None:
    """With local extraction off, a "fallback" is not a fallback.

    Refused at startup rather than accepted and then discovered on the bill.
    """
    with pytest.raises(ValueError, match="requires CONTENT_EXTRACTION_ENABLED"):
        _settings(
            content_extraction_enabled=False,
            firecrawl_enabled=True,
            firecrawl_fallback_extraction_enabled=True,
            firecrawl_api_key="fc-test",
        )


def test_a_monthly_cap_below_the_daily_cap_is_refused() -> None:
    """Two limits that disagree present as "nothing happens", not as an error."""
    with pytest.raises(ValueError, match="BRAVE_MAX_SEARCHES_PER_MONTH must be"):
        _settings(brave_max_searches_per_day=100, brave_max_searches_per_month=50)
    with pytest.raises(ValueError, match="EXA_MAX_SEARCHES_PER_MONTH must be"):
        _settings(exa_max_searches_per_day=100, exa_max_searches_per_month=50)
    with pytest.raises(ValueError, match="FIRECRAWL_MONTHLY_CREDIT_CAP must be"):
        _settings(firecrawl_daily_credit_cap=100, firecrawl_monthly_credit_cap=50)


def test_the_providers_cannot_be_swapped_onto_each_other_by_configuration() -> None:
    """Pointing routine traffic at Exa is a 10x bill for a worse answer.

    Refused rather than permitted, because the mistake is silent: it looks like
    working discovery until the invoice arrives.
    """
    with pytest.raises(ValueError, match="ROUTINE_PROVIDER=exa"):
        _settings(web_discovery_routine_provider="exa")
    with pytest.raises(ValueError, match="SEMANTIC_PROVIDER=brave"):
        _settings(web_discovery_semantic_provider="brave")


def test_an_unknown_brave_result_filter_is_refused_before_a_request_is_made() -> None:
    """The API would reject it after processing the request."""
    with pytest.raises(ValueError, match="documents only"):
        _settings(brave_result_filter="web,podcasts")


def test_a_duplicate_result_filter_is_dropped() -> None:
    assert _settings(brave_result_filter="web,web,news").brave_result_filter == ["web", "news"]
