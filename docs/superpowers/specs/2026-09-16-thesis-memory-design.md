# Thesis Memory — Design

**Date:** 2026-09-16
**Status:** approved for planning
**Predecessors:** `docs/superpowers/plans/2026-09-11-deterministic-position-exits.md`, `docs/superpowers/plans/2026-09-12-volatility-stop.md`

## 1. Problem

Every research run is stateless. `ResearchPacket.previous_thesis` exists but is
never populated on the automatic path (`ResearchService.enqueue_event`), and the
packet carries no current position. Two days of live (demo) trading show the
cost:

- One news story (the 2026-09-14 "slow the frontier" essay) arrived as ~20
  events and produced nine NVDA/AMD/MU trades in 28 minutes — BUY, REDUCE, BUY,
  REDUCE, REDUCE — each run unaware the previous one had just traded the same
  story.
- SMMT was bought twice in 19 minutes from two articles about one Akeso
  readout; the position is 14.5% of NAV.
- Nothing records whether a thesis turned out right. There is no ground truth
  to learn from, and no place a learned thing could change behaviour.

The system needs memory of *what it currently thinks and holds* (continuity)
and a ground-truth record of *how its past calls performed* (calibration) that
feeds both the research prompt and the deterministic risk engine.

## 2. Goals and non-goals

**Goals**

1. Every automatic research run sees the company's standing thesis and live
   position.
2. Every executed thesis-backed trade is graded numerically against a
   benchmark at horizon-relative checkpoints and on close, with zero LLM spend.
3. Grades aggregate into a calibration record per `(event_type, action)` and
   per `(company, action)`.
4. The research packet carries the relevant calibration numbers; the risk
   engine shrinks size for buckets with a demonstrated negative record once a
   sample-size floor is met.
5. Reruns with a historical `as_of` never see memory from after `as_of`.

**Non-goals** (explicitly deferred)

- LLM-written prose lessons. Planned as a follow-up once ≥20 graded outcomes
  exist, generated per bucket, not per trade.
- A post-execution cooldown rule and a REDUCE dust floor (separate plan).
- The TLX exit-sweep spread loop (separate bug).
- Any frontend work. One read-only JSON endpoint only.
- FX-adjusted alpha and per-venue benchmarks (see §9).

## 3. Architecture

Three units, all in PostgreSQL, all deterministic:

```
proposals(EXECUTED, thesis_id)  ──►  thesis_outcomes  ──►  thesis_outcome_grades
                                       (record)              (grade: Yahoo close vs SPY)
                                                                   │
                       ┌───────────────────────────────────────────┤
                       ▼                                           ▼
             ResearchPacket.memory                        RiskInputs.calibration
   (standing thesis, position, calibration)        (calibration_size_modulation rule)
```

- **`MemoryService`** (`stockbrain/intelligence/memory.py`): records outcomes,
  grades them on a scheduled tick, and answers the two read queries (packet
  memory, calibration bucket).
- **Scheduler task `thesis_memory_grade`** registered next to
  `volatility_refresh` in `services.py`, daily cadence, one Yahoo request per
  distinct symbol per tick, 20-hour rate-discipline clock like
  `VolatilityRefreshService`.
- **Packet extension**: `ResearchPacket.memory: ResearchMemory | None`.
- **Risk extension**: `RiskInputs.calibration: CalibrationBucket | None` and a
  new size-factor rule.

## 4. Data model

Alembic head before this work: `e5b1c9d47a02`. Filenames
`YYYYMMDD_HHMM_description.py`.

### 4.1 `thesis_outcomes`

One row per EXECUTED proposal that carries a `thesis_id`.

