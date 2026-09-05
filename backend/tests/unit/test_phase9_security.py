"""Phase 9's own security surface, and the adversarial checks behind it.

The Phase 6/7/8 surface tests cover "no broker mutation route", "no LLM tool",
"no cancel path".  Phase 9 added three things that can leak or overspend — a
credential-carrying Firecrawl client, an FX provider, and a password — plus an
HTTP gate that everything else now sits behind.  Each test here names the
specific way that could go wrong.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE_ROOT = BACKEND_ROOT / "stockbrain"


def _sources() -> dict[str, str]:
    return {
        str(path.relative_to(SOURCE_ROOT)): path.read_text()
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
    }


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
def test_every_new_credential_is_a_secret_str() -> None:
    """``SecretStr`` renders as ``**********`` on accidental interpolation.

    A plain ``str`` reaches a log line, a traceback, an exception message and a
    JSON response by default; the type is the only reason none of those
    happens.
    """
    fields = Settings.model_fields
    for name in ("firecrawl_api_key", "web_owner_password_hash", "stockbrain_secret_key"):
        annotation = str(fields[name].annotation)
        assert "SecretStr" in annotation, f"{name} is not a SecretStr"


def test_the_password_hash_never_appears_in_a_settings_repr() -> None:
    """The whole point of the type, asserted rather than assumed."""
    from stockbrain.api.auth import hash_password

    stored = hash_password("a-password-that-must-not-leak")
    settings = Settings(
        app_env="test",
        web_auth_enabled=False,
        web_owner_password_hash=stored,
        firecrawl_api_key="fc-a-key-that-must-not-leak",
    )
    rendered = repr(settings) + str(settings)
    assert stored not in rendered
    assert "fc-a-key-that-must-not-leak" not in rendered
    assert "a-password-that-must-not-leak" not in rendered


def test_the_firecrawl_budget_holds_no_credential() -> None:
    """Structural: the budget cannot leak a key because it never has one.

    It takes a database and four integers. The API key lives on the client,
    which is a different object with a different job.
    """
    from stockbrain.ingestion.firecrawl_budget import FirecrawlBudget

    signature = inspect.signature(FirecrawlBudget.__init__)
    for forbidden in ("api_key", "key", "token", "secret", "settings"):
        assert forbidden not in signature.parameters, forbidden


def test_a_firecrawl_error_is_recorded_as_a_class_name_not_a_body() -> None:
    """A Firecrawl error body can echo the request, and the request carries an
    ``Authorization: Bearer`` header.

    Both call sites pass ``type(exc).__name__``. A test on the source rather
    than on behaviour, because the failing version would look correct.
    """
    handlers = (SOURCE_ROOT / "jobs" / "handlers.py").read_text()
    firecrawl_section = handlers[handlers.index("handle_firecrawl_topic_search") :]
    firecrawl_section = firecrawl_section[: firecrawl_section.index("async def handle_sec_refresh")]
    assert "error_category=type(exc).__name__" in firecrawl_section
    assert "error_category=str(exc)" not in firecrawl_section


def test_no_module_logs_an_fx_or_firecrawl_response_body() -> None:
    """Provider bodies are parsed, never logged.

    Both providers' errors are already truncated to 200-300 characters by the
    shared client; what must not appear is a raw payload dumped into a log
    event, because that is how a request -- and its headers -- ends up on disk.
    """
    for name in ("fx/alpaca.py", "fx/frankfurter.py", "ingestion/firecrawl.py"):
        source = (SOURCE_ROOT / name).read_text()
        assert "payload=payload" not in source, name
        assert "body=response" not in source, name
        assert 'log.info("...", raw=' not in source, name


def test_the_fx_providers_never_send_a_broker_credential() -> None:
    """Alpaca FX reuses the market-data credential; Frankfurter needs none.

    Neither may reach for a Trading 212 key: an FX request goes to a data
    vendor, and a broker credential in that request is a broker credential
    handed to a third party.
    """
    for name in ("fx/alpaca.py", "fx/frankfurter.py", "fx/service.py", "fx/base.py"):
        source = (SOURCE_ROOT / name).read_text()
        assert "t212" not in source.lower(), name
        assert "trading212" not in source.lower(), name


# ---------------------------------------------------------------------------
# No new capability
# ---------------------------------------------------------------------------
def test_no_phase_9_module_can_reach_a_broker_mutation() -> None:
    """The new subsystems read and account. None of them can send.

    Asserted on the *import* lines rather than the whole file, because a
    docstring that explains what a module does not do would otherwise trip it.
    """
    forbidden_imports = (
        "stockbrain.broker.trading212_orders",
        "stockbrain.execution.service",
        "stockbrain.execution.base",
    )
    for name in (
        "fx/base.py",
        "fx/service.py",
        "fx/alpaca.py",
        "fx/frankfurter.py",
        "ingestion/firecrawl.py",
        "ingestion/firecrawl_budget.py",
        "observability/alerts.py",
        "api/auth.py",
        "hash_password.py",
    ):
        source = (SOURCE_ROOT / name).read_text()
        imports = "\n".join(
            line for line in source.splitlines() if line.lstrip().startswith(("import ", "from "))
        )
        for forbidden in forbidden_imports:
            assert forbidden not in imports, f"{name} imports {forbidden}"


def test_the_fx_provider_interface_is_three_operations_wide() -> None:
    """A rate, a capability statement, and a close.

    No history, no batch, no conversion helper, no "call this endpoint" escape
    hatch. Conversion belongs to :class:`FxRate`, which carries the provenance;
    a provider that could also convert would be a provider that could convert
    *without* it.
    """
    from stockbrain.fx.base import FxRateProvider

    members = {name for name, _ in inspect.getmembers(FxRateProvider) if not name.startswith("_")}
    assert members == {"latest", "capability", "aclose"}


def test_the_alert_scanner_cannot_change_anything() -> None:
    """An alerting bug must not become a trading bug.

    It reads state and writes notification rows. If it could pause, halt,
    cancel or send, a false positive would have consequences.
    """
    source = (SOURCE_ROOT / "observability" / "alerts.py").read_text()
    imports = "\n".join(
        line for line in source.splitlines() if line.lstrip().startswith(("import ", "from "))
    )
    assert "stockbrain.risk" not in imports
    assert "stockbrain.broker" not in imports
    for verb in ("submit(", "place_order(", "cancel_order(", "pause(", "engage_kill_switch("):
        assert f".{verb}" not in source, verb


def test_firecrawl_makes_exactly_two_kinds_of_paid_call() -> None:
    """Search and scrape.  No crawl, no map, no extract, no agent.

    Each of those is a separate Firecrawl product with its own billing, and
    ``/v2/crawl`` in particular can spend an allowance in one request.
    """
    source = (SOURCE_ROOT / "ingestion" / "firecrawl.py").read_text()
    for forbidden in ("/v2/crawl", "/v2/map", "/v2/extract", "/v2/agent", "/v1/"):
        assert forbidden not in source, forbidden
    assert source.count('"/v2/search"') == 1
    assert source.count('"/v2/scrape"') == 1


def test_no_firecrawl_call_site_retries() -> None:
    """A retry is a second *paid* call.

    ``retry_safe`` defaults to ``False`` and both Firecrawl call sites leave it
    there. The Phase 2 version passed ``retry_safe=True, max_attempts=3``, and
    with the queue's own three attempts that was up to nine billable requests
    per scheduled search.
    """
    source = (SOURCE_ROOT / "ingestion" / "firecrawl.py").read_text()
    assert "retry_safe=True" not in source
    assert "max_attempts=" not in source


def test_the_paid_job_types_get_one_attempt() -> None:
    """Both Firecrawl job enqueues cap attempts at one.

    The durable cooldown is the retry; the queue's retry would be a second
    reservation for the same search.
    """
    services = (SOURCE_ROOT / "services.py").read_text()
    search_block = services[services.index("JobType.FIRECRAWL_TOPIC_SEARCH") :][:1200]
    assert "max_attempts=1" in search_block

    ingestion = (SOURCE_ROOT / "ingestion" / "service.py").read_text()
    enrich_block = ingestion[ingestion.index("JobType.FIRECRAWL_ENRICH") :][:1200]
    assert "max_attempts=1" in enrich_block


# ---------------------------------------------------------------------------
# Adversarial: could a client name a quantity, a ticker or a rate?
# ---------------------------------------------------------------------------
def test_no_api_request_body_accepts_an_fx_rate() -> None:
    """A client-supplied rate would be a client-supplied position size.

    The same property Phase 6 established for quantity and ticker, extended to
    the new input. Every mutating body in this codebase is a free-text reason
    with ``extra="forbid"``.
    """
    for path in sorted((SOURCE_ROOT / "api").rglob("*.py")):
        source = path.read_text()
        for forbidden in ("fx_rate:", "rate:", "quantity:", "broker_ticker:", "price:"):
            # Response schemas legitimately carry these; request models are the
            # ones under test, and they all live in `schemas.py` with a
            # `Request` suffix or in a route module.
            if forbidden in source and "Request" in source:
                request_classes = [
                    block
                    for block in source.split("class ")
                    if block.startswith(
                        ("ControlChangeRequest", "KillSwitchRequest", "LoginRequest")
                    )
                ]
                for block in request_classes:
                    assert forbidden not in block, f"{path.name}: {forbidden}"


def test_the_login_body_forbids_extra_fields() -> None:
    """A field nobody reads is a field somebody will eventually start reading."""
    from stockbrain.api.routes.auth import LoginRequest

    assert LoginRequest.model_config.get("extra") == "forbid"
    assert set(LoginRequest.model_fields) == {"username", "password"}


def test_the_session_view_carries_no_secret_field() -> None:
    """The CSRF token travels in its own cookie, not in a JSON body.

    A token in a response body ends up in a browser cache, a proxy log and a
    ``curl`` transcript.
    """
    from stockbrain.api.routes.auth import SessionView

    assert set(SessionView.model_fields) == {
        "authenticated",
        "auth_required",
        "username",
        "expires_at",
        "blockers",
    }


def test_the_csrf_token_is_not_derived_from_the_session() -> None:
    """A token computed from the session cookie could be recomputed by anyone
    who ever saw that cookie, which defeats the point of a second factor."""
    from stockbrain.api.auth import issue_csrf_token

    signature = inspect.signature(issue_csrf_token)
    assert signature.parameters == {}
    assert issue_csrf_token() != issue_csrf_token()


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/proposals/00000000-0000-0000-0000-000000000001/approve",
        "/api/v1/system/kill-switch",
        "/api/v1/execution/attempts/00000000-0000-0000-0000-000000000001/reconcile",
    ],
)
def test_the_dangerous_routes_are_behind_the_gate(path: str) -> None:
    """Named explicitly, so a refactor that made one public fails here.

    ``is_public_path`` is deny-by-default for anything under ``/api``, but the
    three routes that can authorize or touch a broker are worth asserting by
    name.
    """
    from stockbrain.api.auth import is_public_path

    assert not is_public_path(path)


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
def test_no_module_uses_a_naive_now() -> None:
    """``datetime.now()`` without a timezone is the host's local time.

    Every persisted timestamp is timezone-aware UTC, every window is computed
    against one, and a naive value compared against an aware one raises at
    runtime rather than being quietly wrong -- but the ones that *are* quietly
    wrong are the comparisons against a date.
    """
    offenders: list[str] = []
    for name, source in _sources().items():
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            if "datetime.now()" in stripped or "dt.datetime.now()" in stripped:
                offenders.append(f"{name}: {stripped}")
            # `datetime.utcnow()` is deprecated *and* naive. StockBrain's own
            # `utcnow()` helper is the aware one and is what everything calls.
            if "dt.datetime.utcnow()" in stripped or "datetime.datetime.utcnow()" in stripped:
                offenders.append(f"{name}: {stripped}")
    assert offenders == [], offenders


def test_no_module_uses_time_time_for_a_financial_timestamp() -> None:
    """``time.time()`` is a float epoch with no timezone and no provenance.

    Permitted for measuring a duration (``time.perf_counter`` is preferred and
    is what the request middleware uses); never for a timestamp that gets
    persisted or compared against a provider's.
    """
    for name, source in _sources().items():
        if name in {"api/auth.py"}:
            # The session cookie stores an integer epoch deliberately: it is a
            # compact, timezone-free representation *inside a signed blob*, and
            # it is converted back with an explicit UTC on the way out.
            continue
        assert "time.time()" not in source, name


def test_the_budget_windows_are_computed_by_postgresql() -> None:
    """A container with a drifting system clock must not be able to hand itself
    a fresh day.

    ``reserved_at`` defaults to the database clock and the window boundaries are
    computed with the same ``now()``, so the two cannot disagree.
    """
    source = (SOURCE_ROOT / "ingestion" / "firecrawl_budget.py").read_text()
    assert "date_trunc('day', now() AT TIME ZONE 'UTC')" in source
    assert "date_trunc('month', now() AT TIME ZONE 'UTC')" in source


def test_the_discovery_cadence_uses_the_database_clock() -> None:
    """The scheduler compares ``next_eligible_at`` against ``now()`` and the
    handler writes it with ``now()``.

    Phase 1's bug 5 was the job queue using two clocks; the same mistake here
    would make a cooldown negotiable by container clock skew.
    """
    handlers = (SOURCE_ROOT / "jobs" / "handlers.py").read_text()
    block = handlers[handlers.index("handle_firecrawl_topic_search") :]
    block = block[: block.index("async def handle_sec_refresh")]
    assert "next_eligible_at=sa.func.now()" in block
    assert "last_run_at=sa.func.now()" in block

    services = (SOURCE_ROOT / "services.py").read_text()
    sweep = services[services.index("async def _enqueue_due_topic_searches") :]
    sweep = sweep[: sweep.index("async def _enqueue_firecrawl_enrichment")]
    # The sweep reads the clock from PostgreSQL and compares `next_eligible_at`
    # against it, rather than against `datetime.now()` in this process.
    assert "sa.select(sa.func.now())" in sweep
    assert "eligible_at > now" in sweep
    assert "utcnow()" not in sweep


# ---------------------------------------------------------------------------
# Decimal
# ---------------------------------------------------------------------------
def test_no_financial_value_passes_through_a_binary_float() -> None:
    """``Decimal(float)`` persists ``0.1000000000000000055511151231257827``.

    Every provider number is parsed via ``Decimal(str(value))``. A rate, a
    price, a quantity or a notional that has been through a ``float`` has
    already lost the digits.
    """
    for name in (
        "fx/base.py",
        "fx/alpaca.py",
        "fx/frankfurter.py",
        "risk/sizing.py",
        "risk/models.py",
        "risk/rules.py",
    ):
        source = (SOURCE_ROOT / name).read_text()
        assert "Decimal(float(" not in source, name
        # A metric gauge legitimately takes a float; arithmetic does not.
        for line in source.splitlines():
            if "float(" not in line or "METRICS" in line:
                continue
            stripped = line.strip()
            assert (
                stripped.startswith("#") or "age_seconds" in stripped or "-> float" in stripped
            ), f"{name}: {stripped}"


def test_the_fx_rate_is_a_decimal_in_the_model_and_on_the_row() -> None:
    """A rate stored as a float would round-trip differently from the one that
    sized the trade, which makes the audit trail disagree with itself."""
    from decimal import Decimal

    from stockbrain.db.models.proposals import TradeProposal
    from stockbrain.fx.base import FxRate

    assert FxRate.__annotations__["rate"] == "Decimal"
    column = TradeProposal.__table__.columns["fx_rate"]
    assert column.type.python_type is Decimal
    # 12 decimal places: JPY crosses run to five significant figures before the
    # point and a rate is not a place to discover a precision ceiling.
    numeric = column.type
    assert isinstance(numeric, sa.Numeric)
    assert numeric.scale == 12
