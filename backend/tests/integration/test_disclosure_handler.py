"""The disclosure-feed poll handler: known-id stopping, filtering, failure posture."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.models.sources import Source
from stockbrain.db.session import Database
from stockbrain.enums import JobType, ProviderStatus, SourceProvider
from stockbrain.errors import ProviderAuthError, ProviderResponseError
from stockbrain.ingestion.disclosure_feeds import FeedItem
from stockbrain.jobs.disclosure import handle_disclosure_feed_poll
from stockbrain.jobs.registry import HandlerContext
from stockbrain.observability.health import ProviderHealthRegistry, ProviderName
from stockbrain.services import ServiceContainer

pytestmark = pytest.mark.integration


def _settings(database: Database, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "test",
        "log_level": "CRITICAL",
        "web_auth_enabled": False,
        "database_url": database.engine.url.render_as_string(hide_password=False),
        "disclosure_feeds_enabled": False,
        "discovery_enabled": True,
        "alpaca_news_enabled": False,
        "sec_enabled": False,
        "t212_metadata_enabled": False,
        "research_enabled": False,
        "classifier_enabled": False,
        "proposals_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


def _container(database: Database) -> ServiceContainer:
    return ServiceContainer(
        settings=_settings(database), database=database, health=ProviderHealthRegistry()
    )


def _item(
    release_id: str, *, headline: str = "Interim Results", url: str | None = None
) -> FeedItem:
    return FeedItem(
        provider=SourceProvider.INVESTEGATE,
        release_id=release_id,
        language="en",
        url=(
            url
            if url is not None
            else f"https://www.investegate.co.uk/announcement/rns/x/{release_id}"
        ),
        headline=headline,
        published_at=dt.datetime(2026, 9, 21, 11, 0, tzinfo=dt.UTC),
        company_name="Barclays",
        ticker="BARC",
        exchange_hint="London Stock Exchange",
        category="RNS",
    )


class FakeFeed:
    name = "investegate"
    provider = SourceProvider.INVESTEGATE
    native_language = "en"
    max_pages = 1
    allow_empty = False

    def __init__(
        self, pages: dict[int, list[FeedItem]], *, error: Exception | None = None
    ) -> None:
        self._pages = pages
        self._error = error
        self.pages_fetched: list[int] = []
        self.closed = False

    async def fetch_page(self, page: int) -> list[FeedItem]:
        self.pages_fetched.append(page)
        if self._error is not None:
            raise self._error
        return list(self._pages.get(page, []))

    async def aclose(self) -> None:
        self.closed = True


def _context(container: ServiceContainer, feed_name: str = "investegate") -> HandlerContext:
    return HandlerContext(
        job_id=uuid.uuid4(),
        job_type=JobType.DISCLOSURE_FEED_POLL.value,
        payload={"feed": feed_name},
        attempt=1,
        max_attempts=3,
        database=container.database,
        services=container,
    )


async def _count_sources(database: Database) -> int:
    async with database.session() as session:
        return int(
            (await session.execute(sa.select(sa.func.count()).select_from(Source))).scalar_one()
        )


async def test_first_poll_creates_a_source_per_release(clean_tables: Database) -> None:
    container = _container(clean_tables)
    feed = FakeFeed({1: [_item("1"), _item("2")]})
    container.disclosure_feeds = {feed.name: feed}

    await handle_disclosure_feed_poll(_context(container))

    assert await _count_sources(clean_tables) == 2
    assert container.health.get(ProviderName.INVESTEGATE).status is ProviderStatus.HEALTHY


async def test_a_second_poll_stops_at_page_one_and_creates_nothing(
    clean_tables: Database,
) -> None:
    container = _container(clean_tables)
    first = FakeFeed({1: [_item("1"), _item("2")], 2: [_item("3")]})
    container.disclosure_feeds = {first.name: first}
    await handle_disclosure_feed_poll(_context(container))
    assert await _count_sources(clean_tables) == 2

    second = FakeFeed({1: [_item("1"), _item("2")], 2: [_item("3")]})
    container.disclosure_feeds = {second.name: second}
    await handle_disclosure_feed_poll(_context(container))

    assert second.pages_fetched == [1]
    assert await _count_sources(clean_tables) == 2


async def test_boilerplate_is_filtered_before_ingest(clean_tables: Database) -> None:
    container = _container(clean_tables)
    feed = FakeFeed({1: [_item("1", headline="Transaction in Own Shares"), _item("2")]})
    container.disclosure_feeds = {feed.name: feed}

    await handle_disclosure_feed_poll(_context(container))

    assert await _count_sources(clean_tables) == 1


async def test_a_malformed_item_is_skipped_and_counted(clean_tables: Database) -> None:
    container = _container(clean_tables)
    feed = FakeFeed({1: [_item("bad", url=""), _item("good")]})
    container.disclosure_feeds = {feed.name: feed}

    await handle_disclosure_feed_poll(_context(container))

    assert await _count_sources(clean_tables) == 1
    assert container.health.get(ProviderName.INVESTEGATE).metrics["malformed"] == 1


async def test_an_auth_failure_marks_the_feed_down_and_stops(
    clean_tables: Database,
) -> None:
    container = _container(clean_tables)
    feed = FakeFeed({}, error=ProviderAuthError("investegate: HTTP 403"))
    container.disclosure_feeds = {feed.name: feed}

    with pytest.raises(ProviderAuthError):
        await handle_disclosure_feed_poll(_context(container))

    assert feed.pages_fetched == [1]
    assert container.health.get(ProviderName.INVESTEGATE).status is ProviderStatus.DOWN
    assert await _count_sources(clean_tables) == 0


async def test_an_empty_page_marks_the_feed_degraded(clean_tables: Database) -> None:
    container = _container(clean_tables)
    feed = FakeFeed({1: []})
    container.disclosure_feeds = {feed.name: feed}

    with pytest.raises(ProviderResponseError):
        await handle_disclosure_feed_poll(_context(container))

    assert container.health.get(ProviderName.INVESTEGATE).status is ProviderStatus.DEGRADED


async def test_an_empty_inside_information_channel_is_a_healthy_no_op(
    clean_tables: Database,
) -> None:
    container = _container(clean_tables)
    feed = FakeFeed({1: []})
    feed.allow_empty = True
    container.disclosure_feeds = {feed.name: feed}

    await handle_disclosure_feed_poll(_context(container))

    assert await _count_sources(clean_tables) == 0
    assert container.health.get(ProviderName.INVESTEGATE).status is ProviderStatus.HEALTHY


async def test_an_unconfigured_feed_raises(clean_tables: Database) -> None:
    container = _container(clean_tables)
    with pytest.raises(RuntimeError):
        await handle_disclosure_feed_poll(_context(container, "nope"))
