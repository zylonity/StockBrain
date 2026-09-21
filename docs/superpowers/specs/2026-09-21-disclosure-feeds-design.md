# Disclosure Feeds (non-US news) — Design

**Date:** 2026-09-21
**Status:** approved for planning
**Branch:** `feat/disclosure-feeds`
**Predecessors:** `docs/superpowers/specs/2026-09-16-thesis-memory-design.md`
(staged-flag rollout pattern), `backend/alembic/versions/20260905_2400_web_discovery_providers.py`
(enum-extension pattern)

## 1. Problem

Every push-style news feed StockBrain has is US-tagged. `AlpacaNewsClient` is
Benzinga's wire; SEC EDGAR is the only filings feed; Brave and Exa are
query-driven topic searches capped at a handful of calls a day. Trading 212
lists 17,920 instruments, of which ~5,000 stocks trade on London (1,460),
Xetra/Gettex (1,690), Toronto (541), Euronext (617), Madrid (165), SIX (132)
and Vienna (99). Not one of those venues has a feed. A non-US company enters
the pipeline only when a Brave/Exa topic query happens to surface it (34 have,
out of 1,425 resolved companies), and once held it is deaf to every subsequent
story about it.

The user has verified FX sizing (Frankfurter reference-grade, opt-in) and wants
to trade non-US listings. Measured on 2026-09-21: 21 open positions, all
`_US_EQ`; 91 proposals ever, all `_US_EQ`. The gap is real and currently total.

## 2. What was measured (2026-09-21)

Paid providers were ruled out by the user. Of the free options probed:

| Source | Verdict | Evidence |
|---|---|---|
| Finnhub `company-news` | US-only on free tier | `SHEL.L`, `SAP.DE` → `"You don't have access to this resource."` |
| Marketaux free | Monitoring only; not a feed | 3 articles/request, `countries=` means *exchange* country (Toyota tagged `TYT.L`), LSE entity index misses `SHEL` (still `RDSA/RDSB`, retired 2022) |
| LSEG RNS Data Feed | Licensed product, no self-serve | — |
| **Investegate front page** | ✅ all-market RNS list, ticker per row, numeric announcement ID, `?page=N`, full text on announcement page, robots-permitted | fixture `investegate_front_page.html` |
| **EQS News homepage** | ✅ 30-item realtime list, `data-news-item` UUID, `data-news-isin`, `data-news-languages`, `data-news-category`; RSS URLs redirect to homepage | fixture `eqs_home.html` |
| **CNMV RSS** | ✅ two official feeds: *Información privilegiada* (inside information) and *Otra información relevante*; `nreg=` numeric ID | fixtures `cnmv_ip_empty.xml`, `cnmv_oir.xml` |
| **GlobeNewswire country RSS** | ✅ FR/NL/BE/PT/ES/CA, `dc:identifier` release number shared across language variants, `dc:language` | fixtures `globenewswire_{france,netherlands,canada}.xml` |
| **ActusNews EN RSS** | ✅ French issuers, English feed, `COMPANY : headline` title convention | fixture `actusnews_en.xml` |
| Cision / newswire.ca | ✗ every feed now serves PR Newswire's global list | 0/20 TSX-tagged |
| live.euronext.com, AMF BDIF, SEDAR+, SIX, Wiener Börse | ✗ bot-challenged or JS-only | — |

Fixtures live in `backend/tests/fixtures/disclosure_feeds/` and are the
contract every parser is built and tested against.

## 3. Goals and non-goals

**Goals**

1. Regulatory and issuer disclosures for LSE/AIM, Xetra/Gettex/Wien/SIX,
   Madrid, Euronext (FR/NL/BE/PT) and Toronto arrive as
   `RawSourceDocument`s on the ordinary ingestion path, keyless.
2. A release published in several languages becomes **one** event.
3. Provider-known identity (ticker + exchange, or ISIN) reaches instrument
   resolution without depending on the classifier to guess it.
