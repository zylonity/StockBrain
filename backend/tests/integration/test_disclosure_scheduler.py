"""Scheduling for the disclosure feeds: one task per enabled feed, master gate."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.models.system import Job
from stockbrain.db.session import Database
from stockbrain.enums import JobType
from stockbrain.jobs.scheduler import Scheduler
from stockbrain.observability.health import ProviderHealthRegistry
from stockbrain.services import ServiceContainer

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
        # These two services are built unconditionally and would otherwise add
        # their own scheduled tasks, making the exact task-set assertion flaky.
        "content_extraction_enabled": False,
        "memory_grade_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _container(database: Database, **overrides: Any) -> ServiceContainer:
    return ServiceContainer(
        settings=_settings(database, **overrides),
        database=database,
        health=ProviderHealthRegistry(),
    )


async def _jobs(database: Database) -> list[Job]:
    async with database.session() as session:
        return list(
            (
                await session.execute(
                    sa.select(Job).where(Job.job_type == JobType.DISCLOSURE_FEED_POLL.value)
                )
            ).scalars()
        )


async def test_the_master_flag_off_builds_no_feeds(clean_tables: Database) -> None:
    container = _container(
        clean_tables, disclosure_feeds_enabled=False, investegate_enabled=True
    )
    assert container.disclosure_feeds == {}


async def test_an_enabled_feed_is_constructed_and_closed(clean_tables: Database) -> None:
    container = _container(
        clean_tables, disclosure_feeds_enabled=True, investegate_enabled=True
    )
    try:
        assert set(container.disclosure_feeds) == {"investegate"}
    finally:
        await container.stop()


async def test_one_task_per_enabled_feed(clean_tables: Database) -> None:
    container = _container(
        clean_tables,
        disclosure_feeds_enabled=True,
        investegate_enabled=True,
        eqs_enabled=True,
    )
    try:
        scheduler = Scheduler(clean_tables)
        container._register_schedules(scheduler)
        assert {
            name for name in scheduler._tasks if name.startswith("disclosure_feed:")
        } == {
            "disclosure_feed:investegate",
            "disclosure_feed:eqs",
        }
        assert scheduler._tasks["disclosure_feed:investegate"].interval_seconds == 300.0
        assert scheduler._tasks["disclosure_feed:eqs"].interval_seconds == 300.0
    finally:
        await container.stop()


async def test_a_globe_newswire_country_gets_its_own_task(clean_tables: Database) -> None:
    container = _container(
        clean_tables,
        disclosure_feeds_enabled=True,
        globenewswire_enabled=True,
        globenewswire_countries=["France", "Canada"],
    )
    try:
        scheduler = Scheduler(clean_tables)
        container._register_schedules(scheduler)
        assert {
            "disclosure_feed:globenewswire_france",
            "disclosure_feed:globenewswire_canada",
        } <= set(scheduler._tasks)
    finally:
        await container.stop()


async def test_the_master_flag_off_enqueues_nothing(clean_tables: Database) -> None:
    container = _container(
        clean_tables, disclosure_feeds_enabled=False, investegate_enabled=True
    )
    await container._enqueue_disclosure_feed("investegate")
    assert await _jobs(clean_tables) == []


async def test_enqueue_uses_the_feed_dedupe_key_and_priority(clean_tables: Database) -> None:
    container = _container(
        clean_tables, disclosure_feeds_enabled=True, investegate_enabled=True
    )
    try:
        await container._enqueue_disclosure_feed("investegate")
    finally:
        await container.stop()
    jobs = await _jobs(clean_tables)
    assert len(jobs) == 1
    assert jobs[0].payload == {"feed": "investegate"}
    assert jobs[0].dedupe_key == "feed:investegate"
    assert jobs[0].priority == 50
