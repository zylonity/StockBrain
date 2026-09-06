# StockBrain

Self-hosted, event-driven equity research and trade-proposal system.

StockBrain ingests financial news, SEC filings and thematic web search results,
deduplicates them into canonical events, classifies them with a cheap LLM,
researches shortlisted candidates with a multi-agent pipeline, applies a
**deterministic** risk engine, and produces a persistent trade proposal that
must be explicitly authorized — by a person, or by the system under an
explicitly permitted automatic policy — before any broker order is submitted.

The authoritative design document is
[`STOCKBRAIN_TECHNICAL_SPEC.md`](STOCKBRAIN_TECHNICAL_SPEC.md).

## Non-negotiable invariants

```
UNTRUSTED SOURCES → LLM RESEARCH → STRUCTURED THESIS → DETERMINISTIC RISK
→ PERSISTENT PROPOSAL → EXPLICIT HUMAN CONFIRMATION → FRESH REVALIDATION
→ ONE NON-RETRIED BROKER MUTATION → RECONCILIATION + AUDIT
```

* An LLM never receives broker credentials and never has a broker tool.
* The research layer recommends an action; the deterministic risk engine decides
  the quantity.
* Authorizing a proposal is **not** executing it. `APPROVED` records that the
  deterministic risk engine allowed the trade and which authority signed it off;
  no order-submission path exists yet.
* Authorization provenance is always recorded: `HUMAN_WEB`, `HUMAN_TELEGRAM` or
  `SYSTEM_AUTOMATIC`. Manual and automatic deployments converge on the same
  authorized representation and differ only in who signed.
* Automatic authorization is a **broker capability**, not a preference. Trading
  212's API Terms prohibit Algorithmic Trading, so live automatic mode requires
  a flag recording that the required written consent was actually obtained, and
  the process refuses to start without it. Demo (paper) is the supported path.
* Every broker order will require a two-stage human confirmation, in the web GUI
  or Telegram — both drive the same state machine and the same execution path.
* Trading 212's order POST is **non-idempotent**. StockBrain never retries a
  broker mutation. An ambiguous submission becomes `EXECUTION_AMBIGUOUS` and is
  resolved by reconciliation, never by resending.
