# External sources and API verification

Every external dependency, what it is used for, and the current verification
state of the details StockBrain relies on.

**Verification policy.** A provider detail is verified against that provider's
own current documentation *in the phase that implements its adapter*, before the
first line of request-building code. Nothing here is guessed. Where a detail has
not yet been checked in this repository, it is marked as carried from the
specification and must be re-confirmed before use.

Last verification pass: **2026-09-05** (Phase 9; upstream/live checks began September 4).

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

**Live verification PASSED 2026-09-04** against the **live** environment,
read-only, with explicit operator consent. The demo environment could not be
used: the available key pair is a live-environment key, and Trading 212 issues
keys per environment.

```
GET https://live.trading212.com/api/v0/equity/account/summary   -> 200  (auth check)
GET https://live.trading212.com/api/v0/equity/metadata/exchanges -> 200
GET https://live.trading212.com/api/v0/equity/metadata/instruments -> 200

exchanges                 17
working schedules         53
instruments            17452
instrument types       STOCK 11161 · ETF 6268 · WARRANT 23
```

Field population across all 17,452 instruments:

| Field | Populated |
|---|---|
| `ticker`, `name`, `shortName`, `isin`, `currencyCode`, `type`, `extendedHours`, `maxOpenQuantity`, `workingScheduleId`, `addedOn` | **100.00%** |
| `minTradeQuantity` | **0.00%** — confirms the field does not exist |

| Question | Observed |
|---|---|
| ISIN coverage on STOCK/ETF | **100.00%** (17,429 / 17,429; STOCK 11,161/11,161, ETF 6,268/6,268) |
| `workingScheduleId` resolves to an exchange | **100.00%**, zero unmapped schedule ids |
| `shortName` populated | **100.00%** |
| `shortName` agrees with the ticker prefix | **35.7%** — it disagrees on 11,217 rows (64.3%) |
| `shortName` reused across listings | **1,640 distinct symbols** |
| rate-limit headers | all five, on every response |

```
exchanges    : limit=1 period=30 remaining=0 used=1 reset=1788580640
instruments  : limit=1 period=50 remaining=0 used=1 reset=1788580661
```

`period` matches the documented per-endpoint limits exactly (30s and 50s), and
an authentication failure returns **no** rate-limit headers at all — so a 401
costs no request budget, which is worth knowing when diagnosing credentials.

**Three findings that changed the code:**

1. **Trading 212 writes share classes with a slash** (`TAP/A`, `BBD/B`, `HVT/A`
   -- 47 rows), while market-data feeds and language models write `TAP.A` or
   `BRK-B`. The old normaliser dropped `/` outright, which did two bad things:
   a classifier hint of `BRK.B` could never match the stored listing, and six
   real listings collapsed onto unrelated ones -- `HVT/A` became `HVTA`, which
   is a different security (likewise `AGF/B`, `HEI/A`, `CRD/A`, `MOG/B`,
   `EMP/A`). `normalize_ticker` now canonicalises every separator to `.`.
   Re-measured across all 15,573 distinct shortNames: **zero collisions**, and
   the six false ones are gone. A trailing separator is deliberately kept, since
   London's `BP.` must not become New York's `BP`.
2. **64% of tickers use a two-part form** (`VODl_EQ`, not `AAPL_US_EQ`), so a
   missing venue code is the norm rather than the exception.
   `split_broker_ticker` returned the *whole* ticker as the symbol in that case,
   which would have made the fallback market symbol `VODL.EQ`. It now returns
   the first segment and no venue code. `shortName` is preferred and is
   populated on every row, so this is a last-resort path -- but a last-resort
   path should still be right.
3. **`shortName` is not unique**: 1,640 symbols are reused across listings
   (`STN`, `TLS`, `PET`, `XNAS`, ...). This is the "ticker reuse" ambiguity class
   with a real number attached, and it confirms the resolver must treat a bare
   ticker as insufficient. Rung 3 already returns every match and reports
   AMBIGUOUS without an exchange hint.

**Still unverified against the demo environment.** The instrument universe a
demo account sees may differ from live. Nothing in the code depends on the two
being identical -- the sync reads whichever universe the configured environment
returns -- but the numbers above are live-account numbers.

**Superseded: the earlier 401 diagnosis.** For the record, every documented
authentication form was rejected by the **demo** environment with this live key:

```
GET https://demo.trading212.com/api/v0/equity/metadata/exchanges
    Basic key:secret            -> HTTP 401, empty body, no x-ratelimit-* headers
GET https://demo.trading212.com/api/v0/equity/account/summary
    Basic key:secret            -> HTTP 401, empty body, no x-ratelimit-* headers
    Basic secret:key (swapped)  -> HTTP 401
    Authorization: <raw key>    -> HTTP 401   (the documented legacyApiKeyHeader)
```

The same pair returned 200 against `live.trading212.com`, which is what
identified it as a live key. The credential itself was clean throughout (37
alphanumeric characters; secret 43 characters of URL-safe base64, no whitespace,
no quotes, no inline comment), so this was never a parsing or paste error.

Reading reference data from the live environment is gated behind **two**
independent switches in the live test -- `T212_METADATA_ENV=live` and
`T212_ALLOW_LIVE_METADATA_READ=yes` -- mirroring how live execution is gated, so
it cannot happen as a side effect of running the suite. Demo remains the
default. Neither switch is written to `.env`.

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

**Live verification PASSED 2026-09-04 03:40 UTC** (`pytest -m live -s
tests/integration/test_phase4_live.py`, two GETs):

```
GET /v2/stocks/AAPL/quotes/latest?feed=iex      -> HTTP 200 (capability probe)
GET /v2/stocks/AAPL/quotes/latest?feed=iex      -> HTTP 200 (observed quote)

feed requested / returned : iex / iex
capability state          : HEALTHY
entitlement               : IEX confirmed on the Basic plan
price source              : ALPACA_IEX
bid / ask                 : 305.33 / 338.27   (sizes 40 / 40)
mid                       : 321.80            (Decimal, no float artefacts)
currency / tape           : USD / C
conditions                : ["R"]
provider timestamp        : 2026-09-04T20:00:00.006211+00:00
quote age                 : 27,618,397 ms  (~7.67 h)
two-sided                 : True
sizing blockers           : ["quote is 27618397ms old, older than the 15s limit"]
```

Everything documented held: the quote schema, the `as`-keyword alias, the tape
and condition fields, the explicit-feed round trip, and `Decimal` prices.

**Two observations that documentation could not have given, and what changed:**

1. **Outside market hours the "latest" IEX quote is the 16:00 ET closing print**
   -- here 7.67 hours old, with a $33 spread on AAPL. `quote_blockers` correctly
   refused it on age, so the safety property held. But `capability()` reported
   `state: HEALTHY` and `realtime_pricing_usable: True` beside a 15-second
   limit, which was accurate and unreadable at the same time. Entitlement and
   freshness are now reported separately: the state stays HEALTHY, because
   folding closing prints into DEGRADED would make the subsystem look broken
   every night, and `ProviderCapability.probe_quote_stale` plus an explicit
   blocker string state the staleness outright. Regression-tested both ways.
2. **The wide overnight book is a second reason never to size on a stale
   quote.** A $33 spread on a $321 mid is a 10% round trip. Phase 4 records the
   spread on the DTO; a spread ceiling belongs to the Phase 6 risk engine, not
   here, and is noted as a requirement for it.

The Alpaca news WebSocket has still never been connected -- these were REST
market-data calls only.

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

## Phase 5 verified research integration — 2026-09-04

### TradingAgents release selection and package boundary

