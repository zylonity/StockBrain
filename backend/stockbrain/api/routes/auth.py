"""Login, logout, and "am I logged in".

Three routes and no more.  There is no registration, no password reset, no
token endpoint and no user management: this is a single-owner application, the
owner's password hash comes from the environment, and every one of those
additions would be attack surface added to protect one account.

``POST /login`` is itself exempt from the CSRF check, because a client that has
not logged in has no CSRF cookie to echo.  That is safe: a forged cross-site
login can only ever log the victim's browser in *as the attacker*, which grants
the attacker nothing, and the endpoint is rate-limited by the scrypt work factor
rather than by a counter.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from stockbrain.api.auth import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AuthSession,
    cookie_secure,
    issue_csrf_token,
    mint_session,
    read_session,
    verify_password,
    web_auth_blockers,
)
from stockbrain.api.dependencies import SettingsDep
from stockbrain.db.base import utcnow
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = ["router"]

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """Deliberately narrow.

    ``extra="forbid"`` for the same reason every other mutating body in this
    codebase forbids extras: a field nobody reads is a field somebody will
    eventually start reading.
    """

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class SessionView(BaseModel):
    """What the SPA needs to decide whether to show the login form.

    Carries no password state, no hash, no secret and no token -- the CSRF token
    travels in its own readable cookie, not in a JSON body that could end up in
    a log or a browser history entry.
    """

    authenticated: bool
    auth_required: bool
    username: str | None = None
    expires_at: str | None = None
    blockers: list[str] = Field(default_factory=list)


@router.get("/session", response_model=SessionView, summary="Current session state")
async def session_state(request: Request, settings: SettingsDep) -> SessionView:
    """Public: the login page has to be able to ask whether it is needed."""
    blockers = web_auth_blockers(settings)
    if not settings.web_auth_enabled:
        return SessionView(authenticated=True, auth_required=False, blockers=blockers)
    session: AuthSession | None = read_session(
        request.cookies.get(SESSION_COOKIE_NAME),
        secret=settings.stockbrain_secret_key.get_secret_value(),
        now=utcnow(),
    )
    return SessionView(
        authenticated=session is not None,
        auth_required=True,
        username=session.username if session else None,
        expires_at=session.expires_at.isoformat() if session else None,
        blockers=blockers,
    )


@router.post("/login", response_model=SessionView, summary="Start a session")
async def login(payload: LoginRequest, response: Response, settings: SettingsDep) -> SessionView:
    """Verify the owner's password and set the session and CSRF cookies."""
    if not settings.web_auth_enabled:
        # Nothing to log in to. Reported rather than silently succeeding, so a
        # UI cannot show a logged-in state that means nothing.
        return SessionView(authenticated=True, auth_required=False)

    blockers = web_auth_blockers(settings)
    if blockers:
        log.error("login_unavailable", blockers=blockers)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return SessionView(authenticated=False, auth_required=True, blockers=blockers)

    stored = settings.web_owner_password_hash.get_secret_value()
    # Both halves are checked in constant time and the failure is reported
    # identically, so a wrong username and a wrong password are
    # indistinguishable to a caller. The password is verified even when the
    # username is wrong, so the response time does not leak which it was.
    username_ok = hmac.compare_digest(
        payload.username.strip().encode("utf-8"),
        settings.web_owner_username.strip().encode("utf-8"),
    )
    password_ok = verify_password(payload.password, stored)
    if not (username_ok and password_ok):
        # No username in the log line: a failed login is worth recording and the
        # attempted username is attacker-supplied text.
        log.warning("login_rejected")
        METRICS.inc("stockbrain_web_logins_total", labels={"outcome": "rejected"})
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return SessionView(authenticated=False, auth_required=True)

    now = utcnow()
    cookie, session = mint_session(
        username=settings.web_owner_username,
        secret=settings.stockbrain_secret_key.get_secret_value(),
        ttl_seconds=settings.web_session_ttl_seconds,
        now=now,
    )
    secure = cookie_secure(settings)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        cookie,
        max_age=settings.web_session_ttl_seconds,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )
    # Readable by design: the SPA has to echo it in a header, which is the whole
    # mechanism. It authorises nothing on its own -- it is only ever checked
    # *against the cookie*, so knowing it without holding the session cookie is
    # worthless.
    response.set_cookie(
        CSRF_COOKIE_NAME,
        issue_csrf_token(),
        max_age=settings.web_session_ttl_seconds,
        httponly=False,
        samesite="strict",
        secure=secure,
        path="/",
    )
    log.info("login_accepted", expires_at=session.expires_at.isoformat())
    METRICS.inc("stockbrain_web_logins_total", labels={"outcome": "accepted"})
    return SessionView(
        authenticated=True,
        auth_required=True,
        username=session.username,
        expires_at=session.expires_at.isoformat(),
    )


@router.post("/logout", response_model=SessionView, summary="End the session")
async def logout(response: Response, settings: SettingsDep) -> SessionView:
    """Clear both cookies.

    A stateless session cannot be revoked server-side, so this clears the
    browser's copy and nothing more. Revoking *every* session means rotating
    ``STOCKBRAIN_SECRET_KEY``, which the runbook documents as the emergency
    measure it is.
    """
    for name in (SESSION_COOKIE_NAME, CSRF_COOKIE_NAME):
        response.delete_cookie(name, path="/", samesite="strict")
    log.info("logout")
    return SessionView(authenticated=False, auth_required=settings.web_auth_enabled)
