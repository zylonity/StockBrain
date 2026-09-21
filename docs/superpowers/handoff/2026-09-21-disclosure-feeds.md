# Handoff: disclosure feeds (non-US news) — 2026-09-21

**Branch:** `feat/disclosure-feeds` (off `main`, one commit `b3b247d`)
**Spec (approved):** `docs/superpowers/specs/2026-09-21-disclosure-feeds-design.md`
**Fixtures (committed):** `backend/tests/fixtures/disclosure_feeds/` — real captures from 2026-09-21; every parser is built and tested against these.
**Next step:** write the implementation plan, then dispatch implementers.

## What the next agent should do

1. Write `docs/superpowers/plans/2026-09-21-disclosure-feeds.md` following the
   superpowers `writing-plans` skill (bite-sized TDD tasks, verbatim code, no
   placeholders). The spec is complete; the plan argues from it.
2. Execute with the user's DeepSeek harness (`dsh`) — see "dsh notes" below —
   in waves so independent tasks run **concurrently**:
   - **Wave 1 (serial):** foundation — `ingestion/disclosure_feeds.py`
     (`FeedItem`, `DisclosureFeed` protocol, `to_document`, `BoilerplateRule`,
     `group_releases`, shared fetch helper), enum additions + migration,
     settings.
   - **Wave 2 (parallel ×4):** parsers `investegate.py`, `eqs.py`, `cnmv.py`,
     `globenewswire.py` + `actusnews.py` (RSS pair can be one task). Each is
     fixture-driven and independent.
   - **Wave 3 (parallel ×3):** job handler + scheduler wiring + health;
     identity backfill in `intelligence/service.py` + classifier metadata
     lines; language hardening (`extraction/local.py` Accept-Language,
     `instruments/normalize.py` suffixes).
   - **Wave 4 (serial):** `.env.example` docs, opt-in live tests, full
     `make test` / lint / typecheck, final review.
3. Test posture for any live run: `.env` is already `T212_ENV=demo`,
   `TELEGRAM_ENABLED=false`. Also set `EXECUTION_MODE=research_only`. Do **not**
   start StockBrain's compose for unit work; `make test` needs the
   `stockbrain_test` database only.

## Codebase facts already verified (save the re-reading)

- `RawSourceDocument` / `NewsProvider` protocol: `backend/stockbrain/ingestion/base.py:29-90`.
  Fields: `provider, provider_item_id, url, source_name, source_category,
  headline, author, published_at, updated_at_source, body, symbols,
  is_distinct_event, raw_payload, metadata`.
- **Dedupe already checks `(provider, provider_item_id)` first** —
  `ingestion/dedupe.py:68-83` (`DuplicateReason.PROVIDER_ITEM_ID`). The spec's
  §6.3 "gains a lookup" is therefore already satisfied; the plan only needs a
  test asserting it, not a code change. Unique partial index:
  `db/models/sources.py:113-116`.
- Pull-provider handler template: `handle_sec_refresh` at
  `jobs/handlers.py:556-613` (fetch → `services.ingestion.ingest_many(docs)`
  → `services.health.record(ProviderName.X, ProviderStatus.HEALTHY|DEGRADED,
  detail=...)`). `IngestionOutcome.CREATED_EVENT` is the "new event" outcome.
- Handler registration: `register_ingestion_handlers(...)` at
  `jobs/handlers.py:~1070-1116`; add
  `registry.register(JobType.DISCLOSURE_FEED_POLL.value, handle_disclosure_feed_poll)`.
- `HandlerContext` (`jobs/registry.py:20`): `job_id, job_type, payload,
  attempt, max_attempts, services, database`.
- Scheduling: `ServiceContainer._register_schedules` at `services.py:715+`;
  pattern `if self.sec is not None: scheduler.add(ScheduledTask(name=...,
  interval_seconds=300.0, run=self._enqueue_sec_refresh, initial_delay_seconds=30.0))`.
  Enqueue pattern `_enqueue_sec_refresh` at `services.py:1110-1121` (gates on
  `settings.discovery_enabled` and `await self._discovery_paused()`, then
  `self.queue.enqueue(session, JobType.X, payload=..., dedupe_key=..., priority=50)`).
- `ScheduledTask` fields: `name, interval_seconds, run, jitter_ratio=0.15,
  enabled=True, initial_delay_seconds=0.0` (`jobs/scheduler.py:31-44`).
- Enums: `SourceProvider` at `enums.py:68-82` (ALPACA, BRAVE, EXA, FIRECRAWL,
  SEC, MANUAL — with the provenance comment: never rewrite provenance);
  `SourceCategory` includes `REGULATOR, ISSUER, GOVERNMENT, NEWSWIRE, PRESS,
  UNKNOWN`; `JobType` at `enums.py:~490-510`. `ProviderName` (health) is in
  `observability/health.py:45-64`, not `enums.py`.
- Migration precedent for adding enum values:
  `backend/alembic/versions/20260905_2400_web_discovery_providers.py:219-225`
  (`ALTER TYPE source_provider ADD VALUE IF NOT EXISTS 'BRAVE'`). Check
  whether `job_type` / provider-health enums are DB-backed before assuming.
- Classifier receives provider/source metadata as template variables
  (`intelligence/classifier.py:~120`, `PROVIDER`, `SOURCE_NAME`, …) and
  `symbol_hints` from `metadata["symbols"]` in `intelligence/service.py:224-234`;
  impacts are upserted with `exchange_hint` at `intelligence/service.py:~619-639`
  — that is where the spec §7 backfill goes. Prompt file
  `prompts/event_classifier/v1.md` is **not** to change.
