# Operations

## Deployment targets

| Environment | Host | Notes |
|---|---|---|
| Development | macOS + Docker/OrbStack | `compose.yaml` + `compose.dev.yaml` |
| Production | TrueNAS SCALE | `compose.yaml` only |

## TrueNAS deployment

1. Create datasets for the two volumes and point the compose volumes at them as
   bind mounts, so ZFS snapshots cover the data:
   * `postgres_data` → PostgreSQL data directory
   * `stockbrain_data` → application working data
2. Place `.env` on the host with root-only permissions (`chmod 600`). Never bake
   secrets into an image layer.
3. Deploy the stack. `compose.yaml` publishes the GUI on `127.0.0.1:8080`; front
   it with the reverse proxy or Tailscale interface you actually want, and leave
   PostgreSQL unpublished.
4. Do not apply `compose.dev.yaml` in production — it publishes PostgreSQL.

### Networking

```
StockBrain GUI    LAN or Tailscale only
Telegram          outbound HTTPS long polling; no inbound port required
External APIs     outbound HTTPS/WSS
PostgreSQL        Docker internal network only
```

Long polling is used deliberately so that no inbound port, public TLS endpoint
or reverse proxy is needed for the bot. Do not expose the GUI publicly merely to
support Telegram.

## Startup

The entrypoint runs in this order, and the order matters:

1. **validate configuration** — fails fast with exit code **78** (`EX_CONFIG`)
   and a single clear message. A misconfiguration is never transient, so it must
   not be retried inside the database-wait loop;
2. **wait for PostgreSQL** — this one genuinely is transient;
3. **`alembic upgrade head`** — a failed migration stops the container;
4. **start the application**.

Validate a configuration without starting anything:

```bash
docker compose run --rm stockbrain check-config
```

It prints the execution posture and every remaining blocker, and returns 78 on
an invalid configuration. Useful as a pre-deploy gate.

The application then:

1. marks unconfigured providers `DISABLED`;
2. checks PostgreSQL connectivity;
3. compares the applied migration revision against the code's head;
4. **probes every enabled provider** and persists what came back (see below);
5. logs the live-execution posture and its blockers;
6. **restores and logs the durable control state.** A process that crashed while
   paused or killed comes back paused or killed, and says so at WARNING. Silent
   resumption after a crash is the failure that state exists to prevent;
7. starts Telegram long polling, if it is configured, in a supervisor task —
   never blocking the HTTP server or the job workers;
8. **sweeps execution attempts stranded mid-flight.** An attempt recorded as
   sent with no result is marked `EXECUTION_AMBIGUOUS` and queued for
   reconciliation. It is never resent.

### The startup provider probe

`STARTUP_PROBE_ENABLED` (default `true`) actively tests each enabled provider
once, concurrently, and records the result. It exists because every provider
except PostgreSQL used to reach `HEALTHY` only as a side effect of doing real
work, so a healthy restarted container reported `DEGRADED` — `overall_status`
degrades on any `UNKNOWN` — until something incidentally exercised each one.

**No probe makes a billable request.** The metered providers are checked for
credential validity instead, using a request the provider is obliged to reject:

| Provider | Probe | Cost |
|---|---|---|
| Brave | search with `q` omitted → 422; a rejected token says `SUBSCRIPTION_TOKEN_INVALID` | $0 |
| Exa | search with an empty body → 400; a rejected key says 401 | $0 |
| LLM (any provider) | `GET /models` on the configured base URL | free |
| Trading 212 | `GET /equity/account/summary` — one per 5s, rather than the metadata endpoints the instrument sync needs (one per 30s) | free |
| Alpaca market data | the existing capability probe | free |
| SEC | `company_tickers()` | free |
| FRED | the two configured macro series | free |
| Local extraction | reports readiness; needs no credential and no third party | free |

That is deliberate rather than thrifty: `EXA_MAX_SEARCHES_PER_DAY` defaults to
**3**, so probing by searching would let a handful of restarts spend the entire
daily semantic allowance on health checks and leave the real queries deferring
— a health check that breaks the feature it reports on.

A credential probe reports `HEALTHY` with the detail `credentials accepted; no
billable request made`, so the board never implies a search happened when one
did not. For end-to-end proof, run the opt-in live test — one real search per
provider, on a human's decision:

```bash
pytest -m live -s tests/integration/test_web_discovery_live.py
```

Two providers are not probed. **Firecrawl** and anything else already
`DISABLED` is skipped, since probing a switched-off provider spends to learn
nothing. **Alpaca news** is a websocket rather than a request/response call; it
shows `UNKNOWN "connecting"` and promotes itself within seconds of the stream
landing, so blocking startup on it would trade a real signal for a slower boot.

A probe can never fail or stall a boot: every exception is classified rather
than raised, and each probe is capped by `STARTUP_PROBE_TIMEOUT_SECONDS`
(default 10). A credential or entitlement rejection records `DOWN` — a human
must change configuration; a timeout, rate limit or transport failure records
`DEGRADED` — it may well fix itself.

## Health

| Endpoint | Question | Failure behaviour |
|---|---|---|
| `GET /api/health/live` | Is the process up? | Container healthcheck. Does not touch the database, so a database blip does not restart a working app. |
| `GET /api/health/ready` | Can it serve? | 503 when PostgreSQL is down or the schema revision does not match. |
| `GET /api/health` | What works? | Always 200; reports per-subsystem status. |

`/api/health`, `/api/health/ready` and `/api/health/providers` **actively probe**
PostgreSQL rather than replaying a status recorded at startup, with a 2-second
freshness window so a polling dashboard does not become one query per request.
A probe that does not answer within 5 seconds counts as DOWN. The status
recovers on its own once the database returns. `/api/health/live` deliberately
does *not* touch the database, so a database blip cannot restart a working
container.
| `GET /api/health/providers` | Per-provider detail | Includes the reason a provider is disabled or degraded. |
| `GET /metrics` | Prometheus text | No scraper is required. |

