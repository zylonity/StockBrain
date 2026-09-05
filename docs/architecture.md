# Architecture

This document records decisions and their reasons. The design itself is
specified in `STOCKBRAIN_TECHNICAL_SPEC.md`; what follows is what was actually
built and why particular choices were made where the specification left room.

## Shape

A modular monolith and PostgreSQL. Two containers.

```
Alpaca news WS ─┐
SEC EDGAR ──────┼─→ ingestion ─→ dedupe ─→ canonical event
Firecrawl ──────┘                              │
                                               ▼
                                    DeepSeek V4 Flash classifier
                                               │
                                     instrument resolution
                                               │
                                     TradingAgents research
                                               │
                                       structured thesis
                                               │
                                   DETERMINISTIC RISK ENGINE
                                               │
                                        trade proposal
                                          ╱         ╲
                                     Web GUI     Telegram
                                          ╲         ╱
                                     two-stage human approval
                                               │
                                      fresh revalidation
                                               │
                                  ONE non-retried broker POST
                                               │
                                  reconciliation + audit log
```

## Module map

```
stockbrain/
  config.py          typed settings; the live-execution gate lives here
  logging.py         structlog + two-layer secret redaction
  enums.py           shared vocabulary; PostgreSQL native enum types
  errors.py          exception hierarchy; separates definite from ambiguous failure
  startup.py         startup checks and provider capability detection
  main.py            app factory, lifespan, middleware, SPA mount

  api/               FastAPI routers, dependencies, response schemas
  db/                declarative base, session/transaction helpers, models
  proposals/         proposal state machine (approval and execution to follow)
  observability/     provider health registry, metrics registry

  httpclient.py      shared provider HTTP: classified errors, opt-in retries
  services.py        runtime container; owns workers, scheduler, news stream

  ingestion/         providers, normalisation, dedupe, the ingest entry point
  jobs/              PostgreSQL queue, worker pool, scheduler, handlers

  llm/               provider interface, DeepSeek client, pricing, budget, telemetry
  intelligence/      classifier, semantic dedupe, prompts, classification service
  instruments/       resolution ladder, curated aliases, resolve service
  market_data/       provider protocol, Alpaca adapter, sessions, price reaction
  broker/            read-only T212 metadata + instrument sync (phase 4)
  risk/              (phase 6)
  telegram/          (phase 7)
  jobs/              (phase 2+)
```

## Decisions

### Why the risk engine is not an LLM

An LLM produces an argument and a direction. It cannot be relied on to respect a
position cap, a sector cap, a cash buffer or a daily loss limit, and its
"confidence" is a ranking feature, not a calibrated probability. Sizing is
therefore ordinary deterministic Python operating on a fresh broker snapshot.
The research layer's output contains no execution fields at all — that is
enforced by the shape of `ResearchDecision` and by the `theses` table, neither
of which has a quantity column.

### Why safety invariants live in PostgreSQL, not just in services

Trading 212 documents the order POST as non-idempotent: a repeated POST can
create a duplicate order. Application-level guards are necessary but not
sufficient, because a bug, a race or a future refactor can bypass them. The
schema therefore enforces:

| Constraint | Guarantee |
|---|---|
| `uq_execution_attempts_sent_once` — partial unique on `(proposal_id) WHERE sent_to_broker` | At most one attempt per proposal can ever have transmitted an order. |
| `ck_execution_attempts_sent_requires_timestamp` | A "sent" attempt always records when. |
| `uq_execution_attempts_proposal_id_attempt_number` | Attempt numbering cannot collide. |
| `uq_trade_proposals_active_instrument` — partial unique over non-terminal statuses | At most one live proposal per broker instrument. |
| `trade_proposals.version` (optimistic lock) + `SELECT ... FOR UPDATE` | A Telegram confirm and a web confirm racing cannot both win. |
| `uq_approval_actions_opaque_token_hash` | An approval token is single-use and globally unique. |
| `uq_broker_instruments_broker_broker_ticker` | A concurrent metadata sync cannot duplicate an instrument. |
| `uq_broker_exchanges_broker_provider_exchange_id` | Nor an exchange. |
| `uq_company_aliases_authoritative_scope` — partial unique over `(alias, type, exchange, currency) WHERE is_authoritative` | Two authoritative aliases cannot silently contradict each other. |
| `uq_companies_isin` | A company auto-created from broker metadata is created once, whatever the concurrency. |

