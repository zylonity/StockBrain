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
4. persists provider health;
5. logs the live-execution posture and its blockers;
6. **restores and logs the durable control state.** A process that crashed while
   paused or killed comes back paused or killed, and says so at WARNING. Silent
   resumption after a crash is the failure that state exists to prevent;
7. starts Telegram long polling, if it is configured, in a supervisor task —
   never blocking the HTTP server or the job workers.

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
Alpaca news DOWN + Firecrawl HEALTHY   → discovery DEGRADED, application DEGRADED
Trading 212 DOWN                       → execution unavailable, research healthy
PostgreSQL DOWN                        → application DOWN / not ready
```

## Backups

The database holds the entire research and execution audit trail. Container
volumes alone are not a backup.

```bash
docker compose exec -T postgres pg_dump -U stockbrain -Fc stockbrain > stockbrain-$(date +%F).dump
```

Run nightly, keep a retention policy, combine with TrueNAS dataset snapshots,
and test a restore periodically:

```bash
docker compose exec -T postgres pg_restore -U stockbrain -d stockbrain_restore_test < backup.dump
```

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

### LLM budget exhausted

At the soft limit, low-priority thematic searches and research are reduced and
the user is warned. At the hard limit, new deep research stops while ingestion,
deduplication, broker reconciliation and portfolio safety alerts continue.
Broker-state monitoring is never stopped by an LLM budget.

## Log hygiene

Logs are structured JSON with correlation identifiers (`event_id`,
`research_run_id`, `proposal_id`, `broker_order_id`, `job_id`, `request_id`).
Two redaction layers scrub credential-shaped keys and configured secret values.
Never disable them to debug an integration; log the request shape instead.
