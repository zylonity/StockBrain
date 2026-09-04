"""Application-level tests that do not require a database.

The important behaviour here is degradation: with PostgreSQL unreachable the
process must still start and still answer liveness and health, because an
operator needs those endpoints most precisely when the database is broken.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager

from stockbrain.config import Settings
from stockbrain.enums import ProviderStatus
from stockbrain.main import create_app


@pytest.fixture
async def unreachable_db_client() -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        app_env="test",
        log_level="CRITICAL",
        # Port 1 is reserved and never listening, so the connection fails fast.
        database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/stockbrain",
    )
    app = create_app(settings)
    async with LifespanManager(app, startup_timeout=60):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_liveness_survives_database_outage(
    unreachable_db_client: httpx.AsyncClient,
) -> None:
    response = await unreachable_db_client.get("/api/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_readiness_reports_503_when_database_is_down(
    unreachable_db_client: httpx.AsyncClient,
) -> None:
    response = await unreachable_db_client.get("/api/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["database"] == ProviderStatus.DOWN.value


async def test_health_reports_down_without_crashing(
    unreachable_db_client: httpx.AsyncClient,
) -> None:
    response = await unreachable_db_client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == ProviderStatus.DOWN.value


async def test_execution_status_defaults_to_blocked(
    unreachable_db_client: httpx.AsyncClient,
) -> None:
    response = await unreachable_db_client.get("/api/v1/system/execution-status")
    assert response.status_code == 200
    body = response.json()
    assert body["live_execution_permitted"] is False
    assert body["broker_environment"] == "demo"
    assert body["blockers"]
    assert "written consent" in body["notice"]


async def test_execution_status_never_leaks_credentials(
    unreachable_db_client: httpx.AsyncClient,
) -> None:
    response = await unreachable_db_client.get("/api/v1/system/execution-status")
    rendered = response.text.lower()
    for forbidden in ("api_key", "api_secret", "password", "token"):
        assert forbidden not in rendered


async def test_metrics_endpoint_exposes_prometheus_text(
    unreachable_db_client: httpx.AsyncClient,
) -> None:
    response = await unreachable_db_client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "stockbrain_up" in response.text


async def test_unknown_api_path_is_404_not_the_spa_shell(
    unreachable_db_client: httpx.AsyncClient,
) -> None:
    response = await unreachable_db_client.get("/api/v1/does-not-exist")
    assert response.status_code == 404


async def test_request_id_is_echoed(unreachable_db_client: httpx.AsyncClient) -> None:
    response = await unreachable_db_client.get(
        "/api/health/live", headers={"x-request-id": "abc-123"}
    )
    assert response.headers["x-request-id"] == "abc-123"
