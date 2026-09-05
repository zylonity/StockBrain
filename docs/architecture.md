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
                            MANUAL (web/Telegram)   AUTOMATIC (system)
                                          ╲         ╱
                                   authorization + provenance
                                               │
                                      fresh revalidation
                                               │
                                    final pre-send preflight
                                               │
                                  ONE non-retried broker POST
                                               │
                                  reconciliation + audit log
```

All of it exists today. Authorization is still *not* execution: an `APPROVED`
proposal records that deterministic risk allowed the trade and which authority
signed it off, and transmission is a separate step behind a separate gate. What
Phase 8 added is the last two boxes — and the discipline that makes them
survivable, because the broker's order endpoint is documented as
**non-idempotent**.

## Module map

```
stockbrain/
  config.py          typed settings; the live-execution gate lives here
  logging.py         structlog + two-layer secret redaction
  enums.py           shared vocabulary; PostgreSQL native enum types
  errors.py          exception hierarchy; separates definite from ambiguous failure
  startup.py         startup checks and provider capability detection
  main.py            app factory, lifespan, middleware, SPA mount
  hash_password.py   CLI: generate WEB_OWNER_PASSWORD_HASH

  api/               FastAPI routers, dependencies, response schemas
  api/auth.py        session cookie, CSRF double-submit, deny-by-default gate
  db/                declarative base, session/transaction helpers, models
  proposals/         proposal state machine (approval and execution to follow)
  observability/     provider health registry, metrics registry, alert scan

  httpclient.py      shared provider HTTP: classified errors, opt-in retries
  services.py        runtime container; owns workers, scheduler, news stream

  ingestion/         providers, normalisation, dedupe, the ingest entry point
  ingestion/firecrawl_budget.py  durable Firecrawl call/credit ledger
  jobs/              PostgreSQL queue, worker pool, scheduler, handlers

  llm/               provider interface, DeepSeek client, pricing, budget, telemetry
  intelligence/      classifier, semantic dedupe, prompts, classification service
  instruments/       resolution ladder, curated aliases, resolve service
  market_data/       provider protocol, Alpaca adapter, sessions, price reaction
  fx/                typed FX rates, freshness/grade policy, two providers
  broker/            read-only T212 metadata, account state, automation policy
  risk/              deterministic engine, rules, sizing, spread, versioned config
  proposals/         state machine, generation, authorization, expiry, quotes
  control/           durable pause and kill switch, persisted in app_settings
  execution/         preflight, one transmission, classification, reconciliation
  telegram/          long polling, numeric-id auth, opaque tokens, two-stage confirm
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
| `uq_trade_proposals_dedupe_key` | A redelivered generation job cannot create a second proposal. |
| `uq_trade_proposals_active_thesis` — partial unique over non-terminal statuses | One live proposal per published thesis. |
| `ck_trade_proposals_approved_requires_authorization_provenance` | An `APPROVED` proposal always says what authorized it, when, and as whom. |
| `ck_trade_proposals_system_auth_requires_automatic_policy` | A `SYSTEM_AUTOMATIC` authorization is only legal on a proposal generated under the AUTOMATIC policy — so changing the policy cannot retroactively authorize existing work. |
| `ck_trade_proposals_invalidated_requires_status` | An invalidation timestamp and the `INVALIDATED` status cannot disagree. |
| `pg_advisory_xact_lock(broker, account)` | Exposure accounting is serialised across tasks, processes and restarts — which an in-memory lock is not. |
| `uq_notifications_dedupe_key` — partial unique on `(dedupe_key) WHERE dedupe_key IS NOT NULL` | One notification per proposal transition, across a redelivered job, two workers and a restart. |
| `approval_actions.consumed_at` under `SELECT … FOR UPDATE` | A callback token is redeemable at most once, whichever device taps first. |
| `fk_execution_attempts_proposal_environment` — composite FK on `(proposal_id, broker_environment)` | An execution attempt always runs in its proposal's environment. A worker started under a different `T212_ENV` cannot write a row claiming otherwise. |
| `ck_execution_attempts_ambiguous_matches_outcome` | The `ambiguous` flag and the `outcome` enum are two spellings of one fact and cannot disagree. |
| `ck_execution_attempts_broker_order_requires_send` | A broker order id can only exist on an attempt that recorded a transmission. |
| `ck_execution_attempts_broker_outcome_requires_send` | An outcome claiming the broker answered requires a recorded send. |

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

## Phase 5 research contract

`ResearchEngine.analyze` accepts a frozen `ResearchPacket` and returns a validated
`ResearchResult` containing StockBrain's `ResearchDecision`. Only the adapter
knows TradingAgents/LangGraph. The packet preserves the resolved company/listing,
trigger, impact path, timestamp, source references, bounded normalized excerpts,
provider snapshots and degradation. Excerpt truncation is explicit; full sources
remain in `sources`. It also accepts a previous thesis and records lineage,
without implementing portfolio management.

The classifier's text-only interface remains unchanged. Research has a separate
capability boundary: only `read_research_context({})`, which reads the immutable
packet. StockBrain prefetches the allowed Alpaca/FRED context deterministically.
Models cannot choose a URL, symbol, filesystem path, provider or credential.
Upstream templates containing interpolated evidence are demoted to escaped,
fenced user data beneath StockBrain's fixed system policy. No shell, broker,
search, file, optional risk agent, upstream disk log, or external tracing is
constructed. The sentiment and final-decision nodes are StockBrain-specific;
market/fundamentals, bull/bear and manager use pinned upstream factories.

DeepSeek's original assistant messages retain reasoning_content during a tool
exchange, including assistant messages without tool calls. Only public content
and an explicit telemetry allowlist cross the persistence boundary. There is no
graph checkpointer: it would persist hidden transport reasoning. Upstream's
serializer/parser is used directly through StockBrain HTTP, avoiding generic
ChatOpenAI behavior and SDK retries/tracing. Pro handles bull/bear/manager/final;
Flash handles the three analyst roles. Both thinking settings are explicit.

Research remains advisory. `ResearchDecision` contains action, confidence,
horizon, thesis, bull/bear cases, catalysts, risks, invalidation conditions and
validated evidence IDs. It has no quantity, allocation, broker payload or
execution authorization. Unknown output fields are removed at normalization,
and direct schema validation forbids extra fields. Reasoning payloads are
rejected. The GUI renders public strings as text and exposes read-only routes.

### Ownership, spend and recovery

`research_runs.dedupe_key` uniquely identifies event/company/configuration plus
an optional explicit rerun UUID and prior-thesis reference. The default request
is stable across restarts; rerun UUIDs are themselves idempotent. RUN_RESEARCH
uses the existing PostgreSQL queue and active-job dedupe index. A conditional
PENDING→RUNNING update assigns a lease token before any LLM call. Concurrent
workers cannot claim the same run. Identity is rechecked against the resolver
before analysis; later ambiguity or listing changes fail visibly.

Every call uses the Phase 3 BudgetGuard and LlmTelemetry. Research is optional
at the soft limit and cannot start at the hard limit. Pending budget-blocked
runs are swept and can resume after rollover. The guard is checked again before
each call, and spend invalidates its cache. No ingestion or deterministic dedupe
path consults it. As in Phase 3, limits are admission checks against recorded
spend, not exact monetary reservations; already admitted concurrent calls can
finish after a threshold is crossed.

