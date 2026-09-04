"""Typed application configuration.

All configuration enters the process through environment variables (or a local
``.env`` file during development).  Nothing in this module ever writes a secret
to a log or to an HTTP response: every credential is held as a
:class:`pydantic.SecretStr`, so accidental interpolation renders ``**********``.

Safety-critical rule implemented here (spec sections 4.1 and 26): Trading 212
*live* execution requires **all** of the following to be true simultaneously::

    T212_ENV=live
    T212_LIVE_EXECUTION_ENABLED=true
    T212_WRITTEN_CONSENT_CONFIRMED=true
    EXECUTION_MODE=manual_approval

Any other combination resolves to "live execution is not permitted".  The system
never flips from demo to live implicitly.
"""

from __future__ import annotations

import functools
import json
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    Field,
    PostgresDsn,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

__all__ = [
    "AppEnv",
    "BrokerEnvironment",
    "ExecutionMode",
    "LogFormat",
    "Settings",
    "get_settings",
]


class AppEnv(StrEnum):
    LOCAL = "local"
    PRODUCTION = "production"
    TEST = "test"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class BrokerEnvironment(StrEnum):
    """Trading 212 API environment.

    ``demo`` maps to ``https://demo.trading212.com/api/v0``;
    ``live`` maps to ``https://live.trading212.com/api/v0``.
    """

    DEMO = "demo"
    LIVE = "live"


class ExecutionMode(StrEnum):
    """How orders may reach the broker.

    ``manual_approval`` is the only mode that permits any broker mutation, and
    even then only after two explicit human confirmations.  ``research_only``
    generates proposals but refuses every execution path.
    """

    RESEARCH_ONLY = "research_only"
    MANUAL_APPROVAL = "manual_approval"


# ``NoDecode`` stops pydantic-settings from JSON-decoding the raw environment
# value, so the ``_split_csv`` validator below can accept the plain
# ``1,2,3`` form that is natural in a .env file. Without it, pydantic-settings
# fails before any validator runs.
CommaSeparatedInts = Annotated[list[int], NoDecode, Field(default_factory=list)]
CommaSeparatedStrs = Annotated[list[str], NoDecode, Field(default_factory=list)]


