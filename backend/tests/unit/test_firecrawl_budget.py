"""Firecrawl cost estimation and the two-stage request shapes.

These are the numbers the whole Phase 9 Firecrawl fix rests on, so they are
asserted against the published billing model rather than against the
implementation:

* ``/v2/search`` -- 2 credits per 10 results, rounded up per 10
* ``limit`` is per **source**, so two sources double the billed results
* ``/v2/scrape`` -- 1 credit per page
* passing ``scrapeOptions`` to a search adds the per-page charge to every result

Verified 2026-09-05 against <https://docs.firecrawl.dev/billing>.
"""

from __future__ import annotations

from stockbrain.config import Settings
from stockbrain.ingestion.base import DiscoveryQuerySpec
from stockbrain.ingestion.firecrawl import FirecrawlClient
from stockbrain.ingestion.firecrawl_budget import (
    estimate_scrape_credits,
    estimate_search_credits,
)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "web_auth_enabled": False,
        "firecrawl_api_key": "fc-test",
        "firecrawl_enabled": True,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------
def test_one_ten_result_block_costs_two_credits() -> None:
    """The base unit of the published model."""
    assert estimate_search_credits(result_limit=10, source_count=1) == 2
    assert estimate_search_credits(result_limit=5, source_count=2) == 2


def test_the_limit_is_per_source_so_two_sources_double_the_cost() -> None:
    """The subtlety that made Phase 2's cost estimate wrong by 2x before
    ``scrapeOptions`` even entered into it.

    ``limit=10`` with ``web`` and ``news`` is twenty billed results, not ten.
    """
    assert estimate_search_credits(result_limit=10, source_count=2) == 4
    assert estimate_search_credits(result_limit=10, source_count=3) == 6


def test_a_partial_block_rounds_up() -> None:
    """Eleven results is four credits, per the documentation's own example.

    Rounding down would let the cap be crossed by a whole block.
    """
    assert estimate_search_credits(result_limit=11, source_count=1) == 4
    assert estimate_search_credits(result_limit=1, source_count=1) == 2


def test_a_search_never_estimates_zero() -> None:
    """A request that was processed was billed, whatever it returned."""
    assert estimate_search_credits(result_limit=0, source_count=0) == 2


def test_a_scrape_is_one_credit_per_page() -> None:
    assert estimate_scrape_credits() == 1
    assert estimate_scrape_credits(pages=1) == 1
    assert estimate_scrape_credits(pages=7) == 7


def test_the_phase_2_configuration_is_reconstructed_as_24_credits() -> None:
    """The incident, priced.

    Nine enabled queries on 20- and 30-minute intervals is 21 searches an hour.
    Each one asked for ``limit=10`` across two sources -- twenty billed results,
    4 credits -- *and* carried ``scrapeOptions``, adding one credit for every
    one of those twenty pages. 24 credits a search, 504 credits an hour, roughly
    12,000 a day against a 1,000-credit monthly allowance.
    """
    search = estimate_search_credits(result_limit=10, source_count=2)
    scrapes = estimate_scrape_credits(pages=20)
    assert search == 4
    assert scrapes == 20
    assert search + scrapes == 24
    assert 21 * (search + scrapes) == 504


def test_the_phase_9_default_configuration_is_two_credits_a_search() -> None:
    """And what it costs now.

    ``limit=5`` across two sources is exactly one billing block, and content is
    fetched separately for the handful of results that survive triage.
    """
    settings = _settings()
    cost = estimate_search_credits(
        result_limit=settings.firecrawl_search_result_limit,
        source_count=len(settings.firecrawl_search_sources),
    )
    assert cost == 2
    daily = settings.firecrawl_max_searches_per_day * cost
    daily += settings.firecrawl_max_scrapes_per_day * estimate_scrape_credits()
    assert daily <= settings.firecrawl_daily_credit_cap
    # And the daily ceiling, taken every day for a long month, stays inside the
    # monthly ceiling -- which is the constraint a daily cap alone cannot hold.
    assert settings.firecrawl_daily_credit_cap * 31 > settings.firecrawl_monthly_credit_cap
    assert settings.firecrawl_monthly_credit_cap <= 1000