A provider request has no exactly-once billing protocol. Therefore abandoned,
cancelled, timed-out or failed paid runs are terminal and require an explicit
rerun, rather than automatically repeating potentially billed work. A sweep
marks expired leases TIMED_OUT, recovers unstarted pending work, and cancels
pending runs from superseded configurations. Old workers lose permission to
start further calls or publish results. Safe reports and usage are stored in
StockBrain PostgreSQL only; run/thesis publication is one locked transaction.
This deliberately favors visible incomplete research over accidental duplicate
spend. Transport failures can leave unknown provider spend, recorded as failures.

Alpaca remains the market-data source; historical research snapshots never
clear execution-grade quote checks. FRED uses only DFF/DGS10 with a prior-day
Chicago vintage. Missing providers and classified errors appear as degradation.
No optional upstream Yahoo/social/prediction-market network path is enabled.
Research confidence is a ranking feature, not a calibrated probability or
authoritative risk score; Phase 6 below is what consumes it, and it may only
ever shrink a size inside limits the model never sees.

## Phase 6 deterministic risk and proposals

### Where the boundary actually is

`RiskEngine.evaluate` is a pure function from `RiskInputs` to `RiskDecision`:
no database, no HTTP, no clock of its own. Everything it reads is in the input
object, and the research layer contributes exactly two scalars to it — an
`action` and a `confidence`. There is no field on `RiskInputs` for a thesis, a
rationale, a report or a quantity, so "an LLM cannot authorise a trade" is a
property of a type rather than of a code path somebody remembered to call.

Evaluation runs in a fixed order:

1. **Gates** — identity, instrument type, account availability and freshness,
   quote availability, price-source provenance, quote age, two-sidedness,
   spread ceiling, market session, currency alignment, current position,
   duplicate/conflicting proposals, active-proposal count, confidence floor.
2. **Caps** — per-trade notional, per-trade percentage, position concentration,
   aggregate exposure, exposure reserved by other live proposals, cash reserve,
   broker `maxOpenQuantity`. The engine takes the minimum; a cap with no
   headroom becomes a block.
3. **Reductions** — research confidence, and (only under an explicit policy) a
   wide spread. Multiplicative, always in `(0, 1]`, never upward.
4. **Sizing** — side, quantity, reference price.

A `BLOCK` is absolute: the final outcome is computed from the rule list, and no
later stage can remove a rule from it. A blocked decision carries **no size at
all** — not a size that is ignored — so a future refactor cannot start reading
one. Confidence may only ever shrink a size inside the hard caps.

Every rule records `rule_id`, `rule_version`, outcome, the value observed, the
threshold it was compared against and a sentence, and all of that is persisted
on the proposal. A rule that could not run is recorded `WARN`, not `PASS`: a
rule that did not run has not passed.

### Why the spread ceiling is a hard rule

A live Alpaca IEX quote for AAPL at 03:40 UTC was the 16:00 ET closing print
with a **$33 spread on a $321.80 mid** — 1,024 bps, a 10% round trip. The age
check caught that particular quote. A book that wide *during regular hours*
would be fresh and still ruinous, so width is checked independently of age.

Every abnormal book shape gets its own name — `MISSING`, `NON_POSITIVE`,
`ONE_SIDED`, `CROSSED`, `LOCKED`, `EXCESSIVE` — because the causes differ and an
operator reading a refusal needs to know which one happened. Only `EXCESSIVE` (a
well-formed book that is merely too wide) is eligible for the optional `reduce`
policy; the others have no usable mid and block under every policy. A locked
market is refused rather than treated as free liquidity: a zero-width two-sided
equity quote is an artefact of a halt or a feed update.

The threshold comparison is exact. `spread / mid` is frequently non-terminating,
so comparing a rounded ratio would make the boundary depend on the rounding;
the test is `spread * 10000 <= ceiling_bps * mid`, which is pure multiplication.
The boundary is inclusive.

### The sizing contract

| Input | Output |
|---|---|
| research action, bounded confidence factor, current position, fresh quote, account state, risk limits, active-proposal reservations, broker constraints | side, quantity, target notional, reference price, max quantity, max notional, sizing reasons |

Three choices carry their reasons:

* **The reference price is the marketable side, not the mid.** A market buy
  lifts the ask; sizing it against the mid over-commits by half the spread every
  time.
* **Quantities round *down*, to whole shares by default.** Trading 212 supports
  fractional shares but documents no minimum quantity and no step size — Phase 4
  measured `minTradeQuantity` populated on 0 of 17,452 live instruments — so
  nothing here invents one. Rounding down can never breach a cap. Fractional
  sizing exists behind a flag.
* **A size that rounds to nothing is not a trade.** It is reported
  non-executable with the reason rather than as a zero-quantity order.

Action semantics: `BUY` opens or increases a long; `SELL` closes the whole
*available* quantity (shares held inside a pie are owned but not individually
tradable); `REDUCE` takes a configured fraction as a deterministic partial exit
and refuses to become a full exit on a single share; `HOLD` and `NO_ACTION`
produce no executable proposal at all. Short selling is not enabled, so a sell
without a position blocks.

Exposure caps deliberately do **not** apply to `SELL`/`REDUCE`. Limits bound
risk taken, not risk removed, and a cap that can stop a position from being
closed is a hazard rather than a control.

### Currency

Trading 212 documents that orders execute only in the primary account currency
and that multi-currency accounts are not supported through the API, and it
reports every account and position value in that currency. StockBrain has no
verified FX source, so **v1 is same-currency only**, enforced by the
`currency_alignment` rule: an instrument, quote or account currency that
disagrees blocks, and the refusal says that no FX rate source is configured. A
size computed from an invented rate is a wrong size.

### ExecutionPolicy and authorization provenance

Two permissions on deliberately different axes:

| Axis | Setting | Gates |
|---|---|---|
| Authorization | `EXECUTION_POLICY` = `manual` \| `automatic` | whether a proposal may be authorized without a human |
| Transmission | `EXECUTION_MODE` + the four T212 live gates | whether an order may ever reach the broker (phase 8) |

Conflating them would let one be granted by satisfying the other, so the Phase 4
live-execution truth table is untouched and `T212_AUTOMATED_TRADING_CONSENT_CONFIRMED`
is deliberately *not* one of its gates.

`AuthorizationSource` is `HUMAN_WEB | HUMAN_TELEGRAM | SYSTEM_AUTOMATIC`. Human
approval is one source among several rather than the definition of
authorization, so manual and automatic deployments converge on the same
`APPROVED` representation and differ only in recorded provenance. Every
authorization persists the source, the actor, the timestamp and a snapshot of
the policy and broker capability that permitted it — so a later configuration
change cannot rewrite the record of what was relied on.

`execution_policy` is recorded on the proposal **at creation**. A
`SYSTEM_AUTOMATIC` authorization is only legal on a proposal generated under the
AUTOMATIC policy, enforced by a check constraint, so flipping the deployment's
policy can never retroactively authorize existing work.