Statuses: `HEALTHY`, `DEGRADED`, `DOWN`, `DISABLED` (off or unconfigured — not a
fault), `UNKNOWN` (configured, not yet checked).

Expected combinations:

```
Alpaca news DOWN + Brave HEALTHY       → discovery DEGRADED, application DEGRADED
Trading 212 DOWN                       → execution unavailable, research healthy
PostgreSQL DOWN                        → application DOWN / not ready
```

## Web authentication

Specification section 19: *"do not expose the UI unauthenticated"*, *"secure
cookies"*, *"CSRF protection for state-changing operations"*, *"SameSite cookie
settings"*, and — the sentence that matters most here — *"If accessed only over
LAN/Tailscale, still require auth."*

`WEB_AUTH_ENABLED=true` by default. Set the owner's password once, inside the
container so the hash is produced by the same scrypt parameters that will verify
it:

```bash
docker compose exec stockbrain python -m stockbrain.hash_password
# paste the printed WEB_OWNER_PASSWORD_HASH=... line into .env, then
docker compose up -d
```

With no hash set the process still starts and still answers `/api/health/live`
and `/api/health/ready`; **every other route answers 503** naming the missing
hash. That is deliberate: a fresh deployment must come up so an operator can
read the reason, and must grant access to nothing while it waits.

| Property | Value |
|---|---|
| Session | HMAC-signed cookie `sb_session`, `HttpOnly`, `SameSite=Strict`, 12h |
| CSRF | readable cookie `sb_csrf`, echoed in `X-StockBrain-CSRF` on every POST |
| Origin | a state-changing request with a foreign `Origin`/`Referer` is refused |
| Public paths | `/api/health/live`, `/api/health/ready`, and the three `/api/v1/auth/*` routes. Nothing else. |
| Revocation | rotate `STOCKBRAIN_SECRET_KEY` — it invalidates every outstanding session |

Three independent CSRF defences (`SameSite`, double-submit token, origin check)
for one reason: what is behind these routes is an irreversible broker order.

`GET /api/v1/system/web-security` reports the posture rather than assuming it. A
deployment that *thinks* it has authentication and does not is the failure that
endpoint exists to make visible.

**Running without it.** Supported, and only as a recorded decision: set
`WEB_AUTH_ENABLED=false` *and* `WEB_TRUSTED_NETWORK_ACKNOWLEDGED=true`. In
production the second is required — the process refuses to start otherwise —
because "we have an authenticating reverse proxy" and "nobody ever set a
password" look identical from the code's side. Every start logs
`web_authentication_not_protecting` with the reason.

## Web discovery cost control

Three providers can spend real money here, and in Phase 2 one of them did with
nobody watching. See `docs/sources.md` for the full incident reconstruction and
for every price quoted below; the operational summary:

* **29 searches in 62 minutes** on 2026-09-05 (`firecrawl_activity_logs.csv`),
  then HTTP 402 for 127 consecutive jobs.
* Steady state under that configuration was **21 searches/hour → 504/day**, at
  roughly **24 credits each** (4 for a 20-result search plus 1 per scraped
  page) — about **12,000 credits/day** against a 1,000-credit monthly allowance.
* Nothing in the system could have stopped it: the only accounting was an
  integer on a client object and a Prometheus counter.

### Who does what

| Role | Provider | Setting | Price (verified 2026-09-05) |
|---|---|---|---|
| Routine thematic web/news search | **Brave** | `WEB_DISCOVERY_ROUTINE_PROVIDER=brave` | $5 / 1,000 requests; $5/month free credit |
| Semantic second-order search | **Exa** | `WEB_DISCOVERY_SEMANTIC_PROVIDER=exa` | $7 / 1,000 requests; $10/month free credit |
| Page extraction | **local** (trafilatura) | `CONTENT_EXTRACTION_ENABLED=true` | free |
| Difficult-page fallback | **Firecrawl** | `FIRECRAWL_FALLBACK_EXTRACTION_ENABLED` | 1 credit / page |

Fast financial news is **not** on this list: Alpaca news and SEC EDGAR handle
time-critical discovery and both are unmetered by comparison. Web discovery is
for thematic and second-order developments a finance-only feed never carries.

**There is no automatic fallback between the paid providers.** If Brave is
unavailable, routine queries defer — they are not re-run on Exa. That is
deliberate: `Brave fails → Exa called → Firecrawl scrapes → retries` is a cost
multiplier triggered by an outage, at the moment a budget is least able to
absorb it.

### Getting the keys

* **Brave** — <https://api-dashboard.search.brave.com/>. The "Search" plan. A
  card is required for identity verification even on the credit-funded tier;
  Brave states it will not be charged. Check the current terms for whether the
  $5 monthly credit carries an attribution requirement (see the discrepancy
  recorded in `docs/sources.md`). Set `BRAVE_API_KEY`.
* **Exa** — <https://exa.ai/>. New accounts get $20 of credit and the free tier
  adds $10/month. Set `EXA_API_KEY`.
* **Firecrawl** — only if you want the paid fallback. Optional, and off.

A provider with no key is simply DISABLED and says so; the rest of the
application is unaffected.

### What protects the budget

