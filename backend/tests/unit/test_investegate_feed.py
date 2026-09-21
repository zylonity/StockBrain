"""The Investegate front-page parser against the committed fixture."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.disclosure_feeds import is_boilerplate
from stockbrain.ingestion.investegate import InvestegateFeed

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "disclosure_feeds"
FRONT_PAGE = FIXTURES / "investegate_front_page.html"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"app_env": "test", "web_auth_enabled": False}
    base.update(overrides)
    return Settings(**base)


def _feed() -> InvestegateFeed:
    return InvestegateFeed(_settings())


def _items() -> list[Any]:
    return _feed().parse_page(FRONT_PAGE.read_text(encoding="utf-8"))


def test_front_page_yields_fifty_announcements() -> None:
    assert len(_items()) == 50


def test_first_row_fields() -> None:
    first = _items()[0]
    assert first.provider is SourceProvider.INVESTEGATE
    assert first.release_id == "9782378"
    assert first.language == "en"
    assert first.url == (
        "https://www.investegate.co.uk/announcement/rns/barclays--barc/"
        "irish-form-38-5-b-dcc-energy-plc/9782378"
    )
    assert first.headline == "Irish Form 38.5 B DCC ENERGY PLC"
    # The page prints "11:46 AM" in Europe/London local time (BST, UTC+1).
    assert first.published_at == dt.datetime(2026, 9, 21, 10, 46, tzinfo=dt.UTC)
    assert first.company_name == "Barclays"
    assert first.ticker == "BARC"
    assert first.category == "RNS"
    assert first.exchange_hint == "London Stock Exchange"


def test_last_row_fields() -> None:
    last = _items()[-1]
    assert last.release_id == "9782195"
    assert last.headline == "TradersYard Shifts Focus to Futures Trading"
    assert last.company_name == "FinanceWire News"
    assert last.ticker == "FNEWS"
    assert last.category == "FNW"
    assert last.published_at == dt.datetime(2026, 9, 21, 9, 0, tzinfo=dt.UTC)


def test_boilerplate_count_matches_the_fixture() -> None:
    items = _items()
    assert sum(1 for item in items if is_boilerplate(item)) == 33


def test_the_adapter_walks_three_pages_by_default() -> None:
    feed = _feed()
    assert feed.max_pages == 3
    assert feed.allow_empty is False


def test_a_restyled_page_raises_provider_response_error() -> None:
    with pytest.raises(ProviderResponseError):
        _feed().parse_page("<html><body><p>the table is gone</p></body></html>")