### The automatic-mode broker capability gate

Automation is a *broker* capability, not a StockBrain preference, so each broker
advertises its own answer in `broker/automation.py`. A broker with no declared
policy is treated as forbidding it.

Trading 212's answer follows its API Terms (verified 2026-09-04): clause 4.2(a)
expressly prohibits *Algorithmic Trading* — a computer determining order
parameters with limited or no human intervention — and clauses 6.6/6.7 require
prior written consent for an automated customised interface. So:

* **Demo (paper)** — permitted with credentials, and the supported path for
  exercising automatic authorization end to end.
* **Live** — requires `T212_AUTOMATED_TRADING_CONSENT_CONFIRMED=true`, and the
  process *refuses to start* on `EXECUTION_POLICY=automatic` with
  `T212_ENV=live` without it. A deployment that asked for automatic and quietly
  got manual is a deployment nobody is watching.

The flag records a fact about the operator's relationship with their broker. It
is not a bypass, and there is deliberately no general "ignore broker rules"
switch anywhere in the codebase.

Automatic mode fails closed: it never lifts a rule because confidence is high,
never uses a TradingAgents quantity, never authorizes a stale or ambiguous
proposal, and sends no order.

### Proposal state machine

```
DRAFT ─→ READY ─→ NOTIFIED ─→ APPROVAL_PENDING ─→ APPROVED ─→ EXECUTING ─→ EXECUTED
           └────────────┴──────────────┬─────────────┘              ├→ FAILED
                                       │                            └→ EXECUTION_AMBIGUOUS
        REJECTED · EXPIRED · CANCELLED · INVALIDATED  (all terminal)
```

`READY`, `NOTIFIED` and `APPROVAL_PENDING` all reach `APPROVED` directly:
authorization is one act with one full revalidation, whoever performs it.
`APPROVAL_PENDING` remains for the two-stage confirmation that will guard
*execution* in phase 8, which is a different question from "is this authorized".
`APPROVED` is not itself authorizable, which is what makes the double-click case
a refusal rather than a second record.

`INVALIDATED` is new and distinct from the other terminals: the proposal was
still within its TTL and nobody acted on it, but the listing, the position, the
account or the market moved out from under the numbers it carries.

### Authorization-time revalidation

Nothing is trusted from generation time. Approving re-reads the listing under
lock, re-reads broker account state, fetches a **fresh** execution-grade quote,
recomputes the exposure other live proposals have reserved, and re-runs every
deterministic rule plus four that only exist because a proposal exists:
`proposal_ttl`, `reference_price_drift`, `risk_policy_version` and
`authorization_envelope`.

The quantity the operator saw is **not** silently re-sized. If the fresh
envelope no longer permits it, the proposal is invalidated and a new analysis is
required — re-pricing under someone's finger is how a person approves a trade
they did not read. A refusal is *committed and then raised*: raising from inside
the transaction would roll back the invalidation and the evaluation row, and a
refusal nobody can read afterwards is not a durable refusal.

### Why blocked evaluations get their own table

`risk_evaluations` records every run of the engine, allowed or blocked, at
generation and again at authorization. A proposal row cannot serve the purpose:
its quantity, side and reference price are `NOT NULL` and positive, and a
blocked evaluation has none of them — inventing values to satisfy the schema
would be recording a trade that was never contemplated. "Why was nothing
proposed for this thesis?" is a first-class query, for the same reason Phase 4
exposed its `AMBIGUOUS` resolutions.

### Exposure reservation and concurrency

Every non-terminal proposal reserves its notional; only exposure-*increasing*
sides count, because a pending sell frees cash rather than committing it.
Reserving only from `APPROVED` would let a queue of unapproved proposals each be
sized against the same cash, so approving them in sequence would breach every
cap that each individually respected.

Concurrency is handled in PostgreSQL, never in memory:

* a **transaction-scoped advisory lock** on `(broker, account)` serialises every
  exposure calculation — two proposals generated a millisecond apart cannot each
  believe the whole cash buffer is theirs;
* `SELECT ... FOR UPDATE` plus a check of the optimistic `version` column
  serialises authorization, so two browser tabs, or an approve racing a reject,
  produce exactly one winner and one `409`;
* `uq_trade_proposals_active_instrument` and `uq_trade_proposals_active_thesis`
  make "one live proposal per listing" and "per thesis" database guarantees;
* `trade_proposals.dedupe_key` makes a redelivered generation job a no-op at the
  database rather than at a hopeful check.

The expiry sweep deliberately skips `APPROVED`: an authorized proposal belongs
to the execution phase, and a background sweep quietly retracting an
authorization would make "approve then expire" a race whose winner depends on
scheduler timing. Authorization itself still refuses an expired proposal under
lock, which is the property that matters.

### Expiry and invalidation

TTL is configurable (`RISK_PROPOSAL_TTL_MINUTES`, default 30) and an expired
proposal can never be authorized. The once-a-minute sweep also invalidates on
durable precondition failures — the listing retired or changed identity, the
risk policy version changed, a newer thesis superseded this one, or the position
backing a reduction fell below the proposed quantity — and re-prices a bounded
batch of live proposals to catch a market that widened or drifted. A *missing*
quote does not invalidate: providers go down, and retiring every open proposal
because one request failed would make an outage destructive rather than merely
degrading.

### Broker account state

`GET /equity/account/summary` (1 req/5s) and `GET /equity/positions` (1 req/1s),
read-only, mirrored into `portfolio_snapshots` and `positions`. Neither client
in this process has an order, amend or cancel method, and a test scans every
module to keep it that way.

`AccountStateService.load` **fails closed**: no snapshot, a snapshot older than
`RISK_MAX_ACCOUNT_STATE_AGE_SECONDS`, one from the other broker environment, or
one missing a currency or total value all return `None` with a reason, and the
`account_state_available` rule blocks. It never falls back to the last known
balance. `quantityAvailableForTrading` is kept separate from `quantity` all the
way to the engine, because shares inside a pie are owned but not individually
tradable.

## Phase 7 Telegram control and approvals

### Where the trust boundary is

Telegram supplies exactly three things: a numeric user id, a numeric chat id,
and an opaque token. It supplies no ticker, no side, no quantity, no price, no
account and no authorization source, and it receives no broker credential.
`ProposalService.authorize` takes `(proposal_id, source, actor)` and re-reads
everything else from the row under lock — so "Telegram cannot size a trade" is a
property of a function signature rather than of a check somebody remembered.

```
Telegram update
   ↓  numeric user id + numeric chat id           ← never a username
authorizer (allowlists, chat policy)
   ↓  opaque token                                 ← 46 of Telegram's 64 bytes
approval_actions row (proposal, stage, user, chat, expiry, consumed_at)
   ↓
ProposalService.authorize(source=HUMAN_TELEGRAM, actor=<numeric id>)
   ↓  the same lock, CAS, revalidation and provenance the web uses
APPROVED — and still no broker order
```

There is deliberately **no Telegram-specific risk implementation**. A test
asserts that no module in `stockbrain/telegram/` imports `RiskEngine`,
`RiskInputs` or anything under `stockbrain.risk`, and that the only lifecycle
calls it makes are `authorize` and `reject`. Two clients that each own a copy of
the rules are two clients that will eventually disagree about what is permitted.

