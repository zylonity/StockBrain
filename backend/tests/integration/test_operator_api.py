"""The operator-facing API surface: logs, settings, preferences, portfolio, holds.

These endpoints exist because a subsystem's state was already in the process and
nowhere a person could see it.  What they must not do is become a second,
weaker way to change the things the gates protect -- so the tests here check the
reads *and* check that the writes are the four narrow ones they claim to be.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from asgi_lifespan import LifespanManager

from stockbrain.config import Settings
from stockbrain.db.base import utcnow
from stockbrain.db.models.portfolio import PortfolioSnapshot, Position
from stockbrain.db.models.system import AppSetting
from stockbrain.db.session import Database
from stockbrain.enums import Broker
from stockbrain.logging import get_logger
from stockbrain.main import create_app
from stockbrain.services import DISCOVERY_PAUSED_KEY

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(
    migrated_database: str, clean_tables: Database
) -> AsyncIterator[httpx.AsyncClient]:
    """A fresh app against freshly truncated tables.

    ``clean_tables`` rather than ``migrated_database`` because several of these
    assertions are about *defaults*: an `app_settings` row left behind by
    another module would make "the discovery hold starts lifted" quietly
    order-dependent.
    """
    settings = Settings(
        app_env="test",
        # These tests exercise the read API, not the login flow; the auth
        # surface has its own tests in `test_web_auth.py`.
        web_auth_enabled=False,
        log_level="INFO",
        log_buffer_size=200,
        database_url=migrated_database,
        stockbrain_secret_key="test-key",
        telegram_bot_token="1234:super-secret-bot-token",
    )
    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


async def test_logs_are_queryable_and_report_their_own_bounds(
    client: httpx.AsyncClient,
) -> None:
    get_logger("stockbrain.ingestion.brave").info("brave_search_complete", results=7)
    body = (await client.get("/api/v1/system/logs")).json()

    assert body["enabled"] is True
    assert body["capacity"] == 200
    # The page must be able to say "in memory, since this instant": an empty
    # buffer after a restart is honest, a silently truncated one is not.
    assert body["captured_since"] is not None
    assert body["min_captured_level"] == "info"
    assert any(entry["event"] == "brave_search_complete" for entry in body["entries"])


async def test_a_service_filter_answers_the_health_board_link(
    client: httpx.AsyncClient,
) -> None:
    """``View logs`` on a provider is a link, and this is why it works.

    The service keys the log buffer produces are the same strings
    ``/api/health/providers`` reports, so a filtered URL can be built from a
    health row without a translation table anywhere in the frontend.
    """
    get_logger("stockbrain.ingestion.brave").warning("brave_rate_limited")
    get_logger("stockbrain.ingestion.exa").info("exa_search_complete")

    providers = {
        p["provider"] for p in (await client.get("/api/health/providers")).json()["providers"]
    }
    assert "brave" in providers

    body = (await client.get("/api/v1/system/logs?services=brave")).json()
    events = {entry["event"] for entry in body["entries"]}
    assert "brave_rate_limited" in events
    assert "exa_search_complete" not in events


async def test_log_filters_compose(client: httpx.AsyncClient) -> None:
    get_logger("stockbrain.execution.service").warning("attempt_ambiguous", attempt=1)
    get_logger("stockbrain.execution.service").info("attempt_started", attempt=1)

    body = (
        await client.get(
            "/api/v1/system/logs?categories=execution&min_level=warning&search=ambiguous"
        )
    ).json()
    assert {entry["event"] for entry in body["entries"]} == {"attempt_ambiguous"}


async def test_an_unknown_level_is_ignored_rather_than_refused(
    client: httpx.AsyncClient,
) -> None:
    """A bookmarked URL with a stale filter should still show the logs."""
    response = await client.get("/api/v1/system/logs?min_level=verbose")
    assert response.status_code == 200


async def test_the_log_page_size_is_bounded(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/system/logs?limit=10000")).status_code == 422


async def test_log_facets_describe_what_is_present(client: httpx.AsyncClient) -> None:
    get_logger("stockbrain.fx.service").info("fx_rate_resolved")
    body = (await client.get("/api/v1/system/logs/facets")).json()
    assert body["services"].get("fx", 0) >= 1
    assert "fx" in body["categories"]


async def test_the_log_api_exposes_no_filesystem_parameter(client: httpx.AsyncClient) -> None:
    """A "view the logs" feature that took a filename would be an arbitrary-file
    read endpoint wearing a hat."""
    schema = (await client.get("/api/openapi.json")).json()
    parameters = {
        parameter["name"]
        for path, operations in schema["paths"].items()
        if path.startswith("/api/v1/system/logs")
        for operation in operations.values()
        for parameter in operation.get("parameters", [])
    }
    assert parameters == {
        "min_level",
        "services",
        "categories",
        "since_minutes",
        "search",
        "limit",
        "offset",
    }


async def test_a_secret_logged_by_accident_is_not_served_by_the_api(
    client: httpx.AsyncClient,
) -> None:
    get_logger("stockbrain.telegram.runtime").warning(
        "telegram_bootstrap_failed",
        url="https://api.telegram.org/bot1234:super-secret-bot-token/getMe",
        headers={"Authorization": "Bearer abcdefghij"},
    )
    raw = (await client.get("/api/v1/system/logs")).text
    assert "super-secret-bot-token" not in raw
    assert "Bearer abcdefghij" not in raw


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


async def test_settings_describe_every_group_without_a_secret(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.get("/api/v1/system/settings")).json()
    keys = {group["key"] for group in body["groups"]}
    assert keys == {
        "operations",
        "web",
        "llm",
        "classification",
        "discovery",
        "market_data",
        "execution",
        "risk",
        "telegram",
    }
    assert "super-secret-bot-token" not in (await client.get("/api/v1/system/settings")).text

    telegram = next(group for group in body["groups"] if group["key"] == "telegram")
    token = next(item for item in telegram["settings"] if item["key"] == "telegram_bot_token")
    assert token["mutability"] == "SECRET"
    assert token["value"] is None
    assert token["configured"] is True


async def test_the_execution_group_warns_that_its_gates_need_a_restart(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.get("/api/v1/system/settings")).json()
    execution = next(group for group in body["groups"] if group["key"] == "execution")
    assert "restart" in (execution["warning"] or "").lower()
    assert all(item["mutability"] != "RUNTIME" for item in execution["settings"])


async def test_there_is_no_generic_settings_mutation_route(
    client: httpx.AsyncClient,
) -> None:
    """The one test in this file that is about what does *not* exist.

    A generic ``PUT /settings/{key}`` would be a way to write
    ``T212_LIVE_EXECUTION_ENABLED`` over HTTP, and the gates exist precisely so
    that turning live execution on is an act performed on the host.
    """
    schema = (await client.get("/api/openapi.json")).json()
    settings_paths = {
        path: set(operations)
        for path, operations in schema["paths"].items()
        if path.startswith("/api/v1/system/settings")
    }
    assert settings_paths == {"/api/v1/system/settings": {"get"}}


# ---------------------------------------------------------------------------
# Notification preferences
# ---------------------------------------------------------------------------


async def test_preferences_report_defaults_and_blockers(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/v1/system/telegram/preferences")).json()
    categories = {item["category"]: item for item in body["categories"]}
    assert categories["EVENT_DISCOVERED"]["enabled"] is False
    assert categories["EVENT_CANDIDATE"]["enabled"] is True
    assert categories["EXECUTION_CRITICAL"]["locked"] is True
    # No allowlisted user in this deployment, so delivery is not available and
    # the page should say why rather than offering switches that never fire.
    assert body["delivery_available"] is False
    assert body["blockers"]


async def test_preferences_can_be_updated_and_the_locked_one_cannot(
    client: httpx.AsyncClient,
) -> None:
    response = await client.put(
        "/api/v1/system/telegram/preferences",
        json={
            "categories": {
                "EVENT_DISCOVERED": True,
                "EXECUTION_CRITICAL": False,
                "NOT_A_CATEGORY": True,
            }
        },
    )
    assert response.status_code == 200
    categories = {item["category"]: item for item in response.json()["categories"]}
    assert categories["EVENT_DISCOVERED"]["enabled"] is True
    assert categories["EXECUTION_CRITICAL"]["enabled"] is True
    assert response.json()["updated_by"] == "web:owner"

    # And it persisted.
    again = (await client.get("/api/v1/system/telegram/preferences")).json()
    assert {i["category"]: i["enabled"] for i in again["categories"]}["EVENT_DISCOVERED"] is True


async def test_preferences_reject_an_unexpected_body_field(
    client: httpx.AsyncClient,
) -> None:
    response = await client.put(
        "/api/v1/system/telegram/preferences",
        json={"categories": {}, "chat_id": 12345},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Discovery hold
# ---------------------------------------------------------------------------


async def test_the_discovery_hold_can_be_engaged_and_lifted(
    client: httpx.AsyncClient, database: Database
) -> None:
    """The flag was read by the scheduler and had no switch anywhere."""
    paused = await client.post("/api/v1/discovery/pause", json={"reason": "budget review"})
    assert paused.status_code == 200
    assert paused.json()["paused"] is True

    status = (await client.get("/api/v1/discovery/status")).json()
    assert status["paused"] is True

    async with database.session() as session:
        row = await session.get(AppSetting, DISCOVERY_PAUSED_KEY)
        assert row is not None
        assert row.value["paused"] is True
        assert row.value["reason"] == "budget review"

    resumed = await client.post("/api/v1/discovery/resume", json={})
    assert resumed.json()["paused"] is False
    assert (await client.get("/api/v1/discovery/status")).json()["paused"] is False


async def test_the_discovery_hold_is_not_the_trading_pause(
    client: httpx.AsyncClient,
) -> None:
    """Two different questions: spending money on new information, and acting on
    information already held. Conflating them would mean an operator who wanted
    one silently got the other."""
    await client.post("/api/v1/discovery/pause", json={})
    control = (await client.get("/api/v1/system/control")).json()
    assert control["trading_halted"] is False


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------


async def test_portfolio_reports_an_honest_absence(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/v1/portfolio")).json()
    assert body["available"] is False
    assert "snapshot" in (body["reason"] or "")
    assert body["positions"] == []


async def test_portfolio_reports_the_stored_mirror_and_its_age(
    client: httpx.AsyncClient, database: Database
) -> None:
    captured = utcnow() - dt.timedelta(hours=2)
    async with database.transaction() as session:
        session.add(
            PortfolioSnapshot(
                broker=Broker.TRADING212,
                account_id="acct-1",
                currency="GBP",
                broker_environment="demo",
                total_value=Decimal("1000.00"),
                invested_value=Decimal("600.00"),
                result_value=Decimal("12.34"),
                cash_available=Decimal("400.00"),
                cash_reserved=Decimal("0.00"),
                cash_in_pies=Decimal("0.00"),
                captured_at=captured,
            )
        )
        session.add(
            Position(
                account_id="acct-1",
                broker=Broker.TRADING212,
                broker_ticker="AAPL_US_EQ",
                quantity=Decimal("2"),
                quantity_available=Decimal("2"),
                average_price=Decimal("180.00"),
                current_price=Decimal("190.00"),
                ppl=Decimal("20.00"),
                currency="USD",
                last_synced_at=captured,
            )
        )

    body = (await client.get("/api/v1/portfolio")).json()
    assert body["available"] is True
    assert body["currency"] == "GBP"
    assert body["position_count"] == 1
    assert body["positions"][0]["broker_ticker"] == "AAPL_US_EQ"
    # Two hours old against a five-minute freshness limit: the page must say so
    # rather than display it next to a live proposal as if it were current.
    assert body["stale"] is True


async def test_the_portfolio_route_never_calls_the_broker(client: httpx.AsyncClient) -> None:
    """Trading 212 allows one account-summary request every five seconds; a page
    left open must not spend that allowance."""
    import inspect

    from stockbrain.api.routes import portfolio as module

    source = inspect.getsource(module)
    for forbidden in ("Trading212", "httpx", "t212_api_key", "AccountStateService"):
        assert forbidden not in source
