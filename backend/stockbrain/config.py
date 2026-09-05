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

from stockbrain.enums import ExecutionPolicy

__all__ = [
    "AppEnv",
    "BrokerEnvironment",
    "ExecutionMode",
    "ExecutionPolicy",
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

    Deliberately **not** the same axis as :class:`ExecutionPolicy`.  This gates
    *transmission* to the broker (Phase 8) and its four-gate live truth table is
    unchanged by Phase 6; ``ExecutionPolicy`` gates *authorization*.
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

    research_enabled: bool = True
    research_timeout_seconds: float = Field(default=600, ge=30, le=3600)
    research_max_output_tokens: int = Field(default=3000, ge=256, le=16000)

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
    """IEX is the only feed available without a paid subscription, so it is the
    default. Alpaca also documents ``otc``, ``boats`` and ``overnight`` feeds;
    StockBrain does not offer them because none maps to an execution-grade
    price source for a US equity position (see ``docs/sources.md``)."""

    alpaca_market_data_enabled: bool = True
    market_data_max_quote_age_seconds: float = 15.0
    """Above this a quote may not size an order (spec section 20's
    ``max_quote_age_seconds``). Phase 4 records the age; Phase 6 enforces it
    through :func:`stockbrain.market_data.base.quote_blockers`."""

    market_data_probe_symbol: str = "AAPL"
    """Liquid US equity used for the one-request startup entitlement probe."""

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

    t212_automated_trading_consent_confirmed: bool = False
    """Records that Trading 212's prior written consent for automated order
    determination has actually been obtained.

    Their API Terms clause 4.2(a) prohibits Algorithmic Trading -- a computer
    determining order parameters with limited or no human intervention -- and
    clauses 6.6/6.7 require prior written consent for an automated customised
    interface.  ``EXECUTION_POLICY=automatic`` against the **live** environment
    is exactly that activity, so it requires this flag.  The flag states a fact
    about the operator's relationship with their broker; it is not a bypass, and
    there is no general "ignore broker rules" switch."""

    t212_execution_enabled: bool = False
    """Master switch for **any** broker order transmission, demo included.

    Default false so that deploying Phase 8 does not, by itself, start sending
    orders.  Turning it on is a deliberate operator act; it is *not* one of the
    four live gates and never substitutes for them -- with ``T212_ENV=live``
    those still apply on top."""

    t212_order_extended_hours: bool = False
    """Whether a market order may fill outside the regular session.

    Trading 212 defaults this to false and StockBrain keeps that: the Phase 6
    spread ceiling is calibrated on regular-hours books, and an overnight book
    was measured at 1,024 bps."""

    t212_order_timeout_seconds: float = Field(default=30.0, ge=5.0, le=120.0)
    """Timeout for the one order POST.

    Longer than the read timeout used for GETs: a timeout here does not mean
    "try again", it means the outcome is unknown and reconciliation is required,
    so waiting a little longer for a definitive answer is strictly better than
    giving up on one."""

    execution_enqueue_interval_seconds: float = Field(default=30.0, ge=5.0, le=3600.0)
    reconcile_interval_seconds: float = Field(default=60.0, ge=10.0, le=3600.0)

    execution_reconcile_min_age_seconds: float = Field(default=60.0, ge=5.0, le=3600.0)
    """How long after transmission an attempt must be before reconciliation may
    conclude the order was **not** placed.

    A negative conclusion releases the reservation and fails the proposal, so it
    has to survive the broker's own propagation delay: an order that exists but
    has not yet appeared in either the pending list or the history would
    otherwise be read as proof of absence."""

    execution_reconcile_window_seconds: float = Field(default=900.0, ge=60.0, le=86400.0)
    """How far either side of ``sent_at`` a broker order may be and still be
    considered a candidate match for an attempt."""

    execution_reconcile_max_attempts: int = Field(default=20, ge=1, le=1000)
    """After this many inconclusive passes an attempt stops being swept and waits
    for a human. An ambiguous order is not a thing to poll forever."""

    t212_account_refresh_interval_seconds: int = 60
    """How often broker cash and positions are re-read. The summary endpoint
    allows one request per 5 seconds and positions one per second, so a minute
    is comfortable; the risk engine independently refuses a snapshot older than
    ``RISK_MAX_ACCOUNT_STATE_AGE_SECONDS``."""

    t212_metadata_enabled: bool = True
    instrument_refresh_interval_minutes: int = 360
    """Trading 212 refreshes instrument metadata every 10 minutes and rate-limits
    the endpoint to one request per 50 seconds; six-hourly is ample for a
    universe that changes by a handful of listings a week."""

    instrument_staleness_hours: int = 24
    """Older than this and the startup sequence enqueues a refresh (spec section
    23 step 12)."""

    # ------------------------------------------------------------------
    # Execution policy and deterministic risk (spec section 14/20)
    #
    # Every value below is a *limit*, not a recommendation. They are
    # deliberately conservative defaults and are user-editable; nothing here is
    # investment advice. See `stockbrain.risk.config` for the versioned object
    # these feed, which is what a proposal actually records.
    # ------------------------------------------------------------------
    execution_policy: ExecutionPolicy = ExecutionPolicy.MANUAL
    """Who authorizes a proposal. Recorded on each proposal at creation, so
    flipping this can never retroactively authorize existing work."""

    proposals_enabled: bool = True

    risk_allowed_instrument_types: CommaSeparatedStrs
    risk_allowed_sessions: CommaSeparatedStrs
    risk_require_known_session: bool = True

    risk_max_spread_bps: Decimal = Decimal("50")
    """0.50% of mid. A live overnight IEX book showed a $33 spread on a $321
    AAPL mid -- 1024 bps, a 10% round trip. Age alone caught that one; a book
    that wide during regular hours would be fresh and still ruinous."""

    risk_spread_policy: Literal["block", "reduce"] = "block"
    risk_wide_spread_size_factor: Decimal = Decimal("0.5")

    risk_max_account_state_age_seconds: float = 300.0
    risk_require_same_currency: bool = True

    risk_max_notional_per_trade: Decimal = Decimal("500")
    risk_max_trade_pct: Decimal = Decimal("0.02")
    risk_max_position_pct: Decimal = Decimal("0.03")
    risk_max_aggregate_exposure_pct: Decimal = Decimal("0.60")
    risk_min_cash_reserve_pct: Decimal = Decimal("0.10")
    risk_min_trade_notional: Decimal = Decimal("20")
    risk_allow_fractional_quantity: bool = False

    risk_max_active_proposals: int = 5
    risk_max_active_proposal_exposure_pct: Decimal = Decimal("0.10")
    risk_proposal_ttl_minutes: int = 30
    risk_max_reference_price_drift_pct: Decimal = Decimal("0.01")

    risk_min_research_confidence: Decimal = Decimal("0.70")
    risk_confidence_modulates_size: bool = True
    risk_min_confidence_size_factor: Decimal = Decimal("0.5")
    risk_reduce_fraction: Decimal = Decimal("0.5")

    proposal_revalidation_batch: int = 5
    """How many active proposals the invalidation sweep re-prices per tick.
    Bounded so the sweep cannot turn into an unmetered market-data spend."""

    # ------------------------------------------------------------------
    # Telegram
    # ------------------------------------------------------------------
    telegram_enabled: bool = False
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_allowed_user_ids: CommaSeparatedInts
    telegram_allowed_chat_ids: CommaSeparatedInts
    """Numeric ids only, both lists. A Telegram username is chosen by its owner
    and can be released and re-registered by somebody else, so it is never an
    authorization input (spec section 4.8). An empty user allowlist authorises
    nobody -- it is never read as "everyone"."""

    telegram_allow_group_chats: bool = False
    """Whether the bot may be used from a non-private chat at all.

    Stated as its own switch rather than inferred from a non-empty
    ``TELEGRAM_ALLOWED_CHAT_IDS``, because "who may act" and "where they may act
    from" are different questions. With this false, only a private chat between
    the bot and an allowlisted user is accepted, whatever the chat allowlist
    says. With it true, a non-private chat must *also* appear in
    ``TELEGRAM_ALLOWED_CHAT_IDS``: everyone who can read a group can read a
    proposal posted in it."""

    telegram_poll_timeout_seconds: int = Field(default=30, ge=1, le=50)
    """``getUpdates`` long-polling timeout. Telegram documents short polling
    (timeout 0) as "for testing purposes only"."""

    telegram_poll_interval_seconds: float = Field(default=0.0, ge=0.0, le=60.0)

    telegram_health_interval_seconds: float = Field(default=60.0, ge=10.0, le=3600.0)
    """How often the supervisor proves the bot can still reach Telegram. A quiet
    bot receives no updates, so "we have not crashed" is not evidence of
    connectivity; one ``getMe`` is."""

    telegram_max_backoff_seconds: float = Field(default=300.0, ge=1.0, le=3600.0)
    """Ceiling on the supervisor's reconnect backoff. python-telegram-bot retries
    inside its own polling loop (1.5x, capped at 30s); this bounds the outer
    loop that restarts polling after a bootstrap failure."""

    telegram_action_ttl_seconds: int = Field(default=900, ge=30, le=86400)
    """Lifetime of an Approve/Reject/Details callback token. Always additionally
    clamped to the proposal's own expiry: a button may never outlive the trade
    it refers to."""

    telegram_confirm_ttl_seconds: int = Field(default=120, ge=15, le=3600)
    """Lifetime of the stage-two confirmation token. Short on purpose: the
    confirmation exists to prove the person is still there and still means it."""

    telegram_notifications_enabled: bool = True
    telegram_research_interval_seconds: float = Field(default=30.0, ge=0.0, le=3600.0)
    """Minimum spacing between ``/research`` invocations per user (spec section
    20: "manual research should be rate limited"). This command only reads a
    stored thesis, but it is the command most likely to be repeated."""

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
        "risk_allowed_instrument_types",
        "risk_allowed_sessions",
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

    @field_validator("execution_policy", mode="before")
    @classmethod
    def _normalise_execution_policy(cls, value: object) -> object:
        """Accept ``automatic`` as well as ``AUTOMATIC``.

        The enum's values are upper case because they are a persisted
        PostgreSQL type, but ``EXECUTION_POLICY=automatic`` is what an operator
        naturally writes beside ``EXECUTION_MODE=manual_approval``.  Refusing it
        over capitalisation would be a configuration error that teaches nothing.
        """
        return value.strip().upper() if isinstance(value, str) else value

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
    def _validate_automation_gate(self) -> Settings:
        """Refuse live automatic authorization without recorded broker consent.

        Trading 212's API Terms clause 4.2(a) prohibits Algorithmic Trading, and
        6.6/6.7 require prior written consent for an automated customised
        interface.  ``EXECUTION_POLICY=automatic`` against ``T212_ENV=live`` is
        exactly that, so the process refuses to start rather than silently
        degrading to manual -- a deployment that asked for automatic and quietly
        got manual is a deployment nobody is watching.
        """
        if (
            self.execution_policy is ExecutionPolicy.AUTOMATIC
            and self.t212_env is BrokerEnvironment.LIVE
            and not self.t212_automated_trading_consent_confirmed
        ):
            raise ValueError(
                "EXECUTION_POLICY=automatic with T212_ENV=live requires "
                "T212_AUTOMATED_TRADING_CONSENT_CONFIRMED=true: Trading 212's API Terms "
                "clause 4.2(a) prohibits Algorithmic Trading and clauses 6.6/6.7 require prior "
                "written consent before a customised interface determines order parameters "
                "automatically. Refusing to start: StockBrain never resolves an ambiguous "
                "automation configuration in favour of automation. Use T212_ENV=demo to "
                "exercise automatic authorization against paper trading."
            )
        return self

    @model_validator(mode="after")
    def _validate_risk_limits(self) -> Settings:
        """Reject a risk configuration that cannot mean anything.

        A negative or above-one percentage, an inverted confidence band or a
        non-positive TTL are configuration errors, not values to clamp: silently
        correcting a limit is how a limit stops being the one that was chosen.
        """
        fractions = {
            "RISK_MAX_TRADE_PCT": self.risk_max_trade_pct,
            "RISK_MAX_POSITION_PCT": self.risk_max_position_pct,
            "RISK_MAX_AGGREGATE_EXPOSURE_PCT": self.risk_max_aggregate_exposure_pct,
            "RISK_MIN_CASH_RESERVE_PCT": self.risk_min_cash_reserve_pct,
            "RISK_MAX_ACTIVE_PROPOSAL_EXPOSURE_PCT": self.risk_max_active_proposal_exposure_pct,
            "RISK_MAX_REFERENCE_PRICE_DRIFT_PCT": self.risk_max_reference_price_drift_pct,
            "RISK_MIN_RESEARCH_CONFIDENCE": self.risk_min_research_confidence,
            "RISK_MIN_CONFIDENCE_SIZE_FACTOR": self.risk_min_confidence_size_factor,
            "RISK_WIDE_SPREAD_SIZE_FACTOR": self.risk_wide_spread_size_factor,
            "RISK_REDUCE_FRACTION": self.risk_reduce_fraction,
        }
        problems = [
            f"{name} must be between 0 and 1 (got {value})"
            for name, value in fractions.items()
            if value < 0 or value > 1
        ]
        if self.risk_reduce_fraction <= 0:
            problems.append("RISK_REDUCE_FRACTION must be greater than 0")
        if self.risk_max_spread_bps <= 0:
            problems.append("RISK_MAX_SPREAD_BPS must be greater than 0")
        if self.risk_max_notional_per_trade <= 0:
            problems.append("RISK_MAX_NOTIONAL_PER_TRADE must be greater than 0")
        if self.risk_min_trade_notional < 0:
            problems.append("RISK_MIN_TRADE_NOTIONAL must not be negative")
        if self.risk_proposal_ttl_minutes <= 0:
            problems.append("RISK_PROPOSAL_TTL_MINUTES must be greater than 0")
        if self.risk_max_active_proposals <= 0:
            problems.append("RISK_MAX_ACTIVE_PROPOSALS must be greater than 0")
        if self.risk_max_account_state_age_seconds <= 0:
            problems.append("RISK_MAX_ACCOUNT_STATE_AGE_SECONDS must be greater than 0")
        if problems:
            raise ValueError("Invalid risk configuration: " + "; ".join(problems))
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

    @property
    def order_transmission_blockers(self) -> list[str]:
        """Every reason an order may not be transmitted **in this environment**.

        Deliberately a superset of :attr:`execution_blockers`, which is
        live-specific.  Demo transmission is permitted with credentials, the
        master switch and ``EXECUTION_MODE=manual_approval``; live transmission
        additionally requires all four live gates.  Keeping them in one list
        means the GUI banner, the API and the send-time check cannot disagree
        about why nothing is being sent.
        """
        blockers: list[str] = []
        if not self.t212_execution_enabled:
            blockers.append("T212_EXECUTION_ENABLED is false")
        if not self.broker_credentials_present:
            blockers.append("Trading 212 API credentials are not configured")
        if self.execution_mode is not ExecutionMode.MANUAL_APPROVAL:
            blockers.append(f"EXECUTION_MODE is '{self.execution_mode.value}'")
        if self.t212_env is BrokerEnvironment.LIVE:
            blockers.extend(self.execution_blockers)
        return blockers

    @property
    def order_transmission_permitted(self) -> bool:
        """Defined as "no blockers remain", so the two can never disagree.

        Permission to *transmit*, which is a different question from permission
        to *authorize*: the latter is :attr:`automatic_authorization_permitted`.
        Neither implies the other, and a send-time check consults this one.
        """
        return not self.order_transmission_blockers

    @property
    def automation_blockers(self) -> list[str]:
        """Every reason automatic authorization is unavailable, GUI-ready.

        Deliberately a *different* list from :attr:`execution_blockers`.
        Authorizing a proposal without a human and transmitting an order to a
        broker are separate permissions with separate gates; conflating them
        would let one be granted by satisfying the other.
        """
        from stockbrain.broker.automation import automation_capability

        blockers: list[str] = []
        if self.execution_policy is not ExecutionPolicy.AUTOMATIC:
            blockers.append(f"EXECUTION_POLICY is '{self.execution_policy.value}'")
        if not self.proposals_enabled:
            blockers.append("PROPOSALS_ENABLED is false")
        blockers.extend(automation_capability(self).blockers)
        return blockers

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_enabled and self.telegram_bot_token.get_secret_value())

    @property
    def telegram_blockers(self) -> list[str]:
        """Every reason the Telegram bot will not start, in GUI-ready wording.

        Same shape as :attr:`execution_blockers`: the "is it available"
        predicate is defined as "no blockers remain", so a health panel can
        never show a blocker beside a green light.
        """
        blockers: list[str] = []
        if not self.telegram_enabled:
            blockers.append("TELEGRAM_ENABLED is false")
        if not self.telegram_bot_token.get_secret_value():
            blockers.append("TELEGRAM_BOT_TOKEN is not set")
        if not self.telegram_allowed_user_ids:
            blockers.append(
                "TELEGRAM_ALLOWED_USER_IDS is empty; an empty allowlist authorises nobody"
            )
        return blockers

    @property
    def telegram_available(self) -> bool:
        return not self.telegram_blockers

    @property
    def telegram_notification_targets(self) -> list[int]:
        """Numeric chat ids a proposal notification is delivered to.

        An explicit chat allowlist wins; otherwise each allowlisted user is
        messaged in their own private chat, whose id equals their user id. The
        bot never discovers a destination from an incoming message: a chat that
        was never configured is never written to.
        """
        if self.telegram_allowed_chat_ids:
            return list(dict.fromkeys(self.telegram_allowed_chat_ids))
        return list(dict.fromkeys(self.telegram_allowed_user_ids))

    @property
    def automatic_authorization_permitted(self) -> bool:
        """The single authoritative predicate for authorizing without a human.

        Defined as "no blockers remain", so this and :attr:`automation_blockers`
        can never disagree.  It permits *authorization* only: Phase 6 sends no
        broker order under any policy, and Phase 8's transmission gate is
        :attr:`live_execution_permitted`, which this does not touch.
        """
        return not self.automation_blockers


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