### Why numeric ids, and why an empty allowlist authorises nobody

A Telegram username is chosen by its owner, can be released, and can then be
registered by somebody else. Authorizing a financial action on one would mean
authorizing whoever holds that name today. `TelegramIdentity` therefore has
three fields — `user_id`, `chat_id`, `chat_type` — and no field for a username
or a display name, so there is nothing for a later change to start trusting.

An empty `TELEGRAM_ALLOWED_USER_IDS` authorises nobody, never everybody, and the
runtime is not constructed at all: the provider reads `DISABLED` with that exact
reason. This is the configuration mistake most likely to be made by leaving a
variable blank, so it fails closed and loudly.

Chat policy is stated rather than inferred. A private chat's id *is* its user's
id, so a message from one is trivially bound to one identity and is accepted.
Any other chat — group, supergroup, channel — is refused unless
`TELEGRAM_ALLOW_GROUP_CHATS=true` **and** the chat id appears in
`TELEGRAM_ALLOWED_CHAT_IDS`. Two switches, because "may the bot be used outside
a private chat" and "which group" are different decisions, and everyone who can
read a group can read a trade proposal posted into it.

A consequence worth stating: a proposal notification sent to a *group* carries
no approval buttons. A token binds one numeric user, and a group has no single
holder; minting a button for "whoever taps first" would make the user binding
decorative. The message says to use `/proposals` in that chat instead, where the
tokens are minted for the numeric user who actually asked.

### Callback tokens

`callback_data` is `sb:` plus 43 url-safe characters — 46 of the 64 bytes
Telegram documents — and contains no proposal id, no order field, and **not even
which button it is**. The stage lives on the server-side row, so the action a
button performs is decided by the database rather than by the bytes Telegram
hands back.

Only the SHA-256 is stored. The raw value exists in one Telegram message and for
the microseconds it takes to hash an incoming callback; it is never persisted,
never logged and never put in an error message.

Redemption checks five bindings before consuming, in order: proposal, stage,
numeric user, numeric chat, expiry. The row is taken `FOR UPDATE`, so a double
tap serialises and the second finds `consumed_at` set. A token presented by the
wrong user or from the wrong chat is refused **without** being consumed — if a
stranger's tap spent it, obtaining a forwarded copy of the message would be
enough to disable the owner's real button.

Every transition that ends a proposal's authorizable life — authorization,
rejection, cancellation, expiry, invalidation — consumes every outstanding token
for it, in the same transaction. *That* is what makes an old button inert.
Blanking the keyboard is cosmetic and best-effort: a message can be forwarded,
screenshotted, or edited by a client that refuses the edit, and none of that
reaches the row the token names.

### Two-stage confirmation

Stage one consumes an `APPROVE` token, checks the proposal is still authorizable
and that trading is not halted, and mints a short-lived `CONFIRM` token whose
`parent_action_id` is the approve action. It authorizes nothing: a mis-tap opens
a dialog, never a trade. Stage two consumes the `CONFIRM` token, verifies it
descends from an `APPROVE` on the same proposal issued to the same user in the
same chat, and only then calls the shared `authorize`.

The confirmation names the side and the quantity, because the point of a second
step is that the operator confirms what they actually read. "Back" retires the
outstanding confirmation rather than leaving it live for the rest of its TTL — a
retraction that only looks like one is worse than none.

Refusals are reported by *cause*, not collapsed: "you already pressed this",
"this expired", "this is not yours" and "this button is gone" need different
answers, and burying a genuine authorization failure inside ordinary noise is
how it goes unnoticed.

### Concurrency

Every race is settled by the Phase 6 mechanisms, exercised through the Telegram
path rather than re-guarded:

| Race | Outcome |
|---|---|
| Web approves while Telegram confirms | One winner; the loser sees a refusal. The web authorization also retires the Telegram token, so the later tap usually never reaches the proposal. |
| Two Telegram devices, same button | `FOR UPDATE` on the token row; the second tap is `ApprovalActionConsumed`. |
| Telegram approve versus Telegram reject | One terminal state; the loser is refused. |
| Automatic authorization versus a Telegram tap | An AUTOMATIC proposal gets **no** approve button at all, stage one refuses one, and the `system_auth_requires_automatic_policy` check constraint stands behind both. |
| Proposal expires mid-confirmation | `authorize` refuses under lock; the TTL is not advisory. |
| Callback after invalidation | The invalidating transition already consumed the token. |
| Stale confirmation token | Expired, or retired by "Back". |
| Wrong user or wrong chat | Refused without consuming. |

### Why HTML parse mode, not MarkdownV2

Almost everything interesting in a message is untrusted: a company name, a
headline, a sentence a model wrote about a document somebody else published.
MarkdownV2 requires escaping eighteen characters with context-dependent extra
rules inside links, code spans and custom emoji, and one missed escape is a
`400 Bad Request` on the message that mattered most. HTML mode needs exactly
three substitutions — `&` `<` `>` — with no positional exceptions, so the escape
is total and reviewable in one function.

Consequently: every untrusted value goes through `esc`/`trim`; the renderers
build `<a href=…>` from literals only, so a headline containing a URL renders as
its own characters and cannot be disguised as different text; link previews are
disabled so Telegram cannot render a card for one; and values are length-bounded
*before* escaping, because truncating escaped text can cut `&amp;` in half.

### Long polling, and the runtime that supervises it

Transport is `getUpdates`. A webhook would need an inbound port, a public TLS
endpoint and a reverse proxy in front of a home NAS; the specification is
explicit that StockBrain must not be exposed to the internet just to support
Telegram. Telegram documents the two as mutually exclusive, so
python-telegram-bot clears any configured webhook while bootstrapping — and the
opt-in live test asserts none is configured, because a webhook left on the bot
would silently stop polling from ever receiving an update.

The bot runs in the application's own process, inside a supervisor task:

* **it never blocks anything else.** If Telegram is unreachable at boot the
  supervisor backs off and retries while FastAPI, the job workers, the scheduler
  and the news stream come up normally;
* **health is measured, not assumed.** A quiet bot receives no updates, so "we
  have not crashed" proves nothing. A periodic `getMe` proves the process can
  still reach Telegram, and drives HEALTHY / DEGRADED / DOWN;
* **a bad token is not retried forever.** python-telegram-bot's polling loop
  retries network errors indefinitely (1.5×, capped at 30s) but aborts on
  `InvalidToken`; StockBrain mirrors that judgement, recording DOWN with a clear
  reason — the same treatment `ProviderAuthError` gets everywhere else.

Pending updates are dropped at startup. An update queued while StockBrain was
down is of unknown age, and replaying a control command from an unknown time —
`/resume` above all — is not a decision a restart should make on its own.

Error text never reaches a log or a health record: a python-telegram-bot error
message can contain the request URL, and the request URL contains the bot token.
Only the exception's class name is recorded.

### Notifications

Telegram is a delivery channel, never the system of record. Every message
corresponds to a `notifications` row inserted **first**, keyed
`telegram:proposal:<id>:<event>` and protected by `uq_notifications_dedupe_key`.
That index is what makes "one notification per transition" true across a
redelivered job, two workers and a restart, rather than true only while one
process happens to remember.

