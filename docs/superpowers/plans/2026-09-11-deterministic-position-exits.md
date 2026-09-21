# Deterministic Position Exits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give StockBrain a reason to sell — five deterministic exit rules that watch held positions and emit SELL/REDUCE proposals into the existing approval queue.

**Architecture:** A scheduled sweep reads the `positions` mirror the broker refresh already maintains, evaluates pure exit rules against each holding, and calls a new `ProposalService.generate_exit` for the first rule that fires. `generate_exit` recovers the thesis the position was opened on (via the last `EXECUTED` BUY proposal for that ticker) and reuses the existing evaluator, sizing and proposal-construction path unchanged, so an exit proposal is an ordinary proposal with full research lineage. Nothing in this plan reaches a broker or authorizes anything: the sweep enqueues proposals, and the existing authorization and transmission gates are untouched.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x async, Alembic, Pydantic v2, pytest, structlog. No new dependencies.

**Spec:** `STOCKBRAIN_TECHNICAL_SPEC.md` — §14 (deterministic risk engine), §15 (proposal lifecycle), §17 (portfolio-position reassessment), §22 (scheduler). No separate design doc exists; the exit-policy decisions this plan implements are recorded in "Design decisions" below and must travel with it.

---

## Global Constraints

Copied from the spec and the existing code. Every task's requirements implicitly include this section.

- **No broker mutation anywhere in this plan.** The sweep generates proposals only. Authorization stays behind `ProposalService.authorize`, transmission behind the existing execution gates (spec §46).
- **No generic HTTP retry on order POSTs** and no new broker endpoint is called at all here (spec §4.1 "Critical order safety behavior").
- **Broker-supplied prices are display-grade only.** `Position.current_price` is documented "explicitly not real-time" (`db/models/portfolio.py:71`). It may track a high-water mark; it must never be the reference price on a proposal. Execution-grade pricing comes from the existing evaluator, which enforces `_price_source_execution_grade` (`risk/rules.py:253`).
- **Risk-reducing actions are never blocked by exposure controls.** "Preventing a position from being closed is a hazard rather than a control" (`risk/rules.py:18-20`).
- **`mypy --strict` must pass** on `stockbrain` and `tests`; ruff format and check must be clean.
- **Every threshold lives in `RiskConfig`**, whose `__post_init__` hashes all fields into `version` (`risk/config.py:135`) — persisted per proposal, so an exit proposal automatically records the exact policy that produced it. Never read a threshold from `Settings` inside a rule.
- **Settings are `RESTART_REQUIRED`, not runtime-editable.** There is no generic config mutation endpoint and a test asserts `/api/v1/system/settings` has only a `GET`. Do not add a write route.
- **Alembic head before this work is `c3f28a1d6b45`.** Migration filenames follow `YYYYMMDD_HHMM_description.py`.
- **Test database:** `make test` guards on the `stockbrain_test` database. Unit tests that touch Postgres use the `clean_tables` fixture; pure-logic tests use helpers from `tests/risk_helpers.py`.
- **Decimal everywhere for money and quantities.** No floats in price, quantity or ratio arithmetic. `research_confidence` is the one existing float and stays one.

---

## Design decisions

These were settled before planning; do not re-litigate them mid-execution.

1. **Exits reuse the origin thesis.** A position is exited *because* the thesis it was bought on has run its course, so `generate_exit` loads the last `EXECUTED` BUY proposal for the ticker and reuses its `thesis_id` / `research_run_id` / `event_id` / `company_id`. This satisfies §17's "why did we exit?" lineage requirement with no schema change, and it means `_Candidate` and `_build_proposal` are untouched. A position with no such proposal (bought by hand, or pre-dating StockBrain) is skipped and logged — a plan for exiting positions StockBrain never opened is a separate decision.
2. **The peak is tracked from broker prices; the decision is priced from the market-data quote.** Tracking on every account sync is free and needs no extra API call. The trigger is therefore approximate, and the sweep interval is the real resolution of every price-based rule — a −8% stop checked every 5 minutes is "sell at the first observation below −8%", not "sell at −8%". This limitation is documented, not engineered away; the fix is broker-side stop orders in a later plan.
3. **Five rules, one fires, fixed precedence:** `hard_stop` → `trailing_stop` → `thesis_superseded` → `roi_target` → `horizon_elapsed`. Loss-limiting first (most money at stake per unit of delay), then profit-protecting, then judgment, then targets, then time.
4. **`roi_target` proposes REDUCE; every other rule proposes SELL.** Banking half into strength and letting the rest run to the horizon is available today because `reduce_fraction` is already `0.5` (`risk/config.py:127`) and `sizing.py:363` already implements it.
5. **The confidence floor must not block exits.** `_research_confidence_floor` currently BLOCKs any action below `min_research_confidence`, including SELL. Refusing to close a position because the original research was lukewarm is exactly the hazard the module header names. Task 4 makes it skip for risk-reducing actions.
6. **Entry basis is `Position.average_price`**, the broker's own average cost, so adding to a position moves the basis correctly. Ratios cancel currency, so no FX conversion is needed inside the rules.
7. **The exit sweep is off by default** (`EXIT_SWEEP_ENABLED=false`). It ships dark and is enabled deliberately, like every other gate in this system.

## Out of scope — follow-on plans

Do not implement these here. Each needs its own plan.

- **`REASSESS_POSITION` LLM handler** — the `custom_exit` equivalent. The enum exists (`enums.py:472`) with no handler. Depends on this plan's sweep for its trigger.
- **`expected_move_pct` on `ResearchDecision`** — would let `roi_target` compare against what research actually expected rather than a configured percentage. Needs a `prompt_version` and `config_version` bump, which invalidates research dedupe keys.
- **Per-proposal-kind execution policy** — so entries stay MANUAL while protective exits may be AUTOMATIC. Today `ExecutionPolicy` is deployment-global (`enums.py:191`).
- **Broker-side stop orders** — `POST /equity/orders/stop` and `DELETE /equity/orders/{id}` (spec §4.1), neither implemented. The only thing that protects you between sweeps.
- **A separate risk envelope for protective sells** — `max_spread_bps=50` and `max_quote_age_seconds=15` can block an exit exactly when markets are disorderly. Deliberately unchanged here; see "Known limitations".

## Known limitations this plan ships with

State these in the PR description; they are consequences of the decisions above, not defects to fix mid-plan.

- Price-based exits resolve no finer than the sweep interval, and not at all while the process is down.
- An exit proposal is subject to the same spread and quote-freshness gates as an entry, so a disorderly market can block it. The evaluation is still recorded, so a blocked exit is visible rather than silent.
- Positions StockBrain did not open are tracked for peaks but never exited.
- `proposal_ttl_minutes=30` applies, so an unapproved exit proposal expires and the next sweep re-proposes it. That is intended: a stale exit priced half an hour ago should not be approvable.

---

## File Structure

**Created:**
- `backend/alembic/versions/20260911_1200_position_peaks.py` — the one migration.
- `backend/stockbrain/risk/exits.py` — pure exit rules and precedence. No I/O, no session, no settings. The only new risk logic.
- `backend/stockbrain/proposals/exits.py` — `ExitSweepService`: reads positions, calls the rules, calls `generate_exit`. Owns no thresholds.
- `backend/tests/unit/test_risk_exits.py` — rule arithmetic and precedence.
- `backend/tests/integration/test_exit_sweep.py` — peak persistence, origin-thesis recovery, end-to-end sweep.

**Modified:**
- `backend/stockbrain/db/models/portfolio.py` — add `PositionPeak`.
- `backend/stockbrain/broker/account_state.py:94-175` — upsert peaks in `sync`, delete them with closed positions.
- `backend/stockbrain/risk/config.py` — exit thresholds on `RiskConfig` + `risk_config_from_settings`.
- `backend/stockbrain/config.py` — the matching `Settings` fields and validation.
- `backend/stockbrain/risk/rules.py:709` — confidence floor skips risk-reducing actions.
- `backend/stockbrain/proposals/service.py` — `generate_exit`.
- `backend/stockbrain/services.py` — construct `ExitSweepService`, register the schedule.
- `backend/stockbrain/api/settings_model.py` — render the new settings.
- `docs/architecture.md`, `docs/operations.md` — the exit policy and its operator surface.

---

### Task 1: Position peak tracking

The high-water mark nothing currently stores. Trailing stops are impossible without it.

