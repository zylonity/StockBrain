# External sources and API verification

Every external dependency, what it is used for, and the current verification
state of the details StockBrain relies on.

**Verification policy.** A provider detail is verified against that provider's
own current documentation *in the phase that implements its adapter*, before the
first line of request-building code. Nothing here is guessed. Where a detail has
not yet been checked in this repository, it is marked as carried from the
specification and must be re-confirmed before use.

Last verification pass: **2026-09-04** (Phase 4).

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
8. **The response echoes the alias, not a dated model name.** The spec states
   that `deepseek-v4-flash` "resolves to" `DeepSeek-V4-Flash-0731`. A live call
   confirmed the `model` field comes back as the alias `deepseek-v4-flash`
   verbatim. `PricingTable.rates_for` matches on prefix, so a future dated
   variant would still price correctly, but nothing should assume a dated name
   appears in the response.

**Live smoke test observed 2026-09-04** (`pytest -m live
tests/integration/test_deepseek_live.py`, one request, 46 tokens total):

```
model reported:      deepseek-v4-flash
finish_reason:       stop
provider request id: present
prompt tokens:       41  (cache hit 0 / miss 41)
completion tokens:   5
reasoning tokens:    0        <- confirms thinking:{"type":"disabled"} is honoured
had reasoning text:  False
latency:             934ms
estimated cost:      $0.000012
```

The zero reasoning-token count is the important line: it confirms the explicit
`thinking` parameter works and the classifier is genuinely running the cheap
non-thinking path.

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
legacy one. Re-confirmed in Phase 4: the *metadata* endpoints document the same
two schemes, and the client sends only the Basic key+secret form.

### Trading 212 — instrument and exchange metadata (Phase 4)

Sources: <https://docs.trading212.com/api>,
<https://docs.trading212.com/api/instruments/instruments.md>,
<https://docs.trading212.com/api/instruments/exchanges.md>,
<https://docs.trading212.com/api/section/rate-limiting>

| Detail | Status |
|---|---|
| `GET /api/v0/equity/metadata/instruments`, rate limit **1 req / 50s** | confirmed |
| `GET /api/v0/equity/metadata/exchanges`, rate limit **1 req / 30s** | confirmed |
| Both refresh their underlying data every 10 minutes | confirmed |
| Instrument fields: `addedOn`, `currencyCode`, `extendedHours`, `isin`, `maxOpenQuantity`, `name`, `shortName`, `ticker`, `type`, `workingScheduleId` | confirmed |
| `type` enum: `CRYPTOCURRENCY ETF FOREX FUTURES INDEX STOCK WARRANT CRYPTO CVR CORPACT` | confirmed |
| Exchange fields: `id`, `name`, `workingSchedules[].id`, `workingSchedules[].timeEvents[].date/.type` | confirmed |
| `timeEvents.type` enum: `OPEN CLOSE BREAK_START BREAK_END PRE_MARKET_OPEN AFTER_HOURS_OPEN AFTER_HOURS_CLOSE OVERNIGHT_OPEN` | confirmed |
| Documented failure statuses for both endpoints: 401, 403, 408, 429 | confirmed |
| Response headers `x-ratelimit-limit`, `-period`, `-remaining`, `-reset`, `-used` | confirmed |
| Rate limits apply **per account**, not per API key or per IP | confirmed |
| API is enabled only for Invest and Stocks ISA accounts, and is in **beta** | confirmed |

**Discrepancies against StockBrain's spec, and what was implemented:**

1. **The instrument payload has no exchange field.** This is the most
   consequential finding of the phase. An instrument carries only a
   `workingScheduleId`, and *only* `/equity/metadata/exchanges` says which
   exchange owns that schedule. Instrument resolution matches on exchange, so a
   sync that fetched instruments alone would have no exchange to match against.
   Both endpoints are therefore fetched together, exchanges first, and
   `broker_instruments.exchange` is **derived** from the schedule map. An
   instrument whose schedule is absent keeps a NULL exchange rather than one
   inferred from its ticker.
2. **There is no `minTradeQuantity`.** The specification assumed one alongside
   `maxOpenQuantity`. The current documented response has only the maximum. The
   column stays nullable and is parsed only if a future response supplies it;
   nothing invents a minimum.
3. **The rate limits are far stricter than the order endpoint's.** The spec
   records 50 req/min for market orders and says nothing about metadata. One
   request per 50 seconds for instruments is a different order of magnitude, and
   a single shared token bucket would either throttle the exchanges call or
   overrun the instruments one — so each endpoint gets its own bucket, inside
   the retry loop so a 429 retry waits for a token too.
4. **408 is a documented failure status.** Unusual for a GET, and it is treated
   as transient (the shared client already classifies it that way).
5. **`x-ratelimit-period` is documented in seconds.** It is recorded verbatim as
   text, since the header carries no unit.
6. The docs state the burst model explicitly: a limit of "50 per minute" permits
   50 requests in the first five seconds, then a wait until `x-ratelimit-reset`.
   StockBrain paces instead of bursting, because the metadata endpoints have a
   budget of one.