| column | type | notes |
|---|---|---|
| `id` | uuid pk | |
| `proposal_id` | uuid fk → trade_proposals, unique | idempotency key |
| `thesis_id` | uuid fk → theses | |
| `research_run_id` | uuid fk → research_runs | |
| `company_id` | uuid fk → companies | |
| `broker_instrument_id` | uuid fk → broker_instruments | |
| `broker` | enum broker | |
| `broker_ticker` | text | |
| `event_type` | text null | from `events.event_type` via `research_runs.event_id` |
| `action` | enum thesis_action | `research_action` of the proposal |
| `horizon` | enum time_horizon | from the thesis |
| `confidence` | numeric(4,3) | `research_confidence` |
| `is_exit` | bool | true when `proposal.risk_rules` contains an exit rule id (`hard_stop`, `trailing_stop`, `volatility_stop`, `roi_target`, `horizon_elapsed`, `thesis_superseded`) |
| `exit_rule_id` | text null | that rule id |
| `entry_at` | timestamptz | `proposal.executed_at` |
| `entry_date` | date | UTC date of `entry_at`; no exchange calendar (§9) |
| `reference_price` | numeric(24,8) | `proposal.reference_price`, recorded, not used for grading |
| `currency` | char(3) | instrument currency |
| `benchmark_symbol` | text | settings value at record time |
| `status` | enum outcome_status: `PENDING`, `CLOSED`, `ABANDONED` | |
| `closed_at` | timestamptz null | |
| `close_reason` | text null | `sell_executed` / `position_gone` / `<exit_rule_id>` |
| `created_at`, `updated_at` | | `TimestampMixin` |

Indexes: `(status)`, `(company_id, action)`, `(event_type, action)`,
`(broker, broker_ticker)`.

### 4.2 `thesis_outcome_grades`

One row per `(outcome, checkpoint)`.

| column | type | notes |
|---|---|---|
| `id` | uuid pk | |
| `outcome_id` | uuid fk → thesis_outcomes | |
| `checkpoint` | text | `D1`, `D5`, `D20`, `D60`, `CLOSE` |
| `trading_days` | int | bars observed after `entry_date` |
| `entry_close` | numeric(24,8) | Yahoo close on `entry_date` (or first bar after it if the entry date has no bar) |
| `current_close` | numeric(24,8) | latest Yahoo close ≤ grading moment (for `CLOSE`, the close on `closed_at` date) |
| `benchmark_entry_close` | numeric(24,8) | |
| `benchmark_current_close` | numeric(24,8) | |
| `instrument_return` | numeric(10,6) | `current_close / entry_close − 1` |
| `benchmark_return` | numeric(10,6) | |
| `alpha` | numeric(10,6) | `instrument_return − benchmark_return` |
| `correct` | bool | see §5.3 |
| `graded_at` | timestamptz | the `as_of` filter key |

Unique `(outcome_id, checkpoint)`.

No new enum types beyond `outcome_status`; `checkpoint` is a text column
validated in Python (a `StrEnum` in `enums.py`).

## 5. Grading

### 5.1 Recording (each tick, before grading)

Insert a `thesis_outcomes` row for every `trade_proposals` row where
`status = EXECUTED`, `thesis_id IS NOT NULL`, `executed_at IS NOT NULL`, and
no outcome exists for that `proposal_id`. This is a sweep over state, not a
hook on the EXECUTED transition, because EXECUTED is reached from both
`execution/service.py:415` and `execution/reconciliation.py:554` and a sweep is
idempotent under redelivery.

`HOLD` and `NO_ACTION` never produce proposals, so they are never recorded.

### 5.2 Checkpoints

Horizon-relative, in trading days after `entry_date`:

| horizon | checkpoints |
|---|---|
| `intraday`, `days` | `D1`, `D5` |
| `weeks` | `D5`, `D20` |
| `months` | `D20`, `D60` |

Plus `CLOSE` for non-exit, non-risk-reducing outcomes (a BUY) when the
position is gone. A checkpoint is graded once, when `trading_days ≥ N` and no
grade exists for it. Trading days are counted as Yahoo bars strictly after
`entry_date`; no exchange calendar is consulted.

The checkpoint table is a `RiskConfig`-independent constant in
`memory.py`; the settings surface only exposes on/off, cadence and the
benchmark symbol (§8).

### 5.3 Correctness