4. Every feed is individually switchable, defaults off, and degrades loudly
   when the page shape changes.

**Non-goals**

- Marketaux integration (monitoring of known names; a later, separate piece).
- Discovery on venues with no free list (SIX beyond what EQS carries; Euronext
  beyond the newswires).
- Any change to the classifier, dedupe or research prompts. `PROMPT_VERSION`
  is unchanged so `research_runs.prompt_version` stays comparable.
- A cursor table. Idempotency comes from the existing unique index on
  `(provider, provider_item_id)`.

## 4. Architecture

```
scheduler ──(per feed)──▶ DISCLOSURE_FEED_POLL {feed}
                               │
                               ▼
                    handle_disclosure_feed_poll
                               │  fetch_page(1..N) until a known release_id
                               │  boilerplate filter
                               │  group by release_id, prefer 'en'
                               ▼
                    to_document(FeedItem) ─▶ IngestionService.ingest_many
                                                   │
                            existing: normalize → dedupe → classify → resolve
                                                   ▲
                            metadata.{isin, exchange_hint} backfilled onto
                            impacts whose ticker_hint == document symbol
```

New modules, all under `backend/stockbrain/ingestion/`, flat like the
existing `alpaca_news.py` / `sec_edgar.py` / `brave.py`:

- `disclosure_feeds.py` — `FeedItem`, `DisclosureFeed` protocol, `to_document`,
  `BoilerplateRule`, `group_releases`, the shared `httpx` fetch helper.
- `investegate.py` — HTML list parser (lxml), announcement-page fetch is
  **not** here (the existing local extractor handles bodies on demand).
- `eqs.py` — HTML list parser (lxml), reads the `data-news-*` attributes.
- `cnmv.py` — RSS parser (lxml.etree); two feed URLs, one adapter.
- `globenewswire.py` — RSS parser; one adapter instance per country.
- `actusnews.py` — RSS parser.

`lxml` is already a transitive pin (trafilatura, yfinance). No new dependency.

## 5. Data model

### 5.1 `FeedItem` (in-memory DTO)

| field | type | notes |
|---|---|---|
| `provider` | `SourceProvider` | per-wire, never a generic value (provenance rule, `enums.py` FIRECRAWL comment) |
| `release_id` | `str` | language-agnostic: Investegate announcement number, EQS `data-news-item`, CNMV `nreg`, GlobeNewswire `dc:identifier`, ActusNews document path |
| `language` | `str` | ISO 639-1; from `dc:language`, EQS `_xx` URL suffix, or the feed's fixed language |
| `url` | `str` | the language variant chosen |
| `headline` | `str` | |
| `published_at` | `datetime` (UTC) | |
| `company_name` | `str \| None` | as printed by the feed |
| `ticker` | `str \| None` | Investegate ticker; `(TSX: X)`-style regex for GlobeNewswire; else `None` |
| `isin` | `str \| None` | EQS only today |
| `exchange_hint` | `str \| None` | provider-fixed: `"London Stock Exchange"` / `"London Stock Exchange AIM"` for Investegate (the page does not distinguish; both are passed as alternates, see §7), `"Bolsa de Madrid"` for CNMV, `None` for EQS (ISIN suffices) and newswires (country only) |
| `category` | `str \| None` | feed's own label: EQS `data-news-category`, CNMV feed name, Investegate source code (`RNS`, `GNW`, `PRN`…) |
| `alternate_language_urls` | `dict[str, str]` | filled by `group_releases` |
| `raw` | `dict` | the row/item as parsed, for `raw_payload` |

### 5.2 `RawSourceDocument` mapping (`to_document`)

- `provider_item_id = release_id` — this is what makes translations and
  re-polls collapse: `sources` already has a unique partial index on
  `(provider, provider_item_id)`.
