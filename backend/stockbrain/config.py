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
import re
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

from stockbrain.enums import ExecutionPolicy, WebDiscoveryKind, WebDiscoveryProviderName
from stockbrain.llm.pricing import DEFAULT_RATES, ModelRates, PricingTable
from stockbrain.llm.profiles import ProviderProfile, profile_for

__all__ = [
    "AppEnv",
    "BrokerEnvironment",
    "ExecutionMode",
    "ExecutionPolicy",
    "FxProviderName",
    "LogFormat",
    "Settings",
    "WebDiscoveryProviderName",
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


class FxProviderName(StrEnum):
    """Which foreign-exchange source may size a cross-currency trade.

    ``none`` is the default and is a safe, meaningful setting: cross-currency
    sizing stays blocked, which is Phase 6's behaviour.  Choosing a source has
    entitlement and trust consequences, so it is never inferred from a
    credential happening to be present.

    * ``alpaca`` -- ``/v1beta1/forex/latest/rates``, live bid/mid/ask with an
      instant timestamp.  **Measured 2026-09-05: HTTP 403 "insufficient grants"
      on this account** -- forex is not part of the plan that covers IEX equity
      data.
    * ``frankfurter`` -- central-bank reference fixings, no key, no quota, and
      documented by its own authors as "not for live trading".  Usable only with
      ``FX_ALLOW_REFERENCE_GRADE=true``.
    """

    NONE = "none"
    ALPACA = "alpaca"
    FRANKFURTER = "frankfurter"


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

    log_buffer_size: int = Field(default=4000, ge=0, le=50000)
    """How many recent structured log events the in-process buffer keeps for the
    GUI's Logs page.

    Bounded, because an unbounded log buffer in a long-running container is a
    memory leak with a feature request attached. Four thousand entries is a few
    megabytes and covers hours of a quiet pipeline; ``0`` switches capture off
    entirely for a deployment that would rather read ``docker compose logs``.
    Entries are held in memory only and do not survive a restart."""

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
    startup_probe_enabled: bool = True
    """Actively test every enabled provider once at startup.

    On by default because the alternative is a health board that reads
    ``DEGRADED`` on every restart until unrelated work happens to exercise each
    provider.  No probe makes a billable request -- the metered providers are
    checked for credential validity instead -- so leaving this on costs nothing
    but a few concurrent requests at boot.  Turn it off for an air-gapped or
    offline start where outbound calls would simply time out.
    """

    startup_probe_timeout_seconds: float = 10.0
    """Per-probe ceiling.  Probes run concurrently, so this is close to the
    total time the sweep can add to startup."""

    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_flash_model: str = "deepseek-v4-flash"
    deepseek_pro_model: str = "deepseek-v4-pro"
    deepseek_timeout_seconds: float = 120.0
    deepseek_max_attempts: int = 3

    research_enabled: bool = True
    research_timeout_seconds: float = Field(default=600, ge=30, le=3600)
    research_max_output_tokens: int = Field(default=3000, ge=256, le=16000)

    research_min_impact_materiality: float = Field(default=0.25, ge=0.0, le=1.0)
    """Materiality floor applied to each *impact*, not to the event.

    ``classifier_min_materiality`` promotes an event on its **best** company, so
    one company at 0.9 pulls every other impact on that story into research --
    including ones the classifier scored at 0.1.  Measured over six days, impacts
    below 0.40 took 26% of research spend and produced theses no more confident
    than the rest.  The floor sits at 0.25 rather than 0.40 deliberately: measured
    against the research layer's own verdicts it is a blunt proxy -- two 13F
    disclosures the researcher called NO_ACTION at 0.90 confidence scored 0.40 and
    0.50 here -- so it is set to remove the long tail, and the per-event cap is
    left to do the real work.  Set to 0.0 to research every resolved impact again.
    """

    research_max_impacts_per_event: int = Field(default=8, ge=0)
    """Most impacts researched for one event, highest materiality first.

    A market round-up names dozens of tickers in passing and each one is a full
    debate: one story fanned out to 45 runs, another to 42.  0 disables the cap.
    """

    research_evidence_chars: int = Field(default=12000, ge=500, le=20000)
    """How much of each evidence document reaches the researcher.

    This was hard-coded at 1600, which is a headline and a lead paragraph.  The
    analysts were told not to invent figures and then handed a document with the
    figures cut off, so "insufficient evidence" was the only honest verdict
    available to them and every thesis came back HOLD or NO_ACTION.

    The ceiling is :class:`~stockbrain.intelligence.research.EvidenceDocument`'s
    own ``max_length``; a larger value would be silently refused by the packet
    validator rather than truncated, so the bound is enforced here instead.
    """

    finnhub_api_key: SecretStr = SecretStr("")
    finnhub_base_url: str = "https://finnhub.io/api/v1"
    finnhub_enabled: bool = True
    """Analyst ratings and their direction of travel, insider Form 4 activity, and
    the estimate-vs-actual record. Free tier, and the only free source measured
    with complete coverage of foreign listings. Without a key it is simply not
    constructed and research degrades that provider alone."""

    polymarket_enabled: bool = True
    polymarket_base_url: str = "https://gamma-api.polymarket.com"
    """Crowd-implied probabilities for the rate path, inflation and recession.

    Keyless, and macro *only*: single-stock prediction markets are excluded on
    purpose, because a market on where a security closes is the crowd forecasting
    the very thing the research run exists to forecast."""

    yfinance_targets_enabled: bool = True
    """Consensus price targets and forward EPS via yfinance.

    Unofficial: it rides Yahoo's undocumented endpoints with no key, no SLA and no
    stability promise. It is here because it is the only free source of price
    targets that covers ASML and VWAGY, and it is built to be the provider that
    fails -- every call is wrapped and a failure degrades this datum alone."""

    research_debate_rounds: int = Field(default=2, ge=1, le=3)
    """How many times the bull and bear each speak before the manager reads.

    One round -- upstream's default, and what shipped until now -- means the bull
    opens without having seen the bear, and the bear answers holding the bull's
    whole argument and then closes. Over 150 runs the bear argued from absent
    evidence 148 times, the bull conceded the same gaps 137 times, and no manager
    ever recommended a direction.

    Two gives the bull its reply. Each extra round costs one more call from each
    of the two deep-model roles, so the ceiling is deliberately low."""

    research_fundamentals_enabled: bool = True
    """Whether to attach SEC XBRL company facts to the research packet.

    Free, keyless and rate-limited alongside the existing EDGAR client.  Only US
    filers and foreign private issuers that file XBRL are covered; everything
    else degrades explicitly, exactly as a missing price history does."""

    # ------------------------------------------------------------------
    # LLM backend selection
    #
    # StockBrain talks to any OpenAI-compatible chat-completions endpoint. The
    # named profile in ``stockbrain.llm.profiles`` supplies the API contract --
    # which output-token field, which structured-output dialect, whether
    # reasoning can be disabled, where cached tokens are reported -- and the
    # settings below supply the endpoint, the credential, the models and the
    # rates.
    #
    # The default is ``deepseek`` and every DeepSeek setting above still
    # applies, so an existing installation needs no new configuration and
    # behaves exactly as before.
    # ------------------------------------------------------------------
    llm_provider: str = "deepseek"
    """Provider profile name. Must exist in ``stockbrain.llm.profiles.PROFILES``.

    An unknown name is a startup error rather than a fallback to a generic
    profile: a typo must not silently redirect every model call onto a backend
    with different capabilities and different prices.
    """

    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = ""
    llm_model: str = ""
    """Model used for classification, semantic dedupe and quick research roles."""

    llm_deep_model: str = ""
    """Model for deep research roles. Empty means "same as ``llm_model``".

    Providers that expose one model for both depths need only set ``LLM_MODEL``;
    nothing in the calling code requires the two to differ.
    """

    llm_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    llm_max_attempts: int = Field(default=3, ge=1, le=5)

    # Rates in USD per 1,000,000 tokens for the configured models. Required for
    # any provider without built-in rates, because the spend guard sums cost
    # estimates and an unpriced model estimates None -- which would silently
    # exempt it from the daily and monthly caps.
    llm_input_usd_per_mtok: Decimal | None = None
    llm_cached_input_usd_per_mtok: Decimal | None = None
    llm_output_usd_per_mtok: Decimal | None = None

    llm_deep_input_usd_per_mtok: Decimal | None = None
    llm_deep_cached_input_usd_per_mtok: Decimal | None = None
    llm_deep_output_usd_per_mtok: Decimal | None = None
    """Deep-model rates. Unset means the deep model is priced like the quick one,
    which is correct whenever they are the same model."""

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
    # Web discovery
    #
    # Broad thematic and second-order discovery, **not** the fast-news path:
    # Alpaca news and SEC EDGAR handle time-critical financial discovery and
    # both are unmetered by comparison. Two providers, two purposes, two
    # schedules:
    #
    #   ROUTINE  -> Brave    conventional recent-news/thematic search
    #   SEMANTIC -> Exa      second-order and indirect-exposure search
    #
    # There is deliberately **no automatic fallback between them**. If Brave is
    # unavailable, routine queries defer; they are not silently re-run on a
    # provider that costs ten times as much. Cross-provider fan-out is how a
    # cost control becomes a cost multiplier.
    #
    # Every value below exists because Phase 2's cadence emptied a credit
    # allowance in about an hour (``docs/sources.md``, ``docs/operations.md``):
    # 9 enabled queries on 20/30-minute intervals is 21 searches an hour, and
    # each one scraped every result page.
    # ------------------------------------------------------------------
    web_discovery_enabled: bool = True
    """Master switch for both search providers.

    Off leaves discovery running on Alpaca news and SEC EDGAR alone, which is a
    complete and unmetered pipeline."""

    web_discovery_routine_provider: WebDiscoveryProviderName = WebDiscoveryProviderName.BRAVE
    web_discovery_semantic_provider: WebDiscoveryProviderName = WebDiscoveryProviderName.EXA
    """Which backend answers each query kind. ``none`` is a real setting: that
    kind simply does not run and the other one is unaffected."""

    web_discovery_min_query_interval_minutes: int = Field(default=360, ge=15, le=20160)
    """Floor on any routine query's cadence, in minutes.

    A topic row may ask for a *slower* cadence than this but never a faster one.
    Six hours by default: thematic drift is measured in days, and anything
    faster is work Alpaca news already does for free. Raising the floor is safe;
    lowering it is what caused the incident."""

    web_discovery_min_semantic_interval_minutes: int = Field(default=1440, ge=60, le=40320)
    """The same floor for semantic queries, and deliberately much higher.

    A semantic search costs roughly ten times a routine one and answers a
    question -- "who benefits from this constraint" -- whose answer changes over
    weeks, not hours."""

    web_discovery_failure_cooldown_minutes: int = Field(default=120, ge=1, le=20160)
    """How long a query waits after a *failure* before it is eligible again.

    Separate from the success interval: a query that is failing must not be
    retried on the ordinary cadence, because on a metered endpoint a retry is a
    second charge rather than a second chance."""

    web_discovery_freshness_days: int = Field(default=7, ge=1, le=3650)
    """Default recency window for a seeded topic, in days. Provider-neutral;
    each adapter translates it into its own vocabulary."""

    # ------------------------------------------------------------------
    # Brave Search  (routine web/news discovery)
    #
    # Verified 2026-09-05: $5 per 1,000 requests with $5 of monthly credit
    # applied automatically -- roughly 1,000 requests a month at no cost. The
    # caps below are an order of magnitude under that on purpose.
    # ------------------------------------------------------------------
    brave_api_key: SecretStr = SecretStr("")
    brave_base_url: str = "https://api.search.brave.com"
    brave_timeout_seconds: float = Field(default=20.0, ge=5.0, le=120.0)

    brave_max_searches_per_day: int = Field(default=12, ge=0, le=10000)
    """Hard ceiling on paid searches per UTC day, counted durably. Not a
    target: the default schedule asks for about ten."""

    brave_max_searches_per_month: int = Field(default=320, ge=0, le=1000000)
    """Hard ceiling per UTC calendar month.

    The binding constraint, because Brave's free credit is monthly: a daily cap
    alone cannot protect it, since 12 a day for 31 days is 372. 320 keeps a
    comfortable margin inside the ~1,000 requests $5 of credit buys, leaving
    room for a manual search and for the estimate being wrong."""

    brave_result_limit: int = Field(default=10, ge=1, le=20)
    """``count`` sent to ``/res/v1/web/search``. Documented maximum 20, and it
    applies to web results only -- the news cluster comes back alongside at no
    extra charge. Unlike Firecrawl's ``limit`` this does **not** multiply the
    price: one request is one billable request."""

    brave_result_filter: CommaSeparatedStrs
    """``result_filter`` values; defaults to ``web,news``. A relevance knob, not
    a cost knob."""

    brave_safesearch: Literal["off", "moderate", "strict"] = "off"
    """Financial and industrial news is not what this filter is for, and
    ``moderate`` (the API default) has been observed to drop legitimate results
    about weapons procurement and pharmaceuticals."""

    # ------------------------------------------------------------------
    # Exa  (semantic second-order discovery)
    #
    # Verified 2026-09-05: $7 per 1,000 requests for up to 10 results, $1 per
    # 1,000 results above 10, contents $1 per 1,000 pages per content type.
    # New accounts get $20 of credit and the free tier adds $10 a month.
    # ------------------------------------------------------------------
    exa_api_key: SecretStr = SecretStr("")
    exa_base_url: str = "https://api.exa.ai"
    exa_timeout_seconds: float = Field(default=45.0, ge=5.0, le=180.0)

    exa_max_searches_per_day: int = Field(default=3, ge=0, le=1000)
    """Hard ceiling on paid semantic searches per UTC day.

    Deliberately tiny. A semantic query asks a question whose answer moves over
    weeks; running it hourly would spend ten times Brave's price to receive
    yesterday's answer."""

    exa_max_searches_per_month: int = Field(default=70, ge=0, le=100000)
    """Hard ceiling per UTC calendar month: about $0.49 at the verified price,
    well inside the $10 monthly free credit."""

    exa_result_limit: int = Field(default=10, ge=1, le=100)
    """``numResults``. Ten is the ceiling included in the base price; every
    result above it is separately billed."""

    exa_search_type: Literal["auto", "fast", "instant", "deep-lite", "deep", "deep-reasoning"] = (
        "auto"
    )
    """``auto`` lets Exa pick between neural and keyword retrieval at the base
    price. The ``deep*`` modes are $12-15 per 1,000 and are a research tool, not
    a discovery sweep."""

    exa_fetch_contents: bool = False
    """Whether a semantic search may also request page contents.

    **Default off, and the scheduler never turns it on.** This is Exa's
    equivalent of Firecrawl's ``scrapeOptions``: the field that quietly turns a
    metadata call into a per-result page fetch. Extraction happens after triage,
    locally, and is free."""

    exa_snippet_max_chars: int = Field(default=1200, ge=200, le=20000)
    """How much of a returned text body is kept as a triage snippet, on the
    rare occasions contents are requested at all."""

    # ------------------------------------------------------------------
    # Local content extraction
    #
    # The default extractor and, in normal operation, the only one that runs.
    # It costs nothing, which is what makes "search broadly, read selectively"
    # stop being a budget question.
    # ------------------------------------------------------------------
    content_extraction_enabled: bool = True
    content_extract_max_per_day: int = Field(default=60, ge=0, le=10000)
    """How many shortlisted pages may be fetched per UTC day.

    Free, but not unbounded: a runaway sweep is a runaway outbound request rate
    against publishers who did not ask for it."""

    content_extract_timeout_seconds: float = Field(default=20.0, ge=2.0, le=120.0)
    content_extract_max_bytes: int = Field(default=5_000_000, ge=10_000, le=100_000_000)
    """Response body ceiling. Enforced while streaming, so a response that
    declares nothing and sends a gigabyte is abandoned rather than buffered."""

    content_extract_min_chars: int = Field(default=400, ge=50, le=100_000)
    """Below this an extraction counts as failed. A JavaScript shell, a consent
    wall and a paywall stub all return a 200 with a few dozen words, and
    treating that as an article would put nothing useful in front of the
    classifier while marking the URL as read."""

    content_extract_user_agent: str = (
        "StockBrain/0.1 (self-hosted research agent; +https://github.com/)"
    )
    """An honest, contactable identity. Impersonating a browser to get past a
    publisher's block is both dishonest and fragile."""

    # ------------------------------------------------------------------
    # Firecrawl  (fallback extractor only)
    #
    # Firecrawl was the primary discovery provider through Phase 9. It is not
    # any more: the search path is removed, and what remains is one paid page
    # fetch for the case local extraction cannot handle -- a publisher that
    # answers 403 to a plain client, or serves a shell that needs rendering.
    #
    # Firecrawl bills (verified 2026-09-05 against
    # <https://docs.firecrawl.dev/billing>): **scrape = 1 credit per page**, and
    # credits are charged whenever its infrastructure processed the request,
    # even when the target site returned an error.
    # ------------------------------------------------------------------
    firecrawl_api_key: SecretStr = SecretStr("")
    firecrawl_base_url: str = "https://api.firecrawl.dev"

    firecrawl_max_scrapes_per_day: int = Field(default=5, ge=0, le=10000)
    """Hard ceiling on paid fallback fetches per UTC day.

    A fallback only happens for a URL that already survived deduplication, the
    cheap classifier *and* a failed free attempt, so this is deliberately
    small."""

    firecrawl_daily_credit_cap: int = Field(default=10, ge=0, le=1000000)
    """Hard ceiling on estimated credits per UTC day."""

    firecrawl_monthly_credit_cap: int = Field(default=200, ge=0, le=10000000)
    """Hard ceiling on estimated credits per UTC calendar month.

    Firecrawl's allowance is monthly, so a daily cap alone cannot protect it: 10
    a day for 31 days is 310. 200 leaves headroom inside a 1,000-credit
    allowance."""

    firecrawl_scrape_timeout_seconds: float = Field(default=60.0, ge=5.0, le=300.0)

    firecrawl_enabled: bool = False
    """Default **off**.

    The one provider in this system that can spend real money without a human in
    the loop. A fresh deployment discovers through Brave, Exa, Alpaca news and
    SEC EDGAR, and enabling this is a deliberate act taken after reading the
    budget above."""

    firecrawl_fallback_extraction_enabled: bool = False
    """Whether a failed local extraction may escalate to a paid Firecrawl fetch.

    Separate from ``FIRECRAWL_ENABLED`` so that "the credential exists" and "we
    are willing to spend it on this page" stay two different decisions. Both
    must be true, and even then the escalation happens only for a shortlisted
    URL whose local attempt failed for a reason a different fetcher could fix."""

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
    # Foreign exchange (Phase 9)
    #
    # The live account is GBP and the priced universe is USD, so without a
    # verified rate the risk engine blocks every proposal it can price. It
    # blocks by design -- an invented rate is a wrong size -- and these settings
    # are what let a *verified* rate lift that block without ever inferring one.
    # ------------------------------------------------------------------
    fx_provider: FxProviderName = FxProviderName.NONE
    """Which source may size a cross-currency trade.  Default ``none``: the
    conservative Phase 6 behaviour, preserved unless an operator chooses."""

    fx_max_age_seconds: float = Field(default=900.0, ge=1.0, le=86400.0)
    """Freshness limit for an **execution-grade** rate, in seconds.

    Fifteen minutes.  Longer than the 15-second quote limit because an FX rate
    moves in basis points over minutes where an equity book can gap, and because
    the rate here bounds a *cap* rather than setting an execution price -- the
    broker applies its own FX at settlement.  Still short enough that a feed
    that has stopped updating is caught."""

    fx_reference_max_age_seconds: float = Field(default=90000.0, ge=1.0, le=604800.0)
    """Freshness limit for a **reference-grade** fixing, in seconds.

    Twenty-five hours, which covers one publication gap.  Central banks do not
    publish at weekends or on holidays, so a Monday-morning rate is dated the
    previous Friday and cross-currency sizing blocks until the next fixing --
    when equity markets are closed anyway.  Raising this to cover a weekend is
    an explicit decision to size against a rate three days old."""

    fx_allow_reference_grade: bool = False
    """Whether a published fixing may size a trade at all.

    Default false.  Frankfurter's own documentation says it "is not for live
    trading"; honouring that is the difference between using a reference rate
    knowingly and mistaking it for a dealable quote."""

    fx_max_rate_drift_pct: Decimal = Decimal("0.005")
    """How far the FX rate may move between authorization and transmission.

    The analogue of ``RISK_MAX_REFERENCE_PRICE_DRIFT_PCT``, and it exists for
    the same reason: an authorization is a statement about a moment. Half a
    percent on a major pair is a large intraday move; past it the proposal is
    invalidated and re-derived rather than silently resized."""

    fx_frankfurter_base_url: str = "https://api.frankfurter.dev"

    fx_probe_base_currency: str = "GBP"
    fx_probe_quote_currency: str = "USD"
    """The pair used for the one-request entitlement probe.  Defaults to the
    account's own pair, because "forex works" is not the useful question --
    "this pair works on this plan" is."""

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

    t212_max_pending_orders_per_ticker: int = Field(default=50, ge=1, le=1000)
    """Trading 212's documented functional limit: 50 pending orders per ticker
    per account.

    Configuration rather than a constant because it is a *documented functional
    limit*, not an API contract -- if the broker changes it, an operator should
    be able to follow without a deployment. It is never raised above what the
    broker documents by anything StockBrain does."""

    t212_pending_order_headroom: int = Field(default=5, ge=0, le=100)
    """How many of those fifty slots StockBrain refuses to use.

    The broker's list can lag a fill, the operator can queue orders by hand in
    the app between one check and the next, and the limit is enforced by a
    rejection from a **non-idempotent** endpoint. Stopping five short costs
    nothing and means StockBrain never discovers the limit by hitting it."""

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
    risk_default_quantity_precision: int = 2
    """Decimal places a fractional quantity is rounded down to until the broker
    has refused a finer one for that instrument (it publishes no precision).
    Zero means whole shares; the learned per-instrument value, once stored,
    overrides this."""

    risk_max_active_proposals: int = 5
    risk_max_active_proposal_exposure_pct: Decimal = Decimal("0.10")
    risk_proposal_ttl_minutes: int = 30
    risk_max_reference_price_drift_pct: Decimal = Decimal("0.01")

    risk_min_research_confidence: Decimal = Decimal("0.70")
    risk_confidence_modulates_size: bool = True
    risk_min_confidence_size_factor: Decimal = Decimal("0.5")
    risk_reduce_fraction: Decimal = Decimal("0.5")

    risk_exit_hard_stop_pct: Decimal = Decimal("0.08")
    risk_exit_trailing_pct: Decimal = Decimal("0.05")
    risk_exit_trailing_arm_pct: Decimal = Decimal("0.10")
    risk_exit_min_peak_observations: int = 3
    exit_sweep_enabled: bool = False
    exit_sweep_interval_seconds: float = 300.0

    risk_exit_atr_multiplier: Decimal = Decimal("3")
    risk_exit_atr_period: int = 14
    risk_exit_atr_max_age_days: int = 3
    volatility_refresh_enabled: bool = False
    volatility_refresh_interval_seconds: float = 21600.0
    volatility_bars_days: int = 90

    proposal_revalidation_batch: int = 5
    """How many active proposals the invalidation sweep re-prices per tick.
    Bounded so the sweep cannot turn into an unmetered market-data spend."""

    proposal_deferral_max_hours: int = 72
    """How long a thesis blocked only by market state keeps being retried.

    A Friday-evening thesis has to survive the weekend, so the default spans
    three days.  Past this the deferral gives up and the block becomes terminal,
    which is what stops a shut market from retrying a thesis forever."""

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

    telegram_daily_summary_time: str | None = "08:00"
    """UTC ``HH:MM`` at which the once-a-day portfolio summary is sent.

    Empty or unset disables it entirely.  UTC rather than the display timezone
    because the scheduler fires with no operator present to interpret a local
    time, and a restart after the configured moment still sends that day's
    summary as long as the day has no delivery row yet."""

    telegram_research_interval_seconds: float = Field(default=30.0, ge=0.0, le=3600.0)
    """Minimum spacing between ``/research`` invocations per user (spec section
    20: "manual research should be rate limited"). This command only reads a
    stored thesis, but it is the command most likely to be repeated."""

    # ------------------------------------------------------------------
    # Web authentication (Phase 9, spec section 19)
    #
    # "This is a personal app but it handles broker actions." Through Phase 8
    # there was no authentication at all: seven state-changing routes, one of
    # which transmits a real order, reachable by anything that could open a
    # socket to the port. The spec's own words apply: "If accessed only over
    # LAN/Tailscale, still require auth."
    # ------------------------------------------------------------------
    alert_queue_backlog_seconds: float = Field(default=1800.0, ge=60.0, le=86400.0)
    """How long the oldest pending job may wait before it is an alert.

    Thirty minutes. The pipeline's slowest legitimate step is a research run at
    up to ten minutes, so half an hour is well past "busy" and firmly into
    "something has stopped" -- which is the condition Phase 6's bug 12 produced
    and nothing reported."""

    alert_scan_interval_seconds: float = Field(default=300.0, ge=30.0, le=3600.0)
    """How often the operational alert scan runs.  It makes no external call;
    the cost is a handful of aggregate queries."""

    alerts_enabled: bool = True

    web_auth_enabled: bool = True
    """Default **on**.  The only supported way to run without it is to set this
    false *and* acknowledge the trusted-network model below, which is refused in
    production without the acknowledgement."""

    web_owner_username: str = "owner"

    web_owner_password_hash: SecretStr = SecretStr("")
    """scrypt hash of the owner's password, generated by
    ``python -m stockbrain.hash_password``.

    A hash, never the password: the environment of a long-running container is
    readable by anything that can exec into it, and a hash there is worth far
    less than a password there."""

    web_session_ttl_seconds: int = Field(default=43200, ge=300, le=2592000)
    """Twelve hours.  Long enough not to interrupt a working session, short
    enough that a forgotten open browser is not a permanent credential."""

    web_cookie_secure: bool | None = None
    """Force the ``Secure`` cookie flag on or off.

    ``None`` means "on in production, off otherwise", which is what makes a
    plain-HTTP LAN deployment work at all -- a ``Secure`` cookie on an
    ``http://`` origin is never stored, and the operator would see a login that
    silently does nothing. Set true behind a TLS-terminating reverse proxy."""

    web_trusted_network_acknowledged: bool = False
    """Records that the operator has *deliberately* chosen network trust instead
    of authentication -- an authenticating reverse proxy, a Tailscale-only
    listener, or an accepted risk.

    Its only function is to make ``WEB_AUTH_ENABLED=false`` in production an
    explicit decision rather than an omission. There is deliberately no way to
    satisfy it by accident."""

    # ------------------------------------------------------------------
    # Subsystem toggles (env-level defaults; runtime flags live in PostgreSQL)
    # ------------------------------------------------------------------
    discovery_enabled: bool = True
    alpaca_news_enabled: bool = True
    sec_enabled: bool = True
    # ``firecrawl_enabled`` lives with the rest of the Firecrawl budget above:
    # the switch and the money it commits belong beside each other.

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
        "brave_result_filter",
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

    @field_validator("telegram_daily_summary_time", mode="after")
    @classmethod
    def _normalise_daily_summary_time(cls, value: str | None) -> str | None:
        """Normalise ``HH:MM``, treating blank as "disabled".

        A malformed value refuses to start rather than silently degrading to the
        default: a summary that stopped arriving because of a typo is worse than
        a startup error, because nothing says it stopped.
        """
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        match = re.fullmatch(r"(\d{2}):(\d{2})", text)
        if match is None:
            raise ValueError("TELEGRAM_DAILY_SUMMARY_TIME must be HH:MM in UTC, e.g. 08:00")
        hours, minutes = int(match.group(1)), int(match.group(2))
        if hours >= 24 or minutes >= 60:
            raise ValueError(f"TELEGRAM_DAILY_SUMMARY_TIME must be a real UTC time, got {text!r}")
        return text

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
        if not 1 <= self.proposal_deferral_max_hours <= 336:
            problems.append(
                "PROPOSAL_DEFERRAL_MAX_HOURS must be between 1 and 336 "
                f"(got {self.proposal_deferral_max_hours})"
            )
        if self.risk_max_active_proposals <= 0:
            problems.append("RISK_MAX_ACTIVE_PROPOSALS must be greater than 0")
        if not 0 <= self.risk_default_quantity_precision <= 8:
            problems.append(
                "RISK_DEFAULT_QUANTITY_PRECISION must be between 0 and 8 "
                f"(got {self.risk_default_quantity_precision})"
            )
        if self.risk_max_account_state_age_seconds <= 0:
            problems.append("RISK_MAX_ACCOUNT_STATE_AGE_SECONDS must be greater than 0")
        if problems:
            raise ValueError("Invalid risk configuration: " + "; ".join(problems))
        return self

    @model_validator(mode="after")
    def _validate_exit_policy(self) -> Settings:
        """Refuse an exit policy that cannot mean anything.

        A trailing floor wider than the arm threshold would sit below the entry
        price at the moment it armed, so the trailing rule would fire
        immediately on every position that reached its arm level -- a take
        profit wearing a trailing stop's name.
        """
        problems: list[str] = []
        if not 0 < self.risk_exit_hard_stop_pct < 1:
            problems.append(
                "RISK_EXIT_HARD_STOP_PCT must be between 0 and 1, exclusive "
                f"(got {self.risk_exit_hard_stop_pct})"
            )
        if not 0 < self.risk_exit_trailing_pct < 1:
            problems.append(
                "RISK_EXIT_TRAILING_PCT must be between 0 and 1, exclusive "
                f"(got {self.risk_exit_trailing_pct})"
            )
        if not 0 <= self.risk_exit_trailing_arm_pct < 1:
            problems.append(
                "RISK_EXIT_TRAILING_ARM_PCT must be at least 0 and below 1 "
                f"(got {self.risk_exit_trailing_arm_pct})"
            )
        if not 1 <= self.risk_exit_min_peak_observations <= 100:
            problems.append(
                "RISK_EXIT_MIN_PEAK_OBSERVATIONS must be between 1 and 100 "
                f"(got {self.risk_exit_min_peak_observations})"
            )
        if not 30.0 <= self.exit_sweep_interval_seconds <= 3600.0:
            problems.append(
                "EXIT_SWEEP_INTERVAL_SECONDS must be between 30 and 3600 "
                f"(got {self.exit_sweep_interval_seconds})"
            )
        if problems:
            raise ValueError("Invalid risk configuration: " + "; ".join(problems))
        if self.risk_exit_trailing_pct >= self.risk_exit_trailing_arm_pct:
            raise ValueError(
                "RISK_EXIT_TRAILING_PCT must be below RISK_EXIT_TRAILING_ARM_PCT: a floor "
                f"{self.risk_exit_trailing_pct} below a peak armed at "
                f"{self.risk_exit_trailing_arm_pct} would already be under the entry price"
            )
        return self

    @model_validator(mode="after")
    def _validate_volatility_policy(self) -> Settings:
        """Refuse a volatility policy that cannot mean anything.

        The ATR window is measured in trading days, but Yahoo's ``period``
        argument is calendar days, so the bar request has to cover the window
        with room for weekends and holidays.  A bars window shorter than twice
        the period plus a week would starve the ATR of the closes it needs, and
        a starved ATR reads downstream as "no reading yet" rather than as a
        configuration error.
        """
        problems: list[str] = []
        if not 0 < self.risk_exit_atr_multiplier <= 10:
            problems.append(
                "RISK_EXIT_ATR_MULTIPLIER must be above 0 and at most 10 "
                f"(got {self.risk_exit_atr_multiplier})"
            )
        if not 2 <= self.risk_exit_atr_period <= 60:
            problems.append(
                f"RISK_EXIT_ATR_PERIOD must be between 2 and 60 (got {self.risk_exit_atr_period})"
            )
        if not 1 <= self.risk_exit_atr_max_age_days <= 14:
            problems.append(
                "RISK_EXIT_ATR_MAX_AGE_DAYS must be between 1 and 14 "
                f"(got {self.risk_exit_atr_max_age_days})"
            )
        if not 3600.0 <= self.volatility_refresh_interval_seconds <= 86400.0:
            problems.append(
                "VOLATILITY_REFRESH_INTERVAL_SECONDS must be between 3600 and 86400 "
                f"(got {self.volatility_refresh_interval_seconds})"
            )
        if not 30 <= self.volatility_bars_days <= 365:
            problems.append(
                f"VOLATILITY_BARS_DAYS must be between 30 and 365 (got {self.volatility_bars_days})"
            )
        if problems:
            raise ValueError("Invalid risk configuration: " + "; ".join(problems))
        if self.volatility_bars_days < 2 * self.risk_exit_atr_period + 7:
            raise ValueError(
                "VOLATILITY_BARS_DAYS must be at least twice RISK_EXIT_ATR_PERIOD plus "
                "seven, because the ATR window counts trading days and Yahoo's period "
                f"counts calendar days (got {self.volatility_bars_days} days for a "
                f"{self.risk_exit_atr_period}-day period)"
            )
        return self

    @model_validator(mode="after")
    def _validate_pending_order_limit(self) -> Settings:
        """The headroom must leave at least one usable slot.

        Headroom >= limit would mean no order could ever be transmitted, which
        would present as "execution silently does nothing" rather than as a
        configuration error.
        """
        if self.t212_pending_order_headroom >= self.t212_max_pending_orders_per_ticker:
            raise ValueError(
                "T212_PENDING_ORDER_HEADROOM must be less than "
                "T212_MAX_PENDING_ORDERS_PER_TICKER, otherwise no order can ever be sent "
                f"(headroom {self.t212_pending_order_headroom} >= limit "
                f"{self.t212_max_pending_orders_per_ticker})"
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

    # ------------------------------------------------------------------
    # LLM backend resolution
    #
    # ``LLM_*`` wins where it is set. Where it is not, and the provider is
    # DeepSeek, the original ``DEEPSEEK_*`` settings apply -- which is what
    # keeps an existing installation working with no new configuration.
    # ------------------------------------------------------------------

    @property
    def llm_profile(self) -> ProviderProfile:
        """The active provider's API contract. Raises on an unknown name."""
        return profile_for(self.llm_provider)

    @property
    def llm_is_deepseek(self) -> bool:
        return self.llm_provider.strip().lower() == "deepseek"

    @property
    def active_llm_api_key(self) -> SecretStr:
        if self.llm_api_key.get_secret_value():
            return self.llm_api_key
        return self.deepseek_api_key if self.llm_is_deepseek else SecretStr("")

    @property
    def active_llm_base_url(self) -> str:
        if self.llm_base_url:
            return self.llm_base_url
        if self.llm_is_deepseek:
            return self.deepseek_base_url
        return self.llm_profile.base_url

    @property
    def active_llm_quick_model(self) -> str:
        if self.llm_model:
            return self.llm_model
        return self.deepseek_flash_model if self.llm_is_deepseek else ""

    @property
    def active_llm_deep_model(self) -> str:
        """Deep-role model, defaulting to the quick model.

        A provider that serves one model at both depths is the normal case, not
        a special case: nothing downstream requires the two to differ.
        """
        if self.llm_deep_model:
            return self.llm_deep_model
        if self.llm_is_deepseek and not self.llm_model:
            return self.deepseek_pro_model
        return self.active_llm_quick_model

    @property
    def active_llm_timeout_seconds(self) -> float:
        if self.llm_is_deepseek and self.llm_timeout_seconds == 120.0:
            # Unchanged default: defer to the existing DeepSeek setting so an
            # operator who tuned DEEPSEEK_TIMEOUT_SECONDS keeps that value.
            return self.deepseek_timeout_seconds
        return self.llm_timeout_seconds

    @property
    def active_llm_max_attempts(self) -> int:
        if self.llm_is_deepseek and self.llm_max_attempts == 3:
            return self.deepseek_max_attempts
        return self.llm_max_attempts

    def llm_model_rates(self) -> dict[str, ModelRates]:
        """Configured rates, keyed by the model they apply to.

        Empty when nothing is configured, in which case only the built-in rates
        apply. Cached-input rate defaults to the uncached rate rather than to
        zero: a provider that reports no cache split must not be credited with a
        discount it never gave.
        """
        rates: dict[str, ModelRates] = {}
        quick = self.active_llm_quick_model
        if quick and self.llm_input_usd_per_mtok is not None:
            rates[quick] = ModelRates(
                cache_hit_input=(
                    self.llm_cached_input_usd_per_mtok
                    if self.llm_cached_input_usd_per_mtok is not None
                    else self.llm_input_usd_per_mtok
                ),
                cache_miss_input=self.llm_input_usd_per_mtok,
                output=(
                    self.llm_output_usd_per_mtok
                    if self.llm_output_usd_per_mtok is not None
                    else self.llm_input_usd_per_mtok
                ),
            )

        deep = self.active_llm_deep_model
        if deep and self.llm_deep_input_usd_per_mtok is not None:
            rates[deep] = ModelRates(
                cache_hit_input=(
                    self.llm_deep_cached_input_usd_per_mtok
                    if self.llm_deep_cached_input_usd_per_mtok is not None
                    else self.llm_deep_input_usd_per_mtok
                ),
                cache_miss_input=self.llm_deep_input_usd_per_mtok,
                output=(
                    self.llm_deep_output_usd_per_mtok
                    if self.llm_deep_output_usd_per_mtok is not None
                    else self.llm_deep_input_usd_per_mtok
                ),
            )
        return rates

    @field_validator(
        "llm_input_usd_per_mtok",
        "llm_cached_input_usd_per_mtok",
        "llm_output_usd_per_mtok",
        "llm_deep_input_usd_per_mtok",
        "llm_deep_cached_input_usd_per_mtok",
        "llm_deep_output_usd_per_mtok",
        mode="before",
    )
    @classmethod
    def _blank_rate_is_unset(cls, value: object) -> object:
        """An empty variable means "not configured", not "unparseable".

        ``.env.example`` ships these declared and empty so an operator can see
        they exist, and a dotenv file supplies ``""`` rather than omitting the
        name. Without this, the shipped example would refuse to load at all --
        and the resulting error would point at decimal parsing rather than at
        the missing configuration it actually represents.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _validate_llm_backend(self) -> Settings:
        """Refuse a backend that cannot be called, or whose spend cannot be counted.

        The pricing requirement is the load-bearing one. ``BudgetGuard`` sums
        estimated cost from ``llm_calls``, and an unpriced model estimates
        ``None`` -- so shipping an unpriced provider would not raise the spend
        caps, it would silently exempt that provider from them. Requiring rates
        at startup is what keeps ``LLM_DAILY_HARD_USD`` a real ceiling for every
        endpoint rather than only for the ones this codebase happens to know.
        """
        profile_for(self.llm_provider)  # raises on an unknown provider name

        if not (self.classifier_enabled or self.research_enabled):
            return self
        if not self.active_llm_api_key.get_secret_value():
            # No credential means the LLM subsystem stays switched off, which is
            # a supported configuration and is reported by the health board.
            return self

        if not self.active_llm_quick_model:
            raise ValueError(
                f"LLM_MODEL is required for LLM_PROVIDER={self.llm_provider!r} "
                "(the profile supplies no default model)"
            )
        if not self.active_llm_base_url:
            raise ValueError(
                f"LLM_BASE_URL is required for LLM_PROVIDER={self.llm_provider!r} "
                "(the profile supplies no default endpoint)"
            )

        pricing = PricingTable({**DEFAULT_RATES, **self.llm_model_rates()})
        unpriced = sorted(
            {
                model
                for model in (self.active_llm_quick_model, self.active_llm_deep_model)
                if pricing.rates_for(model) is None
            }
        )
        if unpriced:
            raise ValueError(
                "no price is configured for LLM model(s) "
                f"{', '.join(unpriced)}; set LLM_INPUT_USD_PER_MTOK, "
                "LLM_CACHED_INPUT_USD_PER_MTOK and LLM_OUTPUT_USD_PER_MTOK "
                "(and the LLM_DEEP_* equivalents if the deep model differs). "
                "Without rates the model's spend is not counted against "
                "LLM_DAILY_HARD_USD or LLM_MONTHLY_HARD_USD"
            )

        return self

    @field_validator("brave_result_filter", mode="after")
    @classmethod
    def _normalise_brave_result_filter(cls, value: list[str]) -> list[str]:
        """Default to ``web,news`` and refuse a filter the API does not document.

        An unknown filter name would be rejected by the API *after* the request
        was made, so it is caught here instead. Order is preserved and
        duplicates are dropped.
        """
        allowed = {
            "discussions",
            "faq",
            "infobox",
            "locations",
            "news",
            "query",
            "summarizer",
            "videos",
            "web",
        }
        if not value:
            return ["web", "news"]
        cleaned: list[str] = []
        for raw in value:
            name = raw.strip().lower()
            if not name:
                continue
            if name not in allowed:
                raise ValueError(
                    f"BRAVE_RESULT_FILTER contains {raw!r}; "
                    f"the API documents only {sorted(allowed)}"
                )
            if name not in cleaned:
                cleaned.append(name)
        return cleaned or ["web", "news"]

    @model_validator(mode="after")
    def _validate_provider_budgets(self) -> Settings:
        """Reject a provider budget that cannot hold.

        Each check is a combination that would look configured and then spend
        more than the operator intended, or would look configured and be unable
        to spend at all -- both are the class of mistake this whole section
        exists to prevent.
        """
        problems: list[str] = []
        if self.brave_max_searches_per_month < self.brave_max_searches_per_day:
            problems.append(
                "BRAVE_MAX_SEARCHES_PER_MONTH must be >= BRAVE_MAX_SEARCHES_PER_DAY, "
                "otherwise the daily cap can never be reached"
            )
        if self.exa_max_searches_per_month < self.exa_max_searches_per_day:
            problems.append(
                "EXA_MAX_SEARCHES_PER_MONTH must be >= EXA_MAX_SEARCHES_PER_DAY, "
                "otherwise the daily cap can never be reached"
            )
        if self.firecrawl_monthly_credit_cap < self.firecrawl_daily_credit_cap:
            problems.append(
                "FIRECRAWL_MONTHLY_CREDIT_CAP must be >= FIRECRAWL_DAILY_CREDIT_CAP, "
                "otherwise the daily cap can never be reached"
            )
        # A markdown scrape is one credit. A daily credit cap below the cost of
        # the scrapes the daily scrape cap permits is not a stricter limit, it
        # is two limits that disagree.
        if self.firecrawl_max_scrapes_per_day and self.firecrawl_daily_credit_cap < 1:
            problems.append(
                "FIRECRAWL_DAILY_CREDIT_CAP is below the 1 credit a single /v2/scrape "
                "costs, so FIRECRAWL_MAX_SCRAPES_PER_DAY can never be used"
            )
        if self.firecrawl_fallback_extraction_enabled and not self.content_extraction_enabled:
            problems.append(
                "FIRECRAWL_FALLBACK_EXTRACTION_ENABLED requires CONTENT_EXTRACTION_ENABLED: "
                "the paid fallback only ever runs after a local attempt failed, so with "
                "local extraction off it would become the primary extractor"
            )
        if self.web_discovery_routine_provider is WebDiscoveryProviderName.EXA:
            problems.append(
                "WEB_DISCOVERY_ROUTINE_PROVIDER=exa would run every routine keyword search "
                "on the semantic provider's price; Exa answers SEMANTIC queries"
            )
        if self.web_discovery_semantic_provider is WebDiscoveryProviderName.BRAVE:
            problems.append(
                "WEB_DISCOVERY_SEMANTIC_PROVIDER=brave would answer second-order questions "
                "with a keyword index; Brave answers ROUTINE queries"
            )
        if problems:
            raise ValueError("Invalid web discovery budget: " + "; ".join(problems))
        return self

    @field_validator("fx_probe_base_currency", "fx_probe_quote_currency", mode="after")
    @classmethod
    def _normalise_probe_currency(cls, value: str) -> str:
        code = value.strip().upper()
        if len(code) != 3 or not code.isalpha():
            raise ValueError(f"currency codes must be three ISO 4217 letters (got {value!r})")
        return code

    @model_validator(mode="after")
    def _validate_fx(self) -> Settings:
        """Reject an FX configuration that could not size anything, or that lies.

        Two combinations are refused rather than tolerated, because both look
        configured and behave as though FX were unavailable:
        """
        problems: list[str] = []
        if self.fx_probe_base_currency == self.fx_probe_quote_currency:
            problems.append(
                "FX_PROBE_BASE_CURRENCY and FX_PROBE_QUOTE_CURRENCY must differ; "
                "a same-currency pair is not a rate"
            )
        if self.fx_reference_max_age_seconds < self.fx_max_age_seconds:
            problems.append(
                "FX_REFERENCE_MAX_AGE_SECONDS must be >= FX_MAX_AGE_SECONDS: a daily "
                "fixing cannot be held to a stricter freshness bar than a live quote"
            )
        if self.fx_provider is FxProviderName.FRANKFURTER and not self.fx_allow_reference_grade:
            problems.append(
                "FX_PROVIDER=frankfurter requires FX_ALLOW_REFERENCE_GRADE=true: the "
                "service publishes central-bank fixings and documents that it is not "
                "for live trading, so every rate it returns would be refused. Set the "
                "flag to size against a reference rate knowingly, or use "
                "FX_PROVIDER=none to keep cross-currency sizing blocked"
            )
        if self.fx_max_rate_drift_pct <= 0 or self.fx_max_rate_drift_pct > 1:
            problems.append("FX_MAX_RATE_DRIFT_PCT must be between 0 (exclusive) and 1")
        if not self.risk_require_same_currency and self.fx_provider is FxProviderName.NONE:
            # Phase 6 tolerated this pairing and it was the one genuinely unsafe
            # combination in the risk configuration: `currency_alignment`
            # returned WARN for a permitted mismatch, nothing blocked, and
            # sizing then divided an account-currency ceiling by an
            # instrument-currency price. The quantity that came out was wrong by
            # the exchange rate. Refusing to start says so instead.
            problems.append(
                "RISK_REQUIRE_SAME_CURRENCY=false permits cross-currency sizing but "
                "FX_PROVIDER=none supplies no rate to size with. Set FX_PROVIDER, or "
                "leave RISK_REQUIRE_SAME_CURRENCY=true to keep cross-currency proposals "
                "blocked. This pairing previously produced a warning and a quantity "
                "computed by dividing an account-currency cap by an instrument-currency "
                "price"
            )
        if problems:
            raise ValueError("Invalid FX configuration: " + "; ".join(problems))
        return self

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> Settings:
        if self.app_env is AppEnv.PRODUCTION and not self.stockbrain_secret_key.get_secret_value():
            raise ValueError("STOCKBRAIN_SECRET_KEY is required when APP_ENV=production")
        return self

    @model_validator(mode="after")
    def _validate_web_auth(self) -> Settings:
        """Refuse a web-authentication configuration that protects nothing.

        Two refusals, both about the same failure: an unauthenticated API that
        nobody meant to deploy.
        """
        problems: list[str] = []
        # A missing password hash is deliberately *not* a startup failure. It is
        # reported by `web_auth_blockers` and it makes every protected route
        # answer 503 with that reason -- the same shape as an empty Telegram
        # allowlist, which disables the bot rather than the process. A fresh
        # deployment therefore comes up, serves its health endpoints, tells the
        # operator to set a password, and grants access to nothing.
        if not self.web_auth_enabled and (
            self.app_env is AppEnv.PRODUCTION and not self.web_trusted_network_acknowledged
        ):
            problems.append(
                "WEB_AUTH_ENABLED=false in production requires "
                "WEB_TRUSTED_NETWORK_ACKNOWLEDGED=true. Specification section 19 requires "
                "authentication even on a LAN or Tailscale network; running without it is "
                "supported only as a deliberate, recorded choice to rely on an "
                "authenticating reverse proxy"
            )
        if problems:
            raise ValueError("Invalid web authentication configuration: " + "; ".join(problems))
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
    def fx_blockers(self) -> list[str]:
        """Every configuration reason cross-currency sizing is unavailable.

        Deliberately configuration-only.  Whether the *live* source answers, and
        whether the rate it returned is fresh enough, are runtime facts measured
        by :class:`~stockbrain.fx.service.FxService` -- and a rate that arrives
        stale must block the individual trade, not the whole subsystem.
        """
        blockers: list[str] = []
        if self.fx_provider is FxProviderName.NONE:
            blockers.append(
                "FX_PROVIDER is 'none': cross-currency sizing is blocked and no rate "
                "is ever inferred"
            )
            return blockers
        if self.fx_provider is FxProviderName.ALPACA and not (
            self.alpaca_api_key.get_secret_value() and self.alpaca_api_secret.get_secret_value()
        ):
            blockers.append("FX_PROVIDER=alpaca but Alpaca credentials are not configured")
        if self.fx_provider is FxProviderName.FRANKFURTER and not self.fx_allow_reference_grade:
            # Unreachable through the validator above; kept so the predicate
            # stands on its own if the validator is ever relaxed.
            blockers.append(  # pragma: no cover - the validator refuses to start
                "FX_ALLOW_REFERENCE_GRADE is false, so a reference-grade fixing "
                "may not size a trade"
            )
        return blockers

    @property
    def fx_configured(self) -> bool:
        return not self.fx_blockers

    @property
    def brave_blockers(self) -> list[str]:
        """Every reason no paid Brave call will be made, in GUI-ready wording.

        Same shape as :attr:`execution_blockers`: "available" is defined as "no
        blockers remain", so a health panel can never show a blocker beside a
        green light. Budget exhaustion is *not* here -- it is durable runtime
        state read from PostgreSQL, not configuration.
        """
        blockers: list[str] = []
        if not self.discovery_enabled:
            blockers.append("DISCOVERY_ENABLED is false")
        if not self.web_discovery_enabled:
            blockers.append("WEB_DISCOVERY_ENABLED is false")
        if self.web_discovery_routine_provider is not WebDiscoveryProviderName.BRAVE:
            blockers.append(
                f"WEB_DISCOVERY_ROUTINE_PROVIDER is "
                f"{self.web_discovery_routine_provider.value!r}, not 'brave'"
            )
        if not self.brave_api_key.get_secret_value():
            blockers.append("BRAVE_API_KEY is not set")
        if self.brave_max_searches_per_day <= 0:
            blockers.append("BRAVE_MAX_SEARCHES_PER_DAY is 0")
        if self.brave_max_searches_per_month <= 0:
            blockers.append("BRAVE_MAX_SEARCHES_PER_MONTH is 0")
        return blockers

    @property
    def brave_available(self) -> bool:
        return not self.brave_blockers

    @property
    def exa_blockers(self) -> list[str]:
        """Every reason no paid Exa call will be made, in GUI-ready wording."""
        blockers: list[str] = []
        if not self.discovery_enabled:
            blockers.append("DISCOVERY_ENABLED is false")
        if not self.web_discovery_enabled:
            blockers.append("WEB_DISCOVERY_ENABLED is false")
        if self.web_discovery_semantic_provider is not WebDiscoveryProviderName.EXA:
            blockers.append(
                f"WEB_DISCOVERY_SEMANTIC_PROVIDER is "
                f"{self.web_discovery_semantic_provider.value!r}, not 'exa'"
            )
        if not self.exa_api_key.get_secret_value():
            blockers.append("EXA_API_KEY is not set")
        if self.exa_max_searches_per_day <= 0:
            blockers.append("EXA_MAX_SEARCHES_PER_DAY is 0")
        if self.exa_max_searches_per_month <= 0:
            blockers.append("EXA_MAX_SEARCHES_PER_MONTH is 0")
        return blockers

    @property
    def exa_available(self) -> bool:
        return not self.exa_blockers

    @property
    def firecrawl_blockers(self) -> list[str]:
        """Every reason no paid Firecrawl fetch will be made.

        Firecrawl is now the fallback *extractor*, so ``DISCOVERY_ENABLED`` is
        not among these: a page can be worth reading while the sweep that found
        it is paused. What gates it is the extraction pipeline it sits behind.
        """
        blockers: list[str] = []
        if not self.content_extraction_enabled:
            blockers.append("CONTENT_EXTRACTION_ENABLED is false")
        if not self.firecrawl_enabled:
            blockers.append("FIRECRAWL_ENABLED is false")
        if not self.firecrawl_fallback_extraction_enabled:
            blockers.append("FIRECRAWL_FALLBACK_EXTRACTION_ENABLED is false")
        if not self.firecrawl_api_key.get_secret_value():
            blockers.append("FIRECRAWL_API_KEY is not set")
        if self.firecrawl_max_scrapes_per_day <= 0:
            blockers.append("FIRECRAWL_MAX_SCRAPES_PER_DAY is 0")
        if self.firecrawl_daily_credit_cap <= 0:
            blockers.append("FIRECRAWL_DAILY_CREDIT_CAP is 0")
        if self.firecrawl_monthly_credit_cap <= 0:
            blockers.append("FIRECRAWL_MONTHLY_CREDIT_CAP is 0")
        return blockers

    @property
    def firecrawl_available(self) -> bool:
        return not self.firecrawl_blockers

    @property
    def content_extraction_blockers(self) -> list[str]:
        """Every reason no page body will be fetched at all."""
        blockers: list[str] = []
        if not self.content_extraction_enabled:
            blockers.append("CONTENT_EXTRACTION_ENABLED is false")
        if self.content_extract_max_per_day <= 0:
            blockers.append("CONTENT_EXTRACT_MAX_PER_DAY is 0")
        return blockers

    @property
    def content_extraction_available(self) -> bool:
        return not self.content_extraction_blockers

    def min_query_interval_minutes(self, kind: WebDiscoveryKind) -> int:
        """The cadence floor for one query kind.

        Read wherever an interval is *used* rather than where it is stored, so
        an old topic row, a restored backup or a hand-written ``UPDATE`` cannot
        go faster than the operator allowed.
        """
        if kind is WebDiscoveryKind.SEMANTIC:
            return self.web_discovery_min_semantic_interval_minutes
        return self.web_discovery_min_query_interval_minutes

    def provider_for_kind(self, kind: WebDiscoveryKind) -> WebDiscoveryProviderName:
        """Which backend answers this kind of query, by configuration."""
        if kind is WebDiscoveryKind.SEMANTIC:
            return self.web_discovery_semantic_provider
        return self.web_discovery_routine_provider

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