- Resolver rungs (`instruments/resolver.py:177-260`): ISIN → alias → ticker
  (+exchange narrowing) → name+exchange+currency → candidates.
  `ResolutionRequest` fields: `name_hint, ticker_hint, exchange_hint,
  currency_hint, isin_hint, broker`.
- Name normaliser suffix set: `instruments/normalize.py:33-58`.
- Local extractor sets `"Accept-Language": "en"` at `extraction/local.py:121`.
- HTML/XML parsing: `lxml` is already a transitive pin (trafilatura 2.2,
  yfinance). **No `feedparser`; do not add dependencies.**
- Research output language already pinned to English:
  `third_party/TradingAgents/tradingagents/agents/utils/agent_utils.py:52`.
- LLM in use is Meta `muse-spark-1.3-contributor` (`.env` `LLM_PROVIDER=meta`);
  the `deepseek-v4-flash` line in the classifier prompt header is stale.
- Broker ticker encodings (from `broker_instruments`): LSE/AIM `BARCl_EQ`
  (`market_symbol=BARC`), Xetra `SAPd_EQ`, Gettex also `…d_EQ`, Paris `…p_EQ`,
  Amsterdam `…a_EQ`, Madrid `…e_EQ`, SIX `…s_EQ`, Toronto `X_CA_EQ`, Lisbon
  `X_PT_EQ`, Brussels `X_BE_EQ`, Vienna `X_AT_EQ`. Exchange names as stored:
  `London Stock Exchange`, `London Stock Exchange AIM`, `Deutsche Börse Xetra`,
  `Gettex`, `Euronext Paris/Amsterdam/Brussels/Lisbon`, `Bolsa de Madrid`,
  `SIX Swiss Exchange`, `Toronto Stock Exchange`, `Wiener Börse`.

## Fixture facts verified (parsers rely on these)

- **Investegate** (`investegate_front_page.html`): 50 `<tr>` rows +1 header.
  Cells: datetime `21 Sep 2026 11:46 AM`; source link `/source/RNS` with
  class `source-RNS`; company link `/company/BARC` text `Barclays (BARC)`;
  `<a class="announcement-link" href=".../announcement/rns/<slug>/<headline-slug>/9782378">`.
  Announcement number is the last path segment → `release_id`. Pagination
  `?page=2..9`. Headline mix in fixture: 9× "Transaction in Own Shares", 5×
  "Holding(s) in Company", 4× "Director/PDMR Shareholding".
- **EQS** (`eqs_home.html`): items are `<a data-wio="news-feed-list-item"
  data-news-item="<uuid>" data-news-uuid=... data-news-languages='{"0":"en"}'
  data-news-isin="AU0000066086" data-news-category='...' href=".../news/<category>/<slug>/<uuid>_en">`.
  30 items; categories seen: corporate 14, other-capital-market-information 6,
  uk-regulatory 5, media 2, voting-rights 2, directors-dealings 1. Date text
  precedes each item ("21 September 2026"). `release_id = data-news-item`;
  language = URL suffix `_en`/`_de`.
- **CNMV** (`cnmv_oir.xml`, `cnmv_ip_empty.xml`): non-standard RSS — `<Channel>`
  and `<Title>` capitalised. Item: `<Title>ERCROS, S.A. (ERCROS)</Title>`,
  `<link>…Resultado-OIR.aspx?nreg=42823</link>` (`nreg` → `release_id`),
  `<pubDate>`, `<description>` with `<b>12:33  21/09/2026</b> <category text><BR/><BR/><summary>`.
  IP feed URL `…/informacion-privilegiada/RSS.asmx/GetNoticiasCNMV` was empty
  at capture time (Monday morning) — valid channel, zero items is **not** a
  parser failure for this feed; distinguish "channel present, no items" from
  "no channel".
- **GlobeNewswire** (`globenewswire_{france,netherlands,canada}.xml`): tags
  `dc:identifier` (release number, shared across language variants, e.g.
  `3365169` EN+FR), `dc:language`, `dc:publisher`, `dc:subject`, `link` of
  shape `/news-release/2026/09/21/3365169/0/en/<slug>`. Canada feed: 8/20
  items carry `(TSX: XXX)` / `(TSXV: XXX)` in title or description.
- **ActusNews** (`actusnews_en.xml`): 20 items, title `COMPANY : headline`
  (also `COMPANY - headline`), `link` `/en/<company-slug>/pr/2026/09/18/<slug>`,
  `guid` is the document URL. `release_id` = link path after `/pr/`.
- Cision `newswire.ca` feeds are NOT Canadian any more (serve PR Newswire
  global) — excluded on purpose. Do not re-add.

## Marketaux (excluded from this build, key present)

`MARKETAUX_API_KEY` is set in `.env`. Free tier: 100 req/day, 3 articles/req,
`x-usagelimit-remaining` header. Per-symbol polling works for `SAP.DE`,
`SHOP.TO`, `BP.L`; LSE index misses ticker changes (`SHEL` absent). Keep out
of this feature; a later "monitor known names" piece could use it.

## dsh notes (from memory file `dsh-deepseek-harness`)

- Launch from repo root only (`cd /Users/khaleel/Documents/StockBrain`); the
  write sandbox keys off cwd.
- `node ~/.dsh/profiles/node_modules/@deepseek-ai/dsh/lib/bin.js --profile headless --patch <workspace>/dsh-patch.yml "Read the file <dispatch.md> and carry out the instructions in it exactly..."`
- One-shot; cannot ask questions. Dispatch files must say "if blocked, write
  NEEDS_CONTEXT into the report and stop".
- Workers may see `test_group_blockers_are_reported_where_a_group_has_them`
  fail from root `.env` Telegram vars leaking — passes in the controller env;
  ignore.
- Pattern that worked: dsh implementers on plan briefs with verbatim code,
  Sonnet task reviewers, Haiku for tiny re-reviews.