`sent_to_broker` is set in the transaction *before* the HTTP request, not after
the response. That ordering is what makes the index meaningful: it records
"bytes may have left", which is the fact that matters when a response never
arrives.

### Why `EXECUTION_AMBIGUOUS` is a first-class state

A read timeout after an order POST is not a failure — it is an unknown. Treating
it as a failure invites a retry, which is exactly the action that creates a
duplicate order. `ExecutionOutcome` therefore distinguishes:

* `FAILED_BEFORE_SEND` — provably never reached the broker; a new attempt is
  permitted, and it is the *only* outcome that permits one;
* `AMBIGUOUS` — may or may not exist at the broker; reconciliation decides;
* `SUBMITTED` / `REJECTED_BY_BROKER` — a definitive response was received.

The exception hierarchy mirrors this: `DefinitePreSendFailure` versus
`AmbiguousTransportFailure`.

### Why the state machine is a module, not a set of if-statements

The web GUI and the Telegram bot are both clients of the same lifecycle. Putting
the transition table in `proposals/state_machine.py` means the two cannot
diverge, and it makes the important negative rules testable: `EXECUTING` never
returns to `APPROVED`, only `APPROVED` may begin execution, `EXECUTION_AMBIGUOUS`
is never left by resubmitting.

### Why the job queue is PostgreSQL

`SELECT ... FOR UPDATE SKIP LOCKED` gives at-least-once claiming with no extra
infrastructure. The expected workload is events per minute, not per millisecond.
Redis would add a second stateful service to back up, monitor and restore on a
home NAS in exchange for throughput this system will never need. The `jobs`
table also gives the audit trail a queue would not.

### Why provider health is per-subsystem

Losing the news feed must not make the application report itself unhealthy —
reconciliation, research on existing events, and the approval path all keep
working. `aggregate_status` therefore treats redundant subsystems (discovery)
differently from required ones (database), and `DISABLED` is never counted as a
fault, so an unconfigured integration reads as "not set up" rather than "broken".

### Why the SPA is served by FastAPI

Same-origin serving lets the session cookie stay `SameSite=Strict` with no CORS
relaxation and no second web server in the container. The Vite dev server
proxies `/api` for local development instead.

### Why timestamps are timezone-aware UTC and money is `Decimal`

Every `DateTime` column is `timezone=True`; `TIMEZONE` is a display setting only.
Quantities use `NUMERIC(28, 10)` (Trading 212 supports fractional shares) and
cash uses `NUMERIC(24, 4)`. Binary floating point never touches a financial
value: an integration test asserts a `Decimal` round trip through the database.

### Why migrations run in the entrypoint, not in application code

A rollback to an older image must not silently upgrade a schema, and a failed
migration should stop the container rather than leave a half-configured process
serving traffic. `check_schema_current` compares the applied revision against
the code's head and makes `/api/health/ready` return 503 on a mismatch.

### Why deduplication is layered, and why the last layer is missing

Deduplication runs cheapest-first because the expensive answer is rarely needed:

| Layer | Catches | Cost |
|---|---|---|
| 1 — provider id | The same article re-delivered | index lookup |
| 2 — canonical URL | Same document, different tracking parameters | index lookup |
| 3 — content hash | Verbatim syndication across outlets | index lookup |
| 3.5 — normalised headline in a window | Different bodies, same story → one event | index lookup |
| 4 — semantic | Rewritten headlines, story updates | **an LLM call** |

Layer 4 is deliberately absent until the classifier phase. Running a model before
four free checks would be the wrong order, and layer 3.5 is exact-match only on
purpose: a fuzzy match that wrongly merges two different events is far harder to
notice than a duplicated one.

The event-match window (36h by default) exists so that a headline recurring
months later — "Fed holds rates" — starts a new event instead of reviving a
stale one.

### Why the ingest path relies on unique indexes, not locks

`find_duplicate_source` is a `SELECT`, so two workers ingesting the same article
concurrently can both pass it. The loser then violates
`uq_sources_provider_item` and is reported as a duplicate. Check-then-insert is a
race; a unique index is not. The same pattern protects job scheduling via
`uq_jobs_dedupe_key_active`.

### Why retries are opt-in per request