| Control | Default | Where it lives |
|---|---|---|
| `WEB_DISCOVERY_ENABLED` | true | one switch turns off both search backends |
| Routine cadence floor | 360 min | applied where the interval is *used*, so a topic row cannot go faster |
| Semantic cadence floor | 1440 min | ten times the price for an answer that moves over weeks |
| `BRAVE_MAX_SEARCHES_PER_DAY` | 12 | durable ledger, not a process counter |
| `BRAVE_MAX_SEARCHES_PER_MONTH` | **320** | the binding one: the free credit is monthly |
| `EXA_MAX_SEARCHES_PER_DAY` | 3 | a semantic answer moves over weeks |
| `EXA_MAX_SEARCHES_PER_MONTH` | **70** | ≈$0.49 at the published price |
| `CONTENT_EXTRACT_MAX_PER_DAY` | 60 | free, but a runaway sweep is still a runaway request rate |
| `FIRECRAWL_ENABLED` | **false** | and `FIRECRAWL_FALLBACK_EXTRACTION_ENABLED` must *also* be true |
| `FIRECRAWL_MAX_SCRAPES_PER_DAY` | 5 | only after a free attempt failed on a shortlisted URL |
| `FIRECRAWL_MONTHLY_CREDIT_CAP` | 200 | inside a 1,000-credit allowance |
| Retries | none, except Brave | Brave documents that failed requests are not billed; nothing else does |

**Ceiling if every cap were reached every single day: $2.09/month**, against
$15/month of free allowance. Expected steady-state spend at the shipped
defaults — five routine queries at 12-hour intervals, the semantic topic
disabled, Firecrawl off — is **$0.00**.

### The cheap-first pipeline

A search asks for **metadata only** — title, URL, snippet, date. That is enough
for URL deduplication, the deterministic source-category rules and the cheap
DeepSeek classifier. A page body is fetched separately, one page at a time, and
only for a source whose event the classifier promoted to `CANDIDATE`.

Passing `scrapeOptions` to a broad search was the single largest contributor to
the incident — it turned a 4-credit search into a 24-credit one. Exa's
`contents` is the same trap under another name, and `EXA_FETCH_CONTENTS` exists
so that switching it on is visible. Both are off, and no scheduler sets either.

Extraction runs **locally and free** for essentially every page. The paid
Firecrawl fallback runs only when all of the following hold: the local attempt
failed for a reason a different fetcher could plausibly fix (an HTTP error, a
transport failure, or a 200 with almost no text — *not* a refusal or a PDF);
both Firecrawl switches are on; the durable budget grants a reservation; and the
URL has not been attempted before. One attempt per URL, ever, and no retry.

### Enabling semantic discovery

The `second_order_exposure` topic ships **disabled** — it is the one that costs
1.4× a Brave search. To turn it on:

```sql
UPDATE discovery_topics SET enabled = true WHERE slug = 'second_order_exposure';
```

Four queries at a 24-hour cadence is 4 searches/day ≈ $0.028/day ≈ $0.87/month,
which is inside the daily cap of 3 only if you also raise `EXA_MAX_SEARCHES_PER_DAY`
or disable some of the queries. Decide which; the sweep will otherwise spend the
day's allowance on the highest-priority queries and defer the rest, which is a
safe outcome but probably not the intended one.

### Reading the state

**Budget exhaustion is not a failure.** The provider reports
`BUDGET_EXHAUSTED` — a distinct status from `DEGRADED`, because a spending limit
doing its job and a broken provider need different actions — and Alpaca news,
SEC EDGAR, classification, research and broker reconciliation all keep running.
The daily and monthly windows reset on the **database** clock.

```bash
curl -s localhost:8080/api/v1/discovery/status \
  | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["web_discovery"], indent=2))'
```

It reports, per provider: status, blockers, searches and units used today and
this month, the caps, remaining headroom, estimated cost, last call, last
success and last error. Per query: kind, resolved provider, last run, last
success, `next_eligible_at`, effective interval, consecutive failures and
lifetime units. And for extraction: how many pages were read today, how many
locally, how many through the paid fallback, and how many produced nothing.

No API key appears in any of it.

## Foreign exchange

The account is **GBP**; the priced universe is **USD**. Phase 6 measured 14 of
14 live positions in another currency, so `currency_alignment` blocked
everything StockBrain could price. Phase 9 lifts that block only behind a
verified rate — never by inferring one.

| `FX_PROVIDER` | Grade | Notes |
|---|---|---|
| `none` *(default)* | — | cross-currency sizing stays blocked. Safe, and the Phase 6 behaviour. |
| `alpaca` | execution | `/v1beta1/forex/latest/rates`, live bid/mid/ask, instant timestamps. **Measured 2026-09-05: HTTP 403 `insufficient grants`** on the plan that covers IEX equity data. |
| `frankfurter` | reference | central-bank fixings, no key, no quota. Its own docs say it "is not for live trading", so it requires `FX_ALLOW_REFERENCE_GRADE=true`. |

Enabling cross-currency sizing needs **two** settings —
`RISK_REQUIRE_SAME_CURRENCY=false` and a real `FX_PROVIDER`. The pairing of
`false` with `none` is refused at startup: through Phase 8 it produced a warning
and a quantity computed by dividing a GBP cap by a USD ask.

Every proposal records the pair, the rate, the direction it was applied in, the
provider, the grade, the provider's timestamp and the age. The rate is
re-resolved and re-judged at **generation**, at **authorization** and again
**immediately before transmission**; missing, stale, wrong-pair or
untrusted-grade all block. `FX_MAX_RATE_DRIFT_PCT` is the envelope: past it the
proposal is invalidated and re-derived, never silently resized.

```bash
curl -s localhost:8080/api/v1/system/fx | python3 -m json.tool
```

Central banks do not publish at weekends, so a reference rate is dated the
previous Friday on a Monday morning and cross-currency sizing blocks until the
next fixing. Equity markets are closed then anyway.

## Trading 212 pending-order limit

Trading 212 documents a functional limit of **50 pending orders per ticker per
account**, and the Phase 8 demo verification made it reachable: a market order
placed while the market was closed returned HTTP 200 with status `NEW` and sat
in the queue.