**Files:**
- Create: `backend/alembic/versions/20260911_1200_position_peaks.py`
- Modify: `backend/stockbrain/db/models/portfolio.py` (after `Position`, before `BrokerOrder` at line 91)
- Modify: `backend/stockbrain/broker/account_state.py:94-175`
- Test: `backend/tests/integration/test_exit_sweep.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `PositionPeak` with columns `broker`, `account_id`, `broker_ticker`, `peak_price: Decimal`, `peak_at: dt.datetime`, `observations: int`, and unique constraint `uq_position_peaks_broker_account_id_broker_ticker`. `AccountStateService.sync` maintains it.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/integration/test_exit_sweep.py
"""Exit sweep: peak tracking, origin-thesis recovery and proposal generation."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import sqlalchemy as sa

from stockbrain.db.models.portfolio import PositionPeak
from stockbrain.db.session import Database


async def test_the_peak_ratchets_up_and_never_down(clean_tables: Database) -> None:
    database = clean_tables
    service = await _account_service(database, prices=[Decimal("100")])
    await service.sync()

    service.client.prices = [Decimal("130")]
    await service.sync()

    service.client.prices = [Decimal("110")]
    await service.sync()

    async with database.session() as session:
        peak = (await session.execute(sa.select(PositionPeak))).scalar_one()
    assert peak.peak_price == Decimal("130")
    assert peak.observations == 3
```