class Settings(BaseSettings):
    """Process-wide configuration, validated once at import of :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_default=True,
    )

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    app_env: AppEnv = AppEnv.LOCAL
    app_name: str = "StockBrain"
    app_version: str = "0.1.0"
    timezone: str = "Europe/London"
    """Display timezone only.  All persisted timestamps are timezone-aware UTC."""

    http_host: str = "0.0.0.0"  # noqa: S104 - container-internal bind, published via compose
    http_port: int = 8080
    api_prefix: str = "/api/v1"

    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    stockbrain_secret_key: SecretStr = SecretStr("")
    """Signing key for session cookies and approval-token HMACs."""

    cors_allow_origins: CommaSeparatedStrs
    """Only needed for the Vite dev server; production serves the SPA same-origin."""

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+asyncpg://stockbrain:stockbrain@localhost:5432/stockbrain"),
    )
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_pool_timeout_seconds: int = 30
    db_echo: bool = False

    # ------------------------------------------------------------------
    # Jobs / scheduler
    # ------------------------------------------------------------------
    job_worker_concurrency: int = 4
    job_poll_interval_seconds: float = 1.0
    job_claim_timeout_seconds: int = 900
    """A RUNNING job whose lock is older than this is considered abandoned."""

    # ------------------------------------------------------------------
    # DeepSeek
    # ------------------------------------------------------------------
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_flash_model: str = "deepseek-v4-flash"
    deepseek_pro_model: str = "deepseek-v4-pro"
    deepseek_timeout_seconds: float = 120.0
    deepseek_max_attempts: int = 3

    # ------------------------------------------------------------------
    # Classification thresholds
    #
    # Model scores are ranking features, not calibrated probabilities. These
    # decide only where to spend deeper analysis, never what to trade.
    # ------------------------------------------------------------------
    classifier_enabled: bool = True
    classifier_min_importance: float = 0.60
    classifier_min_confidence: float = 0.65
    classifier_min_materiality: float = 0.50

    semantic_dedupe_enabled: bool = True
    semantic_dedupe_min_confidence: float = 0.70
    """A merge must clear this bar. Keeping two events apart is recoverable;
    merging two different events silently is not."""

    # ------------------------------------------------------------------
    # LLM budgets (USD). Telemetry-driven, never part of a trading decision.
    # ------------------------------------------------------------------
    llm_daily_soft_usd: Decimal = Decimal("2.00")
    llm_daily_hard_usd: Decimal = Decimal("5.00")
    llm_monthly_soft_usd: Decimal = Decimal("30.00")
    llm_monthly_hard_usd: Decimal = Decimal("75.00")

    # ------------------------------------------------------------------
    # Alpaca (news + market data; never execution)
    # ------------------------------------------------------------------
    alpaca_api_key: SecretStr = SecretStr("")
    alpaca_api_secret: SecretStr = SecretStr("")
    alpaca_news_ws_url: str = "wss://stream.data.alpaca.markets/v1beta1/news"
    alpaca_data_base_url: str = "https://data.alpaca.markets"
    alpaca_stock_feed: Literal["iex", "sip", "delayed_sip"] = "iex"

    # ------------------------------------------------------------------
    # Firecrawl
    # ------------------------------------------------------------------
    firecrawl_api_key: SecretStr = SecretStr("")
    firecrawl_base_url: str = "https://api.firecrawl.dev"

    # ------------------------------------------------------------------
    # SEC EDGAR (no API key; descriptive User-Agent is mandatory)
    # ------------------------------------------------------------------
    sec_contact_email: str = ""
    sec_base_url: str = "https://data.sec.gov"
    sec_www_base_url: str = "https://www.sec.gov"

    # ------------------------------------------------------------------
    # FRED
    # ------------------------------------------------------------------
    fred_api_key: SecretStr = SecretStr("")
    fred_base_url: str = "https://api.stlouisfed.org/fred"

    # ------------------------------------------------------------------
    # Trading 212
    # ------------------------------------------------------------------
    t212_api_key: SecretStr = SecretStr("")
    t212_api_secret: SecretStr = SecretStr("")
    t212_env: BrokerEnvironment = BrokerEnvironment.DEMO
    t212_live_execution_enabled: bool = False
    t212_written_consent_confirmed: bool = False
    t212_timeout_seconds: float = 20.0
    execution_mode: ExecutionMode = ExecutionMode.MANUAL_APPROVAL

    # ------------------------------------------------------------------
    # Telegram
    # ------------------------------------------------------------------
    telegram_enabled: bool = False
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_allowed_user_ids: CommaSeparatedInts
    telegram_allowed_chat_ids: CommaSeparatedInts

    # ------------------------------------------------------------------
    # Subsystem toggles (env-level defaults; runtime flags live in PostgreSQL)
    # ------------------------------------------------------------------
    discovery_enabled: bool = True
    alpaca_news_enabled: bool = True
    firecrawl_enabled: bool = True
    sec_enabled: bool = True

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------
    @field_validator("database_url", mode="after")
    @classmethod
    def _require_async_driver(cls, value: PostgresDsn) -> PostgresDsn:
        if value.scheme != "postgresql+asyncpg":
            raise ValueError(
                "DATABASE_URL must use the asyncpg driver, e.g. "
                "postgresql+asyncpg://user:pass@host:5432/stockbrain "
                f"(got scheme {value.scheme!r})"
            )
        return value

    @field_validator(
        "telegram_allowed_user_ids",
        "telegram_allowed_chat_ids",
        "cors_allow_origins",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept ``1,2,3`` in addition to JSON lists, which is friendlier in ``.env``.

        These fields are annotated ``NoDecode``, so JSON has to be handled here
        rather than by pydantic-settings' own decoding step.
        """
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return json.loads(stripped)
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return value

    @field_validator("log_level", mode="after")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if level not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}")
        return level

    @model_validator(mode="after")
    def _validate_execution_gate(self) -> Settings:
        """Reject contradictory execution configuration rather than silently degrading.

        Enabling live execution without recorded written consent is a
        configuration error, not something to warn about and continue past.
        """
        if self.t212_live_execution_enabled:
            problems: list[str] = []
            if self.t212_env is not BrokerEnvironment.LIVE:
                problems.append("T212_ENV must be 'live'")
            if not self.t212_written_consent_confirmed:
                problems.append("T212_WRITTEN_CONSENT_CONFIRMED must be true")
            if self.execution_mode is not ExecutionMode.MANUAL_APPROVAL:
                problems.append("EXECUTION_MODE must be 'manual_approval'")
            if problems:
                raise ValueError(
                    "T212_LIVE_EXECUTION_ENABLED=true is incompatible with the rest of the "
                    "configuration: " + "; ".join(problems) + ". Refusing to start: StockBrain "
                    "never resolves an ambiguous live-execution configuration in favour of live."
                )
        return self

    @model_validator(mode="after")
    def _validate_budgets(self) -> Settings:
        """A hard limit below its soft limit would make the soft limit unreachable."""
        if self.llm_daily_hard_usd < self.llm_daily_soft_usd:
            raise ValueError("LLM_DAILY_HARD_USD must be >= LLM_DAILY_SOFT_USD")
        if self.llm_monthly_hard_usd < self.llm_monthly_soft_usd:
            raise ValueError("LLM_MONTHLY_HARD_USD must be >= LLM_MONTHLY_SOFT_USD")
        return self

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> Settings:
        if self.app_env is AppEnv.PRODUCTION and not self.stockbrain_secret_key.get_secret_value():
            raise ValueError("STOCKBRAIN_SECRET_KEY is required when APP_ENV=production")
        return self

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------
    @property
    def database_url_str(self) -> str:
        return str(self.database_url)

    @property
    def sync_database_url(self) -> str:
        """psycopg-free sync URL used only by Alembic's offline mode helpers."""
        return self.database_url_str.replace("postgresql+asyncpg://", "postgresql://", 1)

    @property
    def t212_base_url(self) -> str:
        if self.t212_env is BrokerEnvironment.LIVE:
            return "https://live.trading212.com/api/v0"
        return "https://demo.trading212.com/api/v0"

    @property
    def broker_credentials_present(self) -> bool:
        return bool(
            self.t212_api_key.get_secret_value() and self.t212_api_secret.get_secret_value()
        )

    @property
    def live_execution_permitted(self) -> bool:
        """The single authoritative predicate for submitting a *live* broker order.

        Defined as "no blockers remain", so this property and
        :attr:`execution_blockers` can never disagree -- a banner that lists a
        blocker while the flag says "permitted" would be worse than either
        answer alone.  Credential presence counts: all four gates set with no
        API key cannot submit anything.

        Callers must consult this *and* a persisted kill switch *and* an
        explicit human confirmation before any live mutation.
        """
        return not self.execution_blockers

    @property
    def execution_blockers(self) -> list[str]:
        """Every reason live execution is unavailable, in GUI-ready wording.

        An empty list is the definition of "live execution is permitted"; see
        :attr:`live_execution_permitted`.
        """
        blockers: list[str] = []
        if self.t212_env is not BrokerEnvironment.LIVE:
            blockers.append("T212_ENV is not 'live' (demo environment in use)")
        if not self.t212_live_execution_enabled:
            blockers.append("T212_LIVE_EXECUTION_ENABLED is false")
        if not self.t212_written_consent_confirmed:
            blockers.append(
                "T212_WRITTEN_CONSENT_CONFIRMED is false: Trading 212 API Terms require prior "
                "written consent for a customised live interface"
            )
        if self.execution_mode is not ExecutionMode.MANUAL_APPROVAL:
            blockers.append(f"EXECUTION_MODE is '{self.execution_mode.value}'")
        if not self.broker_credentials_present:
            blockers.append("Trading 212 API credentials are not configured")
        return blockers


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
