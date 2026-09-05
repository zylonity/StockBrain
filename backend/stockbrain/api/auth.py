"""Web authentication and CSRF protection.

Specification section 19 is unambiguous: "do not expose the UI
unauthenticated", "secure cookies", "CSRF protection for state-changing
operations", "SameSite cookie settings", and -- the sentence that matters most
for a home NAS -- "If accessed only over LAN/Tailscale, still require auth."
Through Phase 8 the API had none.  Seven state-changing routes, one of which
transmits a real order to a broker, were reachable by anything that could open
a TCP connection to port 8080.

What this module deliberately is **not**: an identity provider.  There is one
account, it is the owner's, and there are no roles, no registration, no password
reset, no token endpoint and no OAuth.  Adding those would be adding attack
surface to protect a single-user application.

The design, and the reason for each part:

**Deny by default.**  Enforcement is a middleware with an explicit public
allow-list, not a dependency on each route.  A dependency has to be remembered
on every new route; a middleware has to be *un*-remembered, and the failure mode
of forgetting is a 401 rather than an open door.  There is a test that
enumerates the route table and asserts every path is either allow-listed or
protected.

**Scrypt, from the standard library.**  ``hashlib.scrypt`` is memory-hard and
needs no new dependency.  The stored form carries its own parameters, so raising
the cost later does not invalidate existing hashes.  The password itself is
never in the environment -- only the hash is -- and
``python -m stockbrain.hash_password`` generates it.

**An HMAC-signed stateless session cookie.**  No session table: a single-user
application does not need server-side session state, and a table would need
sweeping.  The cookie carries the username and an expiry and is signed with
``STOCKBRAIN_SECRET_KEY``.  Rotating that key invalidates every session, which
is the correct and only revocation mechanism a stateless design has -- and it is
documented as such.

**Double-submit CSRF plus an origin check.**  The session cookie is
``HttpOnly``, ``SameSite=Strict``.  A second, readable cookie carries a CSRF
token that the SPA echoes in ``X-StockBrain-CSRF`` on every state-changing
request; a cross-site attacker can cause the cookie to be sent but cannot read
it to build the header.  ``SameSite=Strict`` alone would very probably suffice
on a current browser -- the double submit and the ``Origin`` check are there
because the thing being protected is an irreversible broker order, and three
independent mechanisms that each fail closed is the right amount for that.

**A trusted-network escape hatch that must be stated out loud.**
``WEB_AUTH_ENABLED=false`` is honoured, and it is the *only* way to run without
authentication.  The process logs a warning naming the risk on every start, the
health payload reports it, and the config validator refuses the combination
``APP_ENV=production`` + ``WEB_AUTH_ENABLED=false`` unless
``WEB_TRUSTED_NETWORK_ACKNOWLEDGED=true`` is also set.  An operator who has put
StockBrain behind an authenticating reverse proxy has a supported path; an
operator who simply never configured a password does not get one by accident.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from stockbrain.config import AppEnv, Settings
from stockbrain.logging import get_logger

__all__ = [
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "PUBLIC_PATHS",
    "PUBLIC_PREFIXES",
    "SESSION_COOKIE_NAME",
    "STATE_CHANGING_METHODS",
    "AuthSession",
    "hash_password",
    "is_public_path",
    "issue_csrf_token",
    "mint_session",
    "read_session",
    "verify_origin",
    "verify_password",
]

log = get_logger(__name__)

SESSION_COOKIE_NAME = "sb_session"
CSRF_COOKIE_NAME = "sb_csrf"
CSRF_HEADER_NAME = "x-stockbrain-csrf"

#: Methods that change server state and therefore need a CSRF token.
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Paths reachable without a session, exactly and only these.
#:
#: ``/api/health/live`` and ``/api/health/ready`` are here because a container
#: healthcheck and an orchestrator probe have no credential and must not be made
#: to carry one -- an unauthenticated liveness probe is the standard contract,
#: and neither endpoint reveals anything beyond "the process is up" and "the
#: schema is current".  The *detailed* health payload is not here: it names the
#: broker environment and the execution posture, which is operational
#: intelligence rather than a liveness signal.
PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/api/health/live",
        "/api/health/ready",
        "/api/v1/auth/login",
        "/api/v1/auth/session",
        "/api/v1/auth/logout",
    }
)

#: Path prefixes served to an unauthenticated browser so it can *render the
#: login form*.  The SPA shell and its assets are not secrets; the data behind
#: them is, and every one of those paths is under ``/api``.
PUBLIC_PREFIXES: tuple[str, ...] = ("/assets/",)

#: scrypt parameters.  RFC 7914's interactive-login suggestion, which costs
#: roughly 16 MiB and a few tens of milliseconds -- irrelevant for one login a
#: day and expensive for an offline attacker.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_PREFIX = "scrypt"


def hash_password(password: str) -> str:
    """Hash a password into the self-describing stored form.

    The parameters travel with the hash, so raising the cost later leaves
    existing hashes verifiable instead of locking the owner out.
    """
    if not password:
        raise ValueError("a password is required")
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=64 * 1024 * 1024,
    )
    return "$".join(
        (
            _SCRYPT_PREFIX,
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            _b64(salt),
            _b64(key),
        )
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification against the stored form.

    Returns ``False`` for a malformed or empty stored hash rather than raising:
    a misconfigured hash must fail the login, not the process. It is also why
    ``web_auth_blockers`` exists -- the configuration error is reported there,
    at startup, instead of being discovered at the login prompt.
    """
    if not password or not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != _SCRYPT_PREFIX:
        log.warning("password_hash_malformed", reason="unrecognised format")
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = _unb64(parts[4])
        expected = _unb64(parts[5])
    except (ValueError, TypeError):
        log.warning("password_hash_malformed", reason="unparseable parameters")
        return False
    try:
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=64 * 1024 * 1024,
        )
    except ValueError:
        log.warning("password_hash_malformed", reason="invalid scrypt parameters")
        return False
    return hmac.compare_digest(candidate, expected)