**Trading 212 pricing rule, re-checked in Phase 4.** The current public API
documentation exposes no market-data endpoint at all — the metadata endpoints
carry no prices, and position/order data is account state rather than a quote.
Nothing in the current documentation guarantees Trading 212 data as real-time
execution pricing, so the spec section 12 rule stands unchanged and is now
enforced in code: `PriceSource.BROKER_T212` is absent from
`EXECUTION_GRADE_PRICE_SOURCES`, and `quote_blockers` refuses any quote carrying
it. Phase 4 implements no Trading 212 price path of any kind.

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

### Alpaca market data — quotes, trades, bars, entitlement

Sources: <https://docs.alpaca.markets/us/reference/stocklatestquotesingle-1>,
<https://docs.alpaca.markets/us/reference/stocklatesttradesingle-1>,
<https://docs.alpaca.markets/us/reference/stockbarsingle-1>,
<https://docs.alpaca.markets/us/docs/about-market-data-api>,
<https://docs.alpaca.markets/us/docs/historical-stock-data-1>,
<https://docs.alpaca.markets/us/docs/market-data-faq>

| Detail | Status |
|---|---|
| Base `https://data.alpaca.markets` (sandbox `data.sandbox.alpaca.markets`) | confirmed |
| `GET /v2/stocks/{symbol}/quotes/latest` -> `{symbol, quote:{t,bx,bp,bs,ax,ap,as,c,z}}` | confirmed |
| `GET /v2/stocks/{symbol}/trades/latest` -> `{symbol, trade:{t,x,p,s,c,i,z}}` | confirmed |
| `GET /v2/stocks/{symbol}/bars` -> `{symbol, bars:[{t,o,h,l,c,v,n,vw}], next_page_token}` | confirmed |
| Auth `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` (HTTP Basic also accepted) | confirmed |
| Rate-limit headers `X-RateLimit-Limit` / `-Remaining` / `-Reset` | confirmed |
| Basic plan: 200 historical req/min, IEX real-time only, 15-minute SIP delay | confirmed |
| Algo Trader Plus ($99/mo): all US exchanges, 10,000 req/min | confirmed |
| `0` on a bid or ask means "no active bid/ask", not a price of zero | confirmed |
| Bars `limit` max 10,000, default 1,000; pagination via `next_page_token` | confirmed |

**Discrepancies against StockBrain's spec, and what was implemented:**

1. **The `feed` enum is wider than the spec's, and differs per endpoint.** The
   spec lists `iex` / `sip` / `delayed_sip`. The *latest* endpoints now accept
   `sip`, `iex`, `delayed_sip`, `otc`, `boats` (Blue Ocean ATS overnight) and
   `overnight`. The **historical** endpoints accept only `iex`, `sip`, `otc` and
   `boats` — `delayed_sip` and `overnight` are not legal bars feeds. Forwarding
   the configured feed blindly would 400 every bars request for a deployment
   using delayed SIP, so the bars call maps an unsupported feed onto `iex` and
   records on every bar which feed actually produced it. StockBrain does not
   offer `otc` (needs a special broker-partner subscription), `boats` or
   `overnight` as configuration values, because none maps to an execution-grade
   price source for a US equity position.
2. **The default feed depends on the account's subscription** —
   documented as "`sip` if the user has the unlimited subscription, otherwise
   `iex`". An omitted `feed` parameter therefore *means something different per
   account*, which is exactly the DeepSeek `thinking` trap. The feed is sent
   explicitly on every request and a test asserts it.
3. **HTTP 403 means both "bad credential" and "missing entitlement".** The FAQ
   documents the entitlement case as a 403 whose body is
   `{"code":42210000,"message":"subscription does not permit querying recent SIP
   data"}`. The shared HTTP client maps 401/403 to `ProviderAuthError` and 402 to
   `ProviderEntitlementError`, so Alpaca's entitlement failures would have been
   misclassified as credential failures — fatal instead of degrading. A
   per-provider `refine_error` hook now inspects the body and re-classifies;
   both cases are tested. This is why the spec's assumption that entitlement
   surfaces as a distinct status does not hold.
4. **The spec's `MarketDataProvider` protocol is `quote()` / `bars()`.** The
   implemented interface adds `latest_trade()` and `capability()`: a trade print
   is the fallback reference price when a venue has no live two-sided quote, and
   the capability probe is how spec section 23 step 8's "Alpaca entitlement
   check" is actually performed.
5. **Sizes are in shares, not round lots, since 3 November 2025** — the schema
   notes the change explicitly. Recorded as integers with no lot conversion.
6. **Quote/trade timestamps are RFC-3339 with nanosecond precision.** Python's
   `datetime` truncates to microseconds; the age calculation is in milliseconds,
   so the loss is immaterial, but nothing rounds a timestamp before comparing it.
7. Alpaca answers 403 (not 401) for an unauthenticated request in the documented
   FAQ, while the OpenAPI definition lists 401 for missing/invalid auth headers.
   Both are handled identically.

**Entitlement posture.** SIP access is never assumed. `ALPACA_STOCK_FEED`
defaults to `iex`, the only feed available without a paid subscription. The
startup probe issues exactly one quote request and records
`HEALTHY` / `AUTH_FAILED` / `ENTITLEMENT_MISSING` / `DEGRADED` / `DOWN` /
`DISABLED`. A missing entitlement degrades pricing only: ingestion,
classification and instrument resolution are structurally unaffected, and
proposal sizing is blocked by `quote_blockers` rather than falling back to
Trading 212 or yfinance data.

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
