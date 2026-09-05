"""API health tests against a real database."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy import text

from stockbrain.config import Settings
from stockbrain.db.session import Database
from stockbrain.main import create_app
from stockbrain.observability.health import ProviderName
from stockbrain.startup import DATABASE_HEALTH_MAX_AGE_SECONDS

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(migrated_database: str) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        app_env="test",
        # These tests exercise the read API, not the login flow.
        web_auth_enabled=False,
        log_level="CRITICAL",
        database_url=migrated_database,
        stockbrain_secret_key="test-key",
    )
    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def test_ready_when_database_and_schema_are_current(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["database"] == "HEALTHY"
    assert body["schema_current"] is True


async def test_health_lists_every_subsystem(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/health")).json()
    subsystems = {entry["subsystem"] for entry in body["subsystems"]}
    assert subsystems == {
        "database",
        "discovery",
        "research",
        "execution",
        "notifications",
    }


async def test_providers_endpoint_reports_every_provider(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.get("/api/health/providers")).json()
    providers = {entry["provider"] for entry in body["providers"]}
    assert "postgres" in providers
    assert "trading212" in providers
    assert len(providers) == 10


async def test_provider_health_is_persisted_at_startup(
    client: httpx.AsyncClient, database: Database
) -> None:
    await client.get("/api/health")
    async with database.session() as session:
        result = await session.execute(
            text("SELECT status FROM provider_health WHERE provider = 'postgres'")
        )
        assert result.scalar_one() == "HEALTHY"


async def test_health_reflects_a_database_outage_that_starts_after_startup(
    client: httpx.AsyncClient, migrated_database: str
) -> None:
    """Health must re-probe, not replay a status recorded at startup.

    Regression test: with PostgreSQL stopped underneath a running application,
    ``/api/health/ready`` continued to report ``ready: true, database: HEALTHY``
    indefinitely, because the database status was recorded once during the
    lifespan handler and never refreshed.
    """
    # Confirm the healthy baseline first.
    assert (await client.get("/api/health/ready")).json()["ready"] is True

    # Repoint the application's engine at a port with nothing listening, which
    # simulates the database going away without touching the test container.
    app = client._transport.app  # type: ignore[attr-defined]
    working = app.state.database
    app.state.database = Database(
        Settings(
            app_env="test",
            web_auth_enabled=False,
            log_level="CRITICAL",
            database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/stockbrain",
        )
    )
    try:
        # Wait out the freshness window so the next request actually re-probes.
        await asyncio.sleep(DATABASE_HEALTH_MAX_AGE_SECONDS + 0.1)

        ready = await client.get("/api/health/ready")
        assert ready.status_code == 503
        body = ready.json()
        assert body["ready"] is False
        assert body["database"] == "DOWN"
        assert body["schema_current"] is False

        health = (await client.get("/api/health")).json()
        assert health["status"] == "DOWN"
        subsystems = {entry["subsystem"]: entry["status"] for entry in health["subsystems"]}
        assert subsystems["database"] == "DOWN"

        # Liveness must keep answering: an operator needs it most right now.
        assert (await client.get("/api/health/live")).status_code == 200
    finally:
        await app.state.database.dispose()
        app.state.database = working

    # And it must recover on its own once the database is reachable again.
    await asyncio.sleep(DATABASE_HEALTH_MAX_AGE_SECONDS + 0.1)
    recovered = await client.get("/api/health/ready")
    assert recovered.status_code == 200
    assert recovered.json()["ready"] is True


async def test_repeated_health_calls_are_throttled(client: httpx.AsyncClient) -> None:
    """A polling dashboard must not turn into one query per client per request."""
    registry = client._transport.app.state.health_registry  # type: ignore[attr-defined]
    await client.get("/api/health")
    first = registry.get(ProviderName.POSTGRES).last_checked_at
    await client.get("/api/health")
    await client.get("/api/health/providers")
    assert registry.get(ProviderName.POSTGRES).last_checked_at == first