Before every send, StockBrain takes the **larger** of two counts:

1. the broker's own pending list for that ticker — the number the limit is
   actually measured against, and the only one that includes orders queued by
   hand in the app;
2. its own transmitted-but-unresolved attempts for that ticker in that
   environment — the window between a successful POST and the broker's list
   catching up, plus anything ambiguous.

At or past `50 − T212_PENDING_ORDER_HEADROOM` (45 by default) the send is
refused with `PENDING_ORDER_LIMIT`. **A failed read is not a count of zero** and
also refuses: an unknown in front of a non-idempotent POST resolves against
sending. The refusal does not invalidate the proposal — a full queue is a
condition of the moment, and the authorization is still good once it drains.

## Backups

The database is the entire audit trail: every event, every classification, every
research run, every risk evaluation, every authorization and — the part that
cannot be reconstructed from anywhere — **every execution attempt and its
outcome**. A ZFS snapshot of a running PostgreSQL data directory is a
crash-consistent copy; it is usually recoverable and it is not a backup.

```bash
scripts/backup.sh                     # -> ./backups/stockbrain-<UTC>.dump
scripts/restore-test.sh backups/stockbrain-20260905T204514Z.dump
```

`backup.sh` runs `pg_dump -Fc` *inside* the container (so the host needs no
PostgreSQL client and the database stays unpublished), writes to a `.partial`
file and renames only on a clean exit, verifies the archive is listable, and
prunes dumps older than `BACKUP_RETAIN_DAYS` (14).

`restore-test.sh` restores into a throwaway database, reports the schema
revision against the code's head, counts the tables that matter, **checks that
the critical unique indexes survived the round trip** — a restore that silently
dropped `uq_execution_attempts_sent_once` would look fine and would permit a
second transmitted attempt for one proposal — and reports how many ambiguous or
unresolved execution attempts the backup contains. It drops the database on exit,
including on failure.

Schedule it on the host, not in the container:

```cron
# 03:17 UTC daily. Staggered off the hour so it does not collide with anything.
17 3 * * * cd /mnt/tank/apps/stockbrain && ./scripts/backup.sh >> /var/log/stockbrain-backup.log 2>&1
# Weekly proof that the newest dump is restorable.
41 4 * * 0 cd /mnt/tank/apps/stockbrain && ./scripts/restore-test.sh "$(ls -t backups/*.dump | head -1)" >> /var/log/stockbrain-backup.log 2>&1
```

Retention: 14 daily dumps on the host, plus TrueNAS dataset snapshots of the
`backups` dataset for longer history. `./backups/` is git-ignored — a directory
of dumps is the audit trail in plain form and must never reach a commit.

## Disaster recovery

The ordering below is the whole point. **Reconcile before you enable
execution**, every time.

### Application or container failure

`restart: unless-stopped` brings it back. On start it restores the durable pause
and kill switch (a process that crashed while halted comes back halted and says
so at WARNING), and sweeps execution attempts stranded mid-flight: an attempt
recorded as sent with no result becomes `EXECUTION_AMBIGUOUS` and is queued for
reconciliation. **It is never resent.**

Nothing in a restart re-runs a paid provider call. Query eligibility is a
committed `discovery_queries.next_eligible_at` column, so ten restarts in a row
spend nothing; every provider budget is a table, so a restart does not forget
what today already cost.

### Host restart

Same as above; compose brings both containers up in dependency order and the
entrypoint waits for PostgreSQL before migrating.

### Database failure

`/api/health/live` stays 200 and `/api/health/ready` returns 503. The
application keeps serving health so the reason is readable. Nothing is proposed,
authorized or transmitted without a database — account state fails closed, and
the risk engine blocks on `account_state_available`.

### Restoring a backup

1. **Stop the application** — not just the database. A running process against a
   restored database will act on it.
   ```bash
   docker compose stop stockbrain
   ```
2. Restore into a fresh database, never over the live one:
   ```bash
   docker compose exec -T postgres psql -U stockbrain -d postgres \
     -c 'ALTER DATABASE stockbrain RENAME TO stockbrain_before_restore;'
   docker compose exec -T postgres psql -U stockbrain -d postgres \
     -c 'CREATE DATABASE stockbrain OWNER stockbrain;'
   docker compose exec -T postgres pg_restore -U stockbrain -d stockbrain \
     --no-owner --no-privileges --exit-on-error < backups/<dump>
   ```
3. **Bring it up with execution off.** `T212_EXECUTION_ENABLED=false` in `.env`
   before starting. This is the step that matters.
4. Start it. The entrypoint runs `alembic upgrade head`, so a dump older than
   the image is migrated forward.
5. **Reconcile against the broker before anything else.** A restored database is
   a database that has forgotten anything that happened after the dump.

### The restore-point problem

A restored database is **behind reality**, and the two dangerous shapes are:

* **An order the broker has and the database does not.** Placed after the dump.
  StockBrain will not know about it, so its exposure is unreserved and a fresh
  proposal for the same listing could double the position. *Check the Trading
  212 app's order history for anything after the dump timestamp before enabling
  execution.*
* **An attempt the database has and the broker does not.** Recorded as sent
  before the dump, resolved after it. It comes back `AMBIGUOUS` and
  reconciliation resolves it by reading — which is safe, and is why
  reconciliation never sends.

```bash
# What the restored database thinks is unresolved:
curl -s localhost:8080/api/v1/execution/attempts?ambiguous_only=true | python3 -m json.tool
# Ask the broker again, per attempt. This endpoint only READS the broker.
curl -sX POST localhost:8080/api/v1/execution/attempts/<id>/reconcile \
     -H 'content-type: application/json' -d '{}'
```

Re-enable `T212_EXECUTION_ENABLED` only once every attempt is resolved and the
broker's order history after the dump timestamp is accounted for.