Sending happens in a `SEND_NOTIFICATION` job rather than inline, so a Telegram
outage cannot fail an authorization that already happened.

A delivery failure is recorded `FAILED` and **not** retried on a later tick. An
automatic resend cannot distinguish "the message never arrived" from "the message
arrived and the status write did not", so it turns one transient network error
into a duplicate trade alert. The failure is visible in the table, the log and
the bot's health; the proposal is unaffected, because a notification is derived
from state rather than being it.

Anti-spam is a rule rather than a rate limiter: terminal announcements
(rejected, invalidated, expired, refused) are sent only for proposals the
operator was actually told about. A proposal that was born blocked, or expired
before anyone saw it, produces no chatter.

Manual and automatic proposals get different *text*, not the same text with
different buttons. An automatically authorized proposal says `SYSTEM_AUTOMATIC`
authorized it and that no human approval was requested; offering an approve
button there would imply a decision that was never asked for.

### Durable pause and kill switch

Two flags, one row each in `app_settings`, read from PostgreSQL on every
consultation. There is deliberately no in-memory cache: the value is read a
handful of times a minute, and a cache is exactly what would let two workers, or
two processes, disagree about whether trading is halted.

| Flag | Command | Stops | Leaves running |
|---|---|---|---|
| `control.trading_paused` | `/pause`, `/resume` | new proposal generation; every authorization path (web, Telegram, automatic) | ingestion, classification, research, broker reconciliation, the expiry sweep |
| `control.kill_switch` | `/kill` | everything a pause stops, plus any future order transmission | the same |

Spec section 20 is explicit that a pause stops new trade proposals and *not*
broker reconciliation; stopping discovery as well would mean a pause silently
costs the operator the news that happened during it.

Enforcement lives inside `ProposalService.authorize`, not in each client, so web,
Telegram and the automatic path are covered by one check and cannot diverge. It
raises `AuthorizationNotPermitted` — what is being refused is the
*authorization*; transmission is Phase 8's separate permission with its own gate.

`/resume` lifts a pause and **does not** release the kill switch. Releasing it is
an explicit second act (the `/api/v1/system/kill-switch` route, or the GUI's
release button), because if the routine control also cleared an emergency stop,
the emergency stop would be one habitual tap away from being undone by somebody
who only meant to restart normal work.

Neither flag closes a position, cancels an order or touches the broker. That is
structural rather than promised: no order, cancel or amend method exists in this
process, a test scans every module for one, and a second test asserts the control
module references no broker identifier at all. A kill switch that liquidated
would turn a precautionary tap into the largest trade of the day.

Startup reads and logs the persisted state before any worker can act. A process
that comes back up halted says so; silently resuming risky behaviour after a
crash is the failure this table exists to prevent.

### What Phase 7 deliberately did not do

No broker order, cancel or modify path — the capability audit still finds zero.
No webhook, no inbound port, no public endpoint. No second risk implementation.
No weakening of automatic authorization: `SYSTEM_AUTOMATIC` remains the source
under `EXECUTION_POLICY=automatic`, and Telegram cannot convert an automatic
proposal into a human-authorized one, because the check constraint that permits
`SYSTEM_AUTOMATIC` only on an AUTOMATIC proposal also refuses the reverse.

## Phase 8 Trading 212 demo execution

### The ordering that is the whole argument

Trading 212 documents the market-order endpoint as **not idempotent**: *"Sending
the same request multiple times may result in duplicate orders."* There is no
idempotency key, no client-supplied reference, and no request field that would
let the broker collapse a duplicate. Every design choice below follows from
taking that one sentence seriously.

```
1. look for an already-transmitted attempt   → if one exists, reconcile, never send
2. preflight                                  (network reads, no locks held)
3. take a local rate-limit token               (before the send transaction)
4. THE SEND TRANSACTION
     lock the proposal row · re-check version, status, expiry
     re-read the kill switch FROM THE DATABASE
     insert the attempt with sent_to_broker = TRUE, sent_at = now
     move the proposal to EXECUTING
     COMMIT
5. POST exactly once                           (no retry, at any layer, ever)
6. classify and persist
```

**Step 4 commits "bytes may have left" before step 5 makes it possible.** That
ordering is deliberate and it is the point: if the process dies anywhere between
the commit and the response, the next worker finds a transmitted attempt and
reconciles rather than resending. `uq_execution_attempts_sent_once` — a partial
unique index on `(proposal_id) WHERE sent_to_broker` — then makes a second
transmission impossible even against a bug that bypassed every service check.

The cost is paid honestly: a *provable* pre-send failure has to be retracted,
which happens in exactly one place and is discussed below.

### The state split

Two questions, two places to answer them, because collapsing them is how a
system decides to resend an order that already exists:

| Question | Answered by |
|---|---|
| Where is this proposal in its life? | `trade_proposals.status` |
| What happened to this transmission? | `execution_attempts.outcome` |

```
APPROVED ──→ EXECUTING ──→ EXECUTED          (broker reports FILLED)
   ↑             ├────────→ FAILED            (broker refused, or ended the order)
   │             └────────→ EXECUTION_AMBIGUOUS
   └──────────────────────  (proven pre-send failure only)
```

`ExecutionOutcome` carries the finer detail — `PENDING`,
`FAILED_BEFORE_SEND`, `SUBMITTED`, `REJECTED_BY_BROKER`, `AMBIGUOUS`,
`RECONCILED_FILLED`, `RECONCILED_NOT_PLACED` — and the four questions it keeps
apart are *did we try*, *did the bytes leave*, *did the broker answer*, and *do
we know*. No new proposal statuses were added; the existing vocabulary already
said everything a proposal needs to say.

An acknowledged-but-unfilled order leaves the proposal `EXECUTING`, which keeps
its exposure reserved. An unfilled order is committed cash that the account
snapshot cannot see yet.

### Why `EXECUTING → APPROVED` now exists

Phase 6 wrote that `EXECUTING` never returns to `APPROVED`. Phase 8 found that
this contradicted `ExecutionOutcome`, which documents `FAILED_BEFORE_SEND` as
"the only outcome from which a *new* attempt may be created". With no way back,
a DNS failure permanently killed an authorized proposal the broker had never
heard of.

The transition therefore exists, for exactly one situation, and its narrowness
is enforced above the state machine. `Trading212OrderClient` raises
`DefinitePreSendFailure` only for httpx exceptions that **cannot occur once a
request line has been written** — `ConnectError`, `ConnectTimeout`,
`PoolTimeout`, `ProxyError`, `LocalProtocolError`, `UnsupportedProtocol`,
`InvalidURL`. Every other transport failure (`WriteTimeout`, `WriteError`,
`ReadTimeout`, `ReadError`, `RemoteProtocolError`, `CloseError`) is
`AmbiguousTransportFailure`, because "the connection broke" and "the connection
broke after the broker read the order" are indistinguishable from this side of
the socket.