- `symbols = [ticker]` when present.
- `source_category`: `REGULATOR` for Investegate rows whose source code is
  `RNS`, all CNMV items, EQS categories in the regulatory set
  (`voting-rights`, `directors-dealings`, `other-capital-market-information`,
  `uk-regulatory`, ad-hoc); `ISSUER` for EQS `corporate`, GlobeNewswire,
  ActusNews, and Investegate non-RNS source codes.
- `is_distinct_event = (source_category is REGULATOR)`. Mirrors SEC. Newswire
  items stay eligible for the headline and semantic dedupe layers so the same
  release on two wires can merge.
- `metadata = {"language", "isin", "exchange_hint", "exchange_hints",
  "alternate_language_urls", "feed_category"}`.
- `body = None`. Bodies are fetched by the existing `CONTENT_EXTRACT` path
  when the classifier wants them; a poll never fetches article pages.

### 5.3 Enum additions (one migration)

`SourceProvider` and `ProviderName` each gain
`INVESTEGATE, EQS, CNMV, GLOBENEWSWIRE, ACTUSNEWS`.
`JobType` gains `DISCLOSURE_FEED_POLL`.
Migration: `ALTER TYPE source_provider ADD VALUE IF NOT EXISTS …` ×5, and the
equivalent for whichever other PostgreSQL enums carry `ProviderName` /
`JobType` values (the Brave/Exa migration is the template; the implementer
checks which enums are database-backed rather than assuming).

## 6. Polling

### 6.1 Walk-until-known, no cursor

```
seen_ids = set()
for page in 1..max_pages:
    items = await feed.fetch_page(page)
    if not items: raise ProviderResponseError("page shape changed")   # on HTTP 200
    known = await ingestion.known_release_ids(provider, [i.release_id for i in items])
    new = [i for i in items if i.release_id not in known]
    collect(new)
    if len(new) < len(items): break          # reached what we already have
```

