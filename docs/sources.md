# External sources and API verification

Every external dependency, what it is used for, and the current verification
state of the details StockBrain relies on.

**Verification policy.** A provider detail is verified against that provider's
own current documentation *in the phase that implements its adapter*, before the
first line of request-building code. Nothing here is guessed. Where a detail has
not yet been checked in this repository, it is marked as carried from the
specification and must be re-confirmed before use.

Last verification pass: **2026-09-04** (Phase 3).

---

## Verified in this repository

These were checked against live provider documentation because Phase 1 encodes
them as configuration defaults.

### DeepSeek — API, models, JSON mode, usage, rate limits, pricing

Sources: <https://api-docs.deepseek.com/>,
<https://api-docs.deepseek.com/api/create-chat-completion>,
<https://api-docs.deepseek.com/guides/json_mode/>,
<https://api-docs.deepseek.com/quick_start/rate_limit>,
<https://api-docs.deepseek.com/quick_start/pricing/>

| Detail | Status |
|---|---|
| Base `https://api.deepseek.com`, endpoint `/chat/completions` | confirmed |
| `Authorization: Bearer <key>`, OpenAI-compatible shape | confirmed |
| `deepseek-v4-flash`, `deepseek-v4-pro` are current model ids | confirmed |
| JSON mode is `response_format: {"type": "json_object"}` | confirmed |
| Prompt must contain the word "json"; example schema recommended | confirmed |
| `usage` splits `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` | confirmed |
| 429 signals the account concurrency limit | confirmed |
| Peak 01:00–04:00 and 06:00–10:00 UTC Mon–Fri; off-peak is half | confirmed |
| V4 Flash: $0.014 / $0.44 / $1.32 per 1M (cache hit / miss / output), peak | confirmed |

**Discrepancies against StockBrain's spec, and what was implemented:**

1. **`thinking` defaults to `{"type": "enabled"}`.** This is the most
   consequential finding of the phase. The spec says the classifier runs
   "thinking disabled" but not how, and the parameter is not mentioned at all.
   Omitting it would silently turn the cheap high-volume triage path into a
   reasoning call, changing both latency and cost. The client therefore sends
   `{"type": "disabled"}` explicitly on every non-thinking call, and a test
   asserts it.
2. **The usage object is richer than the spec implies.** It reports
   `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens` separately, and
   those are priced roughly thirty times apart. A cost estimate that merges them
   is meaningless, so both are stored (`llm_calls.cached_input_tokens` and
   `cache_miss_input_tokens`) and priced separately. Where a provider does not
   report the split, everything is charged at the miss rate — over-estimating,
   which is the safe direction for a budget.
3. **`finish_reason` can be `insufficient_system_resource`**, a DeepSeek-specific
   capacity condition that is not a model answer. It is treated exactly like a
   5xx and retried. `finish_reason: "length"` is reported as truncation by name,
   because it otherwise surfaces as confusing invalid JSON.
4. **JSON mode can return empty content**, which the docs acknowledge. That is
   raised as an error rather than being allowed to become an empty
   classification.
5. **The server holds connections open**, emitting blank lines while queued, and
   gives up only after ten minutes. Read timeouts are set generously (120s
   default) rather than aggressively.
6. **`reasoning_content` may be present on the message.** StockBrain records only
   a boolean that it was present and discards the text at the client boundary,
   so hidden reasoning exists nowhere to leak from. The structured `rationale`
   field of the classifier schema is the explanation shown in the UI.
7. `deepseek-v4-flash-vision-exp` exists; StockBrain does not use it. Documented
   max output is 384K tokens; concurrency limits are 2,500 for Flash and 500 for
   Pro.

Model ids are the defaults of `DEEPSEEK_FLASH_MODEL` / `DEEPSEEK_PRO_MODEL`.
Pricing is **not** in application logic: rates live in
`stockbrain/llm/pricing.py` as configurable data used for telemetry and budget
comparison only, and no trading decision reads a price.

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

### Alpaca news — stream and REST

Sources: <https://docs.alpaca.markets/docs/streaming-real-time-news>,
<https://docs.alpaca.markets/docs/streaming-market-data>,
<https://docs.alpaca.markets/reference/news-3>

| Detail | Status |
|---|---|
| Stream `wss://stream.data.alpaca.markets/v1beta1/news` | confirmed |
| Auth by JSON message `{"action":"auth","key":…,"secret":…}` | confirmed |
| Auth by `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` headers | confirmed (alternative) |
| Subscribe `{"action":"subscribe","news":["*"]}` | confirmed |
| Control frames `{"T":"success","msg":"connected"/"authenticated"}` | confirmed |
| REST `GET https://data.alpaca.markets/v1beta1/news` | confirmed |
| Pagination via `next_page_token` | confirmed |