`correct = (alpha > 0)` for `BUY`; `correct = (alpha < 0)` for `REDUCE` and
`SELL` (the instrument underperformed after the system lightened). Zero alpha
is incorrect for both.

Exit outcomes (`is_exit = true`) are graded on the same rule — a stop that
fired before a rebound is a wrong exit — and are aggregated separately (§6).

### 5.4 Close detection

An outcome with `action = BUY` closes when, at grading time, the `positions`
mirror has no row for `(broker, broker_ticker)` or `quantity <= 0`.
`closed_at` = `executed_at` of the latest EXECUTED SELL-side proposal for that
ticker after `entry_at`, else the grading moment. `close_reason` = that
proposal's exit rule id when it was an exit, else `sell_executed`, else
`position_gone`. The `CLOSE` grade uses the close on `closed_at`'s date.

REDUCE/SELL outcomes never close; they reach `CLOSED` after their last
checkpoint is graded so the pending set stays bounded.

`ABANDONED` is set when the instrument has no Yahoo symbol
(`yahoo_symbol()` returns `None`) or the bar currency mismatches the
instrument currency, after one attempt; the reason goes in `close_reason`.

### 5.5 Data source and discipline

- `DailyBars` protocol from `stockbrain/market_data/yahoo.py`, the
  `YahooDailyBars` instance already built in `services.py`.
- One request per distinct symbol per tick (pending outcomes' tickers + the
  benchmark), fetched with `days = 120` so `D60` is always covered.
- Every fetch under `try/except ProviderError`; a failure degrades that
  symbol's outcomes for this tick only and is counted in a tally logged as
  `thesis_memory_grade_completed`, mirroring `volatility_refresh`.
- Yahoo is research-grade. Nothing in this design may write a `reference_price`
  or touch `EXECUTION_GRADE_PRICE_SOURCES`.
- The benchmark and instrument returns are ratios in their own currencies; FX
  is ignored (§9).

## 6. Calibration

A pure function over grades — no table, no cache. For each outcome the
**effective grade** is its `CLOSE` grade if present, else its latest
checkpoint grade. A `CalibrationBucket` is:

```python
@dataclass(frozen=True, slots=True)
class CalibrationBucket:
    key: str            # e.g. "REGULATORY×REDUCE" or "company:<uuid>×BUY"
    samples: int
    correct: int
    hit_rate: Decimal   # correct / samples
    mean_alpha: Decimal
    latest_graded_at: datetime
```

Two query shapes on `MemoryService`:

- `calibration(session, *, event_type, action, as_of) -> CalibrationBucket | None`
  — over non-exit outcomes; `None` when `samples == 0`.
- `company_calibration(session, *, company_id, action, as_of) -> CalibrationBucket | None`.

Both filter `graded_at <= as_of`. Exit outcomes are excluded from both and
reported only through the endpoint (§7.3) as `exits×<rule_id>` buckets.

## 7. Consumers

### 7.1 Research packet

`ResearchPacket` gains:

```python
class ResearchMemory(BaseModel):
    standing_thesis: StandingThesis | None
    position: PositionMemory | None
    company_record: CalibrationRecord | None   # (company, action=BUY)
    event_type_record: tuple[CalibrationRecord, ...]  # (event_type, action) for BUY, REDUCE, SELL

class CalibrationRecord(BaseModel):
    # Pydantic mirror of the service-side CalibrationBucket dataclass (§6);
    # MemoryService converts at the packet boundary.
    key: str
    samples: int
    correct: int
    hit_rate: Decimal
    mean_alpha: Decimal
    latest_graded_at: AwareDatetime

class StandingThesis(BaseModel):
    thesis_id: uuid.UUID
    published_at: AwareDatetime
    action: ThesisAction
    confidence: float
    horizon: TimeHorizon
    thesis: str                    # truncated to 600 chars
    invalidation_conditions: tuple[str, ...]
    age_hours: float

class PositionMemory(BaseModel):
    quantity: Decimal
    average_price: Decimal | None
    current_price: Decimal | None
    unrealised_pct: Decimal | None
    opened_at: AwareDatetime | None
    synced_at: AwareDatetime
```