Official upstream: https://github.com/TauricResearch/TradingAgents.
Pinned **v0.4.0**, peeled commit
**2448d0a12576f9b2ddcd5980a0630833423d1e1b**, in
`third_party/TradingAgents`. Reviewed releases v0.2.4 through v0.4.0,
current main `9dee508c44662702281a8dbaad1f7b42179b5ba7`, package layout,
provider registry, graph factories, structured fallback, tools, and persistence.

Selected the released v0.4.0 rather than moving main: it includes DeepSeek
reasoning round-trip (`7e9e7b8`), DeepSeek structured-output compatibility
(`22bb91b`), output-token caps, graph-router fixes and point-in-time fixes for
macro/social/memory data. The five later main commits include Reddit transport,
FRED clock and manager-direction changes; StockBrain does not call those
upstream providers and its own policy permits HOLD/NO_ACTION under uncertainty.

Source references:
- [Release](https://github.com/TauricResearch/TradingAgents/releases/tag/v0.4.0)
- [Pinned DeepSeek client](https://github.com/TauricResearch/TradingAgents/blob/2448d0a12576f9b2ddcd5980a0630833423d1e1b/tradingagents/llm_clients/openai_client.py)
- [Pinned graph setup](https://github.com/TauricResearch/TradingAgents/blob/2448d0a12576f9b2ddcd5980a0630833423d1e1b/tradingagents/graph/setup.py)
- [Pinned packaging](https://github.com/TauricResearch/TradingAgents/blob/2448d0a12576f9b2ddcd5980a0630833423d1e1b/pyproject.toml)

The ordinary package requires Redis, other LLM SDKs, backtrader, experimental
LangChain and SQLite checkpointing. Therefore the preferred Git package route
was rejected in favor of the permitted submodule route. StockBrain installs
only the import/runtime subset in its existing compiled requirements files.
The submodule's Python source digests are checked at load and Docker build;
an unreviewed checkout cannot silently replace the pinned engine. **No upstream
source patch.** StockBrain uses the upstream market/fundamentals analysts,
bull/bear researchers, manager, state schema, and dedicated DeepSeek client.
Its adapter constructs the bounded LangGraph, supplies sentiment from existing
evidence, and replaces the sizing-oriented trader with its own research schema.

The upstream sentiment node performs unconditional Yahoo/StockTwits/Reddit
fetches, and the standard graph always includes risk roles and disk persistence.
None is called by StockBrain. Upstream tool functions are never bound or executed;
the single permitted tool reads the already assembled research packet. The
`yfinance` and `stockstats` packages are required by upstream imports, but no
Yahoo data access, cache mutation, or execution-price fallback is enabled.
Alpaca and FRED are the only supplemental providers. Missing fundamentals or
social observations remain explicit limitations of the supplied evidence.

### DeepSeek research protocol and live verification

Rechecked [thinking/tool continuity](https://api-docs.deepseek.com/guides/thinking_mode/)
and [Chat Completions schema](https://api-docs.deepseek.com/api/create-chat-completion/).
For a request carrying `tools`, reasoning_content from **all** prior assistant
turns must be returned, even when a prior turn did not call a tool. With no
tools, it is unnecessary. DeepSeek V4 thinking rejects forced `tool_choice`;
StockBrain sends none. `thinking` is explicit in both modes.

StockBrain invokes the pinned `DeepSeekChatOpenAI` payload serializer and response
parser through its own HTTP transport. This preserves the upstream protocol
behavior while keeping per-call telemetry, budgets, retry policy and secrets
outside the graph. SDK HTTP methods, automatic SDK retries, external tracing,
checkpointers and decision logs are not used. Reasoning stays in transient
assistant messages only; saved reports, decisions, API output and telemetry
contain no reasoning text or raw provider responses. A presence boolean and
usage counts are permitted. No model receives broker/Telegram/database keys.

Model routing is explicit: **Flash** (`deepseek-v4-flash`, thinking disabled)
for market, fundamentals and sentiment; **Pro** (`deepseek-v4-pro`, thinking
enabled) for bull, bear, research manager and final decision. All roles retain
request IDs, usage/cache split, latency, finish/error class and estimated cost
in the existing `llm_calls` accounting. Explicit capacity responses may receive
one bounded retry, recorded separately. Auth, entitlement, rate limit, malformed,
truncated and transport failures terminate visibly; a transport failure is not
blindly replayed because spend may already have occurred.

**Live PASS:** opt-in `tests/integration/test_research_live.py` performed exactly
one Flash thinking-mode assistant/tool/assistant flow: **2 calls, 861 tokens,
$0.000126 estimated**, request IDs present on both, finish reasons `tool_calls`
then `stop`. The original hidden reasoning was echoed internally; only safe
usage facts were printed. Pro shares the same tested transport and was verified
with offline response contracts; no Pro-specific incompatibility appeared, so
no paid Pro call or full research workflow was run. Normal pytest excludes live.

### FRED observations, vintage and degradation

Verified [observations](https://fred.stlouisfed.org/docs/api/fred/series_observations.html),
[keys](https://fred.stlouisfed.org/docs/api/api_key.html),
[errors](https://fred.stlouisfed.org/docs/api/fred/errors.html), and
[v2 overview](https://fred.stlouisfed.org/docs/api/fred/v2/).
The implementation uses **v1**, GET
`https://api.stlouisfed.org/fred/series/observations`, with query `api_key`,
`series_id`, `file_type=json`, `observation_start/end`, `realtime_start/end`,
`units=lin`, `sort_order=desc`, and `limit=100`. No v2 bearer request or bulk
release/catalogue download is needed. Only **DFF** (policy-rate context) and
**DGS10** (long-term-rate context) are enabled.

The response is an `observations` array with `date`, string `value`, and vintage
bounds. `.` means unavailable, not zero. Decimal parsing rejects nonfinite or
malformed values. Date filtering alone does not prevent revision look-ahead:
the adapter also pins the vintage to the previous Chicago calendar day,
conservatively excluding same-day later releases and avoiding the upstream
UTC/FRED-clock mismatch. Historical bars likewise exclude incomplete periods;
Phase 4's reaction calculator receives only completed minute bars at the cutoff,
with a historical reference rather than a later live quote.

Missing/invalid keys can be **HTTP 400**, not merely 401/403. The body names
`api_key`; this becomes ProviderAuthError without persisting the echoed body.
423 is locked/transient, 429 rate-limited, 500 unavailable, and malformed data
is ProviderResponseError. Official guidance documents 429 and discretionary
limits, but the checked pages specify no fixed numerical ceiling. StockBrain
paces at one request/second with a bounded safe GET retry. Missing configuration
or supplemental failure degrades research and leaves other subsystems running.

**Live PASS:** one bounded DFF observations request using locally configured
credentials. This supersedes the handoff's earlier statement that FRED was
unconfigured. No key was printed and no broker endpoint was called.

The September 5 container smoke check loaded the pinned engine and returned 200
from research/provider health endpoints. Overall health remained DEGRADED:
the existing configured Trading 212 **demo** metadata request returned 401.
This does not supersede Phase 4's successful **live** read-only metadata check;
no broker environment or execution gate was changed. Alpaca IEX access remained
healthy, while the out-of-hours quote was explicitly stale and blocked for sizing.
Research model/FRED health in the application remains UNKNOWN until an application
run uses them; isolated opt-in smoke calls do not fabricate production telemetry.

## Phase 6 verified Trading 212 account surface — 2026-09-05

Re-read from the current published documentation, which is now served as one
Markdown document at <https://docs.trading212.com/api.md> (linked from
`llms.txt`; the per-operation pages are at
`https://docs.trading212.com/api/<tag>/<operation>.md`).

| Detail | Status |
|---|---|
| `GET /api/v0/equity/account/summary`, rate limit **1 req / 5s** | confirmed |
| Summary fields: `cash.availableToTrade`, `cash.inPies`, `cash.reservedForOrders`, `currency`, `id`, `investments.currentValue`, `.realizedProfitLoss`, `.totalCost`, `.unrealizedProfitLoss`, `totalValue` | confirmed |
| `GET /api/v0/equity/positions`, rate limit **1 req / 1s**, optional `ticker` query parameter | confirmed |
| Position fields: `averagePricePaid`, `createdAt`, `currentPrice`, `instrument.{currency,isin,name,ticker}`, `quantity`, `quantityAvailableForTrading`, `quantityInPies`, `walletImpact.{currency,currentValue,fxImpact,totalCost,unrealizedProfitLoss}` | confirmed |
| Documented failure statuses for both: 401, 403, 408, 429 | confirmed |
| Orders execute **only in the primary account currency**; multi-currency accounts are not supported through the API, so account, position and result values are all returned in that currency | confirmed |
| Functional limit: maximum **50 pending orders** per ticker per account | confirmed |
| Rate limits are per *account*, regardless of key or IP | confirmed |
| API keys may optionally be IP-restricted from the Trading 212 account settings | confirmed |

**Discrepancies against the shape assumed before this phase, and what was
implemented:**

1. **Open positions are at `/equity/positions`, not `/equity/portfolio`.** The
   spec's endpoint list is already correct here, but any client written from the
   older public shape would look for a flat payload with top-level `ticker` and
   `ppl` and find neither: the current response nests the instrument under
   `instrument` and the money under `walletImpact`. Both are parsed by name.
2. **`quantityAvailableForTrading` is not `quantity`.** Shares held inside a pie
   are owned but not individually tradable, so `quantityInPies` is recorded and
   the *available* quantity is the only number a reduction is sized against.
   Sizing a sell against the total would produce an order the broker refuses.
3. **`walletImpact` is already in the account currency and already accounts for
   FX** (`fxImpact` is stated to apply only when the instrument currency differs
   from the account's). StockBrain therefore uses `walletImpact.currentValue`
   as a position's account-currency value rather than multiplying quantity by
   price, and falls back to the product only when the broker supplies no value.
4. **`currency` and `totalValue` are treated as required** on the summary. A
   snapshot missing either cannot size anything, and defaulting one would
   produce a plausible-looking account state built on a field that was never
   sent.
5. **The account-summary limit (1 req/5s) is five times the positions limit
   (1 req/1s).** Each endpoint gets its own token bucket, inside the retry loop,
   for the same reason the metadata endpoints do.
6. **`currentPrice` on a position is broker data.** The API Terms state
   API-supplied market data is not real-time, so `PriceSource.BROKER_T212`
   remains absent from `EXECUTION_GRADE_PRICE_SOURCES` and this field is display
   and reconciliation only. It can never clear `quote_blockers`.

**API Terms constraints that shape the automatic execution policy** (unchanged
text, re-read 2026-09-05; recorded here because Phase 6 is the first phase to
act on them):

- clause **4.2(a)** expressly prohibits using the API for *Algorithmic Trading*,
  defined as a computer automatically determining order parameters — whether to
  initiate, timing, price, quantity or subsequent management — with limited or
  no human intervention;
- clause **6.6** requires prior written consent for high-speed or automated mass
  data-entry use;
- clause **6.7** requires a customised interface to be tested before live
  deployment and makes its use subject to prior written consent.

`EXECUTION_POLICY=automatic` against `T212_ENV=live` is precisely the activity
clause 4.2(a) names, so it requires
`T212_AUTOMATED_TRADING_CONSENT_CONFIRMED=true` and the process refuses to start
without it. Demo (paper) trading risks no real funds and is the supported path
for exercising automatic authorization. The flag records a fact about the
operator's relationship with their broker; it is not a bypass, and no general
override exists.

### Live read-only verification — 2026-09-05

Three GETs total, read-only, with explicit operator consent and behind two
independent switches (`T212_ACCOUNT_ENV=live` and
`T212_ALLOW_LIVE_ACCOUNT_READ=yes`). No balance, position size, ticker or
account identifier was printed; only derived facts about the contract.
Reproduce with `pytest -m live -s tests/integration/test_phase6_live.py`.

```
GET /equity/account/summary   -> 200
GET /equity/positions         -> 200
```

| Observation | Result |
|---|---|
| Account currency (ISO 4217) | **GBP** |
| `id` type | integer |
| Every money value arrived as `Decimal` (no binary float) | **yes** |
| Open positions | 14 |
| Positions whose instrument currency differs from the account's | **14 of 14** |
| Positions with shares inside a pie (`quantityInPies > 0`) | **13 of 14** |
| Positions where `quantityAvailableForTrading != quantity` | **13 of 14** |
| `walletImpact.currency` values observed | `GBP` only — matches the account |
| Rate-limit headers on both endpoints | all five (`limit`, `period`, `remaining`, `reset`, `used`) |

**Three things this measurement settles, none of which the documentation
could have:**

1. **Same-currency-only is the *actual* operating state, not a hypothetical
   limitation.** This account is denominated in GBP and every one of its 14
   positions is in another currency. Under the `currency_alignment` rule, a US
   listing priced by Alpaca in USD against a GBP account blocks — so on this
   account, sizing is currently blocked for the whole universe StockBrain can
   price. That is the rule working, not failing: without a verified FX source, a
   size computed for a GBP account from a USD price is a wrong size. Supporting
   it needs a real FX rate source, which is a deliberate future decision rather
   than something to infer at sizing time.
2. **`quantityAvailableForTrading` is not a defensive nicety.** It differs from
   `quantity` on 13 of 14 live positions, because pie holdings are owned but not
   individually tradable. Sizing a reduction against `quantity` would produce an
   order the broker refuses on almost every position in this account.
3. **`walletImpact` really is in the primary account currency** (GBP on all 14
   rows, against USD instruments), confirming the documented claim. This is why
   a position's account-currency value is read from
   `walletImpact.currentValue` rather than computed as quantity × price, which
   would silently produce a USD number and compare it against a GBP portfolio.

The **demo** environment returned HTTP 401 to the same two calls, consistent
with the Phase 4 finding that Trading 212 issues API keys per environment and
the configured pair is a live key.

### Alpaca quote against the Phase 6 gates — 2026-09-05

One read-only GET, `AAPL`, outside market hours. Capability `HEALTHY`, feed
`iex`, `price_source` `ALPACA_IEX`.

```
bid / ask        305.33 / 338.27
mid              321.80
spread           32.94
spread           1023.6172 bps   (ceiling 50 bps)  -> EXCESSIVE
quote age        37,493,991 ms   (limit 15s)
sizing blockers  ["quote is 37493991ms old, older than the 15s limit",
                  "spread 1023.6172 bps exceeds the 50 bps ceiling (bid 305.33 / ask 338.27)"]
```

This is the same overnight book that motivated the ceiling, now measured
against the rule built for it: **both** gates fire independently. The age check
alone caught it in Phase 4; the spread check is what would catch the same width
during regular hours, when the age check would not. `Decimal` arithmetic
throughout — 338.27 − 305.33 is exactly 32.94, and the mid is exactly 321.80.

---

## Phase 7 verified Telegram surface — 2026-09-05

Checked against the current official documentation before any request schema
was written, then measured against the live Bot API where a measurement was
possible.

* **Bot API** — <https://core.telegram.org/bots/api>
* **python-telegram-bot** — <https://docs.python-telegram-bot.org/en/v22.8/>
* **Library version** — `python-telegram-bot==22.8`, the current 22.x release,
  which targets **Bot API 10.0** (`telegram.constants.BOT_API_VERSION`).

### Library selection

22.8 declares `httpx>=0.27,<0.29`, which the repository's existing
`httpx>=0.28,<0.29` pin already satisfies, so the base install adds **no new
transitive dependency at all**. No extras are installed:

* `job-queue` (apscheduler) is deliberately omitted — spec section 4.8 says not
  to use Telegram's JobQueue for StockBrain's schedules, and the application's
  own PostgreSQL scheduler is authoritative. `ApplicationBuilder.job_queue(None)`
  is passed explicitly rather than relying on the extra being absent.
* `rate-limiter` (aiolimiter) is omitted because StockBrain's message volume is
  a few messages an hour against Telegram's per-second limits, and every send is
  a sequential `await`.

### Facts verified in the documentation

| Fact | Consequence in StockBrain |
|---|---|
| `InlineKeyboardButton.callback_data` is **"1-64 bytes"** | The payload is `sb:` + 43 url-safe characters = 46 bytes, asserted in a test. Exceeding it fails at *send* time, i.e. in production on the message that matters most. |
| `sendMessage.text` is **"1-4096 characters after entities parsing"** | Messages are bounded by construction and chunked on line boundaries as a fallback. |
| `answerCallbackQuery.text` is **"0-200 characters"** | `CallbackResult.short_alert` truncates to 200. |
| "After the user presses a callback button, Telegram clients will display a progress bar until you call `answerCallbackQuery`" | The handler answers **before** any further API call, so a slow edit never leaves a spinner. |
| `getUpdates` "will not work if an outgoing webhook is set up" | Long polling is the designed transport; the opt-in live test asserts `getWebhookInfo().url` is empty, because a webhook left configured would silently stop polling from receiving anything. |
| `getUpdates.timeout` "Defaults to 0, i.e. usual short polling. Should be positive, short polling should be used for testing purposes only" | `TELEGRAM_POLL_TIMEOUT_SECONDS` defaults to 30. |
| `getUpdates.allowed_updates` filters update types | Only `["message", "callback_query"]` are requested. |
| MarkdownV2 requires escaping `_ * [ ] ( ) ~ ` > # + - = \| { } . !` with extra context-dependent rules inside links, code and custom emoji | **Not used.** |
| HTML mode: "All `<`, `>` and `&` symbols that are not part of a tag or an HTML entity must be replaced with the corresponding HTML entities" | HTML is the parse mode; the escape is exactly three substitutions with no positional exceptions, which is what makes it total. |

### Facts verified by reading the library source

Neither is in the prose documentation, and both change how a failure behaves:

* **`Updater.start_polling` retries network errors indefinitely.** The internal
  `network_retry_loop` runs with `max_retries=-1` and `repeat_on_success=True`,
  backing off `1.5×` up to a 30-second ceiling, honouring `RetryAfter`'s own
  delay, and retrying `TimedOut` immediately. It **aborts on `InvalidToken`**.
  StockBrain mirrors that judgement: a rejected credential is recorded DOWN and
  not retried, exactly as `ProviderAuthError` is treated elsewhere.
* **`error_callback` is not called for every error.** `RetryAfter`, `TimedOut`
  and `InvalidToken` are handled inside the loop and bypass it; only other
  `TelegramError`s reach it. So "the callback has not fired" is not evidence of
  health, which is why the runtime additionally proves connectivity with a
  periodic `getMe`.

The library documents that `error_callback` "must not raise exceptions! If it
does, the loop will be aborted" — StockBrain's callback only records health, and
a test asserts it does not raise.

### Manual application lifecycle

`run_polling()` blocks the event loop and installs its own signal handlers, and
the documentation says so explicitly: *"When combining python-telegram-bot with
other asyncio based frameworks, using this method is likely not the best
choice… Instead, you can manually call the methods listed below."* StockBrain
uses the manual sequence inside its FastAPI lifespan:

```
initialize() → updater.start_polling() → start()
   …
updater.stop() → stop() → shutdown()
```

`drop_pending_updates=True` is passed at startup: an update queued while
StockBrain was down is of unknown age, and replaying a control command of
unknown age — `/resume` above all — is not a decision a restart should make.

`bootstrap_retries=0` is passed deliberately, so a bootstrap failure surfaces to
StockBrain's own supervisor (which backs off visibly and records health) rather
than being retried invisibly inside the library.

### Live verification — 2026-09-05

Two read-only calls against the configured bot, no message sent, nothing
written to the database. Reproduce with
`pytest -m live -s tests/integration/test_phase7_live.py`.

| Observation | Result |
|---|---|
| `getMe` authenticated | yes |
| `is_bot` | `True` |
| `can_join_groups` | `True` |
| `can_read_all_group_messages` (privacy mode) | `False` |
| `supports_inline_queries` | `False` |
| `getWebhookInfo().url` | empty — no webhook configured, so `getUpdates` works |
| `pending_update_count` | 0 |
| Allowlisted destinations | **0** |

**The bot token is configured but the allowlist is empty**, so
`TELEGRAM_ALLOWED_USER_IDS` authorises nobody and the runtime would not start —
which is the designed refusal, not a fault. The message-sending half of the live
test skipped for exactly that reason, and additionally requires
`TELEGRAM_LIVE_SEND=yes`: it never discovers a destination.

`can_read_all_group_messages: False` is worth recording. With Telegram's default
privacy mode a bot in a group receives only commands addressed to it, which is
why group support is off by default and, when enabled, still requires the chat
id to be allowlisted.

### Polling lifecycle, measured — 2026-09-05

The full manual lifecycle was run once against the real API with a placeholder
allowlist, no message sent and no proposal touched:

```
initialize() -> getMe -> updater.start_polling() -> start()
polling              : True
provider health      : HEALTHY          (from UNKNOWN, in one transition)
transport            : long_polling
webhook configured   : False
bot token in status  : absent
updater.stop() -> stop() -> shutdown()
polling after stop   : False
```

**A finding that only a live run could produce:** the first version of this
runtime reported the bot's numeric id in its health payload and in its
`telegram_started` log line. A Telegram bot id is the part of the token *before*
the colon, so that published half the credential every time the process started.
The unit test asserting "no secret in the status payload" passed only because a
runtime that never launched has no id to report. The runtime now keeps a boolean
(`bot_identified`), the log line carries no id, and the test sets the flag before
asserting — so it now checks the case that actually occurs.

### Bug found by the Phase 7 tests

`uq_notifications_dedupe_key` is a **partial** unique index
(`WHERE dedupe_key IS NOT NULL`), and PostgreSQL will only infer a partial index
for `ON CONFLICT` when the predicate is restated in the statement. Without
`index_where=`, the notification insert failed with *"there is no unique or
exclusion constraint matching the ON CONFLICT specification"* — at the moment a
proposal needed to be announced. Any future `ON CONFLICT` against a partial
index in this codebase needs the same treatment.

---

## Phase 8 verified Trading 212 execution surface — 2026-09-05

Checked against the current published documentation before any request schema
was written, then measured against the **demo** environment with one real order.

* One Markdown document: <https://docs.trading212.com/api.md>
* Per-operation pages: `.../api/orders/placemarketorder.md`, `.../api/orders/orders.md`,
  `.../api/orders/orderbyid.md`, `.../api/historical-events/orders_1.md`
* Machine-readable bundle: <https://docs.trading212.com/_bundle/api.yaml>

### The market-order endpoint

```
POST /api/v0/equity/orders/market      operationId: placeMarketOrder
request : {"ticker": "AAPL_US_EQ", "quantity": 0.1, "extendedHours": false}
rate    : 50 req / 1m0s
```

`MarketRequest` declares **no required fields** and defaults `extendedHours` to
`false`. `quantity` is a JSON `number` and `Order.id` is `int64`.

Documented failure statuses, with the wording from the OpenAPI bundle — this is
the whole classification table:

| Status | Documented meaning | StockBrain's reading |
|---|---|---|
| 200 | OK | `CONFIRMED_SUCCESS` |
| 400 | **Failed validation** | definite rejection: no order exists |
| 401 | **Bad API key** | definite rejection |
| 403 | **Scope( orders:execute ) missing for API key** | definite rejection |
| 408 | **Timed-out** | **ambiguous** — the server timed out, before or after creating the order is unknown |
| 429 | Limited: 50 / 1m0s | **ambiguous** — the docs do not say whether the limiter runs before or after acceptance |
| *anything else, 5xx included* | not documented | **ambiguous** by default |

Two of these are new information and both are load-bearing:

* **HTTP 403 means the API key lacks an explicit `orders:execute` scope.** A
  read-only key authenticates perfectly against every GET and then refuses every
  order. It is a configuration fault, not a market one, and it is recorded as
  `BROKER_AUTH_REJECTED` rather than as a transient failure.
* **No 5xx is documented for this endpoint at all**, so an unknown status cannot
  be mapped onto a known meaning. StockBrain treats every undocumented status as
  ambiguous, which is the only direction that cannot lose an order.

**Non-idempotency, verbatim:** *"In this beta version, this endpoint is not
idempotent. Sending the same request multiple times may result in duplicate
orders."* There is **no idempotency key, no client-supplied reference and no
request field of any kind** that would let the broker collapse a duplicate. The
request fingerprint StockBrain computes is therefore an audit hash and nothing
more; a test asserts the module says so.

### The read-only endpoints reconciliation uses

| Endpoint | Rate limit | Notes |
|---|---|---|
| `GET /equity/orders` | **1 req / 5s** | pending (active) orders only; returns a bare array |
| `GET /equity/orders/{id}` | **1 req / 1s** | one pending order; 404s once it fills or cancels |
| `GET /equity/history/orders` | **6 req / 1m0s** | cursor paginated, `items[]` + `nextPagePath`, optional **`ticker` filter**, `limit` max 50 |

The spec documented none of these limits and none of the pagination. Two
consequences:

* **The history endpoint takes a `ticker` filter**, so reconciliation asks about
  one instrument rather than paging the account's whole history.
* **Its rows are wrapped**: each item is `{fill: {...}, order: {...}}`, not a
  bare order, so a client written from the pending-order shape reads nothing.

### `initiatedFrom` is the strongest matching evidence this API offers

The order object carries `initiatedFrom`, enumerated
`API | IOS | ANDROID | WEB | SYSTEM | AUTOINVEST | INSTRUMENT_AUTOINVEST`.
An order StockBrain placed reads `API`; an order the operator placed on their
phone does not. Absent a client reference, this is what stops a manual trade in
the Trading 212 app being attributed to an ambiguous StockBrain attempt — and it
is why reconciliation can ever conclude "no order was placed" at all.

The response also nests an `instrument` object (`{ticker, name, isin, currency}`)
and returns `side` (`BUY`/`SELL`) explicitly, so the sign convention can be
verified as round-tripping rather than assumed.

### Rate limiting is per *account*

*"All rate limits are applied on a per-account basis, regardless of which API
key is used or which IP address the request originates from."* A client-side
token bucket is therefore a courtesy rather than a guarantee: another client of
the same account consumes the same window. StockBrain's order bucket is set to
**49/minute against the documented 50** for that reason, and the limiter is
consulted *before* the transaction that records the send, so a denial stays
provably pre-send.

The limiter is a burst window, not a spacing rule: *"you could make a burst of
all 50 requests in the first 5 seconds"*. Headers are `x-ratelimit-limit`,
`-period` (**duration in seconds**), `-remaining`, `-reset` (**a Unix
timestamp**) and `-used`.

Also confirmed unchanged: base URLs `https://demo.trading212.com/api/v0` and
`https://live.trading212.com/api/v0`; HTTP Basic with the API key as username
and the secret as password; **negative quantity sells**, called "a core
convention of the API"; orders execute only in the primary account currency;
Invest and Stocks ISA only; the API is beta; **50 pending orders per ticker per
account** as a functional limit (still unenforced anywhere — Phase 9 should).

### Live demo verification — 2026-09-05

**The Trading 212 credentials now authenticate against demo.** The Phase 4–7
handoffs recorded a live-only key; a Practice/Demo key is evidently configured
now, and every finding below is from `demo.trading212.com`. **No real-money
order was placed at any point.** Reproduce with
`pytest -m live -s tests/integration/test_phase8_live.py`.

Read-only first, two GETs:

```
GET /equity/orders          authenticated, 0 pending
GET /equity/history/orders  authenticated, 0 items
rate-limit headers          all five, on both endpoints
demo account               GBP, funded, 0 open positions
```

Then **one** market order, behind two further explicit switches
(`T212_DEMO_ORDER=yes` and `T212_DEMO_ORDER_TICKER`, because the test never picks
an instrument), for 1 share of `AAPL_US_EQ`:

| Observation | Result |
|---|---|
| HTTP status | **200** |
| `id` returned | yes |
| `ticker` echoed unchanged | yes |
| `side` | `BUY` (derived by the broker from the positive quantity) |
| `status` | **`NEW`** — the market was closed, so the order queued as documented |
| `type` | `MARKET` |
| `strategy` | `QUANTITY` |
| **`timeInForce`** | **absent** |
| `initiatedFrom` | **`API`** |
| `instrument` object present | yes |
| `quantity` arrived as `Decimal` | yes, positive |
| Rate-limit headers | all five |
| Readable back by `GET /equity/orders/{id}` | **yes** |

Two findings:

* **`timeInForce` is documented as a response field and was not returned** for a
  market order. Every optional field in `T212Order` is therefore genuinely
  optional; a client that required the documented set would have failed on the
  first real order it placed.
* **A closed market queues the order as `NEW` rather than refusing it.** That is
  the documented behaviour and it is the *better* case to have measured, because
  a queued order stays visible in `GET /equity/orders` — which is exactly the
  path reconciliation reads for a known order id, and it answered.

The order was **not** cancelled afterwards: StockBrain has no cancel path and
the test does not add one, so a small demo position exists on the paper account.

**The demo order was placed through the adapter directly, not through the
proposal pipeline**, and deliberately so: the account is GBP-denominated and
`AAPL_US_EQ` is USD, so the Phase 6 `currency_alignment` rule blocks the entire
priced universe on this account. A pipeline run would have refused before
reaching the broker and proved nothing about the adapter. The part only a live
call can verify — request shape, response shape, sign convention, rate-limit
headers, read-back — is what was verified.

### Bug found by the Phase 8 tests

The metadata naming convention (`ck_%(table_name)s_%(constraint_name)s`) is
applied by `op.drop_constraint` as well as by `create_check_constraint`, so
passing an already-rendered name in a downgrade prefixes it twice and then
truncates it to PostgreSQL's 63-character limit with a hash suffix — producing
`ck_execution_attempts_ck_execution_attempts_broker_outc_db79`, a name no
migration can drop. This is Phase 6's bug #13 wearing a different hat. Drop
constraints by their **bare** names.

---

# Phase 9 — cost control, foreign exchange, and the Firecrawl incident

## The Firecrawl credit incident, reconstructed

Evidence: `firecrawl_activity_logs.csv` (29 rows, exported from the Firecrawl
dashboard), the PostgreSQL `jobs` table, the `sources` table, and the Phase 2
implementation as committed in `b6dfdb7`.

### What the log shows

| Fact | Value |
|---|---|
| Calls in the log | **29**, all `kind=search`, all `api_version=v2`, all `origin=api` |
| Window | 2026-09-05 **04:59:16.574Z → 06:01:32.060Z** = 62.1 minutes |
| Observed rate | **28 calls/hour** over the window |
| Distinct queries | **9** — exactly the enabled query set |
| Follow-up scrape calls | **none** in the log; content came from `scrapeOptions` on the search |

Grouped by minute, the pattern is four independent per-topic timers plus one
full sweep at process start:

```
04:59:16-39   9 searches   every enabled query, on start-up
05:19:54      3            ai_infrastructure          (20 min later)
05:30:04-11   6            semiconductors/power_grid/defence (30 min)
05:40:27      3            ai_infrastructure          (20 min)
06:00:33-38   6            the three 30-minute topics
06:01:31-32   2            ai_infrastructure          (third cycle)
```

Corroborated exactly by the job history: `FIRECRAWL_TOPIC_SEARCH` shows
**29 SUCCEEDED** and **127 FAILED**, every failure
`ProviderEntitlementError: firecrawl: subscription does not cover this resource
(HTTP 402)`. The 29 successes are the 29 log rows; the 127 failures are
everything after the allowance ran out.

### Configured cadence, and whether topics ran independently

`DEFAULT_TOPICS` seeded four enabled topics with nine enabled queries:

| Topic | Interval | Queries | Searches/hour |
|---|---|---|---|
| `ai_infrastructure` | 20 min | 3 | 9 |
| `semiconductors` | 30 min | 2 | 4 |
| `power_grid` | 30 min | 2 | 4 |
| `defence` | 30 min | 2 | 4 |
| | | **9** | **21/hour → 504/day** |

Each *query* had its own `last_run_at`, so yes: they ran independently, and the
scheduler's one-minute tick enqueued whichever were due. The eligibility check
was `now - last_run_at < interval`, recomputed on every tick.

### Whether a restart could trigger searches early

**Yes, and this is half the incident.** `Scheduler._loop` calls `task.run()`
*immediately* on start, before its first sleep. Every enabled query whose
`last_run_at` was older than its interval — which, after any outage longer than
20 minutes, is all of them — was enqueued at once. That is the nine calls inside
23 seconds at 04:59:16.

### Whether duplicate jobs could queue, or two schedulers run

Duplicate jobs: **no.** `uq_jobs_dedupe_key_active` on `firecrawl:{query_id}`
allowed one outstanding job per query. Two scheduler instances: nothing
*prevented* it, but the dedupe key would have collapsed their enqueues, and the
observed timing is consistent with one loop. Not the cause.

### Search parameters, and the actual cost

| Parameter | Phase 2 value |
|---|---|
| `limit` | **10** — and it is documented as **per source** |
| `sources` | `[{"type":"web"},{"type":"news"}]` → **two** |
| Billed results per search | up to **20** |
| `scrapeOptions` | **supplied on every search** |

`DiscoveryQuerySpec.scrape_content` defaulted to `True`, and
`handle_firecrawl_topic_search` never overrode it. **This is the single largest
contributor.**

*Does `/v2/search` scrape automatically?* No. Verified against
<https://docs.firecrawl.dev/api-reference/endpoint/search>: without
`scrapeOptions` a result carries title, description/snippet, URL and date. The
scraping was requested, not implicit. StockBrain made **no** separate scrape
calls — consistent with the log containing only `search` rows.

Hard evidence that the scraping happened, from the database:

```
sources WHERE provider='FIRECRAWL':          143 rows
  ... with metadata.has_scraped_markdown:    117
  average length(raw_content):            33,367 bytes
  maximum length(raw_content):         1,130,025 bytes
```

A 1.1 MB body is not a search snippet.

### Billing

Verified 2026-09-05 against <https://docs.firecrawl.dev/billing> and
<https://www.firecrawl.dev/pricing>:

| Operation | Cost |
|---|---|
| `/v2/search` | **2 credits per 10 results**, rounded up per 10 (the docs' own example: 11 results = 4 credits) |
| `/v2/scrape` | **1 credit per page** |
| `scrapeOptions` on a search | the per-page charge, for **every** result |
| JSON extraction / prompt-injection check | +4 credits/page each (never requested) |
| Failed requests | **billed** — "Credits are charged whenever Firecrawl's infrastructure processes a request, even if the target site returns an HTTP error status code" |
| Free allowance | 1,000 credits/month |

So one Phase 2 search cost **≈24 credits**: 4 for a 20-result search plus up to
20 for the scraped pages.

| | Phase 2 | Phase 9 default |
|---|---|---|
| Searches/day | 504 | 10 (capped at 12) |
| Credits/search | ~24 | **2** |
| Scrapes/day | 0 separate (20/search) | ≤6, one credit each |
| **Credits/day** | **~12,000** | **≤30** |
| Credits/month | ~363,000 | **≤900** |

The CSV's 29 searches therefore represent roughly **550–700 credits in 62
minutes** — the whole free allowance, and then HTTP 402.

### Retry behaviour, and whether it multiplied the spend

Two independent retry layers stacked:

* `FirecrawlClient.search` passed `retry_safe=True, max_attempts=3` — up to
  three HTTP calls per handler invocation, on 408/425/429/5xx.
* The job used the queue's default `max_attempts=3`.

Up to **nine billable requests per scheduled search** on a transient failure.
The 402s themselves are an entitlement rejection and are unlikely to have been
processed (and so unlikely to have been billed), but the amplification factor
was real and is now removed: no HTTP retry, `max_attempts=1` on the job, and a
durable failure cooldown instead.

### Root cause

Three faults, in order of contribution:

1. **`scrape_content` defaulted to `True` and nothing overrode it**, so every
   broad thematic search fetched every result page. A 20× cost multiplier on
   the most frequent paid call in the system.
2. **The cadence treated Firecrawl as a fast-news path.** 20- and 30-minute
   intervals across nine queries is 504 searches/day for a provider whose job
   is thematic drift — work Alpaca news and SEC EDGAR already do, unmetered.
3. **There was no ceiling of any kind.** Credit accounting was
   `self.last_credits_used` on the client object plus a Prometheus counter.
   Neither survives a restart and neither can refuse a call, so nothing in the
   system was capable of stopping it. Compounding faults: the start-up sweep
   re-ran everything on every restart, and two retry layers multiplied each
   failure by up to nine.

**No live Firecrawl call was made during this investigation.** The
reconstruction is entirely from the CSV, the job history, the `sources` table
and the committed Phase 2 code.

## Firecrawl `/v2/scrape`

Verified 2026-09-05 against
<https://docs.firecrawl.dev/api-reference/endpoint/scrape>:

| Detail | Finding |
|---|---|
| Path | `POST /v2/scrape` |
| Response | `{"success", "data": {"markdown", "metadata": {...}}}` |
| `creditsUsed` | **not returned** — unlike `/v2/search`. The reservation stands as the charge. |
| `metadata.title` / `.description` | documented as `string` **or** `string[]`; collapsed to the first string rather than stringified, so a two-element array does not become `"['a', 'b']"` in a headline |
| `maxAge` | defaults to 2 days; a cache hit may cost less, which is not documented as a discount and is therefore not assumed |
| Documented errors | 402, 429, 500 |

## Alpaca forex — measured entitlement

Sources: <https://docs.alpaca.markets/reference/latestrates-1>

| Detail | Finding |
|---|---|
| Path | `GET https://data.alpaca.markets/v1beta1/forex/latest/rates` |
| Parameter | `currency_pairs`, comma-separated market-convention pairs (`GBPUSD`) |
| Auth | the same `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` as the equity endpoints |
| Response | `{"rates": {"GBPUSD": {"bp", "mp", "ap", "t"}}}` — bid, mid, ask, and an RFC 3339 instant with **nanosecond** precision |
| **Entitlement, measured live 2026-09-05** | **HTTP 403 `{"message":"forbidden: insufficient grants"}`** |

The credential that reads IEX equity data does **not** cover forex. This is a
fact about the subscription, not about the key — which is exactly the
distinction Phase 4's finding about Alpaca's ambiguous 403 was about, and the
same `refine_error` hook tells them apart. `datetime.fromisoformat` accepts at
most microseconds, so the nanosecond fraction is truncated rather than rejected:
losing nanoseconds from a check measured in seconds is free, losing the rate is
not.

**Consequence:** on the current plan, cross-currency sizing needs another
source, and an execution-grade FX feed for this account requires a paid Alpaca
subscription (or an equivalent provider). This is a real remaining blocker, not
a code gap.

## Frankfurter — the reference-grade fallback

Sources: <https://frankfurter.dev/>, and a live read on 2026-09-05.

| Detail | Finding |
|---|---|
| Path | `GET https://api.frankfurter.dev/v2/rates?base=GBP&quotes=USD` |
| Auth | **none**. "No API key required." |
| Quota | "There are no quotas. Requests are rate-limited to prevent abuse, but there are no monthly or daily caps." |
| Response | a JSON **array**: `[{"date":"2026-09-05","base":"GBP","quote":"USD","rate":1.3521}]` |
| v1 vs v2 | v1 returns `{"amount","base","date","rates":{...}}`. A client written against v1 finds no `rates` key on v2 at all. |
| Data | "daily exchange rates from 84 central banks", ECB included; 201 currencies |
| Suitability | its own documentation states it **"is not for live trading"** |

Two consequences encoded in the adapter:

* **Grade.** `FxRateGrade.REFERENCE`, and a reference-grade rate may size a
  trade only with `FX_ALLOW_REFERENCE_GRADE=true`. Honouring the provider's own
  statement is the difference between using a fixing knowingly and mistaking it
  for a dealable quote.
* **Timestamp precision.** The response carries a *date* and no time, so the
  provider timestamp is taken as **00:00 UTC on that date**. That over-states
  the age, which can only make a freshness check stricter. Central banks do not
  publish at weekends, so a Monday-morning rate is dated the previous Friday and
  cross-currency sizing blocks until the next fixing — when equity markets are
  closed anyway.

## Bugs found by the Phase 9 work

### 22. `RISK_REQUIRE_SAME_CURRENCY=false` silently mis-sized

Phase 6's `currency_alignment` returned **`WARN`** when the operator permitted a
currency mismatch, and nothing else blocked. Sizing then divided an
account-currency ceiling by an instrument-currency price:
`max_notional / reference_price` with a GBP numerator and a USD denominator. The
quantity that came out was wrong by the exchange rate — about 35% too small on
GBP/USD, and in the unsafe direction for the inverse pair — with only a warning
nobody had to act on.

The rule is now split in two: `currency_alignment` is the *policy* ("may these
differ?") and `fx_available` / `fx_freshness` are the *capability* ("is there a
verified rate?"). There is no configuration in which a mismatch is permitted and
unpriced, and the pairing of `RISK_REQUIRE_SAME_CURRENCY=false` with
`FX_PROVIDER=none` is refused at startup.

### 23. A stale quote destroyed authorized proposals

`price_source_execution_grade` reported `quote.provider_blockers`, and
`quote_blockers` includes the *age*. So a quote one minute stale blocked a rule
that Phase 8's send-time preflight classifies as "a statement about the trade" —
non-transient — and the authorized proposal was **invalidated**. A market-data
provider running slightly behind would have retired every authorized proposal it
touched.

`provider_grade_blockers` now returns entitlement and provenance only; age
belongs to `quote_freshness` and width to `spread_ceiling`, each with its own
numbers. This is Phase 4's bug #9 (the capability probe conflating entitlement
with freshness) and Phase 8's bug #21 (`_TRANSIENT_RULE_IDS` too naive) at a
third layer.

### 24. `authorization_envelope` reported a side change on a blocked decision

A blocked decision deliberately carries no size — `RiskEngine.evaluate` does not
call `size_trade` at all — so `sizing.side` is `None`, and the envelope check
reported "the deterministic side changed since generation (BUY → none)". That
reads as a statement about the trade and retires the proposal, and *any* block
produced it: a stale quote, a provider outage, a missing account snapshot.

It now records `WARN` on a blocked decision: a rule that could not run. The
rules that actually blocked are in the list, and their own transient/permanent
classification is the verdict.

### 25. `broker_max_open_quantity` was denominated in the wrong currency

Every other notional cap is computed from the account snapshot and is in the
account currency. This one is `remaining_quantity × mid` — an
*instrument*-currency number — sitting in a list whose minimum is taken. Phase 6
could ignore it because the two currencies were required to match; with FX in
play it is 35% too small on a GBP/USD pair and silently becomes the binding cap.
It is now converted, and omitted entirely rather than guessed when no usable
rate exists.

---

# Provider split — Brave, Exa, and Firecrawl as a fallback extractor

**Verified 2026-09-05.** Firecrawl stops being StockBrain's primary discovery
provider. Brave answers routine thematic search, Exa answers semantic
second-order search, and Firecrawl keeps one job: a paid page fetch when free
local extraction cannot read a page.

Everything in this section was read from each vendor's current documentation on
that date. Where a fact contradicts something StockBrain previously assumed, the
contradiction is stated rather than quietly corrected.

## Brave Search API

<https://api-dashboard.search.brave.com/api-reference/web/search/get>,
`/documentation/guides/rate-limiting`, <https://brave.com/search/api/>

| Fact | Value |
|---|---|
| Endpoint | `GET https://api.search.brave.com/res/v1/web/search` |
| Auth | header **`X-Subscription-Token`** — *not* a bearer token |
| `q` | max **400 characters and 50 words** |
| `count` | 1–20, default 20, **web results only** |
| `offset` | 0–9 |
| `freshness` | `pd` / `pw` / `pm` / `py`, or `YYYY-MM-DDtoYYYY-MM-DD` |
| `result_filter` | comma list of `discussions`, `faq`, `infobox`, `news`, `query`, `summarizer`, `videos`, `web`, `locations` |
| `country` | 2-character code, default `US` |
| `safesearch` | `off` / `moderate` (default) / `strict` |
| Response | `{"type", "query", "web": {"results": [...]}, "news": {"results": [...]}, ...}` |
| Web result | `title`, `url`, `description`, `page_age`, `age`, `meta_url.hostname`, `profile.name`, `extra_snippets` |
| News result | the same, plus `breaking` |
| Rate limit | **429**; every response carries `X-RateLimit-Limit`, `X-RateLimit-Policy` (e.g. `1;w=1, 15000;w=2592000`), `X-RateLimit-Remaining`, `X-RateLimit-Reset` (seconds from now) |

Three of these are load-bearing and each changes the design:

* **One request returns both clusters.** `result_filter=web,news` gives the web
  and news results together, so the separate `/res/v1/news/search` endpoint —
  which would be a *second billable request* — is deliberately not used.
* **`count` does not multiply the price.** This is the opposite of Firecrawl's
  `limit`, which is per *source* and turned `limit=10` with two sources into
  twenty billed results. On Brave, one request is one billable request however
  many results come back. `count` is a relevance knob, not a cost knob.
* **"Only successful requests (non-error responses) are counted against your
  quota and billed."** Also the opposite of Firecrawl. This is why a Brave GET
  is allowed the shared client's bounded retry, and why the ledger *refunds* a
  classified Brave provider error — but not a transport failure, because a
  timeout is exactly the case where nobody knows whether the far side served it.

### Pricing, and a discrepancy worth recording

<https://brave.com/search/api/> states: Search plan **$5 per 1,000 requests**,
**$5 in free monthly credits (automatically applied)**, rate limit **50 queries
per second**, and a credit card required "for identity verification" on free
plans which "will not be charged".

**Discrepancy.** Third-party reporting (e.g. implicator.ai, February 2026) says
Brave *removed* its standing free tier — previously 2,000 queries/month at 1
query/second, raised to 5,000 in August 2025 — and replaced it with the credit
model above, and that the $5 monthly credit is retained **only if you publicly
attribute Brave**. Brave's own pricing page does not mention an attribution
condition. StockBrain does not rely on the free credit being unconditional:

* the caps are set at **320 requests/month** (~$1.60 at the published rate),
  which is affordable even if the credit turns out not to apply;
* the rate limiter is pinned at **1 request/second**, the documented entry-plan
  burst ceiling, rather than at the 50 QPS the paid plan advertises.

An operator who wants the credit should check Brave's current terms for an
attribution requirement. That is a business decision, not a code one.

## Exa

<https://exa.ai/docs/reference/search>, `/reference/pricing`,
`/reference/rate-limits`, `/reference/quickstart`

| Fact | Value |
|---|---|
| Endpoint | `POST https://api.exa.ai/search` |
| Auth | **`Authorization: Bearer <key>`** with `Content-Type: application/json` |
| `type` | `instant` / `fast` / `auto` (default) / `deep-lite` / `deep` / `deep-reasoning` |
| `numResults` | 1–100, default 10 |
| `category` | `company`, `publication`, `news`, `personal site`, `financial report`, `people` |
| Date filters | `startPublishedDate` / `endPublishedDate`, ISO-8601 |
| Contents | **must nest under `contents`**; a top-level `text` is a 400 |
| Response | `{"requestId", "results": [...], "costDollars": {...}, "searchTime"}` |
| Result | `id`, `title`, `url`, `publishedDate`, `author`, `image`, `favicon`, and `text` / `highlights` / `summary` when requested |
| Rate limit | `/search` **10 QPS**, `/contents` 100 QPS, `/answer` 10 QPS |

`publishedDate` is documented as "an estimate of the creation date, from parsing
HTML content" — a claim about the page rather than a fact from a publisher. It
is carried through and never treated as authoritative.

`costDollars` is **the provider's own price for the call**, which no other
provider here reports. It is recorded on the ledger row, and the budget charges
the larger of it and StockBrain's estimate. Exa documents it as an estimate
rather than an invoice, which is why it supplements the estimate rather than
replacing it.

### Pricing

| Item | Price |
|---|---|
| `/search` | **$7 / 1,000 requests**, up to 10 results |
| Each result above 10 | $1 / 1,000 results |
| `/contents` | $1 / 1,000 pages, **per content type** |
| AI page summaries | $1 / 1,000 pages |
| `deep-lite` / `deep` | $12 / 1,000 |
| `deep-reasoning` | $15 / 1,000 |
| Free credit | **$20 on signup**, plus **$10/month** on the free tier |

Two consequences:

* **`contents` is Exa's `scrapeOptions`.** It is the field that quietly turns a
  metadata call into a per-result page fetch. `EXA_FETCH_CONTENTS` exists so the
  decision is visible and **off**, and the scheduler never sets it. Extraction
  happens after triage, locally, and is free.
* **`numResults` stays at 10.** Eleven results is the base price plus an overage
  line, for a semantic query whose value is in its first few hits.

## Firecrawl — what is still used

<https://docs.firecrawl.dev/api-reference/endpoint/scrape>, `/billing`.
Re-verified 2026-09-05; unchanged from Phase 9.

* `POST https://api.firecrawl.dev/v2/scrape`, `Authorization: Bearer`
* body `{"url", "formats", "onlyMainContent", "timeout"}`; `timeout` is
  milliseconds, 1000–300000
* response `{"success", "data": {"markdown", "metadata": {"title",
  "description", "url", "sourceURL", "statusCode", "contentType", "language"}}}`
  — and it carries **no `creditsUsed`**, so the reservation stands as the charge
* `title` and `description` are documented as `string | string[]` on this
  endpoint (unlike on search); both are collapsed to the first value
* **1 credit per page.** `json` extraction is +4, a prompt-injection check is +4,
  zero-data-retention is +1, PDF parsing is +1/page. None is requested.
* **"Credits are charged whenever Firecrawl's infrastructure processes a
  request, even if the target site returns an HTTP error status code."** A retry
  is not a free second chance; it is a second paid call.

`/v2/search` — 2 credits per 10 results, `limit` per *source* — is **removed from
the codebase**, not disabled behind a flag. The Phase 9 analysis of that endpoint
is preserved above; a paid primary-discovery path left in the tree is a paid path
that gets scheduled again.

## Local extraction — trafilatura

`trafilatura` **2.2.0**, released **2026-07-31**, `requires-python >=3.10`,
actively maintained (PyPI, verified 2026-09-05). Dependencies: `certifi`,
`charset_normalizer>=3.4.9`, `courlan>=1.4.0`, `htmldate>=1.10.0`,
`justext>=3.0.2`, `lxml>=6.1.1`, `urllib3`.

Chosen over `readability-lxml` and a hand-rolled BeautifulSoup pass because it is
the one option that is both maintained and evaluated against a public benchmark,
and because `lxml` (6.1.3) and `charset-normalizer` (3.5.1) are already pinned in
this image via yfinance — the marginal additions are four small pure-Python
packages.

**Its network stack is deliberately not used.** `trafilatura.fetch_url` has its
own urllib3 downloader with its own redirect handling and no notion of SSRF. Only
`bare_extraction` is called, on bytes StockBrain fetched itself through
`stockbrain.extraction.ssrf`. A test asserts the module contains no call to the
downloader.

One API note found by using it: `max_tree_size` is **deprecated in 2.x and
raises** if passed as an argument. The bound is set through `settings.cfg`
instead.

## Measured behaviour — 2026-09-05

* **`https://www.sec.gov/` answers HTTP 403** to the generic extractor's honest
  `User-Agent`. SEC EDGAR requires a contact address inside the User-Agent and
  refuses anything else. This is exactly the `HTTP_ERROR` case the paid fallback
  exists for, and it is asserted as a live test. SEC *filings* are unaffected:
  they come from the SEC client, which sends the required header.
* **`https://en.wikipedia.org/wiki/Electric_power_transmission`** extracted
  cleanly: 570,486 bytes read, 63,086 characters of article text, title and
  canonical URL both recovered, no script content in the output.

## Cost model at the shipped defaults

Computed from the verified prices above and the seeded schedule. Asserted in
`tests/unit/test_provider_budget.py` and
`tests/integration/test_web_discovery_scheduler.py`, so changing a default fails
a test rather than a bill.

| | Per day | Per month | At the cap |
|---|---|---|---|
| Brave routine searches (seeded) | 10 | ~300 | — |
| Brave cap | 12 | **320** | **$1.60** |
| Exa semantic searches (seeded) | 0 — the topic ships **disabled** | 0 | — |
| Exa cap | 3 | **70** | **$0.49** |
| Firecrawl fallback scrapes | 0 — ships **off** | 0 | — |
| Firecrawl cap | 5 scrapes / 10 credits | 200 credits | — |
| Local extraction | ≤60 pages | ~1,800 | **$0.00** |

**Ceiling if every cap were reached every day: $2.09/month.** Free allowances
available: $5/month (Brave) + $10/month (Exa) = **$15/month**, plus $20 of Exa
signup credit. Expected steady-state spend at the shipped defaults, with the
semantic topic disabled and Firecrawl off: **$0.00**.

Firecrawl contributes no dollar figure because it bills credits against a monthly
allowance rather than dollars per call; its 200-credit monthly cap sits inside a
1,000-credit allowance. Reporting an invented per-call dollar figure for it would
put a number nobody can check in the one table that exists to be believed.
