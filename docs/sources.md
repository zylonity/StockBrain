# External sources and API verification

Every external dependency, what it is used for, and the current verification
state of the details StockBrain relies on.

**Verification policy.** A provider detail is verified against that provider's
own current documentation *in the phase that implements its adapter*, before the
first line of request-building code. Nothing here is guessed. Where a detail has
not yet been checked in this repository, it is marked as carried from the
specification and must be re-confirmed before use.

Last verification pass: **2026-09-04** (Phase 1).

---

## Verified in this repository

These were checked against live provider documentation because Phase 1 encodes
them as configuration defaults.

### DeepSeek — model aliases and pricing

Source: <https://api-docs.deepseek.com/quick_start/pricing/>

| Detail | Status |
|---|---|
| `deepseek-v4-flash` is a current model alias | confirmed |
| `deepseek-v4-pro` is a current model alias | confirmed |
| 1M context window | confirmed |
| Peak/off-peak pricing tiers as in the spec | confirmed |

Additional detail found: a `deepseek-v4-flash-vision-exp` model exists
(experimental, vision). StockBrain does not use it. Documented max output is
384K tokens. Peak hours are 01:00–04:00 and 06:00–10:00 UTC, Monday–Friday, with
off-peak at half the peak rate.

Encoded as the defaults of `DEEPSEEK_FLASH_MODEL` / `DEEPSEEK_PRO_MODEL`.
Pricing is **not** in application logic; it will be configurable cost rates used
for telemetry only.

### Trading 212 — base URLs, auth, order semantics

Sources: <https://docs.trading212.com/api/orders/placemarketorder>,
<https://t212public-api-docs.redoc.ly/>,
<https://helpcentre.trading212.com/hc/en-us/articles/14584770928157-Trading-212-API-key>

| Detail | Status |
|---|---|
| Demo base `https://demo.trading212.com/api/v0` | confirmed |
| Live base `https://live.trading212.com/api/v0` | confirmed |
| HTTP Basic with API key as username, API secret as password | confirmed |
| Market order: `POST /equity/orders/market` | confirmed |
| Body `{ticker, quantity, extendedHours}`; negative quantity = sell | confirmed |
| Broker instrument ticker format, e.g. `AAPL_US_EQ` | confirmed |
| Market order rate limit 50 req / 1 minute | confirmed |
| **Order POST is not idempotent** | confirmed |

The idempotency warning is quoted verbatim in the docs: *"In this beta version,
this endpoint is not idempotent. Sending the same request multiple times may
result in duplicate orders."* This is the single most important external fact in
the system and is the reason for the `uq_execution_attempts_sent_once` index and
the `EXECUTION_AMBIGUOUS` state.

Discrepancy worth noting for Phase 8: the order endpoint's security schemes are
documented as `authWithSecretKey` **or** `legacyApiKeyHeader`, i.e. a legacy
API-key-only header scheme exists alongside key+secret Basic. The broker adapter
must target the current key+secret scheme and must not silently fall back to the
legacy one. To be re-confirmed when the adapter is built.

---

## Carried from the specification — verify before implementing

### Alpaca (Phase 2, Phase 4)

Used for real-time news, historical news backfill, and US reference prices.
Never for execution.

To confirm before use: the news WebSocket URL
(`wss://stream.data.alpaca.markets/v1beta1/news`), the auth/subscribe message
shapes, the news message field set, the REST `/v1beta1/news` parameters, and the
`iex` / `sip` / `delayed_sip` stock feed endpoints.

Entitlement is subscription-dependent, so a startup capability check is
mandatory: authenticating to a feed the account does not hold returns an
insufficient-subscription error. Losing real-time news must move discovery into
a degraded mode, not crash the application.

### Firecrawl v2 (Phase 2)

Broad thematic web discovery. To confirm: `POST /v2/search`, the `sources`,
`limit`, `tbs`, `includeDomains` / `excludeDomains` and `scrapeOptions`
parameters, and the credits-used field in responses.

Search results are frequently syndicated duplicates of the same story, so the
normalizer must deduplicate rather than trusting the URL.

### SEC EDGAR (Phase 2)

No API key. A descriptive `User-Agent` including a contact address is required,
which is why `SEC_CONTACT_EMAIL` gates the provider as `DISABLED` when unset.

To confirm: the submissions and XBRL endpoint shapes, and the daily/recent index
resources used for global filing discovery. The accession number is the unique
source identifier.

### FRED (Phase 5)

Macro context. API key required. To confirm: series-observation parameters and
the v2 bearer-key resources.

### TradingAgents (Phase 5)

Pin a specific commit or release of `TauricResearch/TradingAgents`. Never
install unpinned `main`.

The pinned version must contain the dedicated DeepSeek client that preserves
`reasoning_content` across turns — older versions break DeepSeek thinking mode
in multi-turn tool calls. Do not substitute a generic `ChatOpenAI`. A manual
pre-deployment integration test performing a real multi-turn DeepSeek tool call
is required.

### Telegram (Phase 7)

`python-telegram-bot` 22.x, long polling. `getUpdates` and `setWebhook` are
mutually exclusive. Authorization uses numeric user IDs only.

---

## Source quality categories

Trust is maintained as transparent data on `sources.source_category`, never as
editorial scoring inside an LLM prompt.

| Category | Examples |
|---|---|
| `REGULATOR` | SEC, other regulators, exchange notices |
| `ISSUER` | Company investor relations, official releases |
| `GOVERNMENT` | Government departments and agencies |
| `NEWSWIRE` | Benzinga via Alpaca, major wires |
| `PRESS` | Reputable outlets |
| `UNKNOWN` | Blogs, aggregators, social sources |

`UNKNOWN` sources must not independently trigger a high-confidence proposal
without corroboration.