Write `_account_service` as a local helper in the same file: a fake client with a mutable `prices` list, returning one position for `AAPL_US_EQ` whose `current_price` is `prices[0]`, plus a minimal account summary. Model it on the existing fake in `backend/tests/unit/test_trading212_account.py` — read that file first and match its shape rather than inventing a new double.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/integration/test_exit_sweep.py -v`
Expected: FAIL with `ImportError: cannot import name 'PositionPeak'`

- [ ] **Step 3: Add the model**

```python
# backend/stockbrain/db/models/portfolio.py — after Position, before BrokerOrder
class PositionPeak(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Highest broker-reported price seen while a position was open.

    Fed by every account sync, which costs no extra API call.  The price is
    broker-supplied and explicitly not real-time, so this is a *trigger*
    reference only: a proposal's reference price always comes from the
    market-data path via the evaluator.

    The row is deleted with the position it belongs to.  A name closed and
    re-bought is a new position with a new thesis, and inheriting the old
    peak would arm a trailing stop against a high this position never saw.
    """

    __tablename__ = "position_peaks"

    broker: Mapped[Broker] = mapped_column(pg_enum(Broker, "broker"), nullable=False)
    account_id: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="default")
    broker_ticker: Mapped[str] = mapped_column(sa.Text, nullable=False)

    peak_price: Mapped[Decimal] = mapped_column(sa.Numeric(24, 8), nullable=False)
    peak_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    observations: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="1")
    """How many syncs contributed.  A peak from one observation is a peak the
    trailing rule should not yet trust, and this is how it can tell."""

    __table_args__ = (
        sa.UniqueConstraint(
            "broker",
            "account_id",
            "broker_ticker",
            name="uq_position_peaks_broker_account_id_broker_ticker",
        ),
    )
```

- [ ] **Step 4: Generate and review the migration**

Run: `make revision m="add position peaks"`

Open the generated file, rename it to `backend/alembic/versions/20260911_1200_position_peaks.py`, and confirm `down_revision = "c3f28a1d6b45"`. Delete any operation autogenerate invented beyond `create_table("position_peaks")` and its unique constraint — `alembic check` reported no pending operations before this task, so anything else is drift and must not ride along.

Then: `make migrate`

- [ ] **Step 5: Maintain the peak in `sync`**

In `backend/stockbrain/broker/account_state.py`, inside the `for position in positions:` loop (after the `Position` upsert, still in the same transaction):

```python
                if position.current_price is not None:
                    peak_values = {
                        "broker": self._broker,
                        "account_id": account_id,
                        "broker_ticker": position.broker_ticker,
                        "peak_price": position.current_price,
                        "peak_at": captured_at,
                        "updated_at": captured_at,
                    }
                    peak_statement = pg_insert(PositionPeak).values(
                        created_at=captured_at, observations=1, **peak_values
                    )
                    await session.execute(
                        peak_statement.on_conflict_do_update(
                            index_elements=[
                                PositionPeak.broker,
                                PositionPeak.account_id,
                                PositionPeak.broker_ticker,
                            ],
                            set_={
                                "peak_price": sa.func.greatest(
                                    PositionPeak.peak_price,
                                    peak_statement.excluded.peak_price,
                                ),
                                # Only restamp the timestamp when the high is
                                # actually new, so `peak_at` answers "when did
                                # this position top out" rather than "when did
                                # we last look".
                                "peak_at": sa.case(
                                    (
                                        peak_statement.excluded.peak_price
                                        > PositionPeak.peak_price,
                                        peak_statement.excluded.peak_at,
                                    ),
                                    else_=PositionPeak.peak_at,
                                ),
                                "observations": PositionPeak.observations + 1,
                                "updated_at": peak_statement.excluded.updated_at,
                            },
                        )
                    )
```

Then extend the closed-position delete immediately below it, so a peak never outlives its position:

```python
            peak_delete = sa.delete(PositionPeak).where(
                PositionPeak.broker == self._broker,
                PositionPeak.account_id == account_id,
            )
            if seen:
                peak_delete = peak_delete.where(PositionPeak.broker_ticker.not_in(seen))
            await session.execute(peak_delete)
```

Add `PositionPeak` to the existing `from stockbrain.db.models.portfolio import ...` line at the top of the file.

- [ ] **Step 6: Run the test**

Run: `cd backend && .venv/bin/pytest tests/integration/test_exit_sweep.py -v`
Expected: PASS

- [ ] **Step 7: Add the closed-position test**

```python
async def test_closing_a_position_deletes_its_peak(clean_tables: Database) -> None:
    database = clean_tables
    service = await _account_service(database, prices=[Decimal("100")])
    await service.sync()

    service.client.positions = []
    await service.sync()

    async with database.session() as session:
        remaining = (await session.execute(sa.select(sa.func.count()).select_from(PositionPeak))).scalar_one()
    assert remaining == 0
```

Run: `cd backend && .venv/bin/pytest tests/integration/test_exit_sweep.py -v`
Expected: both PASS

- [ ] **Step 8: Commit**

```bash
git add backend/stockbrain/db/models/portfolio.py backend/stockbrain/broker/account_state.py backend/alembic/versions/20260911_1200_position_peaks.py backend/tests/integration/test_exit_sweep.py
git commit -m "feat(portfolio): track a high-water mark per open position"
```

---

### Task 2: Exit thresholds in RiskConfig

Thresholds belong where the content hash already versions them.

**Files:**
- Modify: `backend/stockbrain/risk/config.py`
- Modify: `backend/stockbrain/config.py`
- Modify: `backend/stockbrain/api/settings_model.py`
- Test: `backend/tests/unit/test_risk_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: on `RiskConfig` — `exit_hard_stop_pct: Decimal`, `exit_trailing_pct: Decimal`, `exit_trailing_arm_pct: Decimal`, `exit_min_peak_observations: int`, `exit_roi_decay: tuple[tuple[TimeHorizon, int, Decimal], ...]`. On `Settings` — `RISK_EXIT_HARD_STOP_PCT`, `RISK_EXIT_TRAILING_PCT`, `RISK_EXIT_TRAILING_ARM_PCT`, `RISK_EXIT_MIN_PEAK_OBSERVATIONS`, `EXIT_SWEEP_ENABLED`, `EXIT_SWEEP_INTERVAL_SECONDS`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_risk_config.py — append
def test_exit_thresholds_change_the_policy_version() -> None:
    baseline = h.config()
    stricter = h.config(exit_hard_stop_pct=Decimal("0.05"))
    assert baseline.version != stricter.version


def test_the_roi_decay_table_covers_every_horizon() -> None:
    from stockbrain.enums import TimeHorizon

    covered = {horizon for horizon, _minutes, _target in h.config().exit_roi_decay}
    assert covered == set(TimeHorizon)


def test_the_roi_decay_table_is_non_increasing_within_each_horizon() -> None:
    from itertools import groupby

    rows = sorted(h.config().exit_roi_decay, key=lambda row: (row[0].value, row[1]))
    for _horizon, group in groupby(rows, key=lambda row: row[0]):
        targets = [target for _h, _m, target in group]
        assert targets == sorted(targets, reverse=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/unit/test_risk_config.py -v -k exit`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'exit_hard_stop_pct'`

- [ ] **Step 3: Add the fields to `RiskConfig`**

In `backend/stockbrain/risk/config.py`, after the `reduce_fraction` block (around line 127):

```python
    # -- Exit policy -------------------------------------------------------
    exit_hard_stop_pct: Decimal = Decimal("0.08")
    """Loss from average cost at which the whole position is proposed for exit.
    A floor, never widened: the one number in this config whose job is to be
    hit."""

    exit_trailing_pct: Decimal = Decimal("0.05")
    """How far below the high-water mark the trailing floor sits, once armed."""

    exit_trailing_arm_pct: Decimal = Decimal("0.10")
    """Gain from average cost at which trailing switches on.  Below this the
    hard stop is the only floor, so an ordinary wobble after entry does not
    close a position that never went anywhere."""

    exit_min_peak_observations: int = 3
    """Syncs a peak must be built from before the trailing rule trusts it.  One
    observation is an entry price wearing a peak's name."""

    exit_roi_decay: tuple[tuple[TimeHorizon, int, Decimal], ...] = (
        (TimeHorizon.INTRADAY, 0, Decimal("0.04")),
        (TimeHorizon.INTRADAY, 240, Decimal("0.02")),
        (TimeHorizon.INTRADAY, 480, Decimal("0")),
        (TimeHorizon.DAYS, 0, Decimal("0.06")),
        (TimeHorizon.DAYS, 1440, Decimal("0.03")),
        (TimeHorizon.DAYS, 4320, Decimal("0")),
        (TimeHorizon.WEEKS, 0, Decimal("0.15")),
        (TimeHorizon.WEEKS, 10080, Decimal("0.08")),
        (TimeHorizon.WEEKS, 30240, Decimal("0")),
        (TimeHorizon.MONTHS, 0, Decimal("0.30")),
        (TimeHorizon.MONTHS, 43200, Decimal("0.15")),
        (TimeHorizon.MONTHS, 129600, Decimal("0")),
    )
    """Profit required to bank a reduction, by thesis horizon and minutes held.

    Demand a lot early, accept less as the thesis ages.  The terminal ``0`` row
    is the horizon-elapsed boundary: past it, any non-negative result is worth
    taking, because an event thesis that has not resolved by then is no longer
    the reason the position is held.
    """
```

Add `from stockbrain.enums import TimeHorizon` to the imports if absent, and extend `_jsonable` so the tuple digests deterministically:

```python
    if isinstance(value, TimeHorizon):
        return value.value
```

- [ ] **Step 4: Add the `Settings` fields and wire them through**

In `backend/stockbrain/config.py`, beside the existing `risk_*` fields, following their exact declaration style (read the neighbours first and match them):

```python
    risk_exit_hard_stop_pct: Decimal = Field(
        default=Decimal("0.08"), gt=0, lt=1, alias="RISK_EXIT_HARD_STOP_PCT"
    )
    risk_exit_trailing_pct: Decimal = Field(
        default=Decimal("0.05"), gt=0, lt=1, alias="RISK_EXIT_TRAILING_PCT"
    )
    risk_exit_trailing_arm_pct: Decimal = Field(
        default=Decimal("0.10"), ge=0, lt=1, alias="RISK_EXIT_TRAILING_ARM_PCT"
    )
    risk_exit_min_peak_observations: int = Field(
        default=3, ge=1, le=100, alias="RISK_EXIT_MIN_PEAK_OBSERVATIONS"
    )
    exit_sweep_enabled: bool = Field(default=False, alias="EXIT_SWEEP_ENABLED")
    exit_sweep_interval_seconds: float = Field(
        default=300.0, ge=30.0, le=3600.0, alias="EXIT_SWEEP_INTERVAL_SECONDS"
    )
```

Add a validator next to `_validate_risk_limits`:

```python
    @model_validator(mode="after")
    def _validate_exit_policy(self) -> Settings:
        """Refuse an exit policy that cannot mean anything.

        A trailing floor wider than the arm threshold would sit below the entry
        price at the moment it armed, so the trailing rule would fire
        immediately on every position that reached its arm level -- a take
        profit wearing a trailing stop's name.
        """
        if self.risk_exit_trailing_pct >= self.risk_exit_trailing_arm_pct:
            raise ValueError(
                "RISK_EXIT_TRAILING_PCT must be below RISK_EXIT_TRAILING_ARM_PCT: a floor "
                f"{self.risk_exit_trailing_pct} below a peak armed at "
                f"{self.risk_exit_trailing_arm_pct} would already be under the entry price"
            )
        return self
```

Then extend `risk_config_from_settings` with the five mappings (`exit_roi_decay` stays on its dataclass default — a twelve-row table is not an environment variable).

- [ ] **Step 5: Render the settings in the operator surface**

In `backend/stockbrain/api/settings_model.py`, add one `_s(...)` entry per new setting beside the existing `risk_*` entries, each with mutability class `RESTART_REQUIRED`. The existing test asserting that no `risk_*` value is runtime-editable must keep passing — if it fails, the mutability class is wrong, not the test.

- [ ] **Step 6: Add the validator test and run everything**

```python
# backend/tests/unit/test_config.py — append, matching the file's existing style
def test_a_trailing_floor_wider_than_its_arm_threshold_is_refused(
    make_settings: Any,
) -> None:
    with pytest.raises(ValidationError, match="already be under the entry price"):
        make_settings(
            RISK_EXIT_TRAILING_PCT="0.20",
            RISK_EXIT_TRAILING_ARM_PCT="0.10",
        )
```

Run: `cd backend && .venv/bin/pytest tests/unit/test_risk_config.py tests/unit/test_config.py tests/unit/test_settings_model.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add backend/stockbrain/risk/config.py backend/stockbrain/config.py backend/stockbrain/api/settings_model.py backend/tests/unit/test_risk_config.py backend/tests/unit/test_config.py
git commit -m "feat(risk): add exit thresholds to the versioned risk config"
```

---

### Task 3: The exit rules

Pure arithmetic. No session, no settings, no network — which is why this is the task with the most tests and the least risk.

**Files:**
- Create: `backend/stockbrain/risk/exits.py`
- Test: `backend/tests/unit/test_risk_exits.py`

**Interfaces:**
- Consumes: `RiskConfig` exit fields from Task 2; `RuleResult` / `RuleOutcome` from `risk/models.py`; `ThesisAction`, `TimeHorizon` from `enums.py`.
- Produces:
  - `ExitObservation` — frozen dataclass: `broker_ticker: str`, `quantity: Decimal`, `quantity_available: Decimal`, `average_price: Decimal`, `current_price: Decimal`, `peak_price: Decimal | None`, `peak_observations: int`, `opened_at: dt.datetime`, `horizon: TimeHorizon`, `thesis_superseded: bool`.
  - `ExitSignal` — frozen dataclass: `rule_id: str`, `action: ThesisAction`, `reason: str`, `rule: RuleResult`.
  - `evaluate_exit(observation: ExitObservation, config: RiskConfig, *, now: dt.datetime) -> ExitSignal | None`
  - `EXIT_PRECEDENCE: tuple[str, ...]`
  - `roi_target_for(horizon: TimeHorizon, minutes_held: int, config: RiskConfig) -> Decimal`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/unit/test_risk_exits.py
"""Deterministic exits: the arithmetic, and which rule wins when several fire.

Every rule here is a ratio against a broker-supplied price, so the tests state
the *direction* of each threshold and the precedence between them.  Nothing in
this module touches a database, a quote provider or a broker.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from stockbrain.enums import ThesisAction, TimeHorizon
from stockbrain.risk.exits import (
    EXIT_PRECEDENCE,
    ExitObservation,
    evaluate_exit,
    roi_target_for,
)
from tests import risk_helpers as h

NOW = dt.datetime(2026, 9, 11, 15, 0, tzinfo=dt.UTC)


def observe(**overrides: Any) -> ExitObservation:
    defaults: dict[str, Any] = {
        "broker_ticker": "AAPL_US_EQ",
        "quantity": Decimal("10"),
        "quantity_available": Decimal("10"),
        "average_price": Decimal("100"),
        "current_price": Decimal("100"),
        "peak_price": Decimal("100"),
        "peak_observations": 10,
        "opened_at": NOW - dt.timedelta(minutes=30),
        "horizon": TimeHorizon.WEEKS,
        "thesis_superseded": False,
    }
    defaults.update(overrides)
    return ExitObservation(**defaults)


def test_a_flat_position_inside_its_horizon_produces_no_signal() -> None:
    assert evaluate_exit(observe(), h.config(), now=NOW) is None


def test_the_hard_stop_fires_below_the_configured_loss() -> None:
    signal = evaluate_exit(observe(current_price=Decimal("91")), h.config(), now=NOW)
    assert signal is not None
    assert signal.rule_id == "hard_stop"
    assert signal.action is ThesisAction.SELL


def test_the_hard_stop_holds_at_exactly_the_threshold() -> None:
    # 8% below 100 is 92.  The floor is breached *below* it, not at it.
    assert evaluate_exit(observe(current_price=Decimal("92")), h.config(), now=NOW) is None


def test_trailing_does_not_arm_before_the_position_has_run() -> None:
    # Up 6%, below the 10% arm level: a 5% pullback from the peak must not exit.
    signal = evaluate_exit(
        observe(current_price=Decimal("100.70"), peak_price=Decimal("106")),
        h.config(),
        now=NOW,
    )
    assert signal is None


def test_trailing_fires_once_armed_and_pulled_back() -> None:
    signal = evaluate_exit(
        observe(current_price=Decimal("114"), peak_price=Decimal("125")),
        h.config(),
        now=NOW,
    )
    assert signal is not None
    assert signal.rule_id == "trailing_stop"
    assert signal.action is ThesisAction.SELL


def test_trailing_ignores_a_peak_built_from_too_few_observations() -> None:
    signal = evaluate_exit(
        observe(current_price=Decimal("114"), peak_price=Decimal("125"), peak_observations=1),
        h.config(),
        now=NOW,
    )
    assert signal is None


def test_a_superseded_thesis_proposes_a_full_exit() -> None:
    signal = evaluate_exit(observe(thesis_superseded=True), h.config(), now=NOW)
    assert signal is not None
    assert signal.rule_id == "thesis_superseded"
    assert signal.action is ThesisAction.SELL


def test_the_roi_target_banks_half_rather_than_liquidating() -> None:
    signal = evaluate_exit(observe(current_price=Decimal("116")), h.config(), now=NOW)
    assert signal is not None
    assert signal.rule_id == "roi_target"
    assert signal.action is ThesisAction.REDUCE


def test_the_roi_target_decays_with_time_held() -> None:
    config = h.config()
    assert roi_target_for(TimeHorizon.WEEKS, 0, config) == Decimal("0.15")
    assert roi_target_for(TimeHorizon.WEEKS, 10080, config) == Decimal("0.08")
    assert roi_target_for(TimeHorizon.WEEKS, 40000, config) == Decimal("0")


def test_an_elapsed_horizon_exits_a_position_that_is_merely_flat() -> None:
    signal = evaluate_exit(
        observe(current_price=Decimal("100"), opened_at=NOW - dt.timedelta(days=30)),
        h.config(),
        now=NOW,
    )
    assert signal is not None
    assert signal.rule_id == "horizon_elapsed"
    assert signal.action is ThesisAction.SELL


def test_an_elapsed_horizon_does_not_exit_at_a_loss_through_the_roi_path() -> None:
    # Past the terminal row but underwater: the hard stop owns losses, and a
    # zero ROI target must never be read as "sell anything".
    signal = evaluate_exit(
        observe(current_price=Decimal("95"), opened_at=NOW - dt.timedelta(days=30)),
        h.config(),
        now=NOW,
    )
    assert signal is not None
    assert signal.rule_id == "horizon_elapsed"


def test_the_hard_stop_outranks_every_other_rule() -> None:
    signal = evaluate_exit(
        observe(
            current_price=Decimal("80"),
            peak_price=Decimal("130"),
            thesis_superseded=True,
            opened_at=NOW - dt.timedelta(days=60),
        ),
        h.config(),
        now=NOW,
    )
    assert signal is not None
    assert signal.rule_id == "hard_stop"


def test_precedence_is_declared_and_complete() -> None:
    assert EXIT_PRECEDENCE == (
        "hard_stop",
        "trailing_stop",
        "thesis_superseded",
        "roi_target",
        "horizon_elapsed",
    )


@pytest.mark.parametrize("price", [Decimal("0"), Decimal("-1")])
def test_a_non_positive_basis_produces_no_signal(price: Decimal) -> None:
    assert evaluate_exit(observe(average_price=price), h.config(), now=NOW) is None


def test_nothing_is_proposed_when_no_shares_are_available_to_trade() -> None:
    signal = evaluate_exit(
        observe(current_price=Decimal("80"), quantity_available=Decimal("0")),
        h.config(),
        now=NOW,
    )
    assert signal is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/unit/test_risk_exits.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'stockbrain.risk.exits'`

- [ ] **Step 3: Implement the module**

```python
# backend/stockbrain/risk/exits.py
"""Deterministic exit rules for open positions.

Five rules, one of which fires, in a precedence that is declared rather than
emergent.  Ordering is by what is at stake per unit of delay: limiting a loss
first, protecting a gain second, acting on changed research third, banking a
target fourth, and recycling dead capital last.

Every rule is a ratio against ``Position.average_price``, so currency cancels
and no FX conversion belongs here.  The prices are broker-supplied and
explicitly not real-time: a signal from this module is a *trigger*, and the
reference price on the resulting proposal always comes from the market-data
path through the ordinary evaluator.

Nothing here reads a database, a setting or a clock.  ``now`` is a parameter.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from stockbrain.enums import ThesisAction, TimeHorizon
from stockbrain.risk.config import RiskConfig
from stockbrain.risk.models import RuleOutcome, RuleResult

__all__ = [
    "EXIT_PRECEDENCE",
    "ExitObservation",
    "ExitSignal",
    "evaluate_exit",
    "roi_target_for",
]

ZERO = Decimal(0)

#: The order rules are consulted in.  The first to fire wins and the rest are
#: not evaluated, so two rules can never propose two orders for one position.
EXIT_PRECEDENCE: tuple[str, ...] = (
    "hard_stop",
    "trailing_stop",
    "thesis_superseded",
    "roi_target",
    "horizon_elapsed",
)


@dataclass(frozen=True, slots=True)
class ExitObservation:
    """One open position as the sweep found it."""

    broker_ticker: str
    quantity: Decimal
    quantity_available: Decimal
    average_price: Decimal
    current_price: Decimal
    peak_price: Decimal | None
    peak_observations: int
    opened_at: dt.datetime
    horizon: TimeHorizon
    thesis_superseded: bool

    @property
    def gain(self) -> Decimal:
        """Fractional result against average cost.  Positive is profit."""
        return (self.current_price - self.average_price) / self.average_price

    def minutes_held(self, now: dt.datetime) -> int:
        return max(0, int((now - self.opened_at).total_seconds() // 60))


@dataclass(frozen=True, slots=True)
class ExitSignal:
    """Which rule fired, and what it proposes."""

    rule_id: str
    action: ThesisAction
    reason: str
    rule: RuleResult


def roi_target_for(horizon: TimeHorizon, minutes_held: int, config: RiskConfig) -> Decimal:
    """The profit required to bank a reduction at this age.

    The applicable row is the last one whose minute threshold has been passed,
    so the table reads as a decay rather than a set of windows.
    """
    target = ZERO
    threshold = -1
    for row_horizon, row_minutes, row_target in config.exit_roi_decay:
        if row_horizon is not horizon:
            continue
        if row_minutes <= minutes_held and row_minutes > threshold:
            threshold, target = row_minutes, row_target
    return target


def _fired(rule_id: str, reason: str, observed: str, threshold: str) -> RuleResult:
    return RuleResult(
        rule_id=rule_id,
        rule_version=1,
        outcome=RuleOutcome.WARN,
        reason=reason,
        observed=observed,
        threshold=threshold,
    )


def evaluate_exit(
    observation: ExitObservation, config: RiskConfig, *, now: dt.datetime
) -> ExitSignal | None:
    """The first rule in ``EXIT_PRECEDENCE`` that fires, or ``None`` to hold.

    ``None`` covers "nothing to do" and "nothing can be done" alike: a position
    with no tradable shares or no usable cost basis produces no signal, because
    a proposal the broker would refuse is worse than no proposal.
    """
    if observation.average_price <= ZERO or observation.current_price <= ZERO:
        return None
    if observation.quantity_available <= ZERO:
        return None

    gain = observation.gain
    minutes = observation.minutes_held(now)

    # 1. hard_stop -- the floor, never widened.
    if gain < -config.exit_hard_stop_pct:
        return ExitSignal(
            rule_id="hard_stop",
            action=ThesisAction.SELL,
            reason=(
                f"the position is {_pct(gain)} against average cost, past the "
                f"{_pct(-config.exit_hard_stop_pct)} floor"
            ),
            rule=_fired(
                "hard_stop",
                "the hard stop was breached",
                _pct(gain),
                _pct(-config.exit_hard_stop_pct),
            ),
        )

    # 2. trailing_stop -- armed only once the position has genuinely run, and
    #    only against a peak built from enough observations to mean something.
    peak = observation.peak_price
    if (
        peak is not None
        and observation.peak_observations >= config.exit_min_peak_observations
        and (peak - observation.average_price) / observation.average_price
        >= config.exit_trailing_arm_pct
    ):
        floor = peak * (Decimal(1) - config.exit_trailing_pct)
        if observation.current_price < floor:
            return ExitSignal(
                rule_id="trailing_stop",
                action=ThesisAction.SELL,
                reason=(
                    f"the price fell to {observation.current_price} from a peak of {peak}, "
                    f"through the {_pct(config.exit_trailing_pct)} trailing floor at {floor}"
                ),
                rule=_fired(
                    "trailing_stop",
                    "the trailing floor was breached",
                    str(observation.current_price),
                    str(floor),
                ),
            )

    # 3. thesis_superseded -- research has published a newer conclusion about
    #    this company, so the reason recorded for holding is out of date.
    if observation.thesis_superseded:
        return ExitSignal(
            rule_id="thesis_superseded",
            action=ThesisAction.SELL,
            reason="a newer thesis supersedes the one this position was opened on",
            rule=_fired(
                "thesis_superseded",
                "the opening thesis has been superseded",
                "superseded",
                "the opening thesis is current",
            ),
        )

    target = roi_target_for(observation.horizon, minutes, config)

    # 4. roi_target -- bank half into strength.  A zero target belongs to
    #    horizon_elapsed, not here: "any profit at all" is a statement about
    #    the thesis expiring, not about a target being met.
    if target > ZERO and gain >= target:
        return ExitSignal(
            rule_id="roi_target",
            action=ThesisAction.REDUCE,
            reason=(
                f"the position is up {_pct(gain)} against the {_pct(target)} required "
                f"{minutes} minutes into a {observation.horizon.value} thesis"
            ),
            rule=_fired("roi_target", "the decaying profit target was met", _pct(gain), _pct(target)),
        )

    # 5. horizon_elapsed -- the thesis has run out of time.  Any non-negative
    #    result is worth recycling; a negative one is the hard stop's business
    #    and is left to it, so a slow loser is not closed at an arbitrary hour.
    if target == ZERO and gain >= ZERO:
        return ExitSignal(
            rule_id="horizon_elapsed",
            action=ThesisAction.SELL,
            reason=(
                f"the {observation.horizon.value} thesis has been held {minutes} minutes, "
                f"past its horizon, at {_pct(gain)}"
            ),
            rule=_fired(
                "horizon_elapsed",
                "the thesis horizon elapsed",
                f"{minutes} minutes",
                "within the thesis horizon",
            ),
        )

    return None


def _pct(value: Decimal) -> str:
    return f"{(value * 100).quantize(Decimal('0.01'))}%"
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/unit/test_risk_exits.py -v`
Expected: all PASS. If `RuleOutcome.WARN` does not exist, read the enum in `backend/stockbrain/risk/models.py` and use the member that means "noted, not blocking" — do not add a new member.

- [ ] **Step 5: Type-check and lint**

Run: `cd backend && .venv/bin/mypy --strict stockbrain tests && .venv/bin/ruff format --check . && .venv/bin/ruff check .`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add backend/stockbrain/risk/exits.py backend/tests/unit/test_risk_exits.py
git commit -m "feat(risk): add deterministic exit rules with declared precedence"
```

---

### Task 4: The confidence floor must not block an exit

A one-rule semantic fix, separable because a reviewer could reasonably reject it alone.

**Files:**
- Modify: `backend/stockbrain/risk/rules.py:709-731`
- Test: `backend/tests/unit/test_risk_engine.py`

**Interfaces:**
- Consumes: `RISK_REDUCING_ACTIONS` — if no such constant exists beside `EXPOSURE_INCREASING_ACTIONS` in `risk/rules.py`, add it as `frozenset({ThesisAction.SELL, ThesisAction.REDUCE})` and use it in both places.
- Produces: `_research_confidence_floor` returns a SKIP for risk-reducing actions.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_risk_engine.py — append, matching the file's existing style
@pytest.mark.parametrize("action", [ThesisAction.SELL, ThesisAction.REDUCE])
def test_low_confidence_never_blocks_a_risk_reducing_action(action: ThesisAction) -> None:
    """Refusing to close a position because the research was lukewarm is the
    hazard the rules module's own header names."""
    result = _rule_by_id(
        gate_results(
            h.inputs(
                action=action,
                confidence=Decimal("0.10"),
                state=h.account(positions={"AAPL_US_EQ": h.position()}),
            )
        ),
        "research_confidence_floor",
    )
    assert result.outcome is not RuleOutcome.BLOCK


def test_low_confidence_still_blocks_a_buy() -> None:
    result = _rule_by_id(
        gate_results(h.inputs(action=ThesisAction.BUY, confidence=Decimal("0.10"))),
        "research_confidence_floor",
    )
    assert result.outcome is RuleOutcome.BLOCK
```

Add a `_rule_by_id(results, rule_id)` helper to the file if one is not already there — check first; this file likely has an equivalent, in which case use it rather than adding a second.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/unit/test_risk_engine.py -v -k confidence`
Expected: FAIL — the SELL and REDUCE cases BLOCK

- [ ] **Step 3: Make the rule skip risk-reducing actions**

Replace the body of `_research_confidence_floor` in `backend/stockbrain/risk/rules.py`:

```python
def _research_confidence_floor(inputs: RiskInputs) -> RuleResult:
    """A bounded floor on the research layer's own confidence.

    Confidence is a ranking feature, not a calibrated probability, so it is only
    ever used in the conservative direction: below the floor nothing is
    proposed, and above it confidence may shrink -- never grow -- a size that
    the hard caps already permit.

    It gates *entries* only.  A floor that could block a SELL would turn a
    lukewarm opening thesis into a reason a position can never be closed, which
    is the hazard this module's header names rather than a control.
    """
    floor = inputs.config.min_research_confidence
    if inputs.action in RISK_REDUCING_ACTIONS:
        return _skipped(
            "research_confidence_floor",
            2,
            "the confidence floor gates entries; a reduction is never blocked by it",
            threshold=str(floor),
        )
    ok = inputs.confidence >= floor
    return RuleResult(
        rule_id="research_confidence_floor",
        rule_version=2,
        outcome=RuleOutcome.PASS if ok else RuleOutcome.BLOCK,
        reason=(
            f"research confidence {inputs.confidence} meets the {floor} floor"
            if ok
            else f"research confidence {inputs.confidence} is below the {floor} floor"
        ),
        observed=str(inputs.confidence),
        threshold=str(floor),
    )
```

Note the `rule_version` bump to 2 in both branches: the rule's meaning changed, and a persisted evaluation must be able to say which version judged it.

- [ ] **Step 4: Run the full risk suite**

Run: `cd backend && .venv/bin/pytest tests/unit/test_risk_engine.py tests/unit/test_risk_sizing.py tests/unit/test_risk_security.py tests/unit/test_proposal_evaluation.py -v`
Expected: PASS. Any existing test asserting `rule_version=1` for this rule should be updated to 2 — that is the intended change, not a regression.

- [ ] **Step 5: Commit**

```bash
git add backend/stockbrain/risk/rules.py backend/tests/unit/test_risk_engine.py
git commit -m "fix(risk): stop the confidence floor blocking exits"
```

---

### Task 5: `ProposalService.generate_exit`

The bridge from an exit signal to an ordinary proposal, reusing the whole existing path.

**Files:**
- Modify: `backend/stockbrain/proposals/service.py`
- Test: `backend/tests/integration/test_exit_sweep.py`

**Interfaces:**
- Consumes: `ExitSignal` from Task 3; the existing `_Candidate`, `_evaluator()`, `_build_proposal`, `_record_evaluation`, `_notify`, `_lock_account`, `_reload_identity`, `control_blockers`.
- Produces: `async def generate_exit(self, broker_ticker: str, signal: ExitSignal, *, now: dt.datetime | None = None) -> GenerationResult` and `async def origin_thesis_id(self, session: AsyncSession, broker_ticker: str) -> uuid.UUID | None`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/integration/test_exit_sweep.py — append
async def test_an_exit_proposal_inherits_the_origin_thesis(clean_tables: Database) -> None:
    """Section 17's lineage requirement, satisfied without a schema change: the
    exit proposal points at the thesis the position was opened on."""
    database = clean_tables
    fixture = await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")

    service = await _proposal_service(database)
    signal = ExitSignal(
        rule_id="hard_stop",
        action=ThesisAction.SELL,
        reason="the position is -9.00% against average cost",
        rule=_rule("hard_stop"),
    )
    result = await service.generate_exit("AAPL_US_EQ", signal)

    assert result.created
    async with database.session() as session:
        proposal = await session.get(TradeProposal, result.proposal_id)
    assert proposal is not None
    assert proposal.thesis_id == fixture.thesis_id
    assert proposal.research_run_id == fixture.research_run_id
    assert proposal.side is OrderSide.SELL
    assert {item["reason"] for item in proposal.sizing_reasons} >= {signal.reason}
    assert any(rule["rule_id"] == "hard_stop" for rule in proposal.risk_rules)


async def test_a_position_with_no_origin_proposal_is_not_exited(clean_tables: Database) -> None:
    database = clean_tables
    service = await _proposal_service(database)
    result = await service.generate_exit(
        "MSFT_US_EQ",
        ExitSignal(
            rule_id="hard_stop",
            action=ThesisAction.SELL,
            reason="irrelevant",
            rule=_rule("hard_stop"),
        ),
    )
    assert not result.created
    assert result.reason is not None
    assert "no executed StockBrain buy" in result.reason
```

Write `_seed_executed_buy` in the same file: insert a `Company`, a `BrokerInstrument`, an `Event`, an `EventCompanyImpact`, a `ResearchRun`, a `Thesis` and a `TradeProposal` with `status=ProposalStatus.EXECUTED`, `side=OrderSide.BUY` and `executed_at` set, returning their ids. Read `backend/tests/integration/test_research.py` and `test_proposal_lifecycle.py` first and reuse their seeding helpers if they already exist rather than writing a third one.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/integration/test_exit_sweep.py -v -k exit_proposal`
Expected: FAIL with `AttributeError: 'ProposalService' object has no attribute 'generate_exit'`

- [ ] **Step 3: Implement `origin_thesis_id`**

In `backend/stockbrain/proposals/service.py`, beside `_load_candidate`:

```python
    async def origin_thesis_id(
        self, session: AsyncSession, broker_ticker: str
    ) -> uuid.UUID | None:
        """The thesis the current position was opened on, if StockBrain opened it.

        Defined as the most recently executed BUY proposal for this listing in
        this environment.  A position StockBrain did not open has no thesis to
        exit against, and inventing one would put a research conclusion's name
        on a decision it never made.
        """
        return await session.scalar(
            sa.select(TradeProposal.thesis_id)
            .where(
                TradeProposal.broker == self.broker,
                TradeProposal.broker_ticker == broker_ticker,
                TradeProposal.broker_environment == self.settings.t212_env.value,
                TradeProposal.side == OrderSide.BUY,
                TradeProposal.status == ProposalStatus.EXECUTED,
                TradeProposal.thesis_id.is_not(None),
            )
            .order_by(TradeProposal.executed_at.desc())
            .limit(1)
        )
```

- [ ] **Step 4: Implement `generate_exit`**

Add beside `generate`. It deliberately mirrors `generate`'s structure — load candidate, load facts, lock, evaluate, build, notify — and differs only in where the action comes from and in the two extra `sizing_reasons` and `risk_rules` entries that record which exit rule fired:

```python
    async def generate_exit(
        self,
        broker_ticker: str,
        signal: ExitSignal,
        *,
        now: dt.datetime | None = None,
    ) -> GenerationResult:
        """Turn one exit signal into a proposal, on the ordinary risk path.

        The signal decides *that* the position should be reduced and *why*.
        Everything else -- the quantity, the reference price, the spread, the
        session, the FX -- is the existing evaluator's answer, unchanged.  An
        exit is not a privileged order; it is an ordinary proposal whose action
        came from a rule instead of from research.
        """
        moment = now or utcnow()

        if blockers := await self.control_blockers():
            return GenerationResult(
                thesis_id=None,
                created=False,
                reason="proposal generation is halted: " + "; ".join(blockers),
            )

        async with self.database.session() as session:
            thesis_id = await self.origin_thesis_id(session, broker_ticker)
            if thesis_id is None:
                return GenerationResult(
                    thesis_id=None,
                    created=False,
                    reason=(
                        f"{broker_ticker} has no executed StockBrain buy to exit against, "
                        "so there is no thesis this exit could supersede"
                    ),
                )
            candidate = await self._load_candidate(session, thesis_id)

        if candidate is None:
            return GenerationResult(
                thesis_id=thesis_id,
                created=False,
                reason="the opening thesis or its research run is no longer present",
            )

        # The action is the rule's, not the thesis's.  Confidence is carried over
        # from the opening thesis for the audit trail only: task 4 made the
        # confidence floor skip risk-reducing actions, so it gates nothing here.
        candidate = replace(candidate, action=signal.action)

        evaluator = self._evaluator()
        facts = await evaluator.load(
            candidate.instrument, instrument_currency=candidate.identity.currency, now=moment
        )
        account, quote, fx = facts.account, facts.quote, facts.fx
        account_id = account.account_id if account else DEFAULT_ACCOUNT_ID

        async with self.database.transaction() as session:
            await self._lock_account(session, account_id)
            fresh_identity = await self._reload_identity(session, candidate)
            verdict = await evaluator.evaluate(
                session,
                EvaluationContext(
                    candidate.action, candidate.confidence, fresh_identity, account_id
                ),
                facts,
                now=moment,
            )
            decision = verdict.decision

            evaluation = RiskEvaluation(
                stage=STAGE_GENERATION,
                thesis_id=thesis_id,
                broker=self.broker,
                broker_ticker=fresh_identity.broker_ticker,
                broker_instrument_id=fresh_identity.broker_instrument_id,
                outcome=decision.outcome,
                policy_version=decision.policy_version,
                rules=[rule.as_dict() for rule in (*decision.rules, signal.rule)],
                snapshot=decision.as_dict(),
                snapshot_hash=decision.snapshot_hash(),
                actor=f"system:exit:{signal.rule_id}",
                detail="; ".join(decision.blocks) or signal.reason,
            )
            session.add(evaluation)
            await session.flush()

            METRICS.inc(
                "stockbrain_risk_evaluations_total",
                labels={"stage": STAGE_GENERATION, "outcome": decision.outcome.value},
            )

            if not decision.allowed:
                # A blocked exit is recorded, not dropped: "we wanted out and
                # the envelope refused" is the single most important thing an
                # operator can be told about a holding.
                reason = "; ".join(decision.blocks) or "; ".join(decision.sizing.reasons)
                log.warning(
                    "exit_proposal_blocked",
                    broker_ticker=broker_ticker,
                    exit_rule=signal.rule_id,
                    outcome=decision.outcome.value,
                    blocks=list(decision.block_rule_ids),
                )
                return GenerationResult(
                    thesis_id=thesis_id,
                    created=False,
                    evaluation_id=evaluation.id,
                    outcome=decision.outcome,
                    reason=reason,
                    blocks=decision.blocks,
                )

            assert quote is not None and account is not None
            proposal = self._build_proposal(
                candidate=candidate,
                identity=fresh_identity,
                decision=decision,
                quote=quote,
                account=account,
                fx=fx,
                now=moment,
            )
            # Why this proposal exists, in the fields the GUI and Telegram
            # already render.
            proposal.sizing_reasons = [
                {"reason": signal.reason},
                *proposal.sizing_reasons,
            ]
            proposal.risk_rules = [*proposal.risk_rules, signal.rule.as_dict()]
            proposal.research_action = signal.action.value
            session.add(proposal)
            try:
                await session.flush()
            except IntegrityError as exc:
                await session.rollback()
                log.info(
                    "exit_proposal_deduped",
                    broker_ticker=broker_ticker,
                    exit_rule=signal.rule_id,
                    constraint=_constraint_name(exc),
                )
                return GenerationResult(
                    thesis_id=thesis_id,
                    created=False,
                    outcome=decision.outcome,
                    reason="a live proposal already exists for this listing",
                )

            evaluation.proposal_id = proposal.id
            session.add(
                AuditLog(
                    actor_type=ActorType.SYSTEM,
                    actor_id=f"system:exit:{signal.rule_id}",
                    action="proposal.exit_generated",
                    entity_type="trade_proposal",
                    entity_id=proposal.id,
                    details={
                        "thesis_id": str(thesis_id),
                        "broker_ticker": proposal.broker_ticker,
                        "exit_rule": signal.rule_id,
                        "exit_reason": signal.reason,
                        "side": proposal.side.value,
                        "quantity": str(proposal.proposed_quantity),
                        "risk_outcome": decision.outcome.value,
                        "risk_policy_version": decision.policy_version,
                        "execution_policy": proposal.execution_policy.value,
                    },
                )
            )
            if proposal.execution_policy is ExecutionPolicy.MANUAL:
                await self._notify(session, proposal.id, NotificationEvent.PROPOSAL_MANUAL)
            proposal_id = proposal.id
            evaluation_id = evaluation.id

        METRICS.inc("stockbrain_trade_proposals_total", labels={"outcome": decision.outcome.value})
        METRICS.inc("stockbrain_position_exits_total", labels={"rule": signal.rule_id})
        log.info(
            "exit_proposal_generated",
            proposal_id=str(proposal_id),
            broker_ticker=broker_ticker,
            exit_rule=signal.rule_id,
            side=proposal.side.value,
            quantity=str(proposal.proposed_quantity),
        )
        return GenerationResult(
            thesis_id=thesis_id,
            created=True,
            proposal_id=proposal_id,
            evaluation_id=evaluation_id,
            outcome=decision.outcome,
            reason=signal.reason,
        )
```

Add the imports this needs: `from dataclasses import replace`, `from stockbrain.risk.exits import ExitSignal`, and `OrderSide` / `ProposalStatus` if not already imported. Check `GenerationResult.thesis_id` accepts `None`; if it is typed `uuid.UUID`, widen it to `uuid.UUID | None` and run the existing proposal tests.

**Deliberately not done here:** the automatic-authorization tail that `generate` runs at its end. An exit must not auto-authorize until the per-proposal-kind policy exists in a later plan — inheriting the global `AUTOMATIC` policy would auto-send exits the moment anyone enables automation for entries.

- [ ] **Step 5: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/integration/test_exit_sweep.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/stockbrain/proposals/service.py backend/tests/integration/test_exit_sweep.py
git commit -m "feat(proposals): generate exit proposals against the origin thesis"
```

---

### Task 6: The exit sweep

The scheduled loop that connects held positions to the rules. Ships disabled.

**Files:**
- Create: `backend/stockbrain/proposals/exits.py`
- Modify: `backend/stockbrain/services.py`
- Test: `backend/tests/integration/test_exit_sweep.py`

**Interfaces:**
- Consumes: `evaluate_exit`, `ExitObservation` (Task 3); `generate_exit`, `origin_thesis_id` (Task 5); `PositionPeak` (Task 1); `RiskConfig` (Task 2).
- Produces: `ExitSweepService(database, settings, proposals, config)` with `async def sweep(self, *, now: dt.datetime | None = None, limit: int = 25) -> dict[str, int]`, and a `ScheduledTask` named `position_exit_sweep`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/integration/test_exit_sweep.py — append
async def test_the_sweep_proposes_an_exit_for_a_losing_position(clean_tables: Database) -> None:
    database = clean_tables
    await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")
    await _seed_position(
        database,
        broker_ticker="AAPL_US_EQ",
        average_price=Decimal("100"),
        current_price=Decimal("88"),
    )

    sweep = await _exit_sweep(database)
    counts = await sweep.sweep()

    assert counts["proposed"] == 1
    assert counts["signalled"] == 1
    async with database.session() as session:
        proposal = (
            await session.execute(
                sa.select(TradeProposal).where(TradeProposal.side == OrderSide.SELL)
            )
        ).scalar_one()
    assert any(rule["rule_id"] == "hard_stop" for rule in proposal.risk_rules)


async def test_the_sweep_holds_a_healthy_position(clean_tables: Database) -> None:
    database = clean_tables
    await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")
    await _seed_position(
        database,
        broker_ticker="AAPL_US_EQ",
        average_price=Decimal("100"),
        current_price=Decimal("101"),
    )

    counts = await (await _exit_sweep(database)).sweep()

    assert counts["signalled"] == 0
    assert counts["proposed"] == 0


async def test_the_sweep_skips_a_position_that_already_has_a_live_proposal(
    clean_tables: Database,
) -> None:
    """One active proposal per listing is a database invariant; the sweep must
    not spend a quote request discovering it."""
    database = clean_tables
    await _seed_executed_buy(database, broker_ticker="AAPL_US_EQ")
    await _seed_position(
        database,
        broker_ticker="AAPL_US_EQ",
        average_price=Decimal("100"),
        current_price=Decimal("88"),
    )
    await _seed_live_proposal(database, broker_ticker="AAPL_US_EQ")

    counts = await (await _exit_sweep(database)).sweep()

    assert counts["skipped_active_proposal"] == 1
    assert counts["proposed"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/integration/test_exit_sweep.py -v -k sweep`
Expected: FAIL with `ModuleNotFoundError: No module named 'stockbrain.proposals.exits'`

- [ ] **Step 3: Implement the sweep**

```python
# backend/stockbrain/proposals/exits.py
"""The scheduled loop from held positions to exit proposals.

It owns no thresholds and makes no decisions: it assembles an
``ExitObservation`` per holding, asks ``risk.exits`` what to do, and hands any
signal to ``ProposalService.generate_exit``, which re-prices and re-validates
everything on the ordinary path.

Two cheap filters run before any network request, because the expensive part of
a sweep is the quote: a listing that already has a live proposal cannot take
another one, and a position StockBrain never opened has no thesis to exit.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from decimal import Decimal

import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.base import utcnow
from stockbrain.db.models.portfolio import Position, PositionPeak
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.models.research import ResearchRun, Thesis
from stockbrain.db.session import Database
from stockbrain.enums import OrderSide, ProposalStatus, ResearchStatus, TimeHorizon
from stockbrain.logging import get_logger
from stockbrain.proposals.service import ProposalService
from stockbrain.proposals.state_machine import LIVE_STATUSES
from stockbrain.risk.config import RiskConfig
from stockbrain.risk.exits import ExitObservation, evaluate_exit

__all__ = ["ExitSweepService"]

log = get_logger(__name__)
ZERO = Decimal(0)


class ExitSweepService:
    """Evaluates every open position once per tick."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        proposals: ProposalService,
        config: RiskConfig,
    ) -> None:
        self._database = database
        self._settings = settings
        self._proposals = proposals
        self._config = config

    async def sweep(self, *, now: dt.datetime | None = None, limit: int = 25) -> dict[str, int]:
        """Evaluate open positions and propose at most one exit each.

        Returns a tally rather than raising: one position whose instrument has
        gone missing must not stop the other holdings being looked at.
        """
        moment = now or utcnow()
        counts: Counter[str] = Counter()

        async with self._database.session() as session:
            rows = (
                await session.execute(
                    sa.select(Position, PositionPeak)
                    .outerjoin(
                        PositionPeak,
                        sa.and_(
                            PositionPeak.broker == Position.broker,
                            PositionPeak.account_id == Position.account_id,
                            PositionPeak.broker_ticker == Position.broker_ticker,
                        ),
                    )
                    .where(Position.broker == self._proposals.broker, Position.quantity > ZERO)
                    .order_by(Position.broker_ticker)
                    .limit(limit)
                )
            ).all()

            observations: list[ExitObservation] = []
            for position, peak in rows:
                counts["considered"] += 1

                if await self._has_live_proposal(session, position.broker_ticker):
                    counts["skipped_active_proposal"] += 1
                    continue

                origin = await self._origin(session, position.broker_ticker)
                if origin is None:
                    counts["skipped_no_origin"] += 1
                    continue
                opened_at, horizon, thesis_id = origin

                if position.average_price is None or position.current_price is None:
                    counts["skipped_unpriced"] += 1
                    continue

                observations.append(
                    ExitObservation(
                        broker_ticker=position.broker_ticker,
                        quantity=position.quantity,
                        quantity_available=position.quantity_available or ZERO,
                        average_price=position.average_price,
                        current_price=position.current_price,
                        peak_price=peak.peak_price if peak is not None else None,
                        peak_observations=peak.observations if peak is not None else 0,
                        opened_at=opened_at,
                        horizon=horizon,
                        thesis_superseded=await self._superseded(session, thesis_id),
                    )
                )

        for observation in observations:
            signal = evaluate_exit(observation, self._config, now=moment)
            if signal is None:
                continue
            counts["signalled"] += 1
            result = await self._proposals.generate_exit(
                observation.broker_ticker, signal, now=moment
            )
            counts["proposed" if result.created else "blocked"] += 1

        log.info("exit_sweep_complete", **dict(counts))
        return dict(counts)

    async def _has_live_proposal(self, session: sa.orm.Session, broker_ticker: str) -> bool:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(TradeProposal)
            .where(
                TradeProposal.broker == self._proposals.broker,
                TradeProposal.broker_ticker == broker_ticker,
                TradeProposal.status.in_(LIVE_STATUSES),
            )
        )
        return bool(count)

    async def _origin(
        self, session: sa.orm.Session, broker_ticker: str
    ) -> tuple[dt.datetime, TimeHorizon, "uuid.UUID"] | None:
        """When the position was opened, on what horizon, and on which thesis."""
        row = (
            await session.execute(
                sa.select(TradeProposal.executed_at, Thesis.time_horizon, Thesis.id)
                .join(Thesis, Thesis.id == TradeProposal.thesis_id)
                .where(
                    TradeProposal.broker == self._proposals.broker,
                    TradeProposal.broker_ticker == broker_ticker,
                    TradeProposal.broker_environment == self._settings.t212_env.value,
                    TradeProposal.side == OrderSide.BUY,
                    TradeProposal.status == ProposalStatus.EXECUTED,
                    TradeProposal.executed_at.is_not(None),
                )
                .order_by(TradeProposal.executed_at.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        executed_at, horizon, thesis_id = row
        return executed_at, horizon, thesis_id

    async def _superseded(self, session: sa.orm.Session, thesis_id: "uuid.UUID") -> bool:
        """Whether research has published a successor to the opening thesis.

        The same question `proposals.lifecycle` already asks of a pending
        proposal, asked of a holding.
        """
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(Thesis)
            .join(ResearchRun, ResearchRun.id == Thesis.research_run_id)
            .where(
                Thesis.supersedes_thesis_id == thesis_id,
                ResearchRun.status == ResearchStatus.SUCCEEDED,
            )
        )
        return bool(count)
```

Add `import uuid` and fix the session type annotation to whatever the codebase's async session type is — check the imports in `proposals/lifecycle.py` and match them exactly; `sa.orm.Session` above is a placeholder that `mypy --strict` will reject.

If `LIVE_STATUSES` does not exist in `proposals/state_machine.py`, read that module and use the constant it does provide for "not yet terminal"; do not define a second one.

- [ ] **Step 4: Register the service and the schedule**

In `backend/stockbrain/services.py`, construct `ExitSweepService` where the other proposal-dependent services are built (guarded on `self.proposals is not None`), expose it as `self.exits`, and add to `_register_schedules`:

```python
        if self.exits is not None and self.settings.exit_sweep_enabled:
            # Ships disabled. The sweep only ever generates proposals; every
            # authorization and transmission gate is unchanged by it.
            scheduler.add(
                ScheduledTask(
                    name="position_exit_sweep",
                    interval_seconds=self.settings.exit_sweep_interval_seconds,
                    run=self._exit_sweep,
                    initial_delay_seconds=100.0,
                    jitter_ratio=0.1,
                )
            )
```

and the runner beside `_proposal_sweep`:

```python
    async def _exit_sweep(self) -> None:
        if self.exits is not None:
            await self.exits.sweep()
```

- [ ] **Step 5: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/integration/test_exit_sweep.py -v`
Expected: PASS

- [ ] **Step 6: Assert the schedule ships disabled**

```python
# backend/tests/unit/test_phase9_gates.py — append, matching the file's existing style
def test_the_exit_sweep_is_not_scheduled_by_default(make_settings: Any) -> None:
    settings = make_settings()
    assert settings.exit_sweep_enabled is False
```

Run: `cd backend && .venv/bin/pytest tests/unit/test_phase9_gates.py -v -k exit`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add backend/stockbrain/proposals/exits.py backend/stockbrain/services.py backend/tests/integration/test_exit_sweep.py backend/tests/unit/test_phase9_gates.py
git commit -m "feat(proposals): sweep open positions for deterministic exits"
```

---

### Task 7: Documentation and full verification

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/operations.md`
- Modify: `.env.example` (if one exists — check; the settings tests assert the documented variable set matches `Settings`)

- [ ] **Step 1: Document the exit policy**

In `docs/architecture.md`, add a section after the risk engine's, following the surrounding prose style. It must state: the five rules and their precedence; that `roi_target` reduces and the rest sell; that the peak comes from broker-supplied prices while the proposal's reference price comes from the market-data path; and that the sweep interval is the real resolution of every price-based rule.

In `docs/operations.md`, add: how to enable the sweep (`EXIT_SWEEP_ENABLED=true`), what each threshold means, that thresholds are `RESTART_REQUIRED`, and how to read an exit proposal's `risk_rules` to see which rule fired.

- [ ] **Step 2: Add the new variables to `.env.example`**

Run: `grep -rn "EXIT_SWEEP_ENABLED\|RISK_EXIT" /Users/khaleel/Documents/StockBrain/.env.example 2>/dev/null || ls -a /Users/khaleel/Documents/StockBrain | grep env`

Add every new variable with its default and a one-line comment. `backend/tests/unit/test_settings_model.py` and the `dotenv_variable_names` helper in `conftest.py` assert this file and `Settings` agree — if that test fails, the file is incomplete.

- [ ] **Step 3: Confirm no unintended migration drift**

Run: `cd backend && .venv/bin/alembic check && .venv/bin/alembic heads`
Expected: no new operations; exactly one head, the Task 1 revision

- [ ] **Step 4: Run everything CI runs**

Run: `make check`
Expected: ruff format + check clean, `mypy --strict` clean, full backend suite passing with the pre-existing 24 live tests deselected. The baseline before this plan was 1980 passed — the count must be higher and the failure count must be zero.

- [ ] **Step 5: Commit**

```bash
git add docs/architecture.md docs/operations.md .env.example
git commit -m "docs: describe the deterministic exit policy and its operator surface"
```

---

## Self-review

**Spec coverage.** §17's lineage requirement is met by Task 5 (`origin_thesis_id` + the inherited `thesis_id` / `research_run_id`) and by `thesis_superseded` in Task 3. §14's "every threshold in versioned config" is Task 2. §15's proposal lifecycle is reused unmodified. §22's "the scheduler only enqueues" is Task 6. §17's `REASSESS_POSITION` job and its `supersedes_thesis_id` *writing* side are explicitly deferred — this plan reads that lineage rather than creating new theses, and the follow-on plan is named in "Out of scope".

**Placeholders.** None. Four steps deliberately instruct the implementer to read a neighbouring file and match it rather than quoting code: the test doubles in Task 1 Step 1 and Task 5 Step 1, the `Settings` field style in Task 2 Step 4, and the async session type in Task 6 Step 3. Each names the exact file to read and the exact thing to match, because inventing a second fake account client or a second `LIVE_STATUSES` would be the more likely failure.

**Type consistency.** `ExitObservation`, `ExitSignal`, `evaluate_exit`, `roi_target_for` and `EXIT_PRECEDENCE` are defined in Task 3 and used with those exact names in Tasks 5 and 6. `PositionPeak.peak_price` / `.observations` (Task 1) are read in Task 6. `RiskConfig.exit_*` (Task 2) are read only in Task 3. `generate_exit(broker_ticker, signal, *, now)` (Task 5) is called with that signature in Task 6. `RISK_REDUCING_ACTIONS` is introduced in Task 4 and used only there.

**Known risks flagged for the implementer.** Three symbols are asserted from reading the codebase but must be confirmed on contact, with the instruction to adapt rather than add: `RuleOutcome.WARN` (Task 3 Step 4), `LIVE_STATUSES` (Task 6 Step 3), and `GenerationResult.thesis_id` accepting `None` (Task 5 Step 4).