@dataclass(frozen=True, slots=True)
class AuthSession:
    """A verified session, or the reason there is not one."""

    username: str
    issued_at: dt.datetime
    expires_at: dt.datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


def mint_session(
    *, username: str, secret: str, ttl_seconds: int, now: dt.datetime
) -> tuple[str, AuthSession]:
    """Build a signed session cookie value and the session it represents."""
    if not secret:
        raise ValueError("STOCKBRAIN_SECRET_KEY is required to mint a session")
    expires_at = now + dt.timedelta(seconds=ttl_seconds)
    payload = {
        "u": username,
        "i": int(now.timestamp()),
        "e": int(expires_at.timestamp()),
        # A per-session nonce, so two sessions minted in the same second for the
        # same user are distinguishable in a log without either being guessable.
        "n": secrets.token_urlsafe(8),
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64(_sign(body, secret))
    return f"{body}.{signature}", AuthSession(
        username=username,
        issued_at=now.replace(microsecond=0),
        expires_at=expires_at.replace(microsecond=0),
    )


def read_session(cookie: str | None, *, secret: str, now: dt.datetime) -> AuthSession | None:
    """Verify a session cookie.  ``None`` for anything not verifiably valid.

    The signature is checked **before** the payload is parsed, so a forged
    cookie never reaches ``json.loads``.
    """
    if not cookie or not secret:
        return None
    body, _, signature = cookie.partition(".")
    if not body or not signature:
        return None
    try:
        provided = _unb64(signature)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(_sign(body, secret), provided):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    username = payload.get("u")
    issued = payload.get("i")
    expires = payload.get("e")
    if not isinstance(username, str) or not isinstance(issued, int) or not isinstance(expires, int):
        return None
    if expires <= int(now.timestamp()):
        return None
    return AuthSession(
        username=username,
        issued_at=dt.datetime.fromtimestamp(issued, dt.UTC),
        expires_at=dt.datetime.fromtimestamp(expires, dt.UTC),
    )


def issue_csrf_token() -> str:
    """A fresh CSRF token.  256 bits, URL-safe, not derived from the session.

    Independent of the session on purpose: a token derived from the session
    cookie could be recomputed by anyone who ever saw that cookie, which defeats
    the point of the second factor.
    """
    return secrets.token_urlsafe(32)


def verify_origin(origin: str | None, referer: str | None, host: str | None) -> bool:
    """Whether a state-changing request came from this application's own origin.

    A third check beside ``SameSite=Strict`` and the double-submit token.  A
    request with neither ``Origin`` nor ``Referer`` is allowed: those headers are
    absent on plain same-origin requests from some clients and from ``curl``,
    and rejecting them would break the runbook's own diagnostics while stopping
    no browser-driven attack -- a browser making a cross-site request always
    sends ``Origin``.
    """
    candidate = origin or referer
    if not candidate:
        return True
    if not host:
        return False
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return False
    return parsed.netloc == host


def is_public_path(path: str) -> bool:
    """Whether a path may be served without a session.

    Anything under ``/api`` that is not explicitly listed is protected.
    Anything *not* under ``/api`` is the SPA shell, which is served so an
    unauthenticated browser can render the login form; it contains no data.
    """
    if path in PUBLIC_PATHS:
        return True
    if any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES):
        return True
    # `/metrics` is deliberately absent: it is not a secret, but it is
    # operational detail, and a scraper on a home network can carry a session or
    # sit behind the same reverse proxy as the UI.
    return not path.startswith(("/api", "/metrics"))


def web_auth_blockers(settings: Settings) -> list[str]:
    """Every reason web authentication is not actually protecting anything.

    Same shape as the other blocker predicates: "protected" is defined as "no
    blockers remain", so a health panel cannot show a green light beside a
    reason it is off.
    """
    blockers: list[str] = []
    if not settings.web_auth_enabled:
        blockers.append(
            "WEB_AUTH_ENABLED is false: every API route is reachable without a "
            "credential, and the deployment is relying entirely on network trust"
        )
        return blockers
    if not settings.stockbrain_secret_key.get_secret_value():
        blockers.append("STOCKBRAIN_SECRET_KEY is not set, so no session can be signed")
    stored = settings.web_owner_password_hash.get_secret_value()
    if not stored:
        blockers.append(
            "WEB_OWNER_PASSWORD_HASH is not set; generate one with "
            "'python -m stockbrain.hash_password'"
        )
    elif not stored.startswith(f"{_SCRYPT_PREFIX}$"):
        blockers.append(
            "WEB_OWNER_PASSWORD_HASH is not in the expected scrypt format; regenerate it "
            "with 'python -m stockbrain.hash_password'"
        )
    if not settings.web_owner_username.strip():
        blockers.append("WEB_OWNER_USERNAME is empty")
    return blockers


def cookie_secure(settings: Settings) -> bool:
    """Whether to mark cookies ``Secure``.

    On in production.  Off by default in a local or LAN deployment served over
    plain HTTP, because a ``Secure`` cookie on an ``http://`` origin is simply
    never stored and the operator would see a login that silently does nothing.
    ``WEB_COOKIE_SECURE`` overrides either way, which is what an operator behind
    a TLS-terminating reverse proxy needs.
    """
    if settings.web_cookie_secure is not None:
        return settings.web_cookie_secure
    return settings.app_env is AppEnv.PRODUCTION


def _sign(body: str, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