RSS feeds have one page; `max_pages` is 1 for them. Investegate and EQS default
to 3 and 1 respectively (EQS's list has no pagination without JS).

### 6.2 Boilerplate filter

Table-driven, per provider, matched against the headline (and EQS category)
**before** ingestion. Filtered items are counted in the log line and never
stored. Initial rules, from the fixtures:

- Investegate: `Transaction in Own Shares`, `Holding(s) in Company`,
  `Director/PDMR Shareholding`, `Form 8`, `Form 38.5*`, `Total Voting Rights`,
  `Exercise of Warrants`, `Block Listing*`, `Investor Presentation via Investor
  Meet Company`.
- EQS: categories `voting-rights`, `directors-dealings`; headlines
  `Release of a capital market information` (these are Art. 5 buyback
  notices), `Transaction in Own Shares`.
- CNMV OIR: `Programas de recompra de acciones`, `Contratos de liquidez`,
  `Sobre suspensiones` for warrant issuers (`SOCIETE GENERALE EFFEKTEN`,
  `BNP PARIBAS ISSUANCE`, `CITIGROUP GLOBAL MARKETS`, …). The IP feed is never
  filtered.
- GlobeNewswire: `Déclaration des opérations de rachat`, `Disclosure of trading
  in own shares`, `Déclaration hebdomadaire des transactions`, `Number of
  outstanding shares and voting rights`, `Nombre d'actions et de droits de
  vote`.

Rules are data (`tuple[re.Pattern, str]`) so they can grow without code
review of logic.

### 6.3 Language grouping (`group_releases`)

Within one poll batch: group by `(provider, release_id)`; pick the `en`
variant, else the feed's native language, else the first; the rest go into
`alternate_language_urls`. Across polls a later-arriving translation has the
same `provider_item_id` and is rejected by the unique index — **and**
`dedupe.find_existing` gains a `(provider, provider_item_id)` lookup before
the URL and content-hash lookups so this is an explicit `DUPLICATE` outcome,
not an integrity error.

ActusNews: only the `/en/rss` feed is subscribed. CNMV: Spanish only, no
variants.

### 6.4 Scheduling

One `ScheduledTask` per enabled feed, enqueueing `DISCLOSURE_FEED_POLL` with
`payload={"feed": name}`, `dedupe_key=f"feed:{name}"`, priority 50, jitter as
the scheduler already applies. Intervals: Investegate 300 s, EQS 300 s, CNMV
600 s, GlobeNewswire 900 s, ActusNews 900 s. All gated by `discovery_enabled`
and the discovery pause, exactly like `_enqueue_sec_refresh`.

### 6.5 HTTP

One shared helper: `httpx.AsyncClient` from the existing `httpclient.py`
factory, `User-Agent: StockBrain/<version> (+<SEC_CONTACT_EMAIL>)` — the same
contact the EDGAR client sends, `Accept-Language` set to the feed's language,
timeout 20 s, **no retry** (the next scheduled poll is the retry; a metered or
courtesy endpoint must not be hammered on failure).

### 6.6 Failure posture

| condition | outcome |
|---|---|
| HTTP 200, parser yields 0 items | `ProviderResponseError`; health `DEGRADED` "page shape changed"; nothing ingested |
| HTTP 403/429 | `ProviderAuthError` / `ProviderUnavailable`; health `DOWN`; the whole poll stops (no page 2) |
| HTTP 5xx / timeout | `ProviderUnavailable`; health `DOWN` |
| one item fails `to_document` validation | logged, skipped, poll continues; counted as `malformed` |
| ingestion of one document raises | as `ingest_many` already handles |

Health is recorded per `ProviderName` so the operator view shows each feed.

## 7. Identity hand-off to resolution

The classifier returns `ticker_hint` / `exchange_hint` per impact and nothing
else identity-shaped. Provider facts must not depend on the model repeating
them. In `intelligence/service.py`, where `event_company_impacts` are built
from the classifier result:

- if the source document carried exactly one `symbols` entry, and an impact's
  normalised `ticker_hint` equals it, and the impact's `exchange_hint` is null,
  set `exchange_hint` from `metadata.exchange_hint`;
- if `metadata.isin` is present and the impact's `ticker_hint` equals the
  document symbol **or** the impact's `company_name` key equals the document's
  `company_name` key, set the impact's ISIN hint.

For Investegate the page does not say LSE vs AIM. `metadata.exchange_hints`
carries both; `ResolutionRequest.exchange_hint` receives the main-market value
and, if resolution returns `AMBIGUOUS` with candidates on both, the AIM value is
retried once. No resolver change: rung 1 (ISIN) and rung 3 (ticker + exchange
narrowing) already exist.

The classifier's metadata block (`classifier.py`, template variables — not the
prompt file) gains `exchange:` and `isin:` lines so the model sees them too.
The SYSTEM prompt text is untouched; `prompt_version` stays `v1`.

## 8. Language hardening

- `extraction/local.py`: `Accept-Language` becomes per-request, from the
  source's `metadata.language`, default `en`.
- `instruments/normalize.py` `_NAME_SUFFIXES` gains
  `gmbh, kgaa, sarl, sas, bv, srl, sl, sau, sca, scs, oy, aps` (`as`, `ab`,
  `oyj`, `asa` are already present).
- Research output language: already pinned to English by
  `get_language_instruction()` in the vendored TradingAgents — no change.
- Telegram / GUI: generated from structured output — no change.

## 9. Settings

```
DISCLOSURE_FEEDS_ENABLED=false            # master; nothing below runs without it
DISCLOSURE_FEED_USER_AGENT_CONTACT=       # defaults to SEC_CONTACT_EMAIL
DISCLOSURE_FEED_TIMEOUT_SECONDS=20

INVESTEGATE_ENABLED=false
INVESTEGATE_INTERVAL_SECONDS=300
INVESTEGATE_MAX_PAGES=3

EQS_ENABLED=false
EQS_INTERVAL_SECONDS=300

CNMV_ENABLED=false
CNMV_INTERVAL_SECONDS=600

GLOBENEWSWIRE_ENABLED=false
GLOBENEWSWIRE_COUNTRIES=France,Netherlands,Belgium,Portugal,Spain,Canada
GLOBENEWSWIRE_INTERVAL_SECONDS=900

ACTUSNEWS_ENABLED=false
ACTUSNEWS_INTERVAL_SECONDS=900
```

