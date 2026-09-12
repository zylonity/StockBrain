"""The operator-facing view of this deployment's configuration.

StockBrain is configured almost entirely from the environment, validated once at
start-up, and then treated as immutable for the life of the process.  That is a
deliberate design: the risk limits, the four live-execution gates and the
provider budgets are all read by code that runs without a human present, and a
value that could change underneath a running evaluation is a value two halves of
one decision could disagree about.

So this module does **not** make configuration editable.  It makes it
*legible*.  Every setting is described with:

``mutability``
    ``RUNTIME`` -- there is a dedicated, audited endpoint that changes it now.
    ``RESTART_REQUIRED`` -- environment-driven; edit ``.env`` and restart.
    ``READ_ONLY`` -- derived from other settings, so it has no switch of its own.
    ``SECRET`` -- write-only.  The API reports whether one is configured and
    never what it is.

``impact``
    What actually changes if the value changes.  An operator reading
    ``RISK_MAX_TRADE_PCT`` needs to know it caps position size, not that it is a
    decimal.

There is deliberately **no generic mutation endpoint**.  A `PUT /settings/{key}`
would be a way to write ``T212_LIVE_EXECUTION_ENABLED`` over HTTP, and the four
gates exist precisely so that turning live execution on is an act performed on
the host with a restart behind it.  The handful of things that genuinely are
runtime state -- the pause, the kill switch, the discovery hold, the
notification preferences -- each have their own typed route with their own audit
trail, and appear here only so the page can render the right control.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import SecretStr

from stockbrain.config import Settings

__all__ = [
    "Mutability",
    "SettingGroupView",
    "SettingSpec",
    "SettingView",
    "build_settings_view",
]


class Mutability(StrEnum):
    """How -- and whether -- a setting can be changed."""

    RUNTIME = "RUNTIME"
    RESTART_REQUIRED = "RESTART_REQUIRED"
    READ_ONLY = "READ_ONLY"
    SECRET = "SECRET"  # noqa: S105 - a mutability class, not a credential


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """One row of the catalogue.

    ``attr`` names a :class:`~stockbrain.config.Settings` attribute or property.
    A spec whose attribute does not exist is a programming error and is caught
    by ``tests/unit/test_settings_model.py`` rather than by an operator seeing a
    blank row.
    """

    attr: str
    label: str
    mutability: Mutability
    description: str
    env_var: str | None = None
    unit: str | None = None
    impact: str | None = None
    control: str | None = None
    """The runtime endpoint that changes this, for ``RUNTIME`` rows only."""


@dataclass(frozen=True, slots=True)
class SettingView:
    key: str
    label: str
    value: str | None
    mutability: Mutability
    description: str
    env_var: str | None
    unit: str | None
    impact: str | None
    control: str | None
    configured: bool | None
    """Only meaningful for ``SECRET`` rows: whether a credential is present."""


@dataclass(frozen=True, slots=True)
class SettingGroupView:
    key: str
    title: str
    description: str
    settings: tuple[SettingView, ...]
    blockers: tuple[str, ...] = ()
    warning: str | None = None
    docs: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class _Group:
    key: str
    title: str
    description: str
    specs: tuple[SettingSpec, ...]
    blockers_attr: str | None = None
    warning: str | None = None


def _s(
    attr: str,
    label: str,
    mutability: Mutability,
    description: str,
    *,
    env_var: str | None = None,
    unit: str | None = None,
    impact: str | None = None,
    control: str | None = None,
) -> SettingSpec:
    return SettingSpec(
        attr=attr,
        label=label,
        mutability=mutability,
        description=description,
        env_var=env_var if env_var is not None else attr.upper(),
        unit=unit,
        impact=impact,
        control=control,
    )


RESTART = Mutability.RESTART_REQUIRED
RUNTIME = Mutability.RUNTIME
READ_ONLY = Mutability.READ_ONLY
SECRET = Mutability.SECRET


CATALOGUE: tuple[_Group, ...] = (
    _Group(
        key="operations",
        title="Operations",
        description=(
            "Live controls and the identity of this deployment. The pause, the kill "
            "switch and the discovery hold are stored in PostgreSQL, so they survive a "
            "restart and are the same state Telegram reads."
        ),
        specs=(
            _s(
                "trading_halted",
                "Trading control",
                RUNTIME,
                "Whether any control flag is currently stopping proposal generation and "
                "authorization. Neither a pause nor the kill switch closes a position or "
                "cancels a broker order.",
                env_var=None,
                impact="Blocks proposal generation and every authorization path.",
                control="control",
            ),
            _s(
                "discovery_hold",
                "Discovery hold",
                RUNTIME,
                "A reversible hold on scheduled discovery work: web searches, the SEC "
                "sweep and the Alpaca backfill stop being enqueued. Classification, "
                "research and reconciliation carry on.",
                env_var=None,
                impact="Stops new paid discovery work without changing any budget.",
                control="discovery",
            ),
            _s(
                "app_env",
                "Environment",
                RESTART,
                "Selects the production safety rules. Production refuses to start without "
                "a signing key and without either authentication or an explicit "
                "trusted-network acknowledgement.",
                impact="Tightens start-up validation and the default cookie policy.",
            ),
            _s("app_version", "Version", READ_ONLY, "The running build.", env_var=None),
            _s(
                "timezone",
                "Display timezone",
                RESTART,
                "The operator's timezone. Every stored instant is UTC regardless.",
            ),
            _s(
                "log_level",
                "Log level",
                RESTART,
                "The floor for what is written to stdout and captured in the Logs page.",
                impact="A level above INFO hides routine pipeline activity from Logs.",
            ),
            _s("log_format", "Log format", RESTART, "JSON for shipping, console for reading."),
            _s(
                "log_buffer_size",
                "Log buffer",
                RESTART,
                "How many recent log events the Logs page can show. Held in memory only, "
                "so the buffer starts empty after a restart.",
                unit="entries",
                impact="Bounds the memory the Logs page costs.",
            ),
            _s(
                "job_worker_concurrency",
                "Job workers",
                RESTART,
                "How many background jobs run at once against the PostgreSQL queue.",
                unit="workers",
            ),
            _s(
                "alerts_enabled",
                "Operational alerts",
                RESTART,
                "Whether the alert scan runs at all. It writes notification rows; it can "
                "never pause, halt or trade.",
            ),
            _s(
                "alert_scan_interval_seconds",
                "Alert scan interval",
                RESTART,
                "How often the alert conditions are evaluated. No external call is made.",
                unit="seconds",
            ),
            _s(
                "alert_queue_backlog_seconds",
                "Queue backlog alert",
                RESTART,
                "How long the oldest pending job may wait before it is reported as a "
                "stalled pipeline.",
                unit="seconds",
            ),
            _s(
                "startup_probe_enabled",
                "Start-up provider probes",
                RESTART,
                "Actively test every configured provider on boot so the health board "
                "reads as this deployment rather than as 'not yet checked'. No probe "
                "makes a billable request.",
            ),
        ),
    ),
    _Group(
        key="web",
        title="Web access",
        description=(
            "This interface can authorize real broker orders. Authentication is on by "
            "default and every state-changing route additionally requires a CSRF token "
            "and a same-origin request."
        ),
        specs=(
            _s(
                "web_auth_enabled",
                "Password authentication",
                RESTART,
                "Whether a session is required. Turning it off is only supported "
                "alongside an explicit trusted-network acknowledgement.",
                impact="Off means anything that can open a socket to the port can trade.",
            ),
            _s("web_owner_username", "Owner username", RESTART, "The single local account."),
            _s(
                "web_owner_password_hash",
                "Owner password",
                SECRET,
                "An scrypt hash generated by 'python -m stockbrain.hash_password'. The "
                "password itself is never stored or transmitted to this API.",
                impact="Without it, authentication is enabled and unusable, and every "
                "route answers 503.",
            ),
            _s(
                "stockbrain_secret_key",
                "Session signing key",
                SECRET,
                "Signs the session cookie. Rotating it invalidates every open session.",
            ),
            _s(
                "web_session_ttl_seconds",
                "Session lifetime",
                RESTART,
                "How long a signed-in browser stays signed in.",
                unit="seconds",
            ),
            _s(
                "web_cookie_secure",
                "Secure cookie flag",
                RESTART,
                "Unset means on in production and off elsewhere, which is what makes a "
                "plain-HTTP LAN deployment work at all. Set it true behind TLS.",
            ),
            _s(
                "web_trusted_network_acknowledged",
                "Trusted-network model acknowledged",
                RESTART,
                "Records a deliberate choice of network trust instead of a password. It "
                "is not a bypass: it only makes an unauthenticated production deployment "
                "an explicit decision.",
            ),
            _s(
                "cors_allow_origins",
                "CORS origins",
                RESTART,
                "Extra browser origins permitted to call this API. Empty is correct when "
                "the compiled frontend is served from this same process.",
            ),
        ),
        blockers_attr="web_auth_blockers",
    ),
    _Group(
        key="llm",
        title="Language model",
        description=(
            "One OpenAI-compatible endpoint answers classification, semantic "
            "deduplication and research. There is no failover between providers: a "
            "configured backend that fails, fails visibly."
        ),
        specs=(
            _s(
                "llm_provider",
                "Provider profile",
                RESTART,
                "Selects the dialect: how the output cap is spelled, whether JSON schema "
                "is enforced, whether reasoning can be switched off, and how cached "
                "tokens are reported. An unknown value is a start-up error, never a "
                "silent fallback.",
                impact="Changes the request body sent to the endpoint.",
            ),
            _s(
                "active_llm_base_url",
                "Endpoint",
                READ_ONLY,
                "The base URL in use.",
                env_var="LLM_BASE_URL",
            ),
            _s(
                "active_llm_quick_model",
                "Quick model",
                READ_ONLY,
                "Classification and deduplication.",
                env_var="LLM_MODEL",
            ),
            _s(
                "active_llm_deep_model",
                "Deep model",
                READ_ONLY,
                "Research synthesis.",
                env_var="LLM_DEEP_MODEL",
            ),
            _s(
                "llm_api_key",
                "API key",
                SECRET,
                "The credential for the configured endpoint.",
            ),
            _s(
                "active_llm_timeout_seconds",
                "Request timeout",
                READ_ONLY,
                "How long one model call may take.",
                env_var="LLM_TIMEOUT_SECONDS",
                unit="seconds",
            ),
            _s(
                "active_llm_max_attempts",
                "Attempts",
                READ_ONLY,
                "Retries for a transport failure. A refusal is never retried.",
                env_var="LLM_MAX_ATTEMPTS",
            ),
            _s(
                "llm_daily_soft_usd",
                "Daily soft cap",
                RESTART,
                "Past this, only high-priority work is permitted to spend.",
                unit="USD",
            ),
            _s(
                "llm_daily_hard_usd",
                "Daily hard cap",
                RESTART,
                "An absolute ceiling. Model work stops for the rest of the day.",
                unit="USD",
                impact="Reached, the LLM provider reports BUDGET_EXHAUSTED rather than a fault.",
            ),
            _s(
                "llm_monthly_soft_usd",
                "Monthly soft cap",
                RESTART,
                "As above, per month.",
                unit="USD",
            ),
            _s(
                "llm_monthly_hard_usd",
                "Monthly hard cap",
                RESTART,
                "As above, per month.",
                unit="USD",
            ),
            _s(
                "research_enabled",
                "Research",
                RESTART,
                "Whether the multi-agent research runner is built at all.",
            ),
            _s(
                "research_min_impact_materiality",
                "Research materiality floor",
                RESTART,
                "Materiality an individual company impact must reach before it is "
                "researched. The classifier's own threshold promotes an event on its "
                "best-affected company, so without this floor a story's every "
                "passing mention is researched at full depth.",
            ),
            _s(
                "research_max_impacts_per_event",
                "Research fan-out cap",
                RESTART,
                "Most companies researched for one event, highest materiality first. "
                "0 disables the cap. A market round-up names dozens of tickers and "
                "each one is otherwise a full debate.",
            ),
            _s(
                "research_timeout_seconds",
                "Research timeout",
                RESTART,
                "Ceiling on one whole research run.",
                unit="seconds",
            ),
            _s(
                "research_max_output_tokens",
                "Research output cap",
                RESTART,
                "Output ceiling per research call. Reasoning tokens are charged as output "
                "and count against this, so a cap set too low is consumed by reasoning "
                "and truncates the answer.",
                unit="tokens",
            ),
        ),
    ),
    _Group(
        key="classification",
        title="Classification thresholds",
        description=(
            "What makes an ingested article worth promoting. These are the gates between "
            "'discovered' and 'candidate for research', and they are the cheapest place "
            "to control spend."
        ),
        specs=(
            _s(
                "classifier_enabled", "Classifier", RESTART, "Whether events are classified at all."
            ),
            _s(
                "classifier_min_importance",
                "Minimum importance",
                RESTART,
                "Below this the event is not promoted to a research candidate.",
                impact="Raising it reduces research spend and increases what is missed.",
            ),
            _s(
                "classifier_min_confidence",
                "Minimum confidence",
                RESTART,
                "The classifier's own confidence floor for promotion.",
            ),
            _s(
                "classifier_min_materiality",
                "Minimum materiality",
                RESTART,
                "How material the event must be to a specific company before that "
                "company's listing is resolved.",
            ),
            _s(
                "semantic_dedupe_enabled",
                "Semantic deduplication",
                RESTART,
                "Whether near-duplicate events are merged by the model as well as by URL "
                "and title.",
            ),
            _s(
                "semantic_dedupe_min_confidence",
                "Deduplication confidence",
                RESTART,
                "How sure the model must be before two events are merged.",
                impact="Too high leaves duplicate events; too low merges distinct stories.",
            ),
        ),
    ),
    _Group(
        key="discovery",
        title="Discovery and extraction",
        description=(
            "Where events come from, how often, and what each source is allowed to cost. "
            "Every paid call is reserved in the database before the request leaves, so a "
            "process that dies mid-call is charged rather than forgotten."
        ),
        specs=(
            _s("discovery_enabled", "Discovery", RESTART, "The master switch for ingestion."),
            _s("alpaca_news_enabled", "Alpaca news stream", RESTART, "Real-time news WebSocket."),
            _s("sec_enabled", "SEC EDGAR", RESTART, "Regulatory filings for the watchlist."),
            _s("web_discovery_enabled", "Web discovery", RESTART, "Scheduled thematic searches."),
            _s(
                "web_discovery_routine_provider",
                "Routine search provider",
                RESTART,
                "Keyword-shaped recent-news searches.",
            ),
            _s(
                "web_discovery_semantic_provider",
                "Semantic search provider",
                RESTART,
                "Second-order discovery. Roughly ten times the price per call, which is "
                "why it has its own provider and its own cadence floor.",
            ),
            _s(
                "web_discovery_min_query_interval_minutes",
                "Routine cadence floor",
                RESTART,
                "The fastest a stored routine query may be searched, whatever the topic row says.",
                unit="minutes",
            ),
            _s(
                "web_discovery_min_semantic_interval_minutes",
                "Semantic cadence floor",
                RESTART,
                "The same floor for semantic queries.",
                unit="minutes",
            ),
            _s(
                "web_discovery_failure_cooldown_minutes",
                "Failure cooldown",
                RESTART,
                "How long a failing query waits before it is tried again. Written "
                "durably, so a restart does not pay for it.",
                unit="minutes",
            ),
            _s("brave_api_key", "Brave API key", SECRET, "Credential for routine search."),
            _s(
                "brave_max_searches_per_day",
                "Brave daily searches",
                RESTART,
                "Hard cap.",
                unit="searches",
            ),
            _s(
                "brave_max_searches_per_month",
                "Brave monthly searches",
                RESTART,
                "Hard cap.",
                unit="searches",
            ),
            _s(
                "brave_result_limit",
                "Brave results per search",
                RESTART,
                "One request either way; this only bounds what is parsed.",
                unit="results",
            ),
            _s("exa_api_key", "Exa API key", SECRET, "Credential for semantic search."),
            _s(
                "exa_max_searches_per_day",
                "Exa daily searches",
                RESTART,
                "Hard cap.",
                unit="searches",
            ),
            _s(
                "exa_max_searches_per_month",
                "Exa monthly searches",
                RESTART,
                "Hard cap.",
                unit="searches",
            ),
            _s(
                "exa_search_type",
                "Exa search type",
                RESTART,
                "Depth, and therefore price, of a semantic search.",
            ),
            _s(
                "content_extraction_enabled",
                "Content extraction",
                RESTART,
                "Fetch and extract article bodies. Local and free by default.",
            ),
            _s(
                "content_extract_max_per_day",
                "Extractions per day",
                RESTART,
                "Ceiling on local extraction work.",
                unit="pages",
            ),
            _s(
                "firecrawl_api_key",
                "Firecrawl API key",
                SECRET,
                "Credential for the paid fallback extractor.",
            ),
            _s(
                "firecrawl_fallback_extraction_enabled",
                "Firecrawl fallback",
                RESTART,
                "Whether a page local extraction could not read may be fetched by the "
                "paid extractor. Off by default.",
                impact="On, a paywalled page can cost credits.",
            ),
            _s(
                "firecrawl_daily_credit_cap",
                "Firecrawl daily credits",
                RESTART,
                "Hard cap.",
                unit="credits",
            ),
            _s(
                "firecrawl_monthly_credit_cap",
                "Firecrawl monthly credits",
                RESTART,
                "Hard cap.",
                unit="credits",
            ),
        ),
    ),
    _Group(
        key="market_data",
        title="Market data and foreign exchange",
        description=(
            "What may price a trade. Trading 212's own API data is display-only by its "
            "terms and is never an execution-grade price source; a missing or stale "
            "exchange rate blocks sizing rather than assuming 1.0."
        ),
        specs=(
            _s("alpaca_market_data_enabled", "Alpaca market data", RESTART, "Quotes and bars."),
            _s("alpaca_api_key", "Alpaca API key", SECRET, "Credential for news and market data."),
            _s(
                "alpaca_api_secret",
                "Alpaca API secret",
                SECRET,
                "Credential for news and market data.",
            ),
            _s(
                "alpaca_stock_feed",
                "Quote feed",
                RESTART,
                "IEX is a single venue; SIP is consolidated and requires a paid plan.",
                impact="Feed choice changes the spread a proposal is measured against.",
            ),
            _s(
                "market_data_max_quote_age_seconds",
                "Maximum quote age",
                RESTART,
                "Older than this and a quote cannot size a trade.",
                unit="seconds",
            ),
            _s(
                "fx_provider",
                "FX provider",
                RESTART,
                "The rate source for cross-currency sizing. 'none' is a real, safe "
                "setting: cross-currency proposals simply cannot be sized.",
            ),
            _s(
                "fx_max_age_seconds",
                "Execution rate freshness",
                RESTART,
                "How old a rate may be and still size a real order.",
                unit="seconds",
            ),
            _s(
                "fx_reference_max_age_seconds",
                "Reference rate freshness",
                RESTART,
                "The looser limit for display-grade rates.",
                unit="seconds",
            ),
            _s(
                "fx_allow_reference_grade",
                "Permit reference-grade rates",
                RESTART,
                "Whether a daily reference rate may size a trade. Off by default.",
                impact="On, a rate up to a day old can price a real order.",
            ),
            _s(
                "fx_max_rate_drift_pct",
                "Rate drift envelope",
                RESTART,
                "Past this, an authorized proposal is invalidated rather than resized.",
            ),
        ),
        blockers_attr="fx_blockers",
    ),
    _Group(
        key="execution",
        title="Broker and execution gates",
        description=(
            "Four independent gates stand between this deployment and a live order, and "
            "every one of them is environment-driven on purpose. None is editable over "
            "HTTP: turning live execution on is an act performed on the host, with a "
            "restart behind it."
        ),
        warning=(
            "Changing any gate below requires editing .env and restarting the "
            "application. There is no runtime switch, and adding one would defeat the "
            "gates."
        ),
        specs=(
            _s(
                "t212_env",
                "Broker environment",
                RESTART,
                "demo is Trading 212's paper environment. live is real money.",
                impact="The single largest difference in the system's behaviour.",
            ),
            _s(
                "t212_execution_enabled",
                "Order transmission",
                RESTART,
                "The master switch for any broker order transmission, demo included. Off "
                "by default so that deploying does not, by itself, start sending orders.",
                impact="Off, proposals can be authorized but nothing is transmitted.",
            ),
            _s(
                "t212_live_execution_enabled",
                "Live execution gate",
                RESTART,
                "Gate one of four for real-money submission.",
            ),
            _s(
                "t212_written_consent_confirmed",
                "Broker written consent",
                RESTART,
                "Records that Trading 212's prior written consent for a customised "
                "interface has actually been obtained. It states a fact about the "
                "operator's relationship with their broker; it is not a bypass.",
            ),
            _s(
                "t212_automated_trading_consent_confirmed",
                "Automated-trading consent",
                RESTART,
                "Required before an automatic execution policy may run against live. "
                "Trading 212's API Terms prohibit algorithmic trading without it.",
            ),
            _s(
                "execution_mode",
                "Execution mode",
                RESTART,
                "Whether an order may be transmitted at all, and under what approval.",
            ),
            _s(
                "execution_policy",
                "Authorization policy",
                RESTART,
                "Who is expected to authorize a proposal. Recorded on each proposal at "
                "creation, so flipping this can never retroactively authorize existing "
                "work.",
            ),
            _s("t212_api_key", "Trading 212 API key", SECRET, "Broker credential."),
            _s("t212_api_secret", "Trading 212 API secret", SECRET, "Broker credential."),
            _s(
                "t212_order_extended_hours",
                "Extended-hours orders",
                RESTART,
                "Whether a market order may fill outside the regular session. Off by "
                "default: the spread ceiling is calibrated on regular-hours books.",
            ),
            _s(
                "t212_max_pending_orders_per_ticker",
                "Pending order limit",
                RESTART,
                "The broker's documented functional limit.",
                unit="orders",
            ),
            _s(
                "t212_pending_order_headroom",
                "Pending order headroom",
                RESTART,
                "How many of those slots StockBrain refuses to use, so the limit is never "
                "discovered by hitting it.",
                unit="orders",
            ),
            _s(
                "execution_reconcile_min_age_seconds",
                "Reconciliation grace",
                RESTART,
                "How long after transmission an attempt must be before reconciliation may "
                "conclude the order was not placed.",
                unit="seconds",
            ),
            _s(
                "execution_reconcile_max_attempts",
                "Reconciliation attempts",
                RESTART,
                "After this many inconclusive passes an attempt waits for a human.",
                unit="passes",
            ),
        ),
        blockers_attr="execution_blockers",
    ),
    _Group(
        key="risk",
        title="Deterministic risk limits",
        description=(
            "Every value here is a limit, not a recommendation, and every one is applied "
            "server-side by the deterministic risk engine. A proposal records the policy "
            "version it was evaluated under, so changing a limit never rewrites the "
            "history of a decision."
        ),
        specs=(
            _s(
                "proposals_enabled",
                "Proposal generation",
                RESTART,
                "Whether theses become proposals.",
            ),
            _s(
                "risk_max_notional_per_trade",
                "Maximum trade notional",
                RESTART,
                "Absolute per-trade ceiling.",
                unit="account currency",
            ),
            _s(
                "risk_max_trade_pct",
                "Maximum trade share",
                RESTART,
                "As a fraction of account value.",
            ),
            _s(
                "risk_max_position_pct",
                "Maximum position share",
                RESTART,
                "Ceiling on one holding.",
            ),
            _s(
                "risk_max_aggregate_exposure_pct",
                "Maximum total exposure",
                RESTART,
                "Ceiling across every holding.",
            ),
            _s(
                "risk_min_cash_reserve_pct",
                "Minimum cash reserve",
                RESTART,
                "Cash that may never be spent.",
            ),
            _s(
                "risk_min_trade_notional",
                "Minimum trade notional",
                RESTART,
                "Below this a trade is not worth its costs.",
                unit="account currency",
            ),
            _s(
                "risk_allow_fractional_quantity",
                "Fractional quantities",
                RESTART,
                "Whether a fractional share may be ordered.",
            ),
            _s(
                "risk_max_active_proposals",
                "Maximum active proposals",
                RESTART,
                "How many may await authorization at once.",
                unit="proposals",
            ),
            _s(
                "risk_max_active_proposal_exposure_pct",
                "Maximum pending exposure",
                RESTART,
                "Total reserved by unauthorized proposals.",
            ),
            _s(
                "risk_proposal_ttl_minutes",
                "Proposal lifetime",
                RESTART,
                "How long an unauthorized proposal stays actionable.",
                unit="minutes",
            ),
            _s(
                "proposal_deferral_max_hours",
                "Deferral ceiling",
                RESTART,
                "How long a thesis refused only by market state is re-evaluated at "
                "each open before the block becomes final.",
                unit="hours",
            ),
            _s(
                "risk_max_reference_price_drift_pct",
                "Price drift envelope",
                RESTART,
                "Past this an authorized proposal is invalidated, never resized.",
            ),
            _s(
                "risk_max_spread_bps",
                "Maximum spread",
                RESTART,
                "The widest book a trade may cross.",
                unit="basis points",
            ),
            _s(
                "risk_spread_policy",
                "Wide-spread policy",
                RESTART,
                "Block the trade, or reduce its size.",
            ),
            _s(
                "risk_min_research_confidence",
                "Minimum research confidence",
                RESTART,
                "Below this the thesis does not become a proposal.",
            ),
            _s(
                "risk_confidence_modulates_size",
                "Confidence modulates size",
                RESTART,
                "Whether a weaker thesis produces a smaller position.",
            ),
            _s(
                "risk_require_same_currency",
                "Require matching currency",
                RESTART,
                "Refuse a cross-currency trade outright.",
            ),
            _s(
                "risk_require_known_session",
                "Require known session",
                RESTART,
                "Refuse to trade when the venue's session cannot be determined.",
            ),
            _s(
                "risk_max_account_state_age_seconds",
                "Account snapshot freshness",
                RESTART,
                "How stale broker cash and positions may be.",
                unit="seconds",
            ),
            _s(
                "risk_exit_hard_stop_pct",
                "Exit hard stop",
                RESTART,
                "Loss from average cost at which the whole position is proposed for "
                "exit. A floor, never widened.",
            ),
            _s(
                "risk_exit_trailing_pct",
                "Exit trailing distance",
                RESTART,
                "How far below the high-water mark the trailing floor sits, once armed.",
            ),
            _s(
                "risk_exit_trailing_arm_pct",
                "Exit trailing arm",
                RESTART,
                "Gain from average cost at which the trailing floor switches on. Below "
                "it the hard stop is the only floor.",
            ),
            _s(
                "risk_exit_min_peak_observations",
                "Exit peak observations",
                RESTART,
                "Syncs a peak must be built from before the trailing rule trusts it. One "
                "observation is an entry price wearing a peak's name.",
                unit="observations",
            ),
            _s(
                "exit_sweep_enabled",
                "Exit sweep",
                RESTART,
                "Whether the scheduled sweep that proposes deterministic exits runs.",
            ),
            _s(
                "exit_sweep_interval_seconds",
                "Exit sweep interval",
                RESTART,
                "How often the exit sweep runs.",
                unit="seconds",
            ),
            _s(
                "risk_exit_atr_multiplier",
                "Exit ATR multiplier",
                RESTART,
                "How many average true ranges below the peak the volatility floor "
                "sits. When no ATR is available the flat hard stop is the only floor.",
            ),
            _s(
                "risk_exit_atr_period",
                "Exit ATR period",
                RESTART,
                "Trading days of true range averaged into the ATR.",
                unit="days",
            ),
            _s(
                "risk_exit_atr_max_age_days",
                "Exit ATR maximum age",
                RESTART,
                "An ATR whose last bar is older than this is not trusted; the rule "
                "skips and the flat hard stop stands. Three days spans a weekend.",
                unit="days",
            ),
            _s(
                "volatility_refresh_enabled",
                "Volatility refresh",
                RESTART,
                "Whether the scheduled job that fetches daily bars and stores an ATR "
                "per open position runs. Off by default.",
            ),
            _s(
                "volatility_refresh_interval_seconds",
                "Volatility refresh interval",
                RESTART,
                "How often the volatility refresh runs. Daily bars change at most once "
                "a day, so this is measured in hours rather than minutes.",
                unit="seconds",
            ),
            _s(
                "volatility_bars_days",
                "Volatility bars window",
                RESTART,
                "Calendar days of daily bars requested per symbol. Must comfortably "
                "cover the ATR period, which is measured in trading days.",
                unit="days",
            ),
        ),
    ),
    _Group(
        key="telegram",
        title="Telegram",
        description=(
            "The bot is a thin control and notification surface. Numeric ids are the only "
            "authorization input -- a username can be released and re-registered by "
            "somebody else -- and no risk or broker logic lives in a chat handler."
        ),
        specs=(
            _s("telegram_enabled", "Telegram bot", RESTART, "Whether the bot runs at all."),
            _s("telegram_bot_token", "Bot token", SECRET, "The BotFather credential."),
            _s(
                "telegram_allowed_user_ids",
                "Authorized users",
                RESTART,
                "Numeric Telegram user ids. An empty list authorizes nobody; it is never "
                "read as everyone.",
            ),
            _s(
                "telegram_allowed_chat_ids",
                "Authorized chats",
                RESTART,
                "Numeric chat ids a non-private conversation must additionally appear in.",
            ),
            _s(
                "telegram_allow_group_chats",
                "Permit group chats",
                RESTART,
                "Off by default. A group notification never carries approval buttons, "
                "because a button minted for 'whoever taps first' has no holder.",
            ),
            _s(
                "telegram_notifications_enabled",
                "Notifications",
                RESTART,
                "The master switch. Individual categories are runtime-editable below.",
            ),
            _s(
                "notification_preferences",
                "Notification categories",
                RUNTIME,
                "Which pipeline and operational events reach the chat. Stored in "
                "PostgreSQL and changeable now.",
                env_var=None,
                control="telegram_preferences",
            ),
            _s(
                "telegram_action_ttl_seconds",
                "Approval button lifetime",
                RESTART,
                "Always additionally clamped to the proposal's own expiry: a button may "
                "never outlive the trade it refers to.",
                unit="seconds",
            ),
            _s(
                "telegram_confirm_ttl_seconds",
                "Confirmation lifetime",
                RESTART,
                "Stage two of the two-stage approval. Short on purpose.",
                unit="seconds",
            ),
        ),
        blockers_attr="telegram_blockers",
    ),
)


def _render(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, SecretStr):  # pragma: no cover - secrets never rendered
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or None
    return str(value)


def _blockers(settings: Settings, attr: str | None) -> tuple[str, ...]:
    if attr is None:
        return ()
    if attr == "web_auth_blockers":
        from stockbrain.api.auth import web_auth_blockers

        return tuple(web_auth_blockers(settings))
    value = getattr(settings, attr, None)
    return tuple(str(item) for item in value) if isinstance(value, list) else ()


def build_settings_view(
    settings: Settings,
    *,
    runtime_values: dict[str, Any] | None = None,
) -> tuple[SettingGroupView, ...]:
    """Render the catalogue against this deployment.

    ``runtime_values`` supplies the handful of values that live in PostgreSQL
    rather than in :class:`Settings` -- the control state and the discovery hold.
    They are passed in rather than read here so this function stays pure and
    synchronous, and so a caller that cannot reach the database still renders
    every environment-driven row.
    """
    supplied = runtime_values or {}
    groups: list[SettingGroupView] = []
    for group in CATALOGUE:
        views: list[SettingView] = []
        for spec in group.specs:
            configured: bool | None = None
            value: str | None
            if spec.mutability is Mutability.SECRET:
                raw = getattr(settings, spec.attr, None)
                configured = bool(raw.get_secret_value()) if isinstance(raw, SecretStr) else False
                value = None
            elif spec.attr in supplied:
                value = _render(supplied[spec.attr])
            else:
                value = _render(getattr(settings, spec.attr, None))
            views.append(
                SettingView(
                    key=spec.attr,
                    label=spec.label,
                    value=value,
                    mutability=spec.mutability,
                    description=spec.description,
                    env_var=spec.env_var,
                    unit=spec.unit,
                    impact=spec.impact,
                    control=spec.control,
                    configured=configured,
                )
            )
        groups.append(
            SettingGroupView(
                key=group.key,
                title=group.title,
                description=group.description,
                settings=tuple(views),
                blockers=_blockers(settings, group.blockers_attr),
                warning=group.warning,
            )
        )
    return tuple(groups)


def catalogue_attributes() -> Sequence[str]:
    """Every ``Settings`` attribute the catalogue reads, for the coverage test."""
    return [spec.attr for group in CATALOGUE for spec in group.specs]