# ---------------------------------------------------------------------------
# Request shapes
# ---------------------------------------------------------------------------
def test_a_scheduled_search_carries_no_scrape_options() -> None:
    """The single most expensive line of Phase 2, now absent by default.

    ``DiscoveryQuerySpec.scrape_content`` defaulted to ``True`` and the handler
    never overrode it, so every broad thematic search fetched every result page.
    """
    client = FirecrawlClient(_settings())
    body = client.build_request(DiscoveryQuerySpec(query="datacentre power", limit=5))
    assert "scrapeOptions" not in body
    assert body["limit"] == 5
    assert body["sources"] == [{"type": "web"}, {"type": "news"}]


def test_scrape_options_are_still_available_but_must_be_asked_for() -> None:
    """Kept as an explicit opt-in rather than removed.

    A one-off diagnostic search may legitimately want content; what must not
    happen is a *scheduler* asking for it without saying so.
    """
    client = FirecrawlClient(_settings())
    body = client.build_request(DiscoveryQuerySpec(query="q", limit=2, scrape_content=True))
    assert body["scrapeOptions"] == {
        "formats": [{"type": "markdown"}],
        "onlyMainContent": True,
    }


def test_the_configured_sources_are_what_gets_requested() -> None:
    """A cost knob as much as a coverage one, so it is configuration."""
    client = FirecrawlClient(_settings(firecrawl_search_sources="news"))
    assert client.build_request(DiscoveryQuerySpec(query="q"))["sources"] == [{"type": "news"}]


def test_an_unknown_search_source_is_refused_before_it_can_be_billed() -> None:
    """The API would reject it *after* processing the request.

    Catching it in configuration validation is the difference between a startup
    error and a credit charge for a 400.
    """
    import pytest

    with pytest.raises(ValueError, match="documents only"):
        _settings(firecrawl_search_sources="web,videos")


def test_a_duplicate_source_is_dropped_rather_than_requested_twice() -> None:
    """``web,web`` would be billed twice for one set of results."""
    assert _settings(firecrawl_search_sources="web,web,news").firecrawl_search_sources == [
        "web",
        "news",
    ]


def test_the_scrape_body_asks_for_the_cheapest_documented_shape() -> None:
    """Markdown only.

    ``json`` extraction is documented at +4 credits a page and a screenshot is
    of no use to a text classifier, so neither is ever requested.
    """
    body = FirecrawlClient(_settings()).build_scrape_request("https://example.com/a")
    assert body["formats"] == [{"type": "markdown"}]
    assert body["onlyMainContent"] is True
    assert "json" not in str(body)


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------
def test_a_topic_cannot_ask_for_a_faster_cadence_than_the_floor() -> None:
    """A restored backup or a hand-written UPDATE cannot reintroduce 20 minutes.

    The floor is applied where the interval is *used*, not where it is edited,
    so there is no path that bypasses it.
    """
    from stockbrain.jobs.handlers import effective_topic_interval_minutes

    settings = _settings()
    assert settings.firecrawl_min_topic_interval_minutes == 720
    # The Phase 2 cadences, all clamped.
    assert effective_topic_interval_minutes(20, settings) == 720
    assert effective_topic_interval_minutes(30, settings) == 720
    assert effective_topic_interval_minutes(60, settings) == 720
    # A slower request is honoured: the floor is a floor, not a target.
    assert effective_topic_interval_minutes(1440, settings) == 1440


def test_firecrawl_is_disabled_by_default() -> None:
    """The one provider that can spend real money on a schedule with no human.

    A fresh deployment discovers through Alpaca news and SEC EDGAR; enabling
    Firecrawl is a deliberate act taken after reading the budget.
    """
    settings = Settings(app_env="test", web_auth_enabled=False)
    assert settings.firecrawl_enabled is False
    assert not settings.firecrawl_available
    assert any("FIRECRAWL_ENABLED is false" in blocker for blocker in settings.firecrawl_blockers)


def test_a_credit_cap_below_one_search_is_refused_at_startup() -> None:
    """Two limits that disagree would present as "Firecrawl silently does
    nothing" rather than as a configuration error."""
    import pytest

    with pytest.raises(ValueError, match="below the 2 credits"):
        _settings(firecrawl_daily_credit_cap=1, firecrawl_monthly_credit_cap=1)


def test_a_monthly_cap_below_the_daily_cap_is_refused() -> None:
    import pytest

    with pytest.raises(ValueError, match="MONTHLY_CREDIT_CAP must be"):
        _settings(firecrawl_daily_credit_cap=100, firecrawl_monthly_credit_cap=50)
