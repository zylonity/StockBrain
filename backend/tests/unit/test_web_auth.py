"""Web authentication primitives, and the proof that no route escaped the gate.

Specification section 19: "do not expose the UI unauthenticated", "secure
cookies", "CSRF protection for state-changing operations", "SameSite cookie
settings", and "If accessed only over LAN/Tailscale, still require auth."
Through Phase 8 there was none of it -- seven state-changing routes, one of
which transmits a real broker order, reachable by anything that could open a
socket to port 8080.

The route-coverage test at the bottom is the important one.  Everything else
here checks a primitive; that one checks that the primitive is actually in front
of the thing it is supposed to protect.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

import pytest
from fastapi.routing import APIRoute

from stockbrain.api.auth import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    PUBLIC_PATHS,
    SESSION_COOKIE_NAME,
    STATE_CHANGING_METHODS,
    cookie_secure,
    hash_password,
    is_public_path,
    issue_csrf_token,
    mint_session,
    read_session,
    verify_origin,
    verify_password,
    web_auth_blockers,
)
from stockbrain.config import Settings
from stockbrain.main import create_app

NOW = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
SECRET = "a-signing-key-for-tests-only"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "log_level": "CRITICAL",
        "stockbrain_secret_key": SECRET,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def test_a_password_round_trips_through_the_stored_form() -> None:
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("Correct horse battery staple", stored)
    assert not verify_password("", stored)


def test_the_stored_form_carries_its_own_parameters() -> None:
    """So raising the cost later leaves existing hashes verifiable.

    A hash whose parameters live in the code rather than in the hash is a hash
    that locks the owner out the day the code changes.
    """
    stored = hash_password("a-long-enough-password")
    algorithm, n, r, p, salt, digest = stored.split("$")
    assert algorithm == "scrypt"
    assert int(n) >= 2**14
    assert int(r) == 8 and int(p) == 1
    assert salt and digest and salt != digest


def test_two_hashes_of_one_password_differ() -> None:
    """A per-hash salt, so a rainbow table is worthless and two owners with the
    same password do not have the same hash."""
    assert hash_password("same-password-twice") != hash_password("same-password-twice")


def test_a_malformed_stored_hash_fails_the_login_rather_than_the_process() -> None:
    """A misconfigured hash must refuse the login, not raise.

    The configuration error is reported by ``web_auth_blockers`` at startup,
    which is where an operator can act on it -- not at the login prompt as a
    traceback.
    """
    for broken in ("", "not-a-hash", "scrypt$x$y$z$q$r", "bcrypt$1$2$3$4$5"):
        assert not verify_password("anything", broken)


def test_the_hash_is_never_the_password() -> None:
    """Stated as a test because the environment of a running container is
    readable by anything that can exec into it."""
    stored = hash_password("plaintext-must-not-appear")
    assert "plaintext-must-not-appear" not in stored


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def test_a_minted_session_reads_back() -> None:
    cookie, session = mint_session(username="owner", secret=SECRET, ttl_seconds=3600, now=NOW)
    restored = read_session(cookie, secret=SECRET, now=NOW)
    assert restored is not None
    assert restored.username == "owner" == session.username
    assert restored.expires_at == session.expires_at


def test_a_tampered_payload_is_refused() -> None:
    """The signature is checked *before* the payload is parsed.

    A forged cookie must never reach ``json.loads``, which is the one place a
    hostile string gets interpreted.
    """
    cookie, _ = mint_session(username="owner", secret=SECRET, ttl_seconds=3600, now=NOW)
    body, _, signature = cookie.partition(".")
    assert read_session(f"{body}x.{signature}", secret=SECRET, now=NOW) is None
    assert read_session(f"{body}.{signature}x", secret=SECRET, now=NOW) is None
    assert read_session(f"{body}.", secret=SECRET, now=NOW) is None
    assert read_session("", secret=SECRET, now=NOW) is None
    assert read_session(None, secret=SECRET, now=NOW) is None


def test_a_session_signed_with_another_key_is_refused() -> None:
    """Which is also the revocation mechanism a stateless design has.

    Rotating ``STOCKBRAIN_SECRET_KEY`` invalidates every outstanding session;
    the runbook documents it as the emergency measure it is.
    """
    cookie, _ = mint_session(username="owner", secret=SECRET, ttl_seconds=3600, now=NOW)
    assert read_session(cookie, secret="a-different-key", now=NOW) is None


def test_an_expired_session_is_refused() -> None:
    cookie, _ = mint_session(username="owner", secret=SECRET, ttl_seconds=60, now=NOW)
    assert read_session(cookie, secret=SECRET, now=NOW + dt.timedelta(seconds=59)) is not None
    assert read_session(cookie, secret=SECRET, now=NOW + dt.timedelta(seconds=61)) is None


def test_no_session_can_be_minted_without_a_signing_key() -> None:
    """Refused loudly rather than signed with an empty key, which would make
    every cookie forgeable by anyone who noticed."""
    with pytest.raises(ValueError, match="STOCKBRAIN_SECRET_KEY"):
        mint_session(username="owner", secret="", ttl_seconds=60, now=NOW)


def test_two_sessions_for_one_user_are_distinguishable() -> None:
    """A per-session nonce, so a log can tell two logins apart without either
    being guessable."""
    first, _ = mint_session(username="owner", secret=SECRET, ttl_seconds=60, now=NOW)
    second, _ = mint_session(username="owner", secret=SECRET, ttl_seconds=60, now=NOW)
    assert first != second


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
def test_a_csrf_token_is_long_and_unpredictable() -> None:
    tokens = {issue_csrf_token() for _ in range(64)}
    assert len(tokens) == 64
    assert all(len(token) >= 40 for token in tokens)


def test_the_state_changing_methods_are_the_ones_that_change_state() -> None:
    assert {"POST", "PUT", "PATCH", "DELETE"} == STATE_CHANGING_METHODS
    assert "GET" not in STATE_CHANGING_METHODS
    assert "HEAD" not in STATE_CHANGING_METHODS


def test_a_same_origin_request_passes_the_origin_check() -> None:
    assert verify_origin("http://nas.local:8080", None, "nas.local:8080")
    assert verify_origin(None, "http://nas.local:8080/proposals", "nas.local:8080")


def test_a_cross_origin_request_fails_the_origin_check() -> None:
    """A browser making a cross-site request always sends ``Origin``."""
    assert not verify_origin("http://evil.example", None, "nas.local:8080")
    assert not verify_origin(None, "http://evil.example/x", "nas.local:8080")


def test_a_request_with_neither_header_is_allowed() -> None:
    """Absent on plain same-origin requests from some clients and from ``curl``.

    Rejecting them would break the runbook's own diagnostics while stopping no
    browser-driven attack -- and ``SameSite=Strict`` plus the double-submit
    token are still in force.
    """
    assert verify_origin(None, None, "nas.local:8080")


def test_a_malformed_origin_is_refused() -> None:
    assert not verify_origin("http://evil.example", None, None)


# ---------------------------------------------------------------------------
# Configuration posture
# ---------------------------------------------------------------------------
def test_authentication_is_on_by_default() -> None:
    assert Settings(app_env="test").web_auth_enabled is True


def test_a_missing_password_hash_is_a_blocker_not_a_startup_failure() -> None:
    """The same shape as an empty Telegram allowlist.

    A fresh deployment comes up, serves its health endpoints, says what is
    missing, and grants access to nothing. Refusing to start would leave an
    operator with no way to read the reason.
    """
    settings = _settings()
    blockers = web_auth_blockers(settings)
    assert any("WEB_OWNER_PASSWORD_HASH" in blocker for blocker in blockers)
    assert "hash_password" in " ".join(blockers)


def test_a_missing_signing_key_is_a_blocker() -> None:
    """A session cannot be signed without one, so nothing may be trusted.

    ``stockbrain_secret_key`` is passed explicitly as empty: relying on it
    being *absent* from the environment is exactly the mistake Phase 8's bug 20
    recorded -- a test that passes until somebody configures the variable.
    """
    settings = Settings(
        app_env="test",
        stockbrain_secret_key="",
        web_owner_password_hash=hash_password("x" * 12),
    )
    assert any("STOCKBRAIN_SECRET_KEY" in blocker for blocker in web_auth_blockers(settings))


def test_a_fully_configured_deployment_has_no_blockers() -> None:
    settings = _settings(web_owner_password_hash=hash_password("a-real-password"))
    assert web_auth_blockers(settings) == []


def test_disabling_authentication_is_reported_as_the_risk_it_is() -> None:
    settings = _settings(web_auth_enabled=False)
    blockers = web_auth_blockers(settings)
    assert len(blockers) == 1
    assert "reachable without a credential" in blockers[0]
    assert "network trust" in blockers[0]


def test_production_refuses_to_start_unauthenticated_without_an_acknowledgement() -> None:
    """The spec's own sentence, enforced: "If accessed only over LAN/Tailscale,
    still require auth."

    Running behind an authenticating reverse proxy is a supported configuration.
    Never having set a password is not, and the difference has to be something
    the operator states rather than something the code infers.
    """
    with pytest.raises(ValueError, match="WEB_TRUSTED_NETWORK_ACKNOWLEDGED"):
        _settings(app_env="production", web_auth_enabled=False)

    # Acknowledged, and it starts.
    settings = _settings(
        app_env="production",
        web_auth_enabled=False,
        web_trusted_network_acknowledged=True,
    )
    assert settings.web_auth_enabled is False


def test_cookies_are_secure_in_production_and_overridable() -> None:
    """A ``Secure`` cookie on an ``http://`` origin is never stored, so a
    plain-HTTP LAN deployment would see a login that silently does nothing."""
    assert cookie_secure(_settings(app_env="production")) is True
    assert cookie_secure(_settings(app_env="local")) is False
    assert cookie_secure(_settings(app_env="local", web_cookie_secure=True)) is True
    assert cookie_secure(_settings(app_env="production", web_cookie_secure=False)) is False


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def test_the_public_path_list_is_short_and_deliberate() -> None:
    """Every entry justified: two probes and the login flow.

    A liveness probe has no credential and must not be made to carry one; the
    login endpoints cannot require a session by definition. Nothing else.
    """
    assert {
        "/api/health/live",
        "/api/health/ready",
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/auth/session",
    } == PUBLIC_PATHS


def test_the_detailed_health_payload_is_not_public() -> None:
    """It names the broker environment and the execution posture.

    That is operational intelligence rather than a liveness signal, and an
    orchestrator does not need it.
    """
    assert not is_public_path("/api/health")
    assert not is_public_path("/api/health/providers")


def test_metrics_is_not_public() -> None:
    """Not a secret, but operational detail.

    A scraper on a home network can carry a session or sit behind the same
    reverse proxy as the UI.
    """
    assert not is_public_path("/metrics")


def test_the_spa_shell_is_public_so_the_login_form_can_render() -> None:
    """The shell carries no data; everything it reads is under ``/api``."""
    assert is_public_path("/")
    assert is_public_path("/proposals")
    assert is_public_path("/assets/index-abc123.js")


def test_every_api_route_is_either_public_or_protected() -> None:
    """The test that makes the middleware trustworthy.

    Enumerates the real route table and asserts each path is either on the
    short public list or behind the gate. A new route added without a thought
    about authentication lands in the protected set, which is the safe default;
    a new *public* route has to be added to ``PUBLIC_PATHS`` deliberately, and
    that shows up here.
    """
    app = create_app(_settings(web_auth_enabled=False))
    collected: list[str] = []

    def walk(routes: Iterable[object]) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                collected.append(route.path)
                continue
            included = getattr(route, "original_router", None)
            nested = getattr(included, "routes", None) or getattr(route, "routes", None)
            if nested:
                walk(nested)

    walk(app.routes)
    assert collected, "no API routes were discovered; the walk is broken"

    public = {path for path in collected if is_public_path(path)}
    protected = {path for path in collected if not is_public_path(path)}
    assert public <= PUBLIC_PATHS, f"unexpectedly public: {public - PUBLIC_PATHS}"
    # And the routes that matter most are in the protected set, named
    # explicitly so a refactor that made one public fails here.
    for path in (
        "/api/v1/proposals/{proposal_id}/approve",
        "/api/v1/system/kill-switch",
        "/api/v1/execution/attempts/{attempt_id}/reconcile",
        "/api/v1/system/execution-status",
        "/api/v1/proposals",
        "/api/health",
        "/metrics",
    ):
        assert path in protected, path


def test_the_cookie_and_header_names_are_stable() -> None:
    """The SPA reads one and sends the other; renaming either breaks the pair
    silently, and the failure mode is "every POST is a 403"."""
    assert SESSION_COOKIE_NAME == "sb_session"
    assert CSRF_COOKIE_NAME == "sb_csrf"
    assert CSRF_HEADER_NAME == "x-stockbrain-csrf"
