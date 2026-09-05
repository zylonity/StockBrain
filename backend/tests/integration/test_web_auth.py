"""Web authentication and CSRF, against the real application.

The unit tests check the primitives.  These drive the actual ASGI app with a
real cookie jar, because the properties that matter -- "an unauthenticated
browser cannot approve a trade", "a cross-site form cannot approve a trade" --
are properties of the middleware stack rather than of any function.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager

from stockbrain.api.auth import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    hash_password,
)
from stockbrain.config import Settings
from stockbrain.db.session import Database
from stockbrain.main import create_app

pytestmark = pytest.mark.integration

PASSWORD = "a-sufficiently-long-password"
SECRET = "a-signing-key-for-tests-only"


def _settings(database: Database, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "log_level": "CRITICAL",
        "database_url": database.engine.url.render_as_string(hide_password=False),
        "stockbrain_secret_key": SECRET,
        "web_auth_enabled": True,
        "web_owner_username": "owner",
        "web_owner_password_hash": hash_password(PASSWORD),
        # Nothing else needs to start for an authentication test.
        "discovery_enabled": False,
        "alpaca_news_enabled": False,
        "sec_enabled": False,
        "research_enabled": False,
        "classifier_enabled": False,
        "proposals_enabled": False,
        "t212_metadata_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def _client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


@pytest.fixture
async def client(clean_tables: Database) -> AsyncIterator[httpx.AsyncClient]:
    async for http in _client(_settings(clean_tables)):
        yield http


async def _login(client: httpx.AsyncClient, password: str = PASSWORD) -> httpx.Response:
    return await client.post("/api/v1/auth/login", json={"username": "owner", "password": password})


def _csrf(client: httpx.AsyncClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {CSRF_HEADER_NAME: token}


# ---------------------------------------------------------------------------
# Denied by default
# ---------------------------------------------------------------------------
async def test_an_unauthenticated_read_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/proposals")
    assert response.status_code == 401
    assert response.json()["detail"] == "authentication required"


async def test_an_unauthenticated_approval_is_refused(client: httpx.AsyncClient) -> None:
    """The property this whole module exists for.

    Before Phase 9 this returned a 404 or a 409 -- that is, it *reached the
    approval logic* -- from an anonymous caller.
    """
    response = await client.post("/api/v1/proposals/00000000-0000-0000-0000-000000000001/approve")
    assert response.status_code == 401


async def test_an_unauthenticated_kill_switch_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/system/kill-switch", json={"engaged": True})
    assert response.status_code == 401


async def test_the_detailed_health_payload_needs_a_session(
    client: httpx.AsyncClient,
) -> None:
    """It names the broker environment and the execution posture."""
    assert (await client.get("/api/health")).status_code == 401
    assert (await client.get("/api/health/providers")).status_code == 401


async def test_metrics_needs_a_session(client: httpx.AsyncClient) -> None:
    assert (await client.get("/metrics")).status_code == 401


async def test_the_probes_stay_public(client: httpx.AsyncClient) -> None:
    """A container healthcheck and an orchestrator probe carry no credential.

    ``compose.yaml``'s healthcheck calls ``/api/health/live``; making it
    authenticate would make the container permanently unhealthy.
    """
    assert (await client.get("/api/health/live")).status_code == 200
    assert (await client.get("/api/health/ready")).status_code in (200, 503)


async def test_the_login_page_can_ask_whether_it_is_needed(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.get("/api/v1/auth/session")).json()
    assert body["auth_required"] is True
    assert body["authenticated"] is False
    assert body["username"] is None


# ---------------------------------------------------------------------------
# Logging in
# ---------------------------------------------------------------------------
async def test_a_correct_password_starts_a_session(client: httpx.AsyncClient) -> None:
    response = await _login(client)
    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert client.cookies.get(SESSION_COOKIE_NAME)
    assert client.cookies.get(CSRF_COOKIE_NAME)
    # And the session is now usable.
    assert (await client.get("/api/v1/proposals")).status_code == 200


async def test_a_wrong_password_is_refused_and_sets_no_cookie(
    client: httpx.AsyncClient,
) -> None:
    response = await _login(client, password="not-the-password")
    assert response.status_code == 401
    assert response.json()["authenticated"] is False
    assert client.cookies.get(SESSION_COOKIE_NAME) is None


async def test_a_wrong_username_is_refused_indistinguishably(
    client: httpx.AsyncClient,
) -> None:
    """Same status, same body.

    A different answer for "no such user" is a user-enumeration oracle, and on
    a single-account application it would also confirm the account name.
    """
    wrong_user = await client.post(
        "/api/v1/auth/login", json={"username": "somebody", "password": PASSWORD}
    )
    wrong_password = await _login(client, password="nope")
    assert wrong_user.status_code == wrong_password.status_code == 401
    assert wrong_user.json() == wrong_password.json()


async def test_the_session_cookie_is_httponly_and_samesite_strict(
    client: httpx.AsyncClient,
) -> None:
    """``HttpOnly`` keeps it out of reach of any injected script;
    ``SameSite=Strict`` is the first of the three CSRF defences."""
    response = await _login(client)
    header = next(
        value
        for key, value in response.headers.multi_items()
        if key.lower() == "set-cookie" and value.startswith(f"{SESSION_COOKIE_NAME}=")
    )
    assert "HttpOnly" in header
    assert "SameSite=strict" in header or "SameSite=Strict" in header
    assert "Path=/" in header


async def test_the_csrf_cookie_is_readable_because_the_spa_must_echo_it(
    client: httpx.AsyncClient,
) -> None:
    """Deliberately *not* ``HttpOnly`` -- that is the whole mechanism.

    It authorises nothing alone: it is only ever compared against the cookie,
    so knowing the token without holding the session cookie is worthless.
    """
    response = await _login(client)
    header = next(
        value
        for key, value in response.headers.multi_items()
        if key.lower() == "set-cookie" and value.startswith(f"{CSRF_COOKIE_NAME}=")
    )
    assert "HttpOnly" not in header


async def test_a_login_body_forbids_extra_fields(client: httpx.AsyncClient) -> None:
    """Same rule as every other mutating body in this codebase."""
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": "owner", "password": PASSWORD, "is_owner": True},
    )
    assert response.status_code == 422


async def test_logging_out_clears_both_cookies(client: httpx.AsyncClient) -> None:
    await _login(client)
    response = await client.post("/api/v1/auth/logout", headers=_csrf(client))
    assert response.status_code == 200
    assert not client.cookies.get(SESSION_COOKIE_NAME)
    assert (await client.get("/api/v1/proposals")).status_code == 401


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
async def test_a_state_change_without_the_csrf_header_is_refused(
    client: httpx.AsyncClient,
) -> None:
    """The double-submit half.

    A cross-site attacker can cause the session cookie to be sent -- that is
    what CSRF *is* -- but cannot read the CSRF cookie to build this header.
    """
    await _login(client)
    response = await client.post("/api/v1/system/pause", json={})
    assert response.status_code == 403
    assert CSRF_HEADER_NAME in response.json()["detail"]


async def test_a_state_change_with_a_wrong_csrf_header_is_refused(
    client: httpx.AsyncClient,
) -> None:
    await _login(client)
    response = await client.post(
        "/api/v1/system/pause", json={}, headers={CSRF_HEADER_NAME: "not-the-token"}
    )
    assert response.status_code == 403


async def test_a_state_change_with_the_matching_token_succeeds(
    client: httpx.AsyncClient,
) -> None:
    await _login(client)
    response = await client.post("/api/v1/system/pause", json={}, headers=_csrf(client))
    assert response.status_code == 200
    assert response.json()["trading_halted"] is True


async def test_a_cross_origin_state_change_is_refused_even_with_the_token(
    client: httpx.AsyncClient,
) -> None:
    """The third defence, and the one that does not depend on the browser
    honouring ``SameSite``."""
    await _login(client)
    response = await client.post(
        "/api/v1/system/pause",
        json={},
        headers={**_csrf(client), "origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert "cross-origin" in response.json()["detail"]


async def test_a_read_needs_no_csrf_token(client: httpx.AsyncClient) -> None:
    """A GET changes nothing, so requiring a token would only break bookmarks
    and the runbook's own ``curl`` diagnostics."""
    await _login(client)
    assert (await client.get("/api/v1/proposals")).status_code == 200