`ProviderHttpClient.request_json` defaults `retry_safe=False`. A GET opts in
because it has no side effect; Firecrawl's search opts in explicitly because its
only side effect is credit consumption. Nothing else does. There is deliberately
no middleware that could decide on its own to repeat a request — that is the
mechanism which, applied to a Trading 212 order POST, creates a duplicate order.

Errors are classified rather than lumped together because the correct response
differs: an auth failure must never be retried (for SEC it earns an IP block), an
entitlement failure must degrade a subsystem instead of crashing it, and only
transient failures earn a retry.

### Why the scheduler only enqueues

Scheduled tasks never do work; they insert jobs. That keeps the recurring cadence
independent of execution concurrency, so a slow provider delays one job rather
than the whole schedule, and it means per-topic intervals survive a restart
because they are derived from `discovery_queries.last_run_at` rather than from an
in-memory timer.

### Why the LLM interface is text-in, text-out

`stockbrain.llm.base.LlmProvider` exposes exactly one capability: send messages,
receive a string plus token usage. No tools, no function calling, no callbacks.
A model reached through it cannot touch the broker, the filesystem or the
network, so a successful prompt injection has nothing to reach for. Everything a
model returns is validated against a Pydantic schema before any code acts on it —
JSON that parses is not JSON that is correct.

`thinking` is sent explicitly on every call because DeepSeek's API default is
*enabled*; the classifier is the cheap high-volume path and must not silently
become a reasoning call.

### Why classification is idempotent in three places

At-least-once delivery means a `CLASSIFY_EVENT` job will eventually run twice.

| Layer | Mechanism |
|---|---|
| Queue | `uq_jobs_dedupe_key_active` — one pending classify job per event |
| Service | Compare-and-swap `NEW → CLASSIFYING`, so exactly one worker proceeds |
| Database | `uq_event_company_impacts_event_id_company_key` + upsert |

The middle layer matters for cost, not correctness: a check-then-act status read
let two workers each spend a model call on the same event. A conditional UPDATE
lets one win. A stall reaper returns events abandoned in `CLASSIFYING` to `NEW`
after a timeout, mirroring the job queue's own reaper.

### Why semantic deduplication is biased against merging

Layer 4 is the only deduplication layer that costs money, so it runs last, only
against candidates a deterministic token-overlap filter already considers
plausible, and only above a configured confidence threshold.

The asymmetry is deliberate: a duplicate event is visible and correctable, while
a wrong merge silently destroys the distinction between two real occurrences and
nobody notices. So an unrecognised relation falls back to `UNRELATED`, a
hallucinated candidate id is discarded, and documents flagged
`is_distinct_event` (regulatory filings) never reach the layer at all.

A merge archives the folded event with a `merged_into_event_id` pointer rather
than deleting it, moves its source links to the survivor, and records the prior
values in the audit log. Nothing is erased.

### Why budgets stop analysis but never monitoring

The budget guard is consulted only by LLM callers. Nothing in ingestion,
deduplication or broker reconciliation asks it anything, so a hard limit
structurally cannot stop them. At the soft limit optional work (semantic
deduplication) is suppressed; at the hard limit no new model analysis starts and
events simply wait in `NEW` — no data is lost, and classification resumes when
the budget rolls over.

Spend is summed from `llm_calls` rather than a running counter, so it always
matches the recorded history and needs no reconciliation after a restart.

### Why instrument resolution refuses rather than chooses

An LLM produces a company name and, sometimes, a ticker. Neither is an
executable identity. The only thing that may reach an order request is a
`broker_instruments` row that Trading 212 itself supplied, so the resolver
treats every hint as a *search key* into verified metadata and walks a ladder of
evidence, strongest first:

| Rung | Evidence | Confidence |
|---|---|---|
| 1 | Exact ISIN | 0.99 |
| 2 | Curated authoritative alias | 0.97 |
| 3 | Exact ticker + exchange | 0.92 |
| 4 | Name + exchange + currency | 0.82 |
| 5 | Weaker heuristics | candidates only — never resolves |

If a rung matches more than one instrument the answer is `AMBIGUOUS`, carrying
every alternative. That asymmetry is the same one deduplication uses: an
unresolved company is visible and fixable, while a wrongly resolved one buys the
wrong security and nobody notices until the fill. Alphabet's share classes,
Berkshire's A/B, an ADR against its ordinary line, a UK line against a US line,
a reused ticker and a renamed company all reach ambiguity by the *same* route —
more than one verified listing fits the evidence — so there is no special case
for any of them.

