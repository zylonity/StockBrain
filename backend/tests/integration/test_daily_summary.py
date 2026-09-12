"""The once-a-day Telegram summary: one enqueue per UTC day, and only when asked.

The summary is rebuilt from state the system already has, so what these tests
assert is not the arithmetic -- it is the trigger's two safety properties: a
60-second tick that fires repeatedly produces one job, and a deployment that did
not configure a time produces none at all.  The "already sent" check reads the
same ``notifications`` row the delivery side claims, so a restart after the
configured time still sends if the day has not been delivered.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.base import utcnow
from stockbrain.db.models.sources import Event
from stockbrain.db.models.system import Job, Notification
from stockbrain.db.session import Database
from stockbrain.enums import JobType, NotificationClass, NotificationStatus
from stockbrain.observability.health import ProviderHealthRegistry
from stockbrain.services import ServiceContainer
from stockbrain.telegram.notifier import pipeline_dedupe_key, summary_entity_id
from stockbrain.telegram.preferences import PipelineEvent
from stockbrain.telegram.service import TelegramService
from tests import proposal_helpers as ph

pytestmark = pytest.mark.integration


def _settings(database: Database, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "log_level": "CRITICAL",
        "web_auth_enabled": False,
        "database_url": database.engine.url.render_as_string(hide_password=False),
        # Nothing optional should start; these tests are about the trigger.
        "alpaca_news_enabled": False,
        "sec_enabled": False,
        "t212_metadata_enabled": False,
        "research_enabled": False,
        "classifier_enabled": False,
        "proposals_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _container(database: Database, **overrides: object) -> ServiceContainer:
    return ServiceContainer(
        settings=_settings(database, **overrides),
        database=database,
        health=ProviderHealthRegistry(),
    )


async def _notification_jobs(database: Database) -> list[Job]:
    async with database.session() as session:
        return list(
            (
                await session.execute(
                    sa.select(Job)
                    .where(Job.job_type == JobType.SEND_NOTIFICATION.value)
                    .order_by(Job.created_at)
                )
            ).scalars()
        )


async def test_the_daily_summary_is_enqueued_exactly_once_per_day(
    clean_tables: Database,
) -> None:
    """The scheduler ticks every minute; the day's summary is one job."""
    services = _container(clean_tables, telegram_daily_summary_time="00:00")

    await services._telegram_daily_summary()
    await services._telegram_daily_summary()

    jobs = await _notification_jobs(clean_tables)
    assert len(jobs) == 1
    assert jobs[0].payload["pipeline_event"] == PipelineEvent.PORTFOLIO_SUMMARY.value
    assert jobs[0].payload["entity_id"] == str(summary_entity_id(utcnow().date()))


async def test_no_daily_summary_is_enqueued_when_the_time_is_unset(
    clean_tables: Database,
) -> None:
    services = _container(clean_tables, telegram_daily_summary_time=None)

    await services._telegram_daily_summary()

    assert await _notification_jobs(clean_tables) == []


async def test_a_day_already_delivered_is_not_enqueued_again(
    clean_tables: Database,
) -> None:
    """The durable "already sent" mark is the notifications row, not memory.

    A job is inserted directly here rather than delivered, so the job queue's own
    dedupe key cannot mask a broken check: only the notification row can make
    this pass.
    """
    today = utcnow().date()
    entity_id = summary_entity_id(today)
    async with clean_tables.transaction() as session:
        session.add(
            Notification(
                notification_class=NotificationClass.PORTFOLIO_EVENT,
                channel="telegram",
                title="Daily summary",
                body="<b>Daily summary</b>",
                status=NotificationStatus.SENT,
                entity_type="portfolio",
                entity_id=entity_id,
                dedupe_key=pipeline_dedupe_key(entity_id, PipelineEvent.PORTFOLIO_SUMMARY),
                created_at=utcnow(),
            )
        )
    services = _container(clean_tables, telegram_daily_summary_time="00:00")

    await services._telegram_daily_summary()

    assert await _notification_jobs(clean_tables) == []


def test_the_summary_entity_id_is_stable_for_a_date() -> None:
    """The id is derived, not generated: two workers agree without coordinating."""
    day = utcnow().date()
    assert summary_entity_id(day) == summary_entity_id(day)
    assert summary_entity_id(day) != summary_entity_id(day + dt.timedelta(days=1))
    assert isinstance(summary_entity_id(day), uuid.UUID)


async def test_the_service_gathers_a_renderable_summary(clean_tables: Database) -> None:
    """The digest is assembled from stored state, not recomputed from a broker.

    One seeded holding carries a real ``PositionExitStatus``; the proposal that
    opened it is terminal, so the open count is zero; and the research run the
    thesis came from was completed today, with its action.
    """
    from tests.integration.test_exit_sweep import (
        _exit_sweep,
        _seed_executed_buy,
        _seed_position,
    )

    sweep = await _exit_sweep(clean_tables)
    await _seed_executed_buy(clean_tables)
    await _seed_position(
        clean_tables,
        broker_ticker="AAPL_US_EQ",
        average_price=Decimal("180.50"),
        current_price=Decimal("200.25"),
    )
    # ``ph.seed`` records the promoted event but not the promotion instant.
    async with clean_tables.transaction() as session:
        event = await session.get(Event, ph.EVENT_ID)
        assert event is not None
        event.classified_at = utcnow()
    service = TelegramService(
        clean_tables,
        ph.settings(),
        health=ProviderHealthRegistry(),
        control=ControlStateService(clean_tables),
        exits=sweep,
    )

    view = await service.daily_summary()

    assert view.available is True
    assert view.total_value == Decimal("100000")
    assert [position.broker_ticker for position in view.positions] == ["AAPL_US_EQ"]
    assert view.positions[0].exit is not None
    assert view.positions[0].exit.managed is True
    assert view.open_proposals == 0
    assert view.candidates_24h == 1
    assert [action for _, action in view.research_completed_24h] == ["BUY"]