* Trading 212 **live** execution is hard-disabled by default and cannot be
  enabled by accident (see [Execution gate](#execution-gate)).
* A model's ticker hint is a search key, never an identity. Only a Trading 212
  instrument the broker itself supplied can reach an order request, and an
  ambiguous listing — a share class, an ADR, a dual listing — blocks rather than
  resolving to whichever one looked likeliest.
* A price used to size an order must state its source and its age. Trading 212's
  own API data is display and reconciliation only; if no execution-grade quote
  is available, sizing is blocked rather than falling back to unsuitable data.

## Build status

| Phase | Scope | State |
|---|---|---|
| 1 | Skeleton, config, logging, full persistence model, health, GUI shell, Docker | **done** |
| 2 | Discovery ingestion (Alpaca news WS, web search, SEC EDGAR), dedupe, job queue | **done** |
| 3 | DeepSeek event classifier, semantic dedupe, LLM telemetry and budgets | **done** |
| 4 | Instrument resolution and market data | **done** |
| 5 | Pinned TradingAgents research engine | **done** |
| 6 | Risk engine, proposals, web approval | **done** |
| 7 | Telegram control, approvals and durable pause/kill switch | **done** |
| 8 | Trading 212 demo execution and reconciliation | **done** |
| 9 | Production hardening: cost control, FX, web auth, backups, alerts | **done** |
| 10 | Multi-provider web discovery: Brave + Exa search, local extraction, Firecrawl as fallback | **done** |

## Architecture

A modular monolith plus PostgreSQL. Two containers, nothing else:

```
stockbrain   FastAPI + job workers + scheduler + Alpaca news WS + Telegram polling
postgres     source of truth for every stage
```

There is no Redis, Celery, Kafka, Elasticsearch or vector database. Background
work runs on a PostgreSQL-backed queue using `FOR UPDATE SKIP LOCKED`.

See [`docs/architecture.md`](docs/architecture.md) for the module map and the
reasoning behind the boundaries, and [`docs/operations.md`](docs/operations.md)
for deployment and runbooks.

## Requirements

* Docker (Docker Desktop or OrbStack on macOS)
* Python 3.12+ and [uv](https://github.com/astral-sh/uv) for backend development
* Node 22+ for frontend development

## Quick start

```bash
git submodule update --init --recursive
cp .env.example .env
# Edit .env: set POSTGRES_PASSWORD, match it in DATABASE_URL, and generate a key:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"

docker compose -f compose.yaml -f compose.dev.yaml up -d --build

# Set the owner's password. Until you do, the container is healthy and every
# route except the two health probes answers 503 with that exact reason.
docker compose exec stockbrain python -m stockbrain.hash_password
# paste the printed WEB_OWNER_PASSWORD_HASH=... line into .env, then
docker compose up -d
```

Then open <http://127.0.0.1:8080> and sign in.

Verify:

```bash
curl -s localhost:8080/api/health | python3 -m json.tool
curl -s localhost:8080/api/health/providers | python3 -m json.tool
curl -s localhost:8080/api/v1/system/execution-status | python3 -m json.tool
```

A fresh install reports `DEGRADED` overall with most providers `DISABLED`. That
is correct: nothing is configured yet, and `DISABLED` is not a fault. Only
PostgreSQL being unavailable makes the application report `DOWN`.

Three things are **off by default** and each is a deliberate refusal rather
than an oversight:

| Default | Why |
|---|---|
| `BRAVE_API_KEY=` / `EXA_API_KEY=` empty | Web discovery is off until a key is set. Brave answers routine thematic search, Exa answers semantic second-order search, and neither falls back to the other. Read the budget in `.env.example` first. |
| `FIRECRAWL_ENABLED=false` | Firecrawl is now a *fallback extractor only* — its search path is removed. In Phase 2 it emptied a credit allowance in about an hour. It needs two switches, not one. |
| `FX_PROVIDER=none` | Cross-currency sizing stays blocked. An invented exchange rate is a wrong position size, silently. |
| `T212_EXECUTION_ENABLED=false` | Nothing transmits to a broker, demo included, until an operator says so. |

The compose file publishes the GUI on `127.0.0.1` only. Expose it on the LAN or
over Tailscale deliberately; never publish it to the public internet. PostgreSQL
is never published outside the compose network in the production configuration.

## Development

```bash
make setup          # backend venv + frontend dependencies
make up             # start the stack
make check          # format, lint, typecheck, test, dependency audit
make verify         # everything in `check`, plus image build and container health
make audit          # pip-audit + npm audit only
make help           # everything else
```

### Backend

```bash
cd backend
uv venv --python 3.12
uv pip sync requirements-dev.txt
uv pip install --no-deps -e .
```

Run the API against the compose database, with the frontend dev server proxying
to it:

```bash
# terminal 1
cd backend
DATABASE_URL='postgresql+asyncpg://stockbrain:<password>@127.0.0.1:5432/stockbrain' \
  LOG_FORMAT=console .venv/bin/python -m stockbrain.main

# terminal 2
cd frontend && npm run dev     # http://localhost:5173, proxies /api to :8080
```

The dev overlay (`compose.dev.yaml`) publishes PostgreSQL on `127.0.0.1:5432` so
local tooling can reach it. Do not use that overlay on TrueNAS.

### Tests

The test suite needs a real PostgreSQL, because several safety guarantees are
partial unique indexes that cannot be exercised against a mock or SQLite.
Integration tests skip — rather than silently pass — when no database is
configured.

```bash
docker compose exec postgres psql -U stockbrain -d postgres -c \
  'CREATE DATABASE stockbrain_test OWNER stockbrain;'

make test
```

The session fixture rebuilds the schema by running the real Alembic migrations,
so the tests exercise exactly what production applies.

### Migrations

```bash
make revision m="add discovery topic weights"   # autogenerate
make migrate                                    # apply to the running stack
```

The container entrypoint runs `alembic upgrade head` before starting the
application, so a failed migration stops the container instead of leaving a
half-configured process serving traffic. `GET /api/health/ready` returns 503 when
the applied revision does not match the code's head revision.

## Execution gate

Trading 212's API Terms prohibit "Algorithmic Trading" and require prior written
consent for using a customised interface in the live environment. StockBrain
treats these as hard product requirements.

A live order can only be submitted when **all four** of these hold:

```
T212_ENV=live
T212_LIVE_EXECUTION_ENABLED=true
T212_WRITTEN_CONSENT_CONFIRMED=true
EXECUTION_MODE=manual_approval
```

plus a two-stage human confirmation for that specific order, a fresh risk /
broker / quote revalidation, and the durable kill switch being off. The kill
switch and the pause are stored in PostgreSQL, restored at startup, and stop
every authorization path — web, Telegram and automatic — without ever closing a
position or cancelling a broker order.

Any contradictory combination — for example `T212_LIVE_EXECUTION_ENABLED=true`
with `T212_ENV=demo` — makes the process **refuse to start**, with exit code 78
(`EX_CONFIG`) and a single clear message rather than a stack trace. An ambiguous
execution configuration is never resolved in favour of live.

`live_execution_permitted` is defined as "no blockers remain", so the flag and
the GUI banner can never disagree; missing credentials count as a blocker. Check
a configuration without starting the app:

```bash
docker compose run --rm stockbrain check-config
```

Without written consent, StockBrain still ingests, classifies, researches,
proposes and alerts; you execute manually in the official Trading 212 app.

This is an engineering interpretation of the published API terms, not legal
advice.

## Security

* **The HTTP surface is deny-by-default.** Web authentication is on out of the
  box (specification section 19: *"If accessed only over LAN/Tailscale, still
  require auth"*). One account, one scrypt-hashed password held as a hash and
  never as a password, an HMAC-signed `HttpOnly` `SameSite=Strict` session
  cookie, and three independent CSRF defences on every state-changing route —
  `SameSite`, a double-submit token echoed in `X-StockBrain-CSRF`, and an
  `Origin` check. Enforcement is middleware with a five-path public allow-list
  (two health probes and the login flow), so a route added without a thought
  about authentication is protected rather than open, and a test enumerates the
  route table to prove it. Running without it needs
  `WEB_AUTH_ENABLED=false` *and* `WEB_TRUSTED_NETWORK_ACKNOWLEDGED=true`, which
  is refused in production without the acknowledgement.
* **Paid providers cannot run away.** Every metered call is reserved against a
  durable PostgreSQL ledger *before* the request leaves, so a restart cannot
  forget what today already cost and two workers cannot spend the last credit
  twice. Hard caps on searches, content fetches and estimated credits, per day
  and per month, plus a floor on how often a query may run. Nothing retries a
  paid call except Brave, which documents that failed requests are not billed.
  There is no automatic fallback from one paid provider to another.
* Secrets come from the environment or mounted files; `.env` is git-ignored and
  never baked into an image layer.
* Two independent log-redaction layers scrub credential-shaped keys and any
  configured secret value appearing in free text.
* All retrieved web/news/filing content is treated as untrusted data. It never
  becomes an instruction: prompts fence it in `<untrusted_document>` markers,
  state explicitly that instructions inside it are not to be followed, and
  substitution is single-pass so document text cannot introduce prompt
  structure. It is sanitised before rendering.
* Classification uses a text-in/text-out interface. Research can only read its
  supplied packet through an enumerated context tool. Neither has broker access,
  filesystem access or arbitrary network capability. Final model decisions are
  validated against a Pydantic schema before anything acts on them.
* Telegram authorises on numeric user IDs only — never usernames, which the
  owner can change, release, and let somebody else register. An empty allowlist
  authorises nobody and the bot does not start. Group chats are refused unless
  explicitly enabled *and* explicitly allowlisted.
* Approval tokens are opaque and stored only as SHA-256 hashes. Callback data is
  a 46-byte random reference carrying no proposal id, no order field and not even
  which button it is — the action lives on the server-side row. Order parameters
  are never accepted from a callback; they are re-read from the database under
  lock.
* Telegram approval is two-stage, and stage two calls the *same*
  `ProposalService.authorize` the web calls, with the same row lock, the same
  compare-and-swap and the same full revalidation. There is no Telegram-specific
  risk path to diverge from it, and a test asserts the package imports none.
* **Exactly one module can transmit a broker order**, and exactly one HTTP
  `POST` to a broker exists anywhere in the codebase. There is still **no
  cancel, amend or modify path at all**. The seven state-changing HTTP routes
  are approve, reject and cancel a proposal; pause, resume and the kill switch;
  and one reconciliation — which *reads* the broker. There is deliberately no
  resend route: Trading 212 documents the order endpoint as non-idempotent, so a
  retry button would create a second real position. No API path can reach a Trading 212
  mutation, and no order, amend or cancel method exists anywhere in the process
  to be reached: the two Trading 212 clients read instrument metadata and
  account state. Tests enumerate the route table, the OpenAPI schema and every
  module under `stockbrain/` to keep all three statements true.
* A mutating request body carries a free-text reason and nothing else. The
  ticker, side, quantity, price and account are re-read from the proposal row
  under lock — a client that could name a quantity would be a client that could
  size a trade.

### Phase 8 broker execution

* `sent_to_broker` is committed **before** the HTTP request, not after the
  response, and `uq_execution_attempts_sent_once` makes a second transmission
  per proposal impossible. A crash in the send window therefore leaves evidence
  that bytes may have left, and the next worker **reconciles rather than
  resends**.
* Failures are classified, not lumped: a complete HTTP response is definitive; a
  transport failure that provably preceded the request line is recoverable;
  everything else — including a 2xx whose body cannot be parsed — is ambiguous
  and reconciles. An ambiguous attempt is never retried and keeps its exposure
  reserved.
* Every check runs again immediately before the POST, from the same risk engine
  that guarded the authorization. A refusal says whether the proposal survives
  it, so a provider outage defers rather than destroying authorized work.
* Environment isolation is a database guarantee: a composite foreign key binds
  each attempt to its proposal's `broker_environment`, so a worker configured
  for the other environment cannot record an attempt at all.
* `T212_EXECUTION_ENABLED` defaults to **false**. Deploying the execution layer
  does not, by itself, send anything.

### Phase 7 Telegram and execution control

* The bot runs **long polling** inside the application process. No inbound port,
  no public TLS endpoint and no reverse proxy: StockBrain is never exposed to
  the internet to support Telegram. A webhook configured on the bot would
  silently stop polling, so the opt-in live test asserts none is.
* It never blocks anything else. A Telegram outage is supervised in its own
  task, backs off, and reports DEGRADED/DOWN; FastAPI, the job workers and the
  scheduler are unaffected. A missing token means the runtime is never built and
  the provider reads DISABLED.
* `/pause` and `/kill` are **durable**: they live in `app_settings`, are read
  from PostgreSQL on every consultation, and are restored and logged at startup.
  A process that crashed while halted comes back halted. Neither closes a
  position or cancels a broker order — there is no path to do so.
* `/resume` lifts a pause and deliberately does *not* release the kill switch;
  releasing it is an explicit second act.
* Untrusted text (company names, headlines, thesis sentences) is escaped for
  HTML parse mode and length-bounded before escaping, so it cannot inject
  formatting, links or buttons. Link previews are disabled.

### Phase 6 risk and proposals

A published thesis becomes a proposal only when the deterministic engine allows
it against a revalidated listing, fresh broker account state and a fresh
execution-grade quote. The Proposals page shows the company and listing, the
side, quantity, notional and currency, the reference price with the bid, ask,
spread in basis points, quote age and session, the thesis and its confidence,
and every risk rule with the value observed beside the threshold it was compared
against. Refusals are recorded in `risk_evaluations` and stay inspectable even
though they produce no proposal.

Limits live in one versioned object (`RISK_*` in `.env.example`); the version is
persisted with each proposal, so a decision stays auditable after the limits
change and a proposal generated under superseded limits is invalidated rather
than authorized against numbers nobody chose for it. The hard bid/ask ceiling
(`RISK_MAX_SPREAD_BPS`, default 50 bps) exists because a live overnight IEX book
showed a $33 spread on a $321 AAPL mid — the age check caught that one, but a
book that wide during regular hours would be fresh and still ruinous.

`EXECUTION_POLICY=manual` (default) requires a person to authorize each
proposal. `EXECUTION_POLICY=automatic` lets the system authorize proposals that
clear every rule, recording `SYSTEM_AUTOMATIC` provenance. Authorization sends
no broker order under either setting. See
[`docs/architecture.md`](docs/architecture.md) for the full contract.

## Licence

Proprietary. Not investment advice; no warranty of any kind.

### Phase 5 research

Initialize the pinned upstream checkout after cloning:

```bash
git submodule update --init --recursive
```

The Research page shows advisory decisions, evidence, analyst reports, provider
limitations and per-role usage. Classified candidates with a RESOLVED company
and instrument are queued automatically. `RESEARCH_ENABLED=false` disables new
research wiring; `RESEARCH_TIMEOUT_SECONDS` (600) and
`RESEARCH_MAX_OUTPUT_TOKENS` (3000 per call) bound work. Existing LLM budgets
apply. FRED is optional; DFF/DGS10 are the only configured series. No broker
mutation or trade approval is part of this phase.

To enqueue an existing resolved impact manually:

```bash
docker compose exec stockbrain python -m stockbrain.intelligence.research_cli IMPACT_UUID
```

To intentionally create a new version, append `--rerun-id NEW_UUID`; reuse the
same UUID if the command is interrupted. Failed/interrupted paid runs are not
automatically repeated because provider spend can be uncertain. Read-only API:
`GET /api/v1/research`, `/{run_id}`, and `/health`; the list accepts `event_id`,
`status`, `limit`, and `offset` filters.

Minimal live checks remain explicitly opt-in:

```bash
cd backend
.venv/bin/pytest -m live -s tests/integration/test_research_live.py
```

They read credentials from the environment or root `.env`, perform a tiny Flash
thinking/tool continuity flow and one FRED DFF query, and print no secrets.