Two smaller decisions follow from the same reasoning:

* **A company is created only from an ISIN.** ISIN is the one globally unique
  security identifier, and `uq_companies_isin` plus `INSERT ... ON CONFLICT`
  makes "no duplicate companies" a database guarantee rather than a check-then-
  insert race. An instrument without an ISIN still resolves; it simply has no
  company row, which is honest rather than invented.
* **The name-to-company-to-ISIN edge is deliberately cut.** A company row is
  created *by* a successful resolution, so letting a later resolution look that
  company up by name would let a resolution become the evidence that
  re-justifies itself — an ambiguity appearing later would never be noticed. The
  ticker edge remains, because a ticker is a discriminator in its own right.

### Why the alias table is scoped and exclusive

`company_aliases` is the deterministic escape hatch for names evidence cannot
settle, so it must not become a source of the ambiguity it removes. An
*authoritative* alias claims a `(name, type, exchange, currency)` scope, and
`uq_company_aliases_authoritative_scope` — a partial unique index over exactly
that tuple — makes a second, contradicting claim impossible. Last-write-wins
here would let one careless row silently redirect a name to a different
security.

The bare name of a genuinely multi-listed company gets **no** alias. "Alphabet"
resolving to GOOGL would be a decision disguised as data; only the
listing-specific names ("Alphabet class A") are curated.

### Why the exchange of an instrument is derived, not read

Trading 212's instrument payload has no exchange field at all — only a
`workingScheduleId`. Just `/equity/metadata/exchanges` says which exchange owns
that schedule. So the sync fetches exchanges first, stores the schedules with
their dated time events, and fills `broker_instruments.exchange` from the map.
An instrument whose schedule is missing keeps a NULL exchange rather than one
guessed from its ticker suffix, and that suffix is recorded separately as
`market_code` so the derivation is visible for what it is.

The stored time events pay for themselves twice: they are also the only
holiday-aware session data in the system, so "was this event pre-market?" is
answered from the broker's real calendar rather than a hard-coded clock. The
clock fallback exists for US listings whose schedule is not loaded, and it says
so — every session verdict carries its own source and whether it knows about
holidays.

### Why a market-data provider is probed, not trusted

Alpaca's plans differ by *feed*, and a request for a feed the account does not
own fails with an HTTP 403 that looks exactly like a bad credential. Assuming
entitlement would mean discovering the truth at the moment a proposal needs a
price. So the startup sequence issues one quote request and records what came
back as `HEALTHY` / `AUTH_FAILED` / `ENTITLEMENT_MISSING` / `DEGRADED` / `DOWN` /
`DISABLED` — finer-grained than the persisted `ProviderStatus` vocabulary,
because "your key is wrong" and "you have not paid for SIP" need opposite
responses even though both degrade the same subsystem.

A missing entitlement never crashes anything and never silently substitutes a
feed. Ingestion, classification and instrument resolution do not consult market
data at all, so they are structurally unaffected; what stops is *sizing*, and it
stops through one tested predicate, `quote_blockers`, rather than a condition
rewritten at each call site. Trading 212's own price data can never clear that
bar: `PriceSource.BROKER_T212` is simply absent from
`EXECUTION_GRADE_PRICE_SOURCES`, so being the only number available does not make
it usable.

## Untrusted content

Everything retrieved from the web, a news feed or a filing is data, never
instruction. Evidence will be wrapped in explicit document markers, the system
prompt will state that instructions inside documents are not to be followed, and
LLM agents are never given credentials or a broker tool, so a successful prompt
injection has no execution path to reach.

Concretely, as of the ingestion phase:

* `html_to_text` strips `<script>`, `<style>` and `<noscript>` blocks entirely
  before anything is hashed, stored as `normalized_text`, or shown;
* the API returns only that extracted plain text, never `raw_content`, so the
  provider's original markup does not cross the HTTP boundary;
* the frontend renders source excerpts as text nodes — there is no
  `dangerouslySetInnerHTML` anywhere in the codebase — and external links carry
  `rel="noopener noreferrer nofollow"`.

## Deferred by design

OpenBB, NautilusTrader, Redis, Celery, Kafka, Elasticsearch, a vector database
and a self-hosted crawler are extension points, not initial dependencies. Each
would be added only against a concrete blocker, and none has appeared.
