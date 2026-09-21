"""Unit tests for the shared disclosure-feed vocabulary."""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest

from stockbrain.config import Settings
from stockbrain.enums import SourceCategory, SourceProvider
from stockbrain.errors import ProviderUnavailable
from stockbrain.ingestion.disclosure_feeds import (
    FeedHttpClient,
    FeedItem,
    group_releases,
    is_boilerplate,
    to_document,
)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"app_env": "test", "web_auth_enabled": False}
    base.update(overrides)
    return Settings(**base)


def _item(
    *,
    provider: SourceProvider = SourceProvider.INVESTEGATE,
    release_id: str = "9782378",
    language: str = "en",
    url: str = "https://www.investegate.co.uk/announcement/rns/barclays--barc/x/9782378",
    headline: str = "Interim Results",
    published_at: dt.datetime | None = None,
    company_name: str | None = "Barclays",
    ticker: str | None = "BARC",
    isin: str | None = None,
    exchange_hint: str | None = "London Stock Exchange",
    category: str | None = "RNS",
) -> FeedItem:
    return FeedItem(
        provider=provider,
        release_id=release_id,
        language=language,
        url=url,
        headline=headline,
        published_at=published_at or dt.datetime(2026, 9, 21, 11, 46, tzinfo=dt.UTC),
        company_name=company_name,
        ticker=ticker,
        isin=isin,
        exchange_hint=exchange_hint,
        category=category,
        raw={"source": category},
    )


# ---------------------------------------------------------------------------
# to_document
# ---------------------------------------------------------------------------
def test_to_document_maps_provider_identity_and_language() -> None:
    document = to_document(_item())
    assert document.provider is SourceProvider.INVESTEGATE
    assert document.provider_item_id == "9782378"
    assert document.url is not None
    assert document.url.endswith("/9782378")
    assert document.source_name == "Investegate"
    assert document.symbols == ["BARC"]
    assert document.body is None
    assert document.metadata["language"] == "en"
    assert document.metadata["company_name"] == "Barclays"
    assert document.metadata["feed_category"] == "RNS"


def test_regulator_disclosures_are_distinct_events() -> None:
    assert to_document(_item(category="RNS")).source_category is SourceCategory.REGULATOR
    assert to_document(_item(category="RNS")).is_distinct_event is True
    newswire = _item(provider=SourceProvider.GLOBENEWSWIRE, category="Other News")
    assert to_document(newswire).source_category is SourceCategory.ISSUER
    assert to_document(newswire).is_distinct_event is False


def test_an_rns_is_regulator_but_another_investegate_code_is_issuer() -> None:
    assert to_document(_item(category="RNS")).source_category is SourceCategory.REGULATOR
    assert to_document(_item(category="PRN")).source_category is SourceCategory.ISSUER


def test_an_eqs_regulatory_category_is_regulator_but_corporate_is_issuer() -> None:
    regulatory = _item(
        provider=SourceProvider.EQS,
        ticker=None,
        exchange_hint=None,
        category="voting-rights",
        isin="DE0006231004",
    )
    corporate = _item(
        provider=SourceProvider.EQS,
        ticker=None,
        exchange_hint=None,
        category="corporate",
        isin="DE000A1H8BV3",
    )
    assert to_document(regulatory).source_category is SourceCategory.REGULATOR
    assert to_document(regulatory).is_distinct_event is True
    assert to_document(corporate).source_category is SourceCategory.ISSUER
    assert to_document(corporate).is_distinct_event is False


def test_investegate_carries_both_lse_and_aim_as_alternates() -> None:
    metadata = to_document(_item()).metadata
    assert metadata["exchange_hint"] == "London Stock Exchange"
    assert metadata["exchange_hints"] == [
        "London Stock Exchange",
        "London Stock Exchange AIM",
    ]