# ---------------------------------------------------------------------------
# Misconfiguration
# ---------------------------------------------------------------------------
async def test_authentication_enabled_but_unconfigured_refuses_everything(
    clean_tables: Database,
) -> None:
    """ "Cannot check a credential" must never mean "do not check a credential".

    This is the state a fresh deployment is in before a password is generated:
    the container is healthy, the probes answer, and nothing else does.
    """
    settings = _settings(clean_tables, web_owner_password_hash="")
    async for client in _client(settings):
        assert (await client.get("/api/health/live")).status_code == 200
        blocked = await client.get("/api/v1/proposals")
        assert blocked.status_code == 503
        assert any("WEB_OWNER_PASSWORD_HASH" in blocker for blocker in blocked.json()["blockers"])
        # And the login endpoint says the same thing rather than 401-ing, so
        # the reason is visible to the person who has to fix it.
        login = await _login(client)
        assert login.status_code == 503
        assert login.json()["blockers"]


async def test_a_trusted_network_deployment_serves_everything(
    clean_tables: Database,
) -> None:
    """A supported configuration -- an authenticating reverse proxy in front --
    and it reports itself as such rather than pretending to be protected."""
    settings = _settings(clean_tables, web_auth_enabled=False)
    async for client in _client(settings):
        assert (await client.get("/api/v1/proposals")).status_code == 200
        session = (await client.get("/api/v1/auth/session")).json()
        assert session["auth_required"] is False
        assert session["authenticated"] is True
        assert session["blockers"]


async def test_the_posture_endpoint_reports_the_truth(client: httpx.AsyncClient) -> None:
    """Reported, not assumed.

    A deployment that *thinks* it has authentication and does not is the
    failure this endpoint exists to make visible.
    """
    await _login(client)
    body = (await client.get("/api/v1/system/web-security")).json()
    assert body["auth_enabled"] is True
    assert body["auth_effective"] is True
    assert body["blockers"] == []
    assert body["cookie_samesite"] == "strict"
    assert body["csrf_header"] == CSRF_HEADER_NAME
    assert "/api/health/live" in body["public_paths"]
    # No secret, ever.
    rendered = str(body)
    assert SECRET not in rendered
    assert PASSWORD not in rendered
    assert "scrypt" not in rendered


async def test_no_response_ever_carries_the_password_hash(
    client: httpx.AsyncClient,
) -> None:
    """Asserted across every route a session can reach, because a hash in a JSON
    body is a hash in a browser cache and in a log."""
    await _login(client)
    for path in (
        "/api/v1/auth/session",
        "/api/v1/system/web-security",
        "/api/v1/system/execution-status",
        "/api/health",
        "/api/health/providers",
    ):
        body = (await client.get(path)).text
        assert "scrypt$" not in body
        assert PASSWORD not in body
        assert SECRET not in body