`.env.example` documents each with the venue it covers and the measured
volume. Staged rollout as with Thesis Memory: deploy with everything off,
enable Investegate first, watch `sources` and health for a day, then the rest.

Test-time recipe (documented in `.env.example` next to the master flag):
`TELEGRAM_ENABLED=false`, `EXECUTION_MODE=research_only`, `T212_ENV=demo`.

## 10. Testing

Unit (no network, fixtures only):

- One parser test module per adapter: row/item count, first and last item
  fields, `release_id` extraction, language, ticker/ISIN extraction, and a
  *mutated* fixture (table removed / items removed) → `ProviderResponseError`.
- `test_disclosure_feeds.py`: `to_document` mapping incl. `is_distinct_event`
  by category; boilerplate table against every fixture headline (asserting the
  expected count filtered, so a rule that over-matches is caught);
  `group_releases` on the GlobeNewswire France fixture — SPIE's EN+FR (release
  `3365169`) become one item with the FR URL in `alternate_language_urls`.
- `test_dedupe.py` addition: `(provider, provider_item_id)` hit returns
  `DUPLICATE` before URL/hash checks.
- Handler test with a fake `DisclosureFeed`: first run creates N sources,
  second run creates 0 and stops at page 1; 403 → health `DOWN` and no page 2.
- Identity backfill test in `test_intelligence_service`-style: null
  `exchange_hint` on a matching impact is filled; a non-matching impact is not.
- Settings/config test: every new setting parses, master flag gates all.
- Migration test in the `test_migration_phase*` style: enum values present
  after upgrade.

Integration (opt-in, network, skipped by default like
`test_web_discovery_live.py`): one live fetch per adapter asserting ≥1 item
and that `release_id` is non-empty — the early-warning for a restyle.

## 11. Known limitations (accepted)

- Investegate and EQS are HTML scrapes. They will break on a restyle; the
  failure is loud (§6.6) and the fixtures pin today's shape.
- EQS carries only issuers who pay EQS to distribute; DAX/MDAX coverage is
  good, small caps patchy. SIX and Vienna coverage is whatever EQS has.
- Euronext has no exchange-level list; coverage is what issuers push through
  GlobeNewswire/ActusNews. Business Wire is not included (unverified).
- GlobeNewswire's Spain feed is noisy (non-issuer PR); CNMV is the authority
  there and the Spain country feed is included only for issuer press releases
  CNMV would not carry.
- `(TSX: X)` regex tagging on Canadian releases is a hint, not identity; the
  resolver treats it as rung 3 evidence with no exchange narrowing beyond the
  country.
- Cross-wire duplicates (same release on GlobeNewswire and ActusNews, different
  languages) rely on the semantic layer, which costs one cheap-model call.
- Volumes: Investegate ~300–500 RNS/weekday (≈40% boilerplate), EQS ~30–60,
  CNMV IP ~10–30 and OIR ~50–100, GlobeNewswire per country 20–80, ActusNews
  ~10–20. After filtering, classifier load roughly doubles relative to Alpaca
  alone when everything is on; the LLM budget caps remain the backstop.

## 12. Must-not list

- Must not fetch an article/announcement page from a poll. Bodies are the
  extractor's job, on demand, budgeted.
- Must not retry a feed fetch inside the handler.
- Must not store a filtered (boilerplate) item.
- Must not assign a generic provider value; each wire is its own
  `SourceProvider`.
- Must not set `previous_thesis_id` or touch any thesis/proposal code path.
- Must not change any prompt file or `PROMPT_VERSION`.
- Must not enable any feed by default.