def test_the_issuer_name_is_prefixed_onto_a_headline_that_omits_it() -> None:
    # "Barclays" names no company in a generic headline: prefixed.
    assert to_document(_item()).headline == "Barclays: Interim Results"
    # The issuer is already in the headline (any case): left alone.
    same_headline = _item(
        company_name="Barclays", headline="Barclays PLC announces interim results"
    )
    assert to_document(same_headline).headline == "Barclays PLC announces interim results"
    # No company name at all: the raw headline is used verbatim.
    assert to_document(_item(company_name=None)).headline == "Interim Results"


def test_a_malformed_item_is_refused_by_mapping() -> None:
    with pytest.raises(ValueError, match="url is not an absolute http"):
        to_document(_item(url=""))
    with pytest.raises(ValueError, match="url is not an absolute http"):
        to_document(_item(url="/announcement/rns/barclays--barc/x/9782378"))
    with pytest.raises(ValueError, match="headline is empty"):
        to_document(_item(headline=""))
    with pytest.raises(ValueError, match="release_id is empty"):
        to_document(_item(release_id=""))
    with pytest.raises(ValueError, match="published_at is naive"):
        to_document(
            _item(published_at=dt.datetime(2026, 9, 21, 11, 46))  # noqa: DTZ001 -- the point
        )


def test_a_non_utc_timestamp_is_normalised_to_utc() -> None:
    eastern = dt.timezone(dt.timedelta(hours=-4))
    item = _item(published_at=dt.datetime(2026, 9, 21, 7, 46, tzinfo=eastern))
    assert to_document(item).published_at == dt.datetime(2026, 9, 21, 11, 46, tzinfo=dt.UTC)


# ---------------------------------------------------------------------------
# is_boilerplate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "headline",
    [
        "Transaction in Own Shares",
        "Holding(s) in Company",
        "Director/PDMR Shareholding",
        "Form 8.3 - DCC Energy plc 210926",
        "Form 38.5a (EPT/RI) DCC Energy plc BNPP FM",
        "Exercise of Warrants",
        "Investor Presentation via Investor Meet Company",
    ],
)
def test_investegate_boilerplate_headlines_are_filtered(headline: str) -> None:
    assert is_boilerplate(_item(headline=headline)) is True


def test_a_substantive_investegate_headline_survives() -> None:
    assert is_boilerplate(_item(headline="Interim Results")) is False
    assert is_boilerplate(_item(headline="Result of Tender Offer")) is False


def test_eqs_category_and_headline_rules_apply() -> None:
    assert is_boilerplate(
        _item(provider=SourceProvider.EQS, category="voting-rights", headline="")
    )
    assert is_boilerplate(
        _item(provider=SourceProvider.EQS, category="directors-dealings", headline="")
    )
    assert is_boilerplate(
        _item(
            provider=SourceProvider.EQS,
            category="other-capital-market-information",
            headline="Release of a capital market information",
            ticker=None,
            exchange_hint=None,
        )
    )
    assert not is_boilerplate(
        _item(provider=SourceProvider.EQS, category="corporate", headline="Interim Results")
    )


def test_cnmv_inside_information_is_never_filtered() -> None:
    item = _item(
        provider=SourceProvider.CNMV,
        category="Información privilegiada",
        headline="Sobre suspensiones, levantamientos y exclusiones de negociación",
        company_name="SOCIETE GENERALE EFFEKTEN GMBH",
        ticker=None,
        exchange_hint="Bolsa de Madrid",
    )
    assert is_boilerplate(item) is False


def test_cnmv_oir_buyback_is_filtered_but_a_real_suspension_is_not() -> None:
    buyback = _item(
        provider=SourceProvider.CNMV,
        category="Otra información relevante",
        headline="Programas de recompra de acciones, estabilización y autocartera",
        company_name="EDREAMS ODIGEO, S.A.",
        ticker=None,
        exchange_hint="Bolsa de Madrid",
    )
    real = _item(
        provider=SourceProvider.CNMV,
        category="Otra información relevante",
        headline="Sobre suspensiones, levantamientos y exclusiones de negociación",
        company_name="ERCROS, S.A. (ERCROS)",
        ticker=None,
        exchange_hint="Bolsa de Madrid",
    )
    warrant_issuer = _item(
        provider=SourceProvider.CNMV,
        category="Otra información relevante",
        headline="Sobre suspensiones, levantamientos y exclusiones de negociación",
        company_name="SOCIETE GENERALE EFFEKTEN GMBH",
        ticker=None,
        exchange_hint="Bolsa de Madrid",
    )
    assert is_boilerplate(buyback) is True
    assert is_boilerplate(real) is False
    assert is_boilerplate(warrant_issuer) is True


