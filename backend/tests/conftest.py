"""Shared test fixtures.

Integration tests need a real PostgreSQL because several of the system's
safety guarantees are partial unique indexes -- they cannot be exercised
against SQLite or a mock.  They are skipped, not silently passed, when no
database is configured.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text

from stockbrain.config import Settings
from stockbrain.db.session import Database

BACKEND_ROOT = Path(__file__).resolve().parent.parent

TEST_DATABASE_URL = os.environ.get("DATABASE_URL_TEST") or os.environ.get("DATABASE_URL")

# A developer's `.env` is intentionally useful to the running application, but
# it must never change a unit or integration test's premise.  Keep this list at
# the configuration boundary rather than teaching individual tests about the
# local machine.  Tests that need a credential pass a harmless explicit value
# (or set one with ``monkeypatch``) as part of their own arrange step.
_CREDENTIAL_ENVIRONMENT = (
    "STOCKBRAIN_SECRET_KEY",
    "DEEPSEEK_API_KEY",
    # The generic LLM backend. LLM_PROVIDER and the model/rate variables are
    # scrubbed alongside the key because they change which provider profile a
    # test resolves and whether configuration validates at all -- an operator
    # running against a non-default endpoint must not change a test's premise.
    "LLM_API_KEY",
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_DEEP_MODEL",
    "LLM_INPUT_USD_PER_MTOK",
    "LLM_CACHED_INPUT_USD_PER_MTOK",
    "LLM_OUTPUT_USD_PER_MTOK",
    "LLM_DEEP_INPUT_USD_PER_MTOK",
    "LLM_DEEP_CACHED_INPUT_USD_PER_MTOK",
    "LLM_DEEP_OUTPUT_USD_PER_MTOK",
    "ALPACA_API_KEY",
    "ALPACA_API_SECRET",
    "BRAVE_API_KEY",
    "EXA_API_KEY",
    "FIRECRAWL_API_KEY",
    "SEC_CONTACT_EMAIL",
    "FRED_API_KEY",
    "T212_API_KEY",
    "T212_API_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "WEB_OWNER_PASSWORD_HASH",
)

#: The operator's real `.env`, one directory above `backend/`.
REPOSITORY_ENV_FILE = BACKEND_ROOT.parent / ".env"


def dotenv_variable_names(path: Path) -> frozenset[str]:
    """Every variable name a dotenv file defines.

    Names only.  A *value* from the operator's file must never reach a test, so
    reading them would be the mistake this function exists to prevent -- and the
    names are all that is needed in order to delete them.

    Parsed rather than obtained from ``dotenv_values`` so that this stays a
    pure, testable function with no dependency on the loader that caused the
    problem in the first place.
    """
    if not path.is_file():
        return frozenset()
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.partition("=")[0].strip()
        # `export FOO=1` is legal in a dotenv file and names the variable FOO.
        name = name.removeprefix("export ").strip()
        if name.isidentifier():
            names.add(name)
    return frozenset(names)


#: Names the *harness itself* owns, which must survive the scrubbing below.
#:
#: ``alembic/env.py`` calls ``get_settings()`` and overwrites whatever
#: ``sqlalchemy.url`` its caller configured, so a migration reaches the test
#: database only through ``DATABASE_URL`` in the environment -- which
#: ``migrated_database`` sets on purpose.  Deleting it would not fail loudly;
#: it would quietly point migrations at whatever the default resolves to, which
#: is precisely the class of accident this module exists to prevent.
_HARNESS_OWNED_ENVIRONMENT = frozenset({"DATABASE_URL", "DATABASE_URL_TEST"})

#: Deleted from ``os.environ`` before every test.
#:
#: Nulling ``env_file`` is not enough on its own, because by the time any
#: fixture runs the file may already have been merged into the process.  The
#: pinned upstream does exactly that: ``third_party/TradingAgents/
#: tradingagents/__init__.py`` calls ``load_dotenv(find_dotenv(usecwd=True))``
#: at *import* time, and ``usecwd=True`` walks up from ``backend/`` until it
#: finds the repository ``.env`` -- so importing it (as the research tests do)
#: copies the operator's entire configuration into ``os.environ`` for the rest
#: of the session, where it is indistinguishable from a deliberate value.
#:
#: That went unnoticed for as long as the file happened to agree with the test
#: defaults.  It surfaced when a developer set ``T212_LIVE_EXECUTION_ENABLED``
#: and ``FX_PROVIDER`` for their own deployment and 144 tests began failing on
#: their machine and nowhere else -- a suite that had been passing by luck.
#:
#: Deleting by *name* rather than maintaining a second hand-written list is the
#: point: a variable added to `.env.example` and copied into a developer's
#: `.env` is covered the day it appears, with nothing to keep in sync.
_REPOSITORY_ENV_NAMES = dotenv_variable_names(REPOSITORY_ENV_FILE) - _HARNESS_OWNED_ENVIRONMENT


@pytest.fixture(autouse=True)
def isolate_settings_from_local_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ordinary tests independent of process credentials and root `.env`.

    Explicit ``Settings(_env_file=...)`` remains available to opt-in live tests;
    normal tests receive only their explicit constructor values and environment
    values they set themselves with ``monkeypatch``.

    ``monkeypatch`` restores every name afterwards, so a developer's shell keeps
    whatever it had and the live tests -- which read the file directly rather
    than through ``os.environ`` -- are unaffected.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in _CREDENTIAL_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    for name in _REPOSITORY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "log_level": "WARNING",
        "stockbrain_secret_key": "test-secret-key-not-used-in-production",
        # Off for the majority of tests, whose subject is not the login flow.
        # `tests/unit/test_web_auth.py` and `tests/integration/test_web_auth.py`
        # turn it on explicitly and are the tests that prove it works -- so
        # "authentication is enforced" is asserted where it can be asserted
        # properly rather than incidentally in three hundred other tests.
        "web_auth_enabled": False,
    }
    if TEST_DATABASE_URL:
        base["database_url"] = TEST_DATABASE_URL
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
def make_settings() -> object:
    return _settings


@pytest.fixture(scope="session")
def database_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("DATABASE_URL_TEST (or DATABASE_URL) is not set")
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> Iterator[str]:
    """Rebuild the schema from the Alembic migrations once per session.

    Running the real migrations rather than ``metadata.create_all`` means the
    tests exercise exactly what production will apply.
    """
    config = AlembicConfig(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    # A database that has never been migrated has no alembic_version table.
    with contextlib.suppress(Exception):
        command.downgrade(config, "base")
    command.upgrade(config, "head")
    try:
        yield database_url
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@pytest.fixture
async def database(migrated_database: str) -> AsyncIterator[Database]:
    db = Database(_settings(database_url=migrated_database))
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
async def clean_tables(database: Database) -> AsyncIterator[Database]:
    """Truncate mutable tables between tests, keeping the schema in place."""
    tables = (
        "execution_attempts",
        "approval_actions",
        "broker_orders",
        "risk_evaluations",
        "trade_proposals",
        "portfolio_snapshots",
        "positions",
        "theses",
        "research_runs",
        "event_company_impacts",
        "event_sources",
        "sources",
        "events",
        "broker_instruments",
        "broker_working_schedules",
        "broker_exchanges",
        "company_aliases",
        "companies",
        "jobs",
        "provider_calls",
        "discovery_queries",
        "discovery_topics",
        "llm_calls",
        "notifications",
        "audit_log",
        # Durable execution control (pause / kill switch) lives here. Leaving it
        # behind would let one test's emergency stop halt the next one's world.
        "app_settings",
    )
    async with database.transaction() as session:
        await session.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    yield database


@pytest.fixture
async def clean_llm_calls(database: Database) -> AsyncIterator[Database]:
    """Empty ``llm_calls`` only, for budget tests that drive spend directly."""
    async with database.transaction() as session:
        await session.execute(text("TRUNCATE llm_calls RESTART IDENTITY CASCADE"))
    yield database
