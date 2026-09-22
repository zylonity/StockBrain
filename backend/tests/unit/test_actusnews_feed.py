"""The Actusnews Wire English RSS parser against the committed fixture."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.actusnews import ActusNewsFeed
from stockbrain.ingestion.disclosure_feeds import is_boilerplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "disclosure_feeds"
EN_FEED = FIXTURES / "actusnews_en.xml"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"app_env": "test", "web_auth_enabled": False}
    base.update(overrides)
    return Settings(**base)


def _items() -> list[Any]:
    return ActusNewsFeed(_settings()).parse_page(EN_FEED.read_text(encoding="utf-8"))


def test_feed_yields_twenty_releases() -> None:
    assert len(_items()) == 20


def test_first_row_fields() -> None:
    first = _items()[0]
    assert first.provider is SourceProvider.ACTUSNEWS
    assert first.release_id == (
        "2026/09/18/artprice-news-a-world-book-becomes-a-mirror-for-artificial-intelligence"
    )
    assert first.language == "en"
    assert first.url.endswith(
        "/en/artmarket/pr/2026/09/18/"
        "artprice-news-a-world-book-becomes-a-mirror-for-artificial-intelligence"
    )
    assert first.headline == (
        "ARTPRICE NEWS: A WORLD-BOOK BECOMES A MIRROR FOR ARTIFICIAL INTELLIGENCE"
    )
    assert first.company_name == "ARTMARKET.COM"
    assert first.published_at == dt.datetime(2026, 9, 18, 16, 45, tzinfo=dt.UTC)
    assert first.category == "Actusnews"
    assert first.exchange_hint is None


def test_last_row_fields() -> None:
    last = _items()[-1]
    assert last.release_id == (
        "2026/09/14/wavestone-continues-its-expansion-in-the-united-states-"
        "with-the-acquisition-of-sand-cherry"
    )
    assert last.headline == (
        "Wavestone continues its expansion in the United States with the acquisition of Sand Cherry"
    )
    assert last.company_name == "WAVESTONE"
    assert last.published_at == dt.datetime(2026, 9, 14, 5, 30, tzinfo=dt.UTC)


def test_a_title_without_the_company_separator_is_kept_whole() -> None:
    items = _items()
    drone = next(item for item in items if item.company_name == "DRONE VOLT")
    assert drone.headline.startswith("DRONE VOLT announces its results")


def test_actusnews_has_no_boilerplate_rules() -> None:
    items = _items()
    assert sum(1 for item in items if is_boilerplate(item)) == 0


def test_an_empty_feed_raises_provider_response_error() -> None:
    with pytest.raises(ProviderResponseError):
        ActusNewsFeed(_settings()).parse_page("<rss><channel></channel></rss>")