### Stale Telegram callbacks after a restore

Approval tokens live in `approval_actions`, so a restore brings back tokens that
were consumed after the dump. A button pressed now resolves against the restored
row and may find it unconsumed.

Two things limit the damage, and neither is sufficient alone: a token is clamped
to its proposal's own expiry (`RISK_PROPOSAL_TTL_MINUTES`, 30 by default), and
approval only *authorizes* — transmission is separately gated and re-validates
the quote, the account, the exposure and the FX rate at send time. Still, after a
restore:

```bash
docker compose exec -T postgres psql -U stockbrain -d stockbrain \
  -c "UPDATE approval_actions SET consumed_at = now() WHERE consumed_at IS NULL;"
```

Pending updates are dropped at Telegram startup anyway, so a queued `/resume`
of unknown age is never replayed.

## Runbooks

### Live execution is not permitted

Read `GET /api/v1/system/execution-status`. `blockers` lists every unmet
condition verbatim. All four environment gates plus credentials are required;
see the README's "Execution gate" section. This is intended behaviour, not a
bug, and a contradictory combination stops the process at startup.

### `/api/health/ready` returns 503 with `schema_current: false`

The running image expects a different migration revision than the database has.
Either the image is older than the database (roll forward the image) or the
migration did not run (check entrypoint logs). Do not edit `alembic_version`.

### A proposal is stuck in `EXECUTION_AMBIGUOUS`

StockBrain sent an order request and did not receive a definitive response. It
will **not** retry.

1. Do not resubmit, in the app or by hand.
2. Reconciliation checks pending orders, order history and the position delta.
3. If the state is still uncertain afterwards, check the official Trading 212
   app and resolve it there, then record the outcome.

The only outcome that permits a new execution attempt is
`FAILED_BEFORE_SEND` — proof that nothing reached the broker.

### Emergency stop

`/kill` in Telegram, or `POST /api/v1/system/kill-switch` with
`{"engaged": true}`, writes the persistent `control.kill_switch` row in
`app_settings`. It stops new proposal generation, every authorization path (web,
Telegram and automatic) and any future order transmission.

It does **not** liquidate positions and does **not** cancel or modify a broker
order — there is no order, cancel or amend path anywhere in this process, and a
test scans every module to keep it that way. It does not stop broker
reconciliation either: knowing the true account state matters most precisely
when something has gone wrong.

`/pause` (`control.trading_paused`) stops new *proposals* and authorization while
leaving ingestion, classification, research and reconciliation running.

Both flags are durable and are restored at startup. Releasing them:

| To lift | Telegram | HTTP |
|---|---|---|
| Pause | `/resume` | `POST /api/v1/system/resume` |
| Kill switch | *not* `/resume` — it says so and refuses | `POST /api/v1/system/kill-switch` `{"engaged": false}`, or the release button on **System health** |

`/resume` deliberately does not release the kill switch. If the routine control
also cleared an emergency stop, the emergency stop would be one habitual action
away from being undone by somebody who only meant to restart normal work.

### Telegram

The bot is long polling only: outbound HTTPS, no inbound port, no public TLS
endpoint. If it stops working, check in this order:

1. `GET /api/v1/system/telegram` — status, transport, whether a webhook is
   configured, last successful contact, last error *category*, and how many
   numeric ids are allowlisted. It contains no token and no chat content.
2. `DISABLED` with blockers means it was never started: no `TELEGRAM_ENABLED`,
   no token, or an empty `TELEGRAM_ALLOWED_USER_IDS`. An empty allowlist
   authorises nobody by design.
3. `DOWN` with `InvalidToken` means Telegram rejected the token. This is not
   retried — like any provider auth failure — so fix the token and restart.
4. A webhook showing as configured will stop `getUpdates` returning anything;
   Telegram documents the two as mutually exclusive. Clear it with
   `deleteWebhook`.
5. Everything else degrades and retries on its own. python-telegram-bot backs
   off internally (1.5x, capped at 30s) and StockBrain's supervisor rebuilds the
   application after a run of failed health probes.

A failed *notification* is recorded in `notifications` with status `FAILED` and
is deliberately never resent: a resend cannot tell "never arrived" from "arrived
but the status write failed", and the second reading produces a duplicate trade
alert. Query the table to see what was missed.

### An order's state is unknown

`EXECUTION_AMBIGUOUS` means StockBrain transmitted a request and did not receive
a definitive response. The order may or may not exist.

**Do not resend it, and do not place it manually**, until reconciliation reports
an outcome. There is no resend path in the software — the order endpoint is
non-idempotent, so a second POST creates a second position — and the same
applies to a human with the app open.

```bash
curl -s localhost:8080/api/v1/execution/status | python3 -m json.tool
curl -s localhost:8080/api/v1/proposals/<id>/execution | python3 -m json.tool
```

Reconciliation runs on a timer and reads two endpoints: pending orders and order
history, filtered to the instrument. It concludes only when the evidence
supports one:

| Result | Meaning | Next |
|---|---|---|
| `ORDER_FOUND` | one API-initiated order matched exactly | the proposal follows the order |
| `ORDER_NOT_PLACED` | both read paths answered, nothing matched, past the settle window | the proposal fails and releases its reservation |
| `MULTIPLE_CANDIDATES` | two indistinguishable orders match | **stays ambiguous** — resolve in the Trading 212 app and record what you found |
| `BROKER_UNAVAILABLE` / `INCONCLUSIVE` | a read failed, or it is too early to call absence | swept again |

After `EXECUTION_RECONCILE_MAX_ATTEMPTS` inconclusive passes it stops being
swept and waits for you. Ask for one more read with:

```bash
curl -sX POST localhost:8080/api/v1/execution/attempts/<id>/reconcile \
     -H 'content-type: application/json' -d '{}'
```