On that proof, and only there, `sent_to_broker` is set back to `false` and the
proposal returns to `APPROVED`. That is not a weakening of
`uq_execution_attempts_sent_once`: the flag records "bytes may have left", and
here there is proof they did not, so retracting it is what keeps the index
meaningful rather than merely obstructive. Tests assert that exactly one module
names `PRESEND_RECOVERY_TARGET` and exactly one line sets the flag false.

### Classifying the outcome

A complete HTTP response is proof the broker decided, so it is definitive.
Anything less is not:

| Result | Outcome | Proposal | Reservation |
|---|---|---|---|
| 2xx, parseable | `SUBMITTED` | `EXECUTING`, or `EXECUTED` when filled | held until filled |
| 400 / 401 / 403 / 404 / 422 | `REJECTED_BY_BROKER` | `FAILED` | **released** — provably nothing exists |
| Proven pre-send transport failure | `FAILED_BEFORE_SEND` | back to `APPROVED` | held |
| 408 / 429 / 5xx / undocumented status | `AMBIGUOUS` | `EXECUTION_AMBIGUOUS` | **held** |
| Any other transport failure | `AMBIGUOUS` | `EXECUTION_AMBIGUOUS` | held |
| **2xx with an unparseable body** | `AMBIGUOUS` | `EXECUTION_AMBIGUOUS` | held |

The last row is the most dangerous shape there is: the broker accepted the order
and StockBrain cannot read its id. Treating it as a failure would release the
reservation for a position that exists, so it reconciles.

HTTP 429 is treated as *unknown* rather than as a refusal. Trading 212 does not
document whether its limiter runs before or after order acceptance, and the
conservative reading is the only one that cannot lose an order. In practice the
local bucket (49/minute against a documented 50, because limits are per
*account* rather than per key) should mean it never happens.

### The immutable execution snapshot

Written once, immediately before transmission, never updated:
the proposal, both environments plus the provider's, the listing, side, quantity
and signed quantity, the reference price and notional, the authorization source
/ actor / timestamp, the execution policy, the risk policy version and snapshot
hash, the full quote (bid, ask, mid, spread, spread bps, age, source, session),
the account snapshot, the position, the kill/pause state, every transmission
gate, and the request fingerprint.

Kept in a JSONB column on the attempt rather than in a table of its own because
it is strictly one-to-one with the attempt and never queried independently — and
kept **separate from the proposal's own columns** because a proposal records the
decision that was made. Overwriting it with the newer world the order was sent
into would destroy the only record of what the operator actually approved.

### The request fingerprint is not an idempotency key

SHA-256 over the trade's identity: proposal, environment, listing, order type,
side, quantity, signed quantity, session flag, and a scheme version. Money and
quantities are decimal *strings*, so the same trade hashes the same on any
machine. Deliberately no clock and no attempt number — two attempts at the same
trade *should* fingerprint the same, because that collision is what an operator
needs to be able to see.

It exists for audit, for reconciliation evidence and for regression detection.
Nothing may read a matching fingerprint as permission to resend; the module says
so and a test asserts that it says so.

### Final pre-send revalidation

An authorization is a statement about the moment it was given; an order is sent
into a later one. So `ExecutionPreflight` re-runs everything, and **refusal is
the default**:

* **Deterministic risk** comes from `ProposalService.revalidate` — the same
  engine and the same rule functions that guarded the authorization. There is no
  second risk implementation, and a test asserts the execution package imports
  none of `stockbrain.risk`.
* **Permission and state** are the checks that only exist because transmission
  exists: three-way environment agreement, the transmission gates, the durable
  kill switch and pause, the send-time automation consent, `APPROVED` status,
  authorization provenance, the TTL, and the absence of an earlier transmitted
  attempt.
* **Position availability for a sale** is named separately, because selling what
  a pie holds is its own hazard: Phase 6 measured
  `quantityAvailableForTrading` differing from `quantity` on 13 of 14 real
  positions, and the pie can move between authorization and transmission.

A refusal also says whether the proposal **survives** it, which is the
distinction that decides whether an outage is destructive or merely degrading:

| Blocked by | Meaning | Proposal |
|---|---|---|
| `quote_available`, `account_state_available` | an input is missing, so every downstream rule blocks mechanically and the verdict says nothing about the trade | survives, deferred |
| `quote_freshness`, `account_state_freshness` *alone* | a provider is behind | survives, deferred |
| drift, spread, session, any cap, position, identity, TTL | a statement *about the trade* | `INVALIDATED` |
| a permission or state gate (kill switch, master switch, environment) | about the deployment or the moment | survives |

Engaging a kill switch must not silently destroy every authorized proposal it
stops, and failing every trade whenever a market-data provider blinks would make
an outage worse than the outage.

### Environment isolation

A proposal's `broker_environment` is recorded at creation and never changes.
Before any socket opens, **three independent facts must agree**: what the
proposal was created under, what the process is configured for, and what the
constructed provider actually points at. The adapter re-checks the third against
the command itself, so a mis-wired client refuses rather than transmits.

Beneath all of that,
`fk_execution_attempts_proposal_environment` — a composite foreign key into
`trade_proposals (id, broker_environment)` — makes it a *database* fact: a
worker that came up under a different `T212_ENV` cannot write an attempt row
that claims otherwise, whatever its configuration believes.

### Manual and automatic converge

Both reach `APPROVED` and both are transmitted by the same service; they differ
only in recorded provenance. There is no second broker path that could diverge.

Automatic execution against **live** additionally re-checks
`T212_AUTOMATED_TRADING_CONSENT_CONFIRMED` at *send* time and not only at
authorization: the operator's relationship with their broker can change between
the two, and API Terms clause 4.2(a) is about the moment an order is determined
and sent.

Transmission has its own gate, deliberately a superset of the live one:

| Setting | Gates |
|---|---|
| `T212_EXECUTION_ENABLED` | any transmission at all, demo included — default **false**, so deploying Phase 8 does not start sending |
| `EXECUTION_MODE=manual_approval` | `research_only` forbids transmission entirely |
| credentials present | including the `orders:execute` scope, which the broker enforces with HTTP 403 |
| the four live gates | added on top when `T212_ENV=live` |

`order_transmission_permitted` is defined as "no blockers remain", so the flag
and the explanation can never disagree.

### Kill switch and pause at send time

Checked three times, and the last one is the one that matters: the state is
re-read **from PostgreSQL inside the transaction that would write
`sent_to_broker`**. An operator who hits the switch while the preflight is
fetching a quote still stops that order.

If the switch arrives *after* transmission, nothing is cancelled. Trading 212
documents `DELETE /equity/orders/{id}`; StockBrain does not call it and has no
cancel path at all. Cancelling races a fill, its failure is a second unknown on
top of the first, and a kill switch that cancelled would be making a trading
decision rather than stopping one. No automatic liquidation, in any state.

### Reconciliation

Two honest ways out of "the order may or may not exist" — find it, or prove its
absence — and a third answer that matters just as much: **inconclusive**.

Matching is deliberately narrow, because the API supports no client reference:

* `initiatedFrom == "API"` — the strongest evidence available, and the reason a
  buy the operator placed on their phone can never be attributed to StockBrain;
* the exact broker ticker;
* the exact **signed** quantity as a `Decimal` — a buy for 3 and a sell for 3 are
  different orders;