`ResearchService.packet()` populates it:

- **Standing thesis**: latest `Thesis` whose `ResearchRun` has
  `company_id` and `broker_instrument_id` matching, `status = COMPLETED`,
  `completed_at <= as_of`, and `completed_at >= as_of − memory_standing_thesis_max_age_days`.
  When found and the caller passed no `previous_thesis_id`, the packet's
  `previous_thesis_id` is set to it, so the existing supersession chain in
  `research_service.py:405-428` now runs on the automatic path. The existing
  validity check for an explicitly passed `previous_thesis_id` is unchanged.
- **Position**: the `positions` row for `(broker, broker_ticker)` with
  `last_synced_at <= as_of` and `quantity > 0`. A historical rerun therefore
  sees no position unless the mirror predates `as_of` — correct, because the
  historical position is unknown.
- **Calibration**: from §6 with `as_of`.

`memory` is `None` only when `memory_enabled` is false. An empty
`ResearchMemory` (all fields `None`/empty) is still sent so the model learns
the section exists.

The packet is already serialised whole via `model_dump_json()` inside one
fence; memory rides inside it. Its size is bounded: ≤ 600 chars of thesis text,
≤ 6 numbers of position, ≤ 4 calibration rows.

`PROMPT_VERSION` in `intelligence/research.py` becomes `"research-v3"`. This
changes `config_version`, which cancels PENDING runs at deploy by existing
design (`ResearchService.sweep`).

**`SYSTEM_POLICY`** in `tradingagents_adapter.py` gains one paragraph, placed
after the "Set confidence..." sentence:

> The packet's memory section is this system's own prior state, not evidence,
> and must never be cited as a source. When a standing thesis is present you
> must do one of three things and say which: reaffirm it, supersede it with a
> stated reason grounded in the triggering event, or return NO_ACTION because
> the event is already priced into it — a story the standing thesis was itself
> a reaction to is already priced in. A position's unrealised move is the tape,
> not the event. Calibration records describe how this system's past calls in
> the same situation performed; a poor record is a reason for lower confidence,
> never a reason to invert a direction the evidence supports.

### 7.2 Risk engine

`RiskInputs` gains `calibration: CalibrationBucket | None = None`.
`EvaluationContext` gains `event_type: str | None = None`; the two builders in
`proposals/service.py` (generation, ~line 1262; authorization, ~line 835) and
any others found by `grep -n "EvaluationContext("` supply it by joining
`research_runs.event_id → events.event_type` for the proposal's
`research_run_id`. `ProposalEvaluator.evaluate` loads the bucket via
`MemoryService.calibration(session, event_type=..., action=..., as_of=now)` and
passes it in. Exit proposals (the sweep path) pass `calibration=None`.

New rule `calibration_size_factor(inputs) -> RuleResult | None` in
`risk/rules.py`, appended in `RiskEngine.evaluate` right after
`confidence_size_factor`:

- Returns `None` when `config.calibration_modulates_size` is false or
  `inputs.calibration is None`.
- Skipped (`_skipped`, outcome PASS) when `samples < config.calibration_min_samples`.
- Otherwise `factor = clamp(Decimal("0.5") + hit_rate, config.min_calibration_size_factor, 1)`.
  A 50% hit rate is neutral; 25% scales to 0.75; 0% to the floor.
- Outcome `REDUCE` when `factor < 1`, else `PASS`. Reason text names the
  bucket, `correct/samples`, mean alpha and the factor. `size_factor=factor`.
- Never BLOCKs. Never raises a cap. Combined multiplicatively by the existing
  `_combined_size_factor`.

Rule id `calibration_size_modulation`, version 1. Three new `RiskConfig`
fields (hashed into `version` like the rest):
`calibration_modulates_size: bool = True`,
`calibration_min_samples: int = 10`,
`min_calibration_size_factor: Decimal = Decimal("0.5")`.

### 7.3 Operator endpoint

