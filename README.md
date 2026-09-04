# StockBrain

Self-hosted, event-driven equity research and trade-proposal system.

StockBrain ingests financial news, SEC filings and thematic web search results,
deduplicates them into canonical events, classifies them with a cheap LLM,
researches shortlisted candidates with a multi-agent pipeline, applies a
**deterministic** risk engine, and produces a persistent trade proposal that a
human must explicitly approve — twice — before any broker order is submitted.

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
* Every broker order requires a two-stage human confirmation, in the web GUI or
  Telegram — both drive the same state machine and the same execution path.
* Trading 212's order POST is **non-idempotent**. StockBrain never retries a
  broker mutation. An ambiguous submission becomes `EXECUTION_AMBIGUOUS` and is
  resolved by reconciliation, never by resending.
* Trading 212 **live** execution is hard-disabled by default and cannot be
  enabled by accident (see [Execution gate](#execution-gate)).

## Build status

| Phase | Scope | State |
|---|---|---|
| 1 | Skeleton, config, logging, full persistence model, health, GUI shell, Docker | **done** |
| 2 | Discovery ingestion (Alpaca news WS, Firecrawl, SEC EDGAR), dedupe, job queue | **done** |
| 3 | DeepSeek event classifier, semantic dedupe, LLM telemetry and budgets | **done** |
| 4 | Instrument resolution and market data | not started |
| 5 | Pinned TradingAgents research engine | not started |
| 6 | Risk engine, proposals, web approval | not started |
| 7 | Telegram bot | not started |
| 8 | Trading 212 demo execution and reconciliation | not started |
| 9 | Live readiness | not started |

## Architecture

A modular monolith plus PostgreSQL. Two containers, nothing else:

```
stockbrain   FastAPI + job workers + scheduler + (later) Alpaca WS + Telegram
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
cp .env.example .env
# Edit .env: set POSTGRES_PASSWORD, match it in DATABASE_URL, and generate a key:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"

docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

Then open <http://127.0.0.1:8080>.

Verify:

```bash
curl -s localhost:8080/api/health | python3 -m json.tool
curl -s localhost:8080/api/health/providers | python3 -m json.tool
curl -s localhost:8080/api/v1/system/execution-status | python3 -m json.tool
```

A fresh install reports `DEGRADED` overall with most providers `DISABLED`. That
is correct: nothing is configured yet, and `DISABLED` is not a fault. Only
PostgreSQL being unavailable makes the application report `DOWN`.

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
uv pip install -e ".[dev]"
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
broker / quote revalidation, and the kill switch being off.

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

* Secrets come from the environment or mounted files; `.env` is git-ignored and
  never baked into an image layer.
* Two independent log-redaction layers scrub credential-shaped keys and any
  configured secret value appearing in free text.
* All retrieved web/news/filing content is treated as untrusted data. It never
  becomes an instruction: prompts fence it in `<untrusted_document>` markers,
  state explicitly that instructions inside it are not to be followed, and
  substitution is single-pass so document text cannot introduce prompt
  structure. It is sanitised before rendering.
* LLM providers are reached through a text-in/text-out interface with no tools,
  no broker access, no filesystem access and no arbitrary network capability.
  Every model response is validated against a Pydantic schema before anything
  acts on it.
* Telegram authorises on numeric user IDs only — never usernames, which the
  owner can change. An empty allowlist authorises nobody.
* Approval tokens are opaque and stored only as SHA-256 hashes. Order parameters
  are never accepted from a callback; they are re-read from the database under
  lock.

## Licence

Proprietary. Not investment advice; no warranty of any kind.