That endpoint only reads the broker. It cannot send an order, and no endpoint
can.

### A broker order was refused

`REJECTED_BY_BROKER` with HTTP 400 is a validation refusal — the quantity, the
ticker or the account state was unacceptable, and no order exists. HTTP 403 is
different and is a configuration fault: the API key lacks Trading 212's
`orders:execute` scope. Grant it in the account's API settings; the proposal
must be re-derived through the pipeline.

### Turning execution on

`T212_EXECUTION_ENABLED=false` by default. Nothing transmits until it is set,
whatever else is configured. Check the posture before and after:

```bash
curl -s localhost:8080/api/v1/execution/status | python3 -m json.tool
```

`order_transmission_permitted` is "no blockers remain", so the flag and the
`blockers` list can never disagree. Live transmission additionally requires all
four live gates; demo requires only the switch, credentials and
`EXECUTION_MODE=manual_approval`.

### LLM budget exhausted

At the soft limit, low-priority thematic searches and research are reduced and
the user is warned. At the hard limit, new deep research stops while ingestion,
deduplication, broker reconciliation and portfolio safety alerts continue.
Broker-state monitoring is never stopped by an LLM budget.

## Job queue

The queue *is* the audit trail, which is only a virtue if somebody can read it.
Phase 6's bug 12 is the case in point: 144 events sat unclassified for hours,
`/api/health` was green, and the queue recorded every job as **succeeded**.

```bash
curl -s localhost:8080/api/v1/discovery/status | python3 -m json.tool | sed -n '/"queue"/,/^  }/p'
```

| Field | The question it answers |
|---|---|
| `pending` / `running` | depth |
| `oldest_pending_age_seconds` / `_job_type` | *has something stopped?* — the single most useful number here |
| `stuck` / `stuck_job_types` | `RUNNING` past `JOB_CLAIM_TIMEOUT_SECONDS`: a dead worker or a hanging handler |
| `dead` | exhausted its retries; terminal, and will not run again without you |
| `failed` | includes jobs merely between retries |
| `counts_by_type` / `counts_by_status` | the whole distribution |

A stale claim is reclaimed automatically, subject to the same `max_attempts`
budget so a job that reliably kills its worker cannot loop for ever. A job with
no attempts left is marked `FAILED` rather than retried — which is why a
web-discovery search job carries `max_attempts=1`: reclaiming it would be a second
billable request.

## Operational alerts

Uses the Phase 7 Telegram path. Alerts are `Notification` rows first, delivered
by a job, so an alert that already happened does not fail because Telegram is
unreachable — and a failed send is recorded and **never** auto-resent.

| Condition | Class | Repeat |
|---|---|---|
| `EXECUTION_AMBIGUOUS` | critical | 1h |
| `DATABASE_UNHEALTHY` | critical | 1h |
| `BROKER_AUTH_FAILING` | critical | 4h |
| `RECONCILIATION_UNRESOLVED` | critical | 6h |
| `QUEUE_STUCK` | warning | 2h |
| `QUEUE_BACKLOG`, `PROVIDER_DOWN` | warning | 6h |
| `DEAD_JOBS`, `TRADING_HALTED`, `FX_UNAVAILABLE`, `LLM_BUDGET_EXHAUSTED`, `DISCOVERY_BUDGET_EXHAUSTED` | warning | 12h |

Suppression is the `notifications.dedupe_key` unique index carrying a window
stamp — not a timestamp comparison in Python — so two workers cannot both
announce and a restart cannot re-announce. A condition that clears sends one
"resolved" message, so the last thing in the channel is the current state rather
than the worst state.

**A deliberately disabled provider is never an alert.** `DISABLED` is a
configuration statement. Neither is `DEGRADED`: degradation is the designed
response to a provider having a bad minute. An alert stream nobody reads is
worse than no alerts, because it looks like coverage.

## Pre-live checklist

Every line is a thing to *confirm*, not to assume. Nothing here is optional, and
the order is roughly the order in which failing one is cheapest to discover.

**The system**

- [ ] `make check` green: pytest, ruff, mypy `--strict`, Alembic drift, frontend
      lint/typecheck/build, pip-audit, npm audit.
- [ ] `docker compose exec stockbrain check-config` prints the posture with no
      unexpected blocker.
- [ ] `/api/health/ready` is 200 with `schema_current: true`.
- [ ] `/api/health/providers` — every provider you rely on is `HEALTHY`;
      everything else is `DISABLED` **on purpose** and you can say why.
- [ ] Queue: `stuck: 0`, `dead: 0`, `oldest_pending_age_seconds` not growing.

**Backups**

- [ ] `scripts/backup.sh` has run and produced a dump.
- [ ] `scripts/restore-test.sh <newest dump>` prints `restore verified`.
- [ ] The cron entries exist on the host and have actually fired at least once.

**Execution safety**

- [ ] **Zero unresolved ambiguous executions.**
      `/api/v1/execution/attempts?ambiguous_only=true` is empty. This is a hard
      gate: going live with an unknown order outstanding means you cannot tell a
      new position from an old one.
- [ ] A full **demo end-to-end run** has succeeded: event → thesis →
      resolution → quote/account/FX → risk → proposal → authorization →
      execution → reconciliation, with **exactly one** broker POST.
- [ ] The kill switch has been engaged and released, and you watched it block an
      authorization.
- [ ] If you rely on Telegram: `/status` answers, a proposal notification
      arrived, and an Approve button worked end to end.

**Prices and rates**

- [ ] Quote freshness: `/api/v1/market-data/health` shows the feed entitled and
      a probe age you understand. Outside market hours the "latest" quote is the
      closing print — a live probe measured $33 of spread on a $321.80 AAPL mid,
      7.67 hours old. Both gates fire on that, correctly.
