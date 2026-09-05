"""Startup checks.

Implements the parts of the specification's startup sequence that exist in
Phase 1: configuration validation, database connectivity, migration
verification, and marking providers whose credentials are absent as DISABLED
rather than pretending they are healthy.

A failing *optional* provider degrades its subsystem; only PostgreSQL makes the
application not-ready.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text

from stockbrain.config import Settings
from stockbrain.db.session import Database
from stockbrain.enums import ProviderStatus
from stockbrain.logging import get_logger
from stockbrain.observability.health import ProviderHealthRegistry, ProviderName

__all__ = [
    "check_database",
    "check_schema_current",
    "refresh_database_health",
    "register_static_provider_states",
]

#: How long a database health result may be reused before the health endpoints
#: probe again. `SELECT 1` is cheap, but a polling dashboard should not turn
#: into a query per client per second.
DATABASE_HEALTH_MAX_AGE_SECONDS = 2.0

#: A health probe must never hang the health endpoint. If the database cannot
#: answer `SELECT 1` within this budget it is not healthy, by definition.
DATABASE_PROBE_TIMEOUT_SECONDS = 5.0

log = get_logger(__name__)

_ALEMBIC_ROOT = Path(__file__).resolve().parent.parent


async def check_database(
    database: Database,
    registry: ProviderHealthRegistry,
    *,
    timeout_seconds: float = DATABASE_PROBE_TIMEOUT_SECONDS,
) -> bool:
    """Probe PostgreSQL and record the result.  Returns True when healthy.

    A database that cannot answer ``SELECT 1`` inside ``timeout_seconds`` is
    recorded as DOWN rather than being waited on, so a hung server degrades the
    health endpoint's answer instead of its availability.
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            async with database.session() as session:
                await session.execute(text("SELECT 1"))
    except TimeoutError:
        registry.record(
            ProviderName.POSTGRES,
            ProviderStatus.DOWN,
            detail=f"no response within {timeout_seconds:g}s",
        )
        log.error("database_probe_timeout", timeout_seconds=timeout_seconds)
        return False
    except Exception as exc:
        registry.record(
            ProviderName.POSTGRES, ProviderStatus.DOWN, detail=f"{type(exc).__name__}: {exc}"
        )
        log.error("database_unreachable", error=str(exc))
        return False
    registry.record(ProviderName.POSTGRES, ProviderStatus.HEALTHY)
    return True


async def refresh_database_health(
    database: Database,
    registry: ProviderHealthRegistry,
    *,
    max_age_seconds: float = DATABASE_HEALTH_MAX_AGE_SECONDS,
) -> bool:
    """Re-probe the database if the recorded result has gone stale.

    The health endpoints call this so they report the database's *current*
    state. Without it, a status recorded once at startup would keep claiming
    HEALTHY long after PostgreSQL had stopped -- which is precisely the moment
    the answer matters most.
    """
    age = registry.seconds_since_check(ProviderName.POSTGRES)
    if age is not None and age < max_age_seconds:
        return registry.get_database_status() is ProviderStatus.HEALTHY
    return await check_database(database, registry)


def _head_revision() -> str | None:
    config_path = _ALEMBIC_ROOT / "alembic.ini"
    if not config_path.exists():  # pragma: no cover - packaging safety net
        return None
    config = AlembicConfig(str(config_path))
    config.set_main_option("script_location", str(_ALEMBIC_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    return script.get_current_head()


async def check_schema_current(database: Database) -> tuple[bool, str | None]:
    """Compare the applied Alembic revision with the head revision in the code.

    Returns ``(is_current, detail)``.  A mismatch is reported rather than
    auto-migrated: migrations run explicitly at container start so a rollback
    never silently upgrades a schema.
    """
    head = _head_revision()
    if head is None:
        return False, "alembic script directory not found"
    try:
        async with database.session() as session:
            result = await session.execute(text("SELECT version_num FROM alembic_version"))
            applied = result.scalar_one_or_none()
    except Exception as exc:
        return False, f"could not read alembic_version: {type(exc).__name__}: {exc}"

    if applied is None:
        return False, f"no migration applied; expected {head}"
    if applied != head:
        return False, f"database at revision {applied}, code expects {head}"
    return True, None


def register_static_provider_states(settings: Settings, registry: ProviderHealthRegistry) -> None:
    """Mark providers DISABLED when they are switched off or unconfigured.

    Doing this at startup means the GUI shows an honest "not configured" rather
    than an alarming "unknown" for every integration that has not been set up.
    """
    if not settings.deepseek_api_key.get_secret_value():
        registry.set_disabled(ProviderName.DEEPSEEK, "DEEPSEEK_API_KEY is not set")
        registry.set_disabled(ProviderName.TRADINGAGENTS, "requires a DeepSeek API key")

    alpaca_configured = bool(
        settings.alpaca_api_key.get_secret_value() and settings.alpaca_api_secret.get_secret_value()
    )
    if not settings.alpaca_news_enabled:
        registry.set_disabled(ProviderName.ALPACA_NEWS, "ALPACA_NEWS_ENABLED is false")
    elif not alpaca_configured:
        registry.set_disabled(ProviderName.ALPACA_NEWS, "Alpaca credentials are not set")
    if not settings.alpaca_market_data_enabled:
        registry.set_disabled(
            ProviderName.ALPACA_MARKET_DATA, "ALPACA_MARKET_DATA_ENABLED is false"
        )
    elif not alpaca_configured:
        registry.set_disabled(ProviderName.ALPACA_MARKET_DATA, "Alpaca credentials are not set")

    if not settings.firecrawl_enabled:
        registry.set_disabled(ProviderName.FIRECRAWL, "FIRECRAWL_ENABLED is false")
    elif not settings.firecrawl_api_key.get_secret_value():
        registry.set_disabled(ProviderName.FIRECRAWL, "FIRECRAWL_API_KEY is not set")

    if not settings.sec_enabled:
        registry.set_disabled(ProviderName.SEC, "SEC_ENABLED is false")
    elif not settings.sec_contact_email:
        # data.sec.gov requires a descriptive User-Agent including a contact.
        registry.set_disabled(
            ProviderName.SEC, "SEC_CONTACT_EMAIL is required for the SEC User-Agent header"
        )

    if not settings.fred_api_key.get_secret_value():
        registry.set_disabled(ProviderName.FRED, "FRED_API_KEY is not set")

    if not settings.t212_metadata_enabled:
        registry.set_disabled(ProviderName.TRADING212, "T212_METADATA_ENABLED is false")
    elif not settings.broker_credentials_present:
        # Read-only metadata still needs a key pair; without one the instrument
        # universe cannot be synced and resolution reports NOT_FOUND honestly.
        registry.set_disabled(ProviderName.TRADING212, "Trading 212 credentials are not set")

    # One list, one wording: `telegram_blockers` is also what the bot's own
    # health panel and `/status` render, so a disabled reason cannot drift
    # between the startup log and the answer an operator reads in Telegram.
    # Refusing to run an allowlist-free bot is deliberate: an empty allowlist
    # must never mean "everyone".
    if blockers := settings.telegram_blockers:
        registry.set_disabled(ProviderName.TELEGRAM, "; ".join(blockers))


def instance_identity() -> str:
    """Stable-ish identifier for this process, used to stamp job locks."""
    return f"{os.uname().nodename}:{os.getpid()}"
