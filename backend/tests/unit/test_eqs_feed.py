"""The EQS homepage parser against the committed fixture."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.disclosure_feeds import is_boilerplate
from stockbrain.ingestion.eqs import EQSFeed

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "disclosure_feeds"
HOME = FIXTURES / "eqs_home.html"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"app_env": "test", "web_auth_enabled": False}
    base.update(overrides)
    return Settings(**base)


def _items() -> list[Any]:
    return EQSFeed(_settings()).parse_page(HOME.read_text(encoding="utf-8"))


def test_homepage_yields_thirty_releases() -> None:
    assert len(_items()) == 30


def test_first_row_fields() -> None:
    first = _items()[0]
    assert first.provider is SourceProvider.EQS
    assert first.release_id == "a0a61b7f-2644-4343-bc8b-c5d1eaa908cc"
    assert first.language == "en"
    assert first.url.endswith(
        "/first-commercial-scale-production-of-vulcans-proprietary-vulsorb-"
        "lithium-extraction-material/a0a61b7f-2644-4343-bc8b-c5d1eaa908cc_en"
    )
    assert first.headline.startswith("First commercial scale production of Vulcan")
    # The page prints "12:33" in Europe/Berlin local time (CEST, UTC+2).
    assert first.published_at == dt.datetime(2026, 9, 21, 10, 33, tzinfo=dt.UTC)
    assert first.company_name == "Vulcan Energy Resources Limited"
    assert first.isin == "AU0000066086"
    assert first.category == "corporate"
    assert first.exchange_hint is None


def test_last_row_fields() -> None:
    last = _items()[-1]
    assert last.release_id == "f1a7a533-7e59-4d93-b645-d9fd6c2b3824"
    assert last.language == "de"
    assert last.headline.startswith("Comarch im IDC MarketScape 2026")
    assert last.published_at == dt.datetime(2026, 9, 21, 7, 45, tzinfo=dt.UTC)
    assert last.company_name == "Comarch"
    # ``noisin084340`` is EQS's placeholder, not an ISIN.
    assert last.isin is None


def test_the_directors_category_comes_from_the_url_slug() -> None:
    """The fixture's attribute is malformed, so the URL path is authoritative."""
    assert _items()[16].category == "directors-dealings"


def test_boilerplate_count_matches_the_fixture() -> None:
    items = _items()
    assert sum(1 for item in items if is_boilerplate(item)) == 10


def test_the_homepage_has_no_pagination() -> None:
    feed = EQSFeed(_settings())
    assert feed.max_pages == 1
    assert feed.allow_empty is False


def test_a_restyled_page_raises_provider_response_error() -> None:
    with pytest.raises(ProviderResponseError):
        EQSFeed(_settings()).parse_page("<html><body><p>no feed here</p></body></html>")