- [ ] FX, **only if trading a currency other than the account's**:
      `/api/v1/system/fx` reports `available: true`, and you have read which
      *grade* of rate is sizing your trades. A reference fixing is not a dealing
      rate.
- [ ] Account snapshot age is inside `RISK_MAX_ACCOUNT_STATE_AGE_SECONDS`.

**Cost**

- [ ] Web discovery: Brave and Exa keys set or not — decide deliberately. If set, the caps and
      the cadence in `/api/v1/discovery/status` are numbers you are willing to
      pay every day for a month.
- [ ] LLM budgets reflect what you will actually accept
      (`LLM_DAILY_HARD_USD`, `LLM_MONTHLY_HARD_USD`).

**Broker**

- [ ] `T212_ENV=live` and the API key is a **live** key. A key from the wrong
      environment returns HTTP 401 with an empty body; it does not fail
      helpfully.
- [ ] The live key has the `orders:execute` scope, granted knowingly, and is
      **IP-restricted** in the Trading 212 account settings.
- [ ] A separate read-only key is used for metadata and account reads.
- [ ] `T212_WRITTEN_CONSENT_CONFIRMED=true` — and you have actually obtained
      Trading 212's prior written consent. API Terms 6.6/6.7.
- [ ] For automatic authorization live only:
      `T212_AUTOMATED_TRADING_CONSENT_CONFIRMED=true`, and you have actually
      obtained consent for it. API Terms 4.2(a) prohibits Algorithmic Trading
      without it.
- [ ] **One tiny order placed by hand in the Trading 212 app first**, so the
      account, the instrument and the scope are known to work before software
      places one.
- [ ] `RISK_*` limits reviewed against the **real** account balance, not the
      demo one. Every one of them is a limit, not a recommendation.
- [ ] Account currency behaviour verified: you know whether the listings you
      intend to trade are in the account's currency, and if not, that FX is
      configured, fresh and of a grade you accept.

**Access**

- [ ] `/api/v1/system/web-security` reports `auth_effective: true`, or
      `WEB_TRUSTED_NETWORK_ACKNOWLEDGED=true` with a reverse proxy you can name.
- [ ] The GUI is not reachable from the public internet. Loopback binding plus
      Tailscale or a LAN-only reverse proxy.
- [ ] PostgreSQL is unpublished (`compose.yaml` only; never the dev overlay).
- [ ] `.env` is `chmod 600` and owned by root on the host.

**Then, and only then**

- [ ] Set `T212_LIVE_EXECUTION_ENABLED=true` and restart. The process refuses to
      start if any of the four gates disagree, which is the last line of
      defence rather than the first.

## Live execution gate truth table

Four independent gates, all required. Exactly 1 of 16 combinations permits live
transmission, and there is an exhaustive test.

| `T212_ENV` | `LIVE_EXECUTION_ENABLED` | `WRITTEN_CONSENT_CONFIRMED` | `EXECUTION_MODE` | Live? |
|---|---|---|---|---|
| live | true | true | manual_approval | **yes** |
| live | true | true | research_only | no |
| live | true | false | *any* | no — *and the process refuses to start* |
| live | false | *any* | *any* | no |
| demo | *any* | *any* | *any* | no |

Beside them, and deliberately **not** among them:

| Setting | Gates | Why it is separate |
|---|---|---|
| `T212_EXECUTION_ENABLED` | *any* transmission, demo included | So deploying execution does not, by itself, start sending. Default false. |
| `T212_AUTOMATED_TRADING_CONSENT_CONFIRMED` | live *automatic authorization* | Authorizing and transmitting are different permissions. Re-checked at send time. |
| `EXECUTION_POLICY` | who authorizes | A different axis from `EXECUTION_MODE`; conflating them would let one be granted by satisfying the other. |
| kill switch / pause | everything | Durable, in PostgreSQL, re-read inside the send transaction. |

A contradictory combination is a startup failure with exit code 78, not a
warning. `live_execution_permitted` is defined as "no blockers remain", so the
flag and the blocker list can never disagree.

## Update and rollback

```bash
# Update
scripts/backup.sh                                   # always first
git pull && docker compose build stockbrain
docker compose up -d stockbrain                     # entrypoint migrates
curl -sf localhost:8080/api/health/ready | python3 -m json.tool
```

Rollback is the asymmetric case. Migrations run **forward** on start, so an
older image against a newer schema reports `schema_current: false` and 503s
rather than guessing.

```bash
git checkout <previous tag> && docker compose build stockbrain
docker compose up -d stockbrain
# If readiness reports schema_current: false, the schema is ahead of the image.
cd backend && alembic downgrade <that image's head>
```

Never edit `alembic_version` by hand. Every migration in this repository has a
tested downgrade and the round trip is part of `make check`.

## Emergency shutdown

In order of severity, and knowing what each one does *not* do:

1. **Kill switch** — `/kill` in Telegram, or
   `POST /api/v1/system/kill-switch {"engaged": true}`. Durable. Stops new
   proposals, every authorization path and every transmission. Takes effect
   inside the send transaction, so an order being prepared right now is stopped.
2. **Stop transmission only** — `T212_EXECUTION_ENABLED=false`, restart.
   Proposals still generate and can still be authorized; nothing is sent.
3. **Stop the container** — `docker compose stop stockbrain`. 30 seconds of
   grace to drain workers and close the Telegram poller.
4. **Revoke the API key** in the Trading 212 app. The only measure that does not
   depend on StockBrain behaving correctly, and therefore the right one if you
   are unsure.

**None of these closes a position or cancels an order.** There is no order,
cancel, amend or modify path anywhere in this process — a test scans every
module to keep it that way — and cancelling races a fill. To exit a position,
use the Trading 212 app.