* `type == "MARKET"`;
* `createdAt` inside a bounded window around `sent_at`;
* and an order id not already mirrored against another attempt.

More than one survivor means `MULTIPLE_CANDIDATES` and the attempt stays
ambiguous. Two identical API-initiated market orders on the same listing in the
same window are genuinely indistinguishable through this API; the limitation is
documented rather than papered over.

**Absence is only evidence under two conditions**: both read paths
(`GET /equity/orders` and `GET /equity/history/orders`) answered, *and* the
attempt is older than `EXECUTION_RECONCILE_MIN_AGE_SECONDS`. An order that
exists but has not yet propagated would otherwise be read as proof of its own
absence — and that conclusion releases a reservation and fails a proposal, so it
has to be earned. After `EXECUTION_RECONCILE_MAX_ATTEMPTS` inconclusive passes
the attempt stops being swept and waits for a person.

Nothing in the module can transmit. A test asserts it never names `submit`.

### Crash boundaries

| Crash point | State found afterwards | What happens |
|---|---|---|
| A — before the send flag | no attempt, proposal `APPROVED` | the sweep re-drives it; nothing was sent |
| B — after the flag commits, before the request | attempt `sent_to_broker`, `PENDING` | **reconcile**, never resend |
| C — during the send | same as B | reconcile |
| D — broker accepted, response not persisted | same as B | reconcile; the order is found by id or by match |
| E — response received, final commit lost | same as B | reconcile; an already-mirrored order is not double-claimed |

Boundary B is the one the ordering exists for, and the one that costs something:
the outcome is unknown even though nothing was sent. `recover_incomplete` sweeps
attempts stranded in `PENDING` past twice the order timeout and marks them
ambiguous — because a worker can die without the process doing so.

### The API and the GUI

Read-only, apart from one mutating route, and that route is
**reconciliation** — a read of the broker, safe to repeat.

There is no resend endpoint and there must not be one: a retry button on an
ambiguous attempt is a button that creates a second real position. Tests assert
that the served OpenAPI schema contains no path matching `retry`, `resend`,
`resubmit`, `place-order` or `submit-order`, and that the only mutating
`/execution` route is the reconciliation POST. Every mutating request body in the
system accepts at most a free-text reason and a boolean, with `extra="forbid"`.

The GUI shows the environment as a badge (live reads "LIVE — REAL MONEY"), the
attempt table with its outcome and error category, the broker-order mirror with
`initiated_from` and whether reconciliation discovered it, and — for an ambiguous
attempt — an unmissable **DO NOT RESEND** banner explaining that the order may
already exist. Telegram announces submitted, rejected, failed, ambiguous,
reconciled and confirmed through the existing notification architecture; the
ambiguous one is `CRITICAL` and shouts the same warning. No Telegram handler
gained broker logic, and a test asserts the package references no execution
module.

### What Phase 8 deliberately did not do

No cancel, amend, modify or replace path — the capability audit still finds
none, and `DELETE /equity/orders/{id}` is never called. No limit, stop or
stop-limit orders: each needs its own price revalidation and its own rate limit,
and shipping them alongside the first mutation this system has ever performed
would be two experiments at once. No generic retry, no retry middleware, and
exactly one HTTP `POST` to a broker anywhere in the codebase. No real-money
order: the single live verification was one share on the paper account.

### The live-readiness boundary

What must be true before `T212_ENV=live` is set deliberately. Phase 9 closed the
first two and reduced the third to a subscription decision; the rest are acts
only a person can perform.

| Requirement | State after Phase 9 |
|---|---|
| The **50 pending orders per ticker** functional limit | **enforced** — counted from the broker's list *and* the local ledger, larger wins, failed read refuses |
| The currency gap | **implemented** — typed FX, freshness and grade policy, drift envelope. Alpaca forex is **not entitled** on the current plan (measured HTTP 403), so an execution-grade rate needs a paid subscription or the reference-grade opt-in |
| Web authentication in front of the approval routes | **implemented**, on by default |
| IP-restricted API credentials | operator action, in the Trading 212 account settings |
| `orders:execute` granted knowingly on a **live** key, read-only key kept for everything else | operator action |
| `T212_WRITTEN_CONSENT_CONFIRMED` with consent actually obtained | operator action; the process refuses to start if the flag disagrees with the other gates |
| `T212_AUTOMATED_TRADING_CONSENT_CONFIRMED`, only if automatic authorization is genuinely wanted live | operator action |
| One tiny manual live order, placed by a human in the app first | operator action |
| Backup taken **and restored** | `scripts/backup.sh` + `scripts/restore-test.sh` |
| Zero unresolved ambiguous executions | query it; going live with an unknown order outstanding means a new position is indistinguishable from an old one |

The full list, in the order it is cheapest to fail, is
`docs/operations.md` → "Pre-live checklist".

No code flips from demo to live on its own, and none should be added that could.

---

## Phase 9 production hardening

Phase 9 adds no new capability to the trading pipeline. Everything in it exists
because something could cost money, lose an order, or be reached by somebody who
should not have reached it.

### The durable-ledger pattern, generalised

Phase 8 established one: `execution_attempts.sent_to_broker` is committed
**before** the HTTP request, so the row means "bytes may have left" rather than
"bytes left", and a process that dies mid-call leaves a pessimistic record
rather than an absent one.

Phase 9 applies the same shape to the other thing that spends real money.
`firecrawl_calls` is written and committed before each paid Firecrawl request,
carrying the worst-case cost computed from the published billing model, and is
reconciled afterwards against the provider's own `creditsUsed`. Three properties
follow that a counter cannot have:

* it survives a restart — the Phase 2 counter was an attribute on a client
  object, which is why nothing could stop 21 searches an hour;
* it can **refuse**, because the check and the insert happen in one transaction
  behind a transaction-scoped advisory lock, so two workers cannot both spend
  the last credit;
* an unreconciled row is *charged*, not forgiven. A call whose process vanished
  has committed the operator's money as far as anyone here can tell.

`credits_charged` is the single column the caps are compared against, so the
estimate→actual reconciliation happens in exactly one place. The daily and
monthly windows are computed by **PostgreSQL**, so a container with a drifting
system clock cannot hand itself a fresh day.

### Firecrawl is two stages, not one

A broad thematic search now asks for metadata only. That is enough for the
layers that are free: URL and content-hash deduplication, the deterministic
source-category table, and the cheap DeepSeek classifier that already scores
importance, confidence and materiality. Full article content is a *separate*
call, made only for a source whose event the classifier promoted to `CANDIDATE`,
and separately budgeted.

The alternative — `scrapeOptions` on the search — is what turned a 4-credit
search into a 24-credit one. Reusing the existing dedupe and the existing
classifier as the triage means no new crawler, no second dedupe implementation
and no additional LLM spend: the classification was going to happen anyway.

`sources.content_fetched_at` makes "do we already have the body" a fact on the
row rather than a guess from the length of `raw_content`, and the enrichment job
is idempotent against it — a redelivered job cannot pay twice for one page.

### Eligibility is a column

`discovery_queries.next_eligible_at` replaces `now - last_run_at < interval`
recomputed at read time. Three failures that arithmetic could not survive:

