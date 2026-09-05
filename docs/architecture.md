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
                                  ONE non-retried broker POST   ← phase 8
                                               │
                                  reconciliation + audit log
```

Everything above the phase 8 line exists today. Authorization is *not*
execution: an `APPROVED` proposal records that deterministic risk allowed the
trade and which authority signed it off. No order-submission path exists yet.

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
  broker/            read-only T212 metadata, account state, automation policy
  risk/              deterministic engine, rules, sizing, spread, versioned config
  proposals/         state machine, generation, authorization, expiry, quotes
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
| `uq_trade_proposals_dedupe_key` | A redelivered generation job cannot create a second proposal. |
| `uq_trade_proposals_active_thesis` — partial unique over non-terminal statuses | One live proposal per published thesis. |
| `ck_trade_proposals_approved_requires_authorization_provenance` | An `APPROVED` proposal always says what authorized it, when, and as whom. |
| `ck_trade_proposals_system_auth_requires_automatic_policy` | A `SYSTEM_AUTOMATIC` authorization is only legal on a proposal generated under the AUTOMATIC policy — so changing the policy cannot retroactively authorize existing work. |
| `ck_trade_proposals_invalidated_requires_status` | An invalidation timestamp and the `INVALIDATED` status cannot disagree. |
| `pg_advisory_xact_lock(broker, account)` | Exposure accounting is serialised across tasks, processes and restarts — which an in-memory lock is not. |

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

### The Phase 8 execution boundary

Phase 6 stops at authorization. What phase 8 must preserve when it adds
transmission:

* `uq_execution_attempts_sent_once`, and setting `sent_to_broker` *before* the
  HTTP request;
* re-running the full authorization-time revalidation immediately before the
  POST, against a snapshot no older than the freshness limits;
* the `broker_environment` recorded on the proposal, so a demo proposal cannot
  execute against live;
* `EXECUTION_AMBIGUOUS` and no blind retry;
* `execution_policy` and `authorization_source` as the record of *who* permitted
  the trade, and the four live-execution gates as the separate record of whether
  transmission is permitted at all.