## Log hygiene

Logs are structured JSON with correlation identifiers (`event_id`,
`research_run_id`, `proposal_id`, `broker_order_id`, `job_id`, `request_id`).
Two redaction layers scrub credential-shaped keys and configured secret values.
Never disable them to debug an integration; log the request shape instead.


## Choosing an LLM backend

StockBrain talks to any OpenAI-compatible chat-completions endpoint.
`LLM_PROVIDER` names a *profile* — the API contract, not the model. The shipped
default is `deepseek`, and an existing installation needs no new configuration.

| Profile | Endpoint | Output cap | JSON | Reasoning off? | Cached tokens |
|---|---|---|---|---|---|
| `deepseek` | `api.deepseek.com` | `max_tokens` | `json_object` | yes, `thinking` | hit/miss split |
| `meta` | `api.meta.ai/v1` | `max_completion_tokens` | `json_schema` | **no**, floor only | subset counter |
| `openai` | `api.openai.com/v1` | `max_completion_tokens` | `json_object` | n/a | subset counter |
| `generic` | you supply it | `max_tokens` | `json_object` | n/a | not accounted |
| `generic-no-json` | you supply it | `max_tokens` | none | n/a | not accounted |

An unknown name is a startup error, not a fallback onto `generic`: a typo must
not silently redirect every model call onto a backend with different
capabilities and different prices.

### You must configure prices, and why

Any provider other than DeepSeek requires `LLM_INPUT_USD_PER_MTOK`,
`LLM_CACHED_INPUT_USD_PER_MTOK` and `LLM_OUTPUT_USD_PER_MTOK`, and startup
refuses to run without them.

This is not bookkeeping. `BudgetGuard` sums estimated cost from `llm_calls`, and
`PricingTable.estimate` returns `None` — not `0` — for a model it cannot price.
An unpriced provider is therefore not merely mis-reported in the spend panel; it
is silently **exempt** from `LLM_DAILY_HARD_USD` and `LLM_MONTHLY_HARD_USD`.
Requiring rates at startup is what keeps those a real ceiling for every
endpoint rather than only for the ones the codebase happens to know.

Leaving the cached-input rate unset bills cached tokens at the full input rate.
That over-estimates, which is the safe direction: a budget that under-counts
spends past its ceiling, and only over-counting is recoverable. Where a provider
reports no cache split at all, StockBrain never credits it with a hit.

To look up published rates:

```
python -m stockbrain.llm.rates_cli --search muse-spark
python -m stockbrain.llm.rates_cli meta/muse-spark-1.3-contributor
```

It prints the variables ready to paste. It is an authoring aid only — the
runtime never fetches prices, because a spend ceiling must not depend on a third
party being reachable, current and honest. Aggregator prices also disagree with
first-party ones: see `docs/sources.md` for a 5x discrepancy observed on
2026-09-06. Check the provider's own pricing page, then record the date and
source.

### There is no failover between providers

A configured backend that fails, fails visibly. StockBrain will not retry the
call on a second provider. Automatic failover would hide the outage, change the
cost characteristics of whatever ran next, and make `llm_calls` ambiguous about
which model actually produced a stored classification — the same reasoning that
keeps Brave and Exa on separate budgets with no fallback between them.

### Switching provider

1. Set `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL` and the three rate variables.
2. Restart. The startup probe calls `GET /models` on the configured base URL,
   which is free and proves the credential.
3. The health board reports the backend under the provider key `llm`.

To roll back, set `LLM_PROVIDER=deepseek` and clear `LLM_API_KEY`. The
`DEEPSEEK_*` settings and the DeepSeek adapter are unchanged and still fully
tested.

### Meta Muse Spark 1.3 Contributor — data use warning

If you configure `muse-spark-1.3-contributor`, be aware what the discount buys.
Meta documents the contributor tier as offering "heavily discounted token
pricing in exchange for permission to use your prompts and completions to train
future Meta models".

Everything StockBrain sends the classifier — article text, your symbol hints,
your prompts — and everything the model returns becomes Meta training data.
Nothing blocks its use; the implication is simply explicit. The non-contributor
`muse-spark-1.3` carries no such condition at $1.25 / $0.15 / $4.25 per 1M
tokens against the contributor tier's $0.10 / $0.002 / $0.20
(verified 2026-09-06).

Two operational differences from DeepSeek:

* **Reasoning cannot be disabled.** `reasoning_effort: "none"` is a documented
  400. The classifier — the cheap high-volume triage path — therefore always
  pays for some reasoning tokens on this provider. StockBrain sends the
  documented floor, `minimal`. Watch `reasoning_tokens` in `llm_calls` after
  switching.
* **The contributor tier is limited to 100 RPM** (standard is 3,000).

### Comparing two backends before committing to one

`stockbrain.llm.benchmark_cli` runs a fixed case set through two or more
backends and prints machine-readable rows — per case and arm: schema success,
classification, latency, input / cached / output tokens and estimated cost.

```
python -m stockbrain.llm.benchmark_cli --arms arms.json --cases cases.jsonl --limit 20
```

Arms are described in a JSON file (API keys are named by environment variable,
never passed on the command line where they would land in shell history); cases
are JSON Lines. It writes nothing — no `llm_calls` rows, no events — so a
comparison run cannot pollute spend history, and it is capped at 50 cases so a
typo cannot start an unbounded paid loop.

It reports no winner. A handful of cases proves API compatibility and exposes
obvious degradation; it does not establish that one model classifies better than
another. If score distributions differ materially between backends, that is a
finding to investigate — not a reason to retune
`CLASSIFIER_MIN_IMPORTANCE`, `CLASSIFIER_MIN_CONFIDENCE` or
`CLASSIFIER_MIN_MATERIALITY`, which are unchanged by provider choice.
