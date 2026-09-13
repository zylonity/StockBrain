# StockBrain

StockBrain reads the news, decides whether a story matters to a company you can trade, researches it, sizes a position, and asks you on Telegram before anything is bought or sold. Once you hold something, it watches the position and asks you again when it thinks you should get out.

It runs on your own machine against a Trading 212 account (demo or live). Nothing is ever sent to the broker without a human tapping **approve** — and every decision it makes, including the ones it refuses to act on, is written down with its reasons.

---

## How it works

```
news · SEC filings · web search
            │
            ▼
   dedupe → classify (cheap LLM: does this matter, and to whom?)
            │
            ▼
   research (multi-agent debate)  →  a thesis: BUY / SELL / HOLD, confidence, horizon
            │
            ▼
   risk engine (deterministic, no LLM) → size it, or refuse it and say why
            │
            ▼
   trade proposal  →  Telegram / web: approve or reject
            │
            ▼
   broker order  →  reconciliation  →  position
            │
            ▼
   exit rules watch the position → SELL / REDUCE proposals back to you
```

Two things are worth knowing about that picture:

**The LLM never touches money.** It classifies stories and argues about companies. Everything downstream — how much to buy, whether the quote is fresh enough, whether you already hold too much, whether the market is even open — is plain arithmetic with a written rule for every refusal. A research result that says BUY with 68% confidence against a 70% floor is refused, and you get a message saying exactly that.

**Getting out is rules, not opinions.** Once you hold a position the system checks it every few minutes against six rules, in a fixed order: a hard stop from cost, a volatility-scaled floor (ATR, from free daily bars), a trailing floor once the position has run, a newer research thesis superseding the one you bought on, a profit target that shrinks the longer the thesis takes, and the thesis's own time horizon running out. The first one that fires becomes a SELL proposal in your approval queue. You can see every position's floors on the Portfolio page, in `/positions`, and in a daily Telegram summary.

## What you get

- **Web GUI** at `http://127.0.0.1:8080` — dashboard, events, research, proposals, portfolio with exit floors, logs, system health, settings.
- **Telegram bot** — approve or reject trades; `/portfolio`, `/positions`, `/proposals`, `/events`, `/status`; `/pause`, `/resume`, `/kill`. Notification categories you can switch on or off individually, plus a once-a-day summary.
- **A full audit trail** — every proposal carries the quote it was priced on, the exchange rate, the risk rules that passed and failed, and the research it came from.

## Running it

You need Docker and a Trading 212 account. Everything else is optional and off until you add a key.

```bash
git submodule update --init --recursive
cp .env.example .env
# Edit .env: set POSTGRES_PASSWORD (and the same value inside DATABASE_URL),
# and generate a secret key:  python -c "import secrets; print(secrets.token_urlsafe(48))"

docker compose -f compose.yaml -f compose.dev.yaml up -d --build

# Set the owner's password for the web GUI:
docker compose exec stockbrain python -m stockbrain.hash_password
# paste the printed WEB_OWNER_PASSWORD_HASH=... line into .env, then
docker compose up -d
```

Open <http://127.0.0.1:8080> and sign in. A fresh install shows most providers as `DISABLED` — that is correct, nothing is configured yet.

### The switches that matter

`.env.example` documents every setting with its default and what it does. These are the ones that decide whether the system can act:

| Setting | Default | What it means |
|---|---|---|
| `T212_ENV` | `demo` | Paper account. Start here. |
| `T212_EXECUTION_ENABLED` | `false` | Nothing is sent to the broker, demo included, until you say so. |
| `EXECUTION_POLICY` | `manual` | A human approves every trade. |
| `EXIT_SWEEP_ENABLED` | `false` | Turn on the exit rules for positions the system bought. |
| `VOLATILITY_REFRESH_ENABLED` | `false` | Turn on the daily ATR fetch that feeds the volatility floor. |
| `RISK_*` | conservative | Position sizing. The defaults assume a five-figure account; for a small one, raise the percentages and allow fractional shares or nothing will clear the minimum trade size. |

Anything that changes what the system may do requires a restart. There is deliberately no way to change these from the web page.

### Costs

Classification and research use DeepSeek. A typical day of a few dozen stories costs well under a dollar. The exit rules, sizing, and everything after research cost nothing. Web discovery (Brave, Exa, Firecrawl) is off by default because it is the part that can run away with a credit balance.

## Developing

```bash
make setup     # backend venv + frontend dependencies
make up        # start the stack with the dev overlay (publishes Postgres on 127.0.0.1:5432)
make check     # format, lint, typecheck, test, dependency audit
make help      # everything else
```

Tests need a real PostgreSQL because several safety guarantees are partial unique indexes: `docker compose exec postgres psql -U stockbrain -d postgres -c 'CREATE DATABASE stockbrain_test OWNER stockbrain;'` once, then `make test`. The suite runs the real migrations.

To run the API and frontend outside Docker, see the Development section of [`docs/operations.md`](docs/operations.md).

## Where to read next

- [`docs/operations.md`](docs/operations.md) — running it day to day: every setting, what each Telegram message means, how to read the exit floors, backups, what to do when something is degraded.
- [`docs/architecture.md`](docs/architecture.md) — how each subsystem is built and why: the job queue, dedupe, the risk rules, the execution state machine, the exit policy.
- [`STOCKBRAIN_TECHNICAL_SPEC.md`](STOCKBRAIN_TECHNICAL_SPEC.md) — the original design document. Long; the two above are the maintained ones.
- [`docs/superpowers/plans/`](docs/superpowers/plans/) — the implementation plans for recent features, with the decisions recorded.

## What it is not

It is not autonomous, and Trading 212's API terms forbid making it so without their written consent — the code refuses to start in an automatic live configuration without that consent recorded. It is not investment advice. Research confidence is a model's ranking, not a probability. Run it on demo until you have watched it be wrong a few times.

## Licence

Proprietary. Not investment advice; no warranty of any kind.
