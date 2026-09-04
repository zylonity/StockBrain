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

  ingestion/         (phase 2)
  intelligence/      (phase 3, 5)
  market_data/       (phase 4)
  risk/              (phase 6)
  telegram/          (phase 7)
  broker/            (phase 8)
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

## Untrusted content

Everything retrieved from the web, a news feed or a filing is data, never
instruction. Evidence is wrapped in explicit document markers, the system prompt
states that instructions inside documents are not to be followed, and rendered
content is sanitised. LLM agents are never given credentials or a broker tool,
so a successful prompt injection has no execution path to reach.

## Deferred by design

OpenBB, NautilusTrader, Redis, Celery, Kafka, Elasticsearch, a vector database
and a self-hosted crawler are extension points, not initial dependencies. Each
would be added only against a concrete blocker, and none has appeared.