**Discrepancies against StockBrain's spec, and what was implemented:**

1. **The news payload has more fields than the spec lists.** The spec's field
   set omits `symbols` (array of related tickers) and `source` (the originating
   outlet). Both are captured: `symbols` feeds company resolution as a *hint*,
   and `source` feeds the trust category. The REST shape additionally returns an
   `images` array, which is counted but not stored.
2. **`limit` is capped at 50 per page** on the historical endpoint. The spec does
   not mention a cap. The backfill client pages with `next_page_token` and
   bounds the *total* documents rather than the page size.
3. **Documented WebSocket error codes** are handled explicitly, because the
   correct response differs per code: 402 (auth failed) and 404 (auth timeout)
   are credential failures that must not be retried; 409/410 (insufficient
   subscription) are entitlement failures that degrade discovery rather than
   crash it; everything else is transient and reconnects with backoff.
4. Sandbox `wss://stream.data.sandbox.alpaca.markets/v1beta1/news` exists but is
   intended for Broker API users; the production URL stays the default for an
   individual account, per the spec.

Rate-limit headers on the REST endpoint are `X-RateLimit-Limit` (100/min),
`-Remaining` and `-Reset`; the client parses these case-insensitively and keeps a
client-side token bucket below the ceiling.

### Firecrawl v2 search

Source: <https://docs.firecrawl.dev/api-reference/endpoint/search>

| Detail | Status |
|---|---|
| `POST https://api.firecrawl.dev/v2/search`, `Authorization: Bearer` | confirmed |
| `limit` 1–100 | confirmed |
| `tbs` freshness, `includeDomains`, `excludeDomains`, `country` | confirmed |
| `scrapeOptions.formats: [{"type":"markdown"}]` returns page content | confirmed |
| Response `{success, data:{web,news,images}, creditsUsed, id, warning}` | confirmed |

**Discrepancies against StockBrain's spec, and what was implemented:**

1. **`sources` is an array of objects, not strings.** The spec shows
   `"sources": ["web", "news"]`. The current API documents
   `[{"type": "web"}, {"type": "news"}]`, and that is what the client sends. This
   is the most consequential difference found in this phase: the spec's form
   would have been rejected or silently defaulted.
2. **Web and news results have different shapes.** Web results carry
   `description` (plus `markdown` when scraping is on); news results carry
   `snippet`, `date` and `imageUrl`. The parser handles both explicitly rather
   than assuming one shape.
3. **`query` is capped at 500 characters**, which the spec does not mention.
   Queries are truncated client-side.
4. New parameters exist that StockBrain does not use: `categories`,
   `highlights`, `enterprise`, `threatProtection`.

`creditsUsed` is recorded per query for cost control, as the spec requires.

### SEC EDGAR

Sources: <https://www.sec.gov/search-filings/edgar-application-programming-interfaces>,
<https://www.sec.gov/about/developer-resources>,
<https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data>

| Detail | Status |
|---|---|
| No API key for `data.sec.gov` | confirmed |
| Descriptive `User-Agent` with contact required, else 403 | confirmed |
| `https://data.sec.gov/submissions/CIK##########.json` | confirmed |
| CIK zero-padded to 10 digits | confirmed |
| Ticker map `https://www.sec.gov/files/company_tickers.json` | confirmed |
| Daily index under `/Archives/edgar/daily-index` | confirmed |

**Discrepancies against StockBrain's spec, and what was implemented:**

1. **The rate limit is 10 requests/second per IP across all EDGAR domains**, and
   exceeding it returns 403 and blocks the IP for roughly ten minutes. The spec
   does not state this at all, and it is the single most important operational
   fact about this provider — the penalty is a block, not a 429 that could be
   backed off from. The client therefore runs a shared token bucket at **5 req/s**
   across both the `data.sec.gov` and `www.sec.gov` clients, since the limit is
   per IP rather than per host.
2. **`filings.recent` is columnar, not a list of objects.** It returns parallel
   arrays where `form[i]` and `accessionNumber[i]` describe the same filing. The
   parser flattens them and stops at the shortest required column, so a truncated
   column can never produce a filing with fields taken from two different rows.
3. A 403 aborts the whole watchlist sweep rather than continuing, so a
   User-Agent mistake or an existing block is not deepened by walking every
   company.

---

## Carried from the specification — verify before implementing

### Alpaca market data (Phase 4)

The `iex` / `sip` / `delayed_sip` stock feeds are not yet used. Entitlement is
subscription-dependent, so a startup capability check is required before relying
on any of them for pre-trade pricing.

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