* **A restart re-ran everything.** `Scheduler._loop` runs each task once
  immediately on start, so every query whose last run predated its interval was
  enqueued at once. That is nine searches inside 23 seconds in the incident log,
  and ten restarts would have been ten sweeps.
* **A failure needs a different cooldown from a success.** Firecrawl bills a
  request it processed even when the answer was an error, so a failing query
  retried on the success cadence is a failing query that costs money.
* **The interval floor has to bind wherever the interval is used.** A topic row
  asking for twenty minutes gets `FIRECRAWL_MIN_TOPIC_INTERVAL_MINUTES`
  instead, so a restored backup or a hand-written `UPDATE` cannot reintroduce
  the Phase 2 cadence.

The scheduler claims the slot at enqueue time and the handler rewrites it on
completion, so two scheduler loops cannot queue one query twice even before
`uq_jobs_dedupe_key_active` is consulted.

### Foreign exchange: a rate is a measurement

`FxRate` carries base and quote currencies, the rate, the provider, the
provider's own timestamp, the receipt instant, and a **grade**. No function in
`stockbrain.fx` accepts or returns a bare `Decimal`, because a bare number
cannot be checked for freshness, attributed to a source, or audited afterwards.

Four rules make cross-currency sizing safe rather than merely possible:

* **Direction is explicit.** `rate` is units of `quote` per one unit of `base`.
  Conversion refuses any pair the rate was not measured for, and inverts
  arithmetically for the other direction — `amount / rate`, never
  `amount * (1/rate)`, so the round trip stays exact and a converted cap can be
  compared against the cap it came from.
* **No multi-hop.** GBP/USD plus EUR/USD does not become GBP/EUR. Two published
  rates multiplied together produce a number no source published and no
  counterparty will honour.
* **Grade is a property of the provider.** A live dealable quote and a daily
  central-bank fixing are both "the exchange rate" and are not interchangeable.
  Frankfurter's own documentation says it "is not for live trading";
  `FX_ALLOW_REFERENCE_GRADE` is how an operator says they have read that.
* **`FxSnapshot` cannot represent a silent 1.0.** Same-currency is its own
  state — `same_currency=True`, no rate, direction `IDENTITY`. There is no path
  through the type that converts two different currencies at parity.

**The caps move to the price, never the price to the caps.** Every `RISK_*` money
limit is denominated in the account currency and every price in the instrument's.
Converting the ceiling into the instrument's currency and dividing there is the
arithmetic that is defined; dividing a GBP ceiling by a USD ask is what Phase 6
refused to do and what `RISK_REQUIRE_SAME_CURRENCY=false` used to permit with
only a warning (bug 22).

FX is re-resolved and re-judged at generation, at authorization and again
immediately before transmission, and `fx_rate_drift` is the envelope: on a GBP
account buying USD, a one percent move in the pair moves the account-currency
notional by one percent — straight through the per-trade cap, the cash reserve
and the concentration limit, none of which were re-derived. Past the envelope the
proposal is invalidated and re-derived, **never silently resized**.

`fx_available` and `fx_freshness` are deliberately two rules, because the
send-time preflight treats a *missing* input as transient (the proposal survives
a provider outage) and a *stale* one as a deferral. `fx_rate_drift` is neither:
a rate that moved is a statement about the trade and retires the proposal, just
as a price that moved does.

### The pending-order limit is counted twice

Trading 212 documents 50 pending orders per ticker per account, and Phase 8's
live test proved orders really do queue. The count before every send is the
**larger** of the broker's own pending list for that ticker — the number the
limit is measured against, and the only one that includes orders the operator
queued by hand in the app — and StockBrain's own transmitted-but-unresolved
attempts for that ticker in that environment, which covers the window between a
successful POST and the broker's list catching up.

A read that failed is not a count of zero. `read_ok=False` refuses, for the same
reason reconciliation will not conclude "not placed" from one failed read: an
unknown standing in front of a non-idempotent POST resolves against sending.

`PENDING_ORDER_LIMIT` is its own failure category rather than a generic
preflight refusal, because the remedy is different — nothing about the trade is
wrong and the authorization is still good; the queue has to drain. It does not
invalidate the proposal.

### Deny by default at the HTTP edge

Specification section 19 required web authentication from the beginning and
Phase 8 shipped without it: seven state-changing routes, one of which transmits
a real broker order, reachable by anything that could open a socket.

The gate is **middleware with an explicit public allow-list**, not a dependency
on each route. A dependency has to be remembered on every new route; a
middleware has to be *un*-remembered, and the failure mode of forgetting is a
401 rather than an open door. A test enumerates the route table and asserts
every path is either allow-listed or behind the gate.

One account, one password (scrypt, from the standard library, parameters stored
with the hash), an HMAC-signed stateless session cookie, and three independent
CSRF defences — `SameSite=Strict`, a double-submit token the SPA echoes in a
header, and an `Origin` check. Three, for one reason: what is behind these
routes is an irreversible broker order, and each mechanism fails closed on its
own.

There is no session table, no registration, no password reset, no roles and no
token endpoint. Revocation is rotating `STOCKBRAIN_SECRET_KEY`, which is the
only mechanism a stateless design has and is documented as such.

A missing password hash is **not** a startup failure — it is a blocker, the same
shape as an empty Telegram allowlist. The process comes up, serves its liveness
and readiness probes, answers 503 with the reason on everything else, and grants
access to nothing. Refusing to start would leave an operator with no way to read
why.

### Alerts are rows, and silence is a feature

The alert scanner reads state and writes `Notification` rows; delivery is a job,
exactly as it is for a proposal transition. It imports no risk engine, no broker
client and no execution service — an alerting bug must not become a trading bug,
and a test asserts the import list.

Suppression is the `notifications.dedupe_key` unique index carrying a window
stamp, so a provider down for six hours produces one message rather than three
hundred and sixty, two workers cannot both announce, and a restart cannot
re-announce. A cleared condition sends one "resolved" message so the last thing
in the channel is the current state.

**A deliberately disabled provider is never an alert.** `DISABLED` is a
configuration statement and `DEGRADED` is the designed response to a provider
having a bad minute. Alerting on either would train the operator to ignore the
channel — which is the worst outcome available, because an ignored alert stream
looks like coverage.

### What Phase 9 deliberately did not do

* **No FX cache.** A rate is fetched when it is needed. Caching is how a stale
  number gets used after the check that would have rejected it. The one
  exception is a memo scoped to a single invalidation sweep, where every
  proposal is judged against the same instant anyway.
* **No FX fallback provider.** One configured source. Silently substituting a
  daily fixing when a live feed is down would change what a proposal means
  without changing anything a reader can see.
* **No cancel path, still.** Not for the kill switch, not for a full pending
  queue, not for expiry. Cancelling races a fill, and a kill switch that
  cancelled would be making a trading decision rather than stopping one.
* **No resend, retry or force path for an execution attempt.** Unchanged from
  Phase 8, and the pending-order check added a broker *read* in front of the
  send without adding a mutation.
* **No live Firecrawl call during the investigation.** The reconstruction is
  from `firecrawl_activity_logs.csv`, the job history, the `sources` table and
  the committed Phase 2 code.
