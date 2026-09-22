"""The news watermark must only ever move forward.

The stream, backfill and disclosure feeds do not deliver in published order,
so an older article can arrive after a newer one.  A watermark that moved
backwards would make the next restart re-backfill an already-covered window.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.models.system import AppSetting
from stockbrain.db.session import Database
from stockbrain.observability.health import ProviderHealthRegistry
from stockbrain.services import NEWS_WATERMARK_KEY, ServiceContainer

pytestmark = pytest.mark.integration


def _settings(database: Database, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "test",
        "log_level": "CRITICAL",
        "web_auth_enabled": False,
        "database_url": database.engine.url.render_as_string(hide_password=False),
        "alpaca_news_enabled": False,
        "sec_enabled": False,
        "t212_metadata_enabled": False,
        "research_enabled": False,
        "classifier_enabled": False,
        "proposals_enabled": False,
        "content_extraction_enabled": False,
        "memory_grade_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


def _container(database: Database) -> ServiceContainer:
    return ServiceContainer(
        settings=_settings(database),
        database=database,
        health=ProviderHealthRegistry(),
    )


async def _stored_watermark(database: Database) -> dt.datetime:
    async with database.session() as session:
        row = await session.get(AppSetting, NEWS_WATERMARK_KEY)
    assert row is not None
    parsed = dt.datetime.fromisoformat(str(row.value["last_seen_at"]))
    assert parsed.tzinfo is not None
    return parsed


async def test_an_older_article_does_not_regress_the_watermark(clean_tables: Database) -> None:
    container = _container(clean_tables)
    later = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.UTC)
    earlier = dt.datetime(2026, 9, 22, 11, 0, tzinfo=dt.UTC)

    await container._record_news_watermark(later)
    assert await _stored_watermark(clean_tables) == later

    await container._record_news_watermark(earlier)
    assert await _stored_watermark(clean_tables) == later


async def test_a_newer_article_advances_the_watermark(clean_tables: Database) -> None:
    container = _container(clean_tables)
    earlier = dt.datetime(2026, 9, 22, 11, 0, tzinfo=dt.UTC)
    later = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.UTC)

    await container._record_news_watermark(earlier)
    await container._record_news_watermark(later)

    assert await _stored_watermark(clean_tables) == later


async def test_the_first_write_creates_the_row(clean_tables: Database) -> None:
    async with clean_tables.session() as session:
        existing = await session.get(AppSetting, NEWS_WATERMARK_KEY)
    assert existing is None

    container = _container(clean_tables)
    seen = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.UTC)
    await container._record_news_watermark(seen)

    async with clean_tables.session() as session:
        row = await session.execute(
            sa.select(AppSetting).where(AppSetting.key == NEWS_WATERMARK_KEY)
        )
        created = row.scalar_one()
    assert dt.datetime.fromisoformat(str(created.value["last_seen_at"])) == seen
