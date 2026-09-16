"""Read-only memory endpoints: every bucket, and outcomes with their grades."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from asgi_lifespan import LifespanManager

from stockbrain.config import Settings
from stockbrain.db.session import Database
from stockbrain.enums import ThesisAction
from stockbrain.main import create_app
from tests.integration.test_research import seed
from tests.integration.test_research_packet_memory import _graded_outcome

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(
    migrated_database: str, clean_tables: Database
) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        app_env="test",
        web_auth_enabled=False,
        log_level="WARNING",
        database_url=migrated_database,
        stockbrain_secret_key="test-key",
        telegram_bot_token="1234:super-secret-bot-token",
        memory_grade_enabled=True,
    )
    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def test_calibration_lists_every_bucket_with_counts(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    value = await seed(clean_tables)
    when = dt.datetime(2026, 9, 10, tzinfo=dt.UTC)
    await _graded_outcome(
        clean_tables,
        value,
        action=ThesisAction.BUY,
        event_type="EARNINGS",
        correct=True,
        alpha="0.05",
        graded_at=when,
    )
    await _graded_outcome(
        clean_tables,
        value,
        action=ThesisAction.BUY,
        event_type="EARNINGS",
        correct=False,
        alpha="-0.01",
        graded_at=when,
    )
    response = await client.get("/api/v1/memory/calibration")
    assert response.status_code == 200
    body = response.json()
    assert body["graded_outcomes"] == 2
    assert body["pending_outcomes"] == 2
    buckets = {row["key"]: row for row in body["buckets"]}
    assert buckets["EARNINGS×BUY"]["samples"] == 2  # noqa: RUF001
    assert buckets["EARNINGS×BUY"]["correct"] == 1  # noqa: RUF001
    assert Decimal(buckets["EARNINGS×BUY"]["hit_rate"]) == Decimal("0.5")  # noqa: RUF001
    company_keys = [key for key in buckets if key.startswith("company:")]
    assert len(company_keys) == 1


async def test_outcomes_list_newest_first_with_grades(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    value = await seed(clean_tables)
    await _graded_outcome(
        clean_tables,
        value,
        action=ThesisAction.BUY,
        event_type="EARNINGS",
        correct=True,
        alpha="0.05",
        graded_at=dt.datetime(2026, 9, 10, tzinfo=dt.UTC),
    )
    response = await client.get("/api/v1/memory/outcomes", params={"limit": 10})
    assert response.status_code == 200
    (row,) = response.json()["outcomes"]
    assert row["broker_ticker"] == value.company.broker_ticker
    assert row["action"] == "BUY"
    assert row["status"] == "PENDING"
    assert [grade["checkpoint"] for grade in row["grades"]] == ["D5"]


async def test_the_limit_is_bounded(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/memory/outcomes", params={"limit": 10000})
    assert response.status_code == 422