`GET /api/v1/memory/calibration` (read-only, session-authenticated like
`research.py`): returns every bucket — `(event_type, action)`,
`(company, action)` and `exits×<rule_id>` — with `samples`, `correct`,
`hit_rate`, `mean_alpha`, `latest_graded_at`, plus `pending_outcomes` and
`graded_outcomes` counts. `GET /api/v1/memory/outcomes?limit=` lists outcomes
with their grades, newest first. No write routes.

## 8. Settings

All `RESTART_REQUIRED`, registered in `api/settings_model.py` beside the
volatility block, documented in `.env.example`:

| setting | default | notes |
|---|---|---|
| `MEMORY_ENABLED` | `false` | packet memory + grading task; off by default like `VOLATILITY_REFRESH_ENABLED` |
| `MEMORY_GRADE_INTERVAL_SECONDS` | `21600` | validated 3600–86400 like the volatility interval |
| `MEMORY_BENCHMARK_SYMBOL` | `SPY` | Yahoo symbol |
| `MEMORY_STANDING_THESIS_MAX_AGE_DAYS` | `14` | 1–90 |
| `RISK_CALIBRATION_MODULATES_SIZE` | `true` | |
| `RISK_CALIBRATION_MIN_SAMPLES` | `10` | ≥ 3 |
| `RISK_MIN_CALIBRATION_SIZE_FACTOR` | `0.5` | (0, 1] |

The live `.env` sets `MEMORY_ENABLED=true` after deploy; that is an operator
step, not part of this work.

## 9. Known limitations (accepted)

- Alpha ignores FX: a GBX instrument vs a USD benchmark compares ratios in
  different currencies. Acceptable for a hit-rate signal; noted in the
  endpoint's docstring.
- SPY for every venue. Per-venue benchmarks are a one-table follow-up.
- UTC entry date; no exchange calendar. Off-by-one trading day at worst.
- Calibration learns slowly: buckets need `calibration_min_samples` before the
  rule acts. At the current ~15 trades/day this is days, not months.
- `positions` is a live mirror; historical reruns see no position.

## 10. Testing

Unit (`tests/unit/`):
- `test_memory_grading.py`: checkpoint selection per horizon; return/alpha
  arithmetic in `Decimal`; `correct` sign per action; entry-close fallback to
  the first bar after `entry_date`; insufficient bars → no grade.
- `test_memory_calibration.py`: effective-grade selection (CLOSE beats latest
  checkpoint); bucket aggregation; `as_of` filter; exits excluded.
- `test_risk_calibration_rule.py`: `None` when disabled/absent; skipped under
  the floor; factor curve at hit rates 1.0/0.5/0.25/0; combined with the
  confidence factor; never BLOCK.

Integration (`tests/integration/`, existing DB fixture):
- `test_thesis_memory_sweep.py`: fake `DailyBars`; record from EXECUTED
  proposals is idempotent; grades appear at the right checkpoints; close
  detection via a missing position row and via a SELL proposal with an exit
  rule; `ABANDONED` on unknown symbol; provider failure degrades one symbol.
- `test_research_packet_memory.py`: standing thesis chosen by recency and
  age; `previous_thesis_id` set on the automatic path; position included only
  when synced ≤ `as_of`; calibration rows present; `memory` present-but-empty
  when nothing is known; absent when disabled.
- `test_proposal_calibration_input.py`: evaluator loads the bucket and the rule
  appears in `risk_rules` on a generated proposal.
- `test_memory_api.py`: both endpoints, auth required.

`mypy --strict`, `ruff format`, `ruff check`, full suite via `make test`
(needs `stockbrain_test`; `TEST_DATABASE_URL` in the Makefile). Baseline: the
suite as of `cf69e87` passes; the plan records the count.

## 11. Must-not list

- No LLM call anywhere in `memory.py`, the grading task, or the rule.
- No new `PriceSource`; Yahoo closes never become a `reference_price`.
- No broker mutation. Nothing here authorizes or sizes upward.
- No in-memory state across ticks; every read is a query.
- No change under `execution/` — recording is a sweep over proposal state.
- `previous_thesis` explicit-argument validation stays as strict as today.
