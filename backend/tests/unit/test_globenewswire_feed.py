"""The GlobeNewswire per-country RSS parser against the committed fixtures."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.disclosure_feeds import group_releases, is_boilerplate
from stockbrain.ingestion.globenewswire import GlobeNewswireFeed

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "disclosure_feeds"
FRANCE = FIXTURES / "globenewswire_france.xml"
NETHERLANDS = FIXTURES / "globenewswire_netherlands.xml"
CANADA = FIXTURES / "globenewswire_canada_valid.xml"
CANADA_404 = FIXTURES / "globenewswire_canada.xml"

SPIE_FRENCH_URL = (
    "https://www.globenewswire.com/news-release/2026/09/21/3365169/0/fr/"
    "spie-annonce-le-lancement-d-une-%C3%A9mission-obligataire-au-format-"
    "sustainability-linked.html"
)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"app_env": "test", "web_auth_enabled": False}
    base.update(overrides)
    return Settings(**base)


def _france_items() -> list[Any]:
    feed = GlobeNewswireFeed(_settings(), country="France")
    return feed.parse_page(FRANCE.read_text(encoding="utf-8"))


def _canada_items() -> list[Any]:
    feed = GlobeNewswireFeed(_settings(), country="Canada")
    return feed.parse_page(CANADA.read_text(encoding="utf-8"))


def test_france_yields_twenty_releases() -> None:
    assert len(_france_items()) == 20


def test_france_first_and_last_rows() -> None:
    first = _france_items()[0]
    assert first.provider is SourceProvider.GLOBENEWSWIRE
    assert first.release_id == "3365280"
    assert first.language == "fr"
    assert first.headline.startswith("FLEURY MICHON : Déclaration des opérations de rachat")
    assert first.published_at == dt.datetime(2026, 9, 21, 9, 42, tzinfo=dt.UTC)
    assert first.company_name == "FLEURY MICHON"

    last = _france_items()[-1]
    assert last.release_id == "3364880"
    assert last.language == "fr"
    assert last.headline.startswith("Succès du programme de rachat")
    assert last.published_at == dt.datetime(2026, 9, 18, 16, 7, tzinfo=dt.UTC)
    assert last.company_name == "Amundi"


def test_france_boilerplate_count_matches_the_fixture() -> None:
    items = _france_items()
    assert sum(1 for item in items if is_boilerplate(item)) == 5


def test_netherlands_yields_twenty_and_filters_none() -> None:
    feed = GlobeNewswireFeed(_settings(), country="Netherlands")
    items = feed.parse_page(NETHERLANDS.read_text(encoding="utf-8"))
    assert len(items) == 20
    assert sum(1 for item in items if is_boilerplate(item)) == 0


def test_grouping_collapses_a_translation_and_keeps_the_other_url() -> None:
    feed = GlobeNewswireFeed(_settings(), country="France")
    items = feed.parse_page(FRANCE.read_text(encoding="utf-8"))
    grouped = group_releases(items, feed.native_language)
    assert len(grouped) == 12
    spie = next(item for item in grouped if item.release_id == "3365169")
    assert spie.language == "en"
    assert spie.alternate_language_urls == {"fr": SPIE_FRENCH_URL}


def test_canada_yields_twenty_releases_and_extracts_tsx_tickers() -> None:
    items = _canada_items()
    assert len(items) == 20
    assert items[0].release_id == "3365350"
    assert items[0].language == "en"
    assert items[0].company_name == "Kruger Products Inc."
    assert items[0].published_at == dt.datetime(2026, 9, 21, 11, 0, tzinfo=dt.UTC)
    assert items[-1].release_id == "3364980"
    assert items[-1].company_name == "Liberty Gold Corp."
    # ``(TSX: CNR)`` on release 3365354 and ``(TSX: MX)`` on release 3365336.
    tickers = {item.release_id: item.ticker for item in items if item.ticker}
    assert tickers == {"3365354": "CNR", "3365336": "MX"}


def test_canada_has_no_boilerplate_and_groups_three_translations() -> None:
    feed = GlobeNewswireFeed(_settings(), country="Canada")
    items = feed.parse_page(CANADA.read_text(encoding="utf-8"))
    assert sum(1 for item in items if is_boilerplate(item)) == 0
    grouped = group_releases(items, feed.native_language)
    assert len(grouped) == 17


def test_the_country_carries_a_native_language() -> None:
    assert GlobeNewswireFeed(_settings(), country="France").native_language == "fr"
    assert GlobeNewswireFeed(_settings(), country="Canada").native_language == "en"


def test_the_committed_canada_404_capture_raises() -> None:
    """A 404 XHTML page is a shape change, not an empty feed."""
    feed = GlobeNewswireFeed(_settings(), country="Canada")
    with pytest.raises(ProviderResponseError):
        feed.parse_page(CANADA_404.read_text(encoding="utf-8"))