def test_globenewswire_boilerplate_survives_typographic_apostrophes() -> None:
    item = _item(
        provider=SourceProvider.GLOBENEWSWIRE,
        headline=(
            "RIBER: INFORMATION MENSUELLE RELATIVE AU NOMBRE TOTAL "
            "D\u2019ACTIONS ET DE DROITS DE VOTE COMPOSANT LE CAPITAL SOCIAL"
        ),
        ticker=None,
        exchange_hint=None,
    )
    assert is_boilerplate(item) is True


def test_actusnews_has_no_boilerplate_rules() -> None:
    item = _item(
        provider=SourceProvider.ACTUSNEWS,
        headline="Number of outstanding shares and voting rights",
        ticker=None,
        exchange_hint=None,
    )
    assert is_boilerplate(item) is False


# ---------------------------------------------------------------------------
# group_releases
# ---------------------------------------------------------------------------
def test_group_releases_prefers_english_and_keeps_the_other_urls() -> None:
    english = _item(language="en", url="https://example.com/en")
    french = _item(language="fr", url="https://example.com/fr")
    grouped = group_releases([french, english])
    assert len(grouped) == 1
    assert grouped[0].language == "en"
    assert grouped[0].url == "https://example.com/en"
    assert grouped[0].alternate_language_urls == {"fr": "https://example.com/fr"}


def test_group_releases_falls_back_to_native_then_first() -> None:
    german = _item(language="de", url="https://example.com/de")
    french = _item(language="fr", url="https://example.com/fr")
    assert group_releases([german, french], "de")[0].language == "de"
    assert group_releases([german, french], "it")[0].language == "de"


def test_group_releases_keeps_distinct_releases_separate() -> None:
    first = _item(release_id="1", language="en")
    second = _item(release_id="2", language="en")
    assert len(group_releases([first, second])) == 2


# ---------------------------------------------------------------------------
# FeedHttpClient
# ---------------------------------------------------------------------------
async def test_feed_http_client_returns_text_and_maps_429_to_unavailable() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "30"}, text="slow down")
        return httpx.Response(200, text="<rss>ok</rss>")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://feeds.example"
    )
    http = FeedHttpClient(
        _settings(), provider="test", base_url="https://feeds.example", language="en", client=client
    )
    try:
        with pytest.raises(ProviderUnavailable):
            await http.get("/feed", language="en")
        # No retry: one request for one poll attempt.
        assert len(calls) == 1
        assert await http.get("/feed", language="en") == "<rss>ok</rss>"
    finally:
        await http.aclose()
    # The injected client belongs to the caller and must not be closed.
    assert client.is_closed is False
    await client.aclose()


async def test_feed_http_client_sends_the_request_language_and_user_agent() -> None:
    """Headers must reach the actual request even through an injected client.

    ``ProviderHttpClient.__init__`` only applies its ``headers=`` kwarg when it
    creates its own ``httpx.AsyncClient``; an injected client (every test's
    only way to control the transport) silently drops them. ``FeedHttpClient``
    must therefore carry its own default headers and pass them on every
    request explicitly, not rely on the client construction path.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["language"] = request.headers.get("accept-language", "")
        seen["user-agent"] = request.headers.get("user-agent", "")
        seen["accept"] = request.headers.get("accept", "")
        return httpx.Response(200, text="ok")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://feeds.example"
    )
    http = FeedHttpClient(
        _settings(), provider="test", base_url="https://feeds.example", language="en", client=client
    )
    try:
        await http.get("/feed", language="de")
    finally:
        await http.aclose()
        await client.aclose()
    assert seen["language"] == "de"
    assert seen["user-agent"] == "StockBrain/0.1.0 (+unset)"
    assert seen["accept"].startswith("text/html")
