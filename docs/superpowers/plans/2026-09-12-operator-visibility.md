# Operator Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make sure nothing the system decides about the operator's money happens silently — a blocked BUY reaches Telegram with its reasons, a thesis blocked only by a closed market waits for the open instead of dying, and every open position shows its exit floors in the GUI, in `/positions`, and in a daily Telegram summary.

**Architecture:** Three independent features on the existing notification, proposal and exit machinery. (A) A `PROPOSAL_BLOCKED` pipeline event enqueued from `ProposalService.generate`'s blocked branch, rendered from the run's latest generation evaluation. (B) A `deferred` flag on `RiskEvaluation`: a block whose every rule is in a declared *transient* set is recorded but not treated as terminal by `enqueue_pending`, which retries it once per proposal TTL until a deferral age limit. (C) A pure `exit_floors()` beside the exit rules, a shared `ExitSweepService.status()` that reuses the sweep's observation builder, and three consumers — the portfolio API/GUI, `/positions`, and a scheduled daily summary. Nothing here touches a broker, authorizes anything, or calls an LLM.

**Tech Stack:** Python 3.12 / SQLAlchemy async / Alembic / python-telegram-bot (existing) / React + TypeScript + Vite (existing). No new dependencies.

**Spec:** `STOCKBRAIN_TECHNICAL_SPEC.md` §15 (proposal lifecycle), §20 (Telegram command set), §21 (notification policy), §22 (scheduler). Predecessor plans: `2026-09-11-deterministic-position-exits.md`, `2026-09-12-volatility-stop.md` (both merged on `main`).

---

## Global Constraints

- **Zero LLM tokens.** No task imports `stockbrain.llm` or `stockbrain.intelligence`.
- **No broker mutation; nothing auto-authorizes.** Deferral re-*evaluates*; it never approves.
- **Notification categories are quiet-by-default only when noisy.** New categories here (`PROPOSAL_BLOCKED`, `PROPOSAL_DEFERRED`, `DAILY_SUMMARY`) default **on** — they are low-volume and each is the kind of message the operator asked for. `EXECUTION_CRITICAL` stays unsilenceable; do not touch it.
- **One message per thing.** Pipeline dedupe keys are `pipeline-notify:{entity_id}:{event}`; a deferred thesis announces its deferral once, its final block once.
- **Settings are `RESTART_REQUIRED`; no write route.**
- **Every threshold lives on `RiskConfig` or `Settings`.** The transient rule set is a code constant (it names rule ids, not numbers).
- **`mypy --strict`, ruff, frontend `tsc -b` + eslint `--max-warnings 0` clean.** Baseline: backend 2122 passed; frontend 97 tests.
- **Alembic head before this work: `cd8d1b706008`.**
- **Frontend renders the API's numbers; it never recomputes money** (the Portfolio page's own test asserts this).
- **Decimal for money.**

## Design decisions

1. **`PROPOSAL_BLOCKED` is a pipeline event on the research run**, not a proposal event — there is no proposal. It fires only for executable actions (BUY/SELL/REDUCE); HOLD/NO_ACTION already show in "Research completed".
2. **Transient = market state, not judgment.** `TRANSIENT_RULE_IDS = {price_source_execution_grade, quote_available, quote_freshness, quote_two_sided, spread_ceiling, market_session, account_state_available, account_state_freshness, fx_available, fx_freshness}`. Currency policy, confidence, concentration, cash, duplicates are *never* transient.
3. **Deferral has a clock and a ceiling.** Retry no more often than `proposal_ttl_minutes`; give up (terminal block + `PROPOSAL_BLOCKED`) once the thesis is older than `PROPOSAL_DEFERRAL_MAX_HOURS` (default 72 — a Friday-evening thesis survives the weekend).
4. **Floors are computed, never stored.** `exit_floors()` derives them from the same `ExitObservation` the sweep uses, so what the operator sees is exactly what the rules will act on.
5. **The daily summary is a pipeline notification with a synthetic entity id** (`uuid5(NAMESPACE, date)`), so the existing dedupe/claim/delivery path guarantees one per day without new tables. A minute-tick scheduled task fires it when the configured UTC time has passed and today's id has not been claimed.
6. **Unmanaged positions are labelled, not hidden.** A position with no StockBrain origin shows "not managed — no StockBrain buy behind it" everywhere the floors would be.

## Out of scope

Editing thresholds from the GUI; per-position charts/history; exiting unmanaged positions; Telegram buttons on the summary; localisation of the summary time (UTC only).

---

## File Structure

**Created:** `backend/alembic/versions/20260912_1400_risk_evaluation_deferred.py`; `backend/tests/unit/test_exit_floors.py`; `backend/tests/integration/test_proposal_deferral.py`; `backend/tests/integration/test_daily_summary.py`.

**Modified (backend):** `telegram/preferences.py`, `telegram/notifier.py`, `telegram/messages.py`, `telegram/service.py`; `proposals/service.py`, `proposals/lifecycle.py`, `proposals/exits.py`; `risk/rules.py`, `risk/exits.py`; `db/models/proposals.py`; `config.py`, `api/settings_model.py`, `api/schemas.py`, `api/routes/portfolio.py`; `services.py`.

**Modified (frontend):** `frontend/src/api/types.ts`, `frontend/src/pages/Portfolio.tsx`, `frontend/src/pages/Portfolio.test.tsx`.

**Docs:** `docs/operations.md`, `.env.example`.

---

### Task 1: `PROPOSAL_BLOCKED` reaches Telegram

**Files:**
- Modify: `backend/stockbrain/telegram/preferences.py` (`PipelineEvent`, `NotificationCategory`, details, defaults, mapping)
- Modify: `backend/stockbrain/telegram/notifier.py:154-165` (`_PIPELINE_ENTITY`, `_PIPELINE_TITLES`)
- Modify: `backend/stockbrain/telegram/service.py` (`ResearchRunView.block_reasons`, loader)
- Modify: `backend/stockbrain/telegram/messages.py:482` (`render_research_stage`)
- Modify: `backend/stockbrain/proposals/service.py` (ctor takes `preferences`; enqueue in blocked branch)
- Modify: `backend/stockbrain/services.py` (pass `preferences=self.notification_preferences`)
- Test: `backend/tests/unit/test_telegram_messages.py`, `backend/tests/integration/test_proposals.py`

**Interfaces:**
- `PipelineEvent.PROPOSAL_BLOCKED = "PROPOSAL_BLOCKED"`; `NotificationCategory.PROPOSAL_BLOCKED` (default True; detail: "Research recommended a trade and the risk engine refused it. Lists the blocking rules.").
- `ResearchRunView.block_reasons: tuple[str, ...] = ()` — the `reason` of every BLOCK rule in the latest `GENERATION` evaluation for the run's thesis.
- `ProposalService.__init__(..., preferences: NotificationPreferences | None = None)`.

- [ ] **Step 1: Failing tests**

```python
# backend/tests/unit/test_telegram_messages.py — append, using the file's existing ResearchRunView factory
def test_a_blocked_proposal_lists_the_rules_that_refused_it() -> None:
    view = _research_view(action="BUY", confidence=0.64, block_reasons=(
        "research confidence 0.64 is below the 0.70 floor",
        "instrument USD != account GBP",
    ))
    text = render_research_stage(view, PipelineEvent.PROPOSAL_BLOCKED)
    assert "Trade blocked" in text
    assert "0.70 floor" in text and "USD != account GBP" in text
    assert "never authorizes" not in text  # that line belongs to RESEARCH_STARTED


# backend/tests/integration/test_proposals.py — append, using the file's seeding helpers
async def test_a_blocked_buy_enqueues_a_proposal_blocked_notification(clean_tables: Database) -> None:
    # Seed a BUY thesis whose research confidence is below the floor so generation blocks.
    ids = await ph.seed(clean_tables, action="BUY", confidence=0.10)
    service = ph.service(clean_tables, preferences=NotificationPreferences(clean_tables))
    result = await service.generate(ids.thesis_id)
    assert not result.created
    async with clean_tables.session() as session:
        job = (await session.execute(
            sa.select(Job).where(Job.job_type == "SEND_NOTIFICATION")
        )).scalar_one()
    assert job.payload["pipeline_event"] == "PROPOSAL_BLOCKED"
    assert job.payload["entity_id"] == str(ids.research_run_id)


async def test_a_hold_does_not_announce_a_block(clean_tables: Database) -> None:
    ids = await ph.seed(clean_tables, action="HOLD", confidence=0.9)
    service = ph.service(clean_tables, preferences=NotificationPreferences(clean_tables))
    await service.generate(ids.thesis_id)
    async with clean_tables.session() as session:
        count = (await session.execute(
            sa.select(sa.func.count()).select_from(Job).where(Job.job_type == "SEND_NOTIFICATION")
        )).scalar_one()
    assert count == 0
```

Read `tests/proposal_helpers.py` for the real names of `seed`/`service` and what `seed` returns; adapt the calls (not the assertions). If `ph.service` does not accept `preferences`, add the kwarg there.

- [ ] **Step 2: Run → fail (no `PROPOSAL_BLOCKED`, no `preferences` kwarg)**

- [ ] **Step 3: Preferences** — in `telegram/preferences.py` add `PROPOSAL_BLOCKED` to `PipelineEvent` and `NotificationCategory`, a `CategoryDetail` beside `RESEARCH_COMPLETED`'s, default `True`, and the mapping entry `PipelineEvent.PROPOSAL_BLOCKED: NotificationCategory.PROPOSAL_BLOCKED`. Check `tests/unit/test_telegram_*` for a test enumerating categories/defaults and extend it.

- [ ] **Step 4: Notifier + view + renderer**
  - `notifier.py`: `_PIPELINE_ENTITY[PipelineEvent.PROPOSAL_BLOCKED] = "research_run"`, `_PIPELINE_TITLES[...] = "Trade blocked by risk"`.
  - `telegram/service.py`: add `block_reasons: tuple[str, ...] = ()` to `ResearchRunView`; in `research_run()` after loading the thesis, query the latest `RiskEvaluation` with `stage == STAGE_GENERATION and thesis_id == thesis.id` (order by `created_at desc`, limit 1) and set `block_reasons = tuple(r["reason"] for r in evaluation.rules if r["outcome"] == "BLOCK")`.
  - `messages.py` `render_research_stage`: before the generic body, `if event is PipelineEvent.PROPOSAL_BLOCKED:` render: headline `bold("Trade blocked by risk")`, the thesis line (action, confidence, horizon — reuse the existing thesis-line code), then `bold("Blocked by:")` and one `• reason` line per `block_reasons` (escape each; cap at 8 lines with "… and N more"), then `esc("The proposal was refused before it was created. Nothing was sent to the broker.")`.

- [ ] **Step 5: Enqueue from the blocked branch** — in `proposals/service.py`: store `self._preferences = preferences` in `__init__`; in `generate()`'s `if not decision.allowed:` branch, after `session.flush()` of the evaluation and only when `candidate.action in {BUY, SELL, REDUCE}`: `await enqueue_pipeline_notification(session, self.queue, self._preferences, entity_id=candidate.run.id, event=PipelineEvent.PROPOSAL_BLOCKED)`. Import from `stockbrain.jobs.notifications`. The HOLD early-return path enqueues nothing. Wire `preferences=self.notification_preferences` in `services.py` where `ProposalService(...)` is constructed.

- [ ] **Step 6: Tests → PASS** (both files + `tests/unit/test_telegram_*`); mypy; ruff.
- [ ] **Step 7: Commit** — `feat(telegram): announce a research trade the risk engine refused, with its reasons`

---

### Task 2: Transient blocks defer instead of dying

**Files:**
- Modify: `backend/stockbrain/risk/rules.py` (`TRANSIENT_RULE_IDS`)
- Modify: `backend/stockbrain/db/models/proposals.py` (`RiskEvaluation.deferred`)
- Create: `backend/alembic/versions/20260912_1400_risk_evaluation_deferred.py`
- Modify: `backend/stockbrain/config.py`, `backend/stockbrain/api/settings_model.py` (`PROPOSAL_DEFERRAL_MAX_HOURS`)
- Modify: `backend/stockbrain/proposals/service.py` (`generate` blocked branch), `backend/stockbrain/proposals/lifecycle.py` (`enqueue_pending`)
- Modify: `backend/stockbrain/telegram/preferences.py`, `notifier.py`, `messages.py` (`PROPOSAL_DEFERRED`)
- Test: `backend/tests/integration/test_proposal_deferral.py`, `backend/tests/unit/test_risk_engine.py`

**Interfaces:**
- `TRANSIENT_RULE_IDS: frozenset[str]` in `risk/rules.py`, exported.
- `RiskEvaluation.deferred: bool` (server_default false).
- `Settings.proposal_deferral_max_hours: int = 72` (bounds 1..336), `RESTART_REQUIRED`.
- `PipelineEvent.PROPOSAL_DEFERRED` / `NotificationCategory.PROPOSAL_DEFERRED` (default True; "A research trade was blocked only by market state (closed session, stale quote). It will be re-evaluated at the next open.").

- [ ] **Step 1: Failing tests**

```python
# backend/tests/unit/test_risk_engine.py — append
def test_the_transient_rule_set_names_only_market_state_rules() -> None:
    from stockbrain.risk.rules import TRANSIENT_RULE_IDS
    assert TRANSIENT_RULE_IDS == frozenset({
        "price_source_execution_grade", "quote_available", "quote_freshness", "quote_two_sided",
        "spread_ceiling", "market_session", "account_state_available", "account_state_freshness",
        "fx_available", "fx_freshness",
    })
    for judgment in ("currency_alignment", "research_confidence_floor", "max_position_concentration",
                     "min_cash_reserve", "duplicate_or_conflicting_proposal"):
        assert judgment not in TRANSIENT_RULE_IDS


# backend/tests/integration/test_proposal_deferral.py
"""A thesis blocked only by a closed market waits; one blocked by judgment does not."""
from __future__ import annotations
import datetime as dt
import sqlalchemy as sa
from stockbrain.db.models.proposals import RiskEvaluation
from stockbrain.db.models.research import Thesis
from stockbrain.db.session import Database
from tests import proposal_helpers as ph


async def test_a_closed_market_block_is_recorded_as_deferred(clean_tables: Database) -> None:
    ids = await ph.seed(clean_tables, action="BUY", confidence=0.9)
    service = ph.service(clean_tables, market_session="CLOSED")   # see helpers: the stub quote's session
    result = await service.generate(ids.thesis_id)
    assert not result.created
    async with clean_tables.session() as session:
        evaluation = (await session.execute(sa.select(RiskEvaluation))).scalar_one()
    assert evaluation.deferred is True


async def test_a_deferred_thesis_is_retried_after_the_ttl_and_not_before(clean_tables: Database) -> None:
    ids = await ph.seed(clean_tables, action="BUY", confidence=0.9)
    service = ph.service(clean_tables, market_session="CLOSED")
    await service.generate(ids.thesis_id)
    assert await service.enqueue_pending() == 0          # just deferred: not yet
    async with clean_tables.transaction() as session:
        await session.execute(sa.update(RiskEvaluation).values(
            created_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=31)))
    assert await service.enqueue_pending() == 1          # TTL (30 min) elapsed: retried


async def test_a_judgment_block_is_terminal(clean_tables: Database) -> None:
    ids = await ph.seed(clean_tables, action="BUY", confidence=0.10)   # below the confidence floor
    service = ph.service(clean_tables, market_session="CLOSED")
    await service.generate(ids.thesis_id)
    async with clean_tables.session() as session:
        evaluation = (await session.execute(sa.select(RiskEvaluation))).scalar_one()
    assert evaluation.deferred is False
    assert await service.enqueue_pending() == 0


async def test_deferral_gives_up_after_the_age_limit(clean_tables: Database) -> None:
    ids = await ph.seed(clean_tables, action="BUY", confidence=0.9)
    async with clean_tables.transaction() as session:
        await session.execute(sa.update(Thesis).values(
            created_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=73)))
    service = ph.service(clean_tables, market_session="CLOSED")
    await service.generate(ids.thesis_id)
    async with clean_tables.session() as session:
        evaluation = (await session.execute(sa.select(RiskEvaluation))).scalar_one()
    assert evaluation.deferred is False
```

`ph.service(...)`/`ph.seed(...)` must be read first; if the stub market-data provider's session is not controllable via a kwarg, add one (`market_session=`) to the helper — the helpers already construct the stub, so this is the one place to add it.

- [ ] **Step 2: Run → fail**
- [ ] **Step 3: Constant + column + migration** — `TRANSIENT_RULE_IDS` in `rules.py` beside `RISK_REDUCING_ACTIONS` with a docstring stating the definition (market state, never judgment); `deferred: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())` on `RiskEvaluation` with a docstring; autogenerate the migration (keep only that column), `down_revision = "cd8d1b706008"`, `alembic check`/`heads` clean.
- [ ] **Step 4: Settings** — `proposal_deferral_max_hours: int = 72`, validated 1..336 in the house style, a `RESTART_REQUIRED` row.
- [ ] **Step 5: `generate()` blocked branch** — compute
  ```python
  transient_only = bool(decision.block_rule_ids) and set(decision.block_rule_ids) <= TRANSIENT_RULE_IDS
  fresh_enough = (moment - candidate.thesis.created_at) <= dt.timedelta(hours=self.settings.proposal_deferral_max_hours)
  evaluation.deferred = transient_only and fresh_enough
  ```
  When deferred: log `proposal_deferred` (thesis_id, blocks), enqueue `PROPOSAL_DEFERRED` (dedupe makes it once per run), and do **not** enqueue `PROPOSAL_BLOCKED`. When not deferred and the action is executable: `PROPOSAL_BLOCKED` as Task 1. Return `GenerationResult(..., reason="deferred: " + reason)` when deferred.
- [ ] **Step 6: `enqueue_pending`** — replace the `RiskEvaluation` exists-clause with two: `~exists(evaluation where thesis_id == Thesis.id and stage == GENERATION and policy_version == current and deferred.is_(False))` and `~exists(same but deferred.is_(True) and created_at > now - timedelta(minutes=config.proposal_ttl_minutes))`. Docstring: a deferred block is retried once per TTL; a terminal one never.
- [ ] **Step 7: `PROPOSAL_DEFERRED` notification** — category (default True), entity `research_run`, title "Trade waiting for the market", renderer: thesis line + `Blocked for now by:` list + `esc("Only market-state rules refused it. StockBrain will re-evaluate at the next open, for up to N hours.")` where N is the setting (pass it via the view or format from settings in the notifier — read how the notifier reaches settings).
- [ ] **Step 8: Tests → PASS**; also `tests/integration/test_proposals.py`, `tests/integration/test_exit_sweep.py`; mypy; ruff.
- [ ] **Step 9: Commit** — `feat(proposals): defer a thesis blocked only by market state until the next open`

---

### Task 3: `exit_floors()` — pure

**Files:**
- Modify: `backend/stockbrain/risk/exits.py`
- Test: `backend/tests/unit/test_exit_floors.py`

**Interfaces:**
- `@dataclass(frozen=True, slots=True) class ExitFloors: hard_stop: Decimal; volatility_floor: Decimal | None; trailing_floor: Decimal | None; roi_target_price: Decimal | None; horizon_ends_at: dt.datetime; nearest_floor: Decimal; nearest_rule: str`
- `def exit_floors(observation: ExitObservation, config: RiskConfig, *, now: dt.datetime) -> ExitFloors`

Semantics (must match `evaluate_exit` exactly): `hard_stop = average_price × (1 − exit_hard_stop_pct)`; `volatility_floor = peak − k·ATR` only when the volatility rule *would consult it* (peak present, ATR present, fresh, observations ≥ min); `trailing_floor = peak × (1 − exit_trailing_pct)` only when armed (same arming test as the rule); `roi_target_price = average_price × (1 + target)` when `roi_target_for(...) > 0` else None; `horizon_ends_at = opened_at + minutes of the horizon's terminal (target == 0) row`; `nearest_floor/nearest_rule` = the highest of the non-None floors and its rule id.

- [ ] **Step 1: Failing tests**

```python
# backend/tests/unit/test_exit_floors.py
from __future__ import annotations
import datetime as dt
from decimal import Decimal
from stockbrain.enums import TimeHorizon
from stockbrain.risk.exits import ExitObservation, evaluate_exit, exit_floors
from tests import risk_helpers as h

NOW = dt.datetime(2026, 9, 12, 15, 0, tzinfo=dt.UTC)

def observe(**o):  # noqa: ANN003, ANN201 - test factory, mirrors test_risk_exits.py
    d = dict(broker_ticker="AAPL_US_EQ", quantity=Decimal("10"), quantity_available=Decimal("10"),
             average_price=Decimal("100"), current_price=Decimal("100"), peak_price=Decimal("100"),
             peak_observations=10, opened_at=NOW - dt.timedelta(days=2), horizon=TimeHorizon.WEEKS,
             thesis_superseded=False)
    d.update(o); return ExitObservation(**d)

def test_the_hard_stop_and_horizon_are_always_present() -> None:
    f = exit_floors(observe(), h.config(), now=NOW)
    assert f.hard_stop == Decimal("92")
    assert f.horizon_ends_at == NOW - dt.timedelta(days=2) + dt.timedelta(minutes=30240)
    assert f.volatility_floor is None and f.trailing_floor is None

def test_armed_floors_appear_and_the_nearest_is_the_highest() -> None:
    f = exit_floors(observe(current_price=Decimal("120"), peak_price=Decimal("125"),
                            atr=Decimal("2"), atr_as_of=NOW.date()), h.config(), now=NOW)
    assert f.trailing_floor == Decimal("118.75")
    assert f.volatility_floor == Decimal("119")
    assert f.nearest_rule == "volatility_stop" and f.nearest_floor == Decimal("119")

def test_the_roi_target_price_follows_the_decay_table() -> None:
    f = exit_floors(observe(), h.config(), now=NOW)          # weeks, 2 days held → 15%
    assert f.roi_target_price == Decimal("115")
    late = exit_floors(observe(opened_at=NOW - dt.timedelta(days=30)), h.config(), now=NOW)
    assert late.roi_target_price is None                     # past the terminal row

def test_floors_agree_with_the_rule_that_would_fire() -> None:
    obs = observe(current_price=Decimal("118"), peak_price=Decimal("125"), atr=Decimal("2"), atr_as_of=NOW.date())
    f = exit_floors(obs, h.config(), now=NOW)
    signal = evaluate_exit(obs, h.config(), now=NOW)
    assert signal is not None and signal.rule_id == f.nearest_rule
```

- [ ] **Step 2: Run → fail (no `exit_floors`)**
- [ ] **Step 3: Implement** in `risk/exits.py` after `evaluate_exit`, sharing the arming predicates — extract the volatility and trailing arming conditions into two small private functions (`_volatility_armed(obs, config, now) -> Decimal | None` returning the floor, `_trailing_armed(...)`) and make `evaluate_exit` call them too, so the two can never disagree. Horizon end: the smallest `minutes` among rows for the horizon whose `target == 0`.
- [ ] **Step 4: Tests → PASS** (this file + `test_risk_exits.py`); mypy; ruff.
- [ ] **Step 5: Commit** — `feat(risk): compute a position's exit floors from the same rules that fire them`

---

### Task 4: Exit status in the portfolio API

**Files:**
- Modify: `backend/stockbrain/proposals/exits.py` (`observations()` shared builder; `status()`)
- Modify: `backend/stockbrain/api/schemas.py` (`PositionExitResponse`, field on `PortfolioPositionResponse`)
- Modify: `backend/stockbrain/api/routes/portfolio.py`
- Test: `backend/tests/integration/test_exit_sweep.py`, `backend/tests/integration/test_operator_api.py`

**Interfaces:**
- `@dataclass(frozen=True, slots=True) class PositionExitStatus: managed: bool; reason: str | None; floors: ExitFloors | None; peak_price: Decimal | None; atr: Decimal | None; horizon: str | None`
- `ExitSweepService.status(session) -> dict[str, PositionExitStatus]` keyed by `broker_ticker`.
- `PositionExitResponse(managed: bool, reason: str | None, hard_stop, volatility_floor, trailing_floor, roi_target_price: Decimal | None, horizon_ends_at: datetime | None, nearest_floor: Decimal | None, nearest_rule: str | None, peak_price, atr: Decimal | None, horizon: str | None)`; `PortfolioPositionResponse.exit: PositionExitResponse | None = None`.

- [ ] **Step 1: Failing tests** — in `test_exit_sweep.py`: seed an executed BUY + position + peak, call `status()`, assert `managed is True` and `floors.hard_stop == avg × 0.92`; seed a position with no origin, assert `managed is False` and `reason` mentions "no StockBrain buy". In `test_operator_api.py`: hit `/api/v1/portfolio` (authenticated, as the file's other tests do) with the same seed and assert `positions[0]["exit"]["hard_stop"]` and `["managed"]`.
- [ ] **Step 2: Run → fail**
- [ ] **Step 3: Refactor the sweep's observation build into `async def observations(self, session) -> list[tuple[Position, ExitObservation | None, str | None]]`** (position, observation or None, skip reason: `"no StockBrain buy behind it"`, `"unpriced"`), used by both `sweep()` (which keeps its tallies and its active-proposal skip) and `status()` (which never skips on active proposals). `status()` maps each to `PositionExitStatus` via `exit_floors`.
- [ ] **Step 4: API** — schema + route: `exits = getattr(request.app.state.services, "exits", None)` (read how the route reaches the service container — match the existing dependency pattern); when present, `statuses = await exits.status(session)` and attach `exit=` per position.
- [ ] **Step 5: Tests → PASS**; mypy; ruff.
- [ ] **Step 6: Commit** — `feat(api): expose each position's exit floors on the portfolio snapshot`

---

### Task 5: Exit floors on the Portfolio page

**Files:**
- Modify: `frontend/src/api/types.ts:1158` (`PortfolioPosition.exit`), `frontend/src/pages/Portfolio.tsx`, `frontend/src/pages/Portfolio.test.tsx`

- [ ] **Step 1: Failing test** — in `Portfolio.test.tsx`, extend the fixture position with `exit: {managed: true, hard_stop: "92.00", volatility_floor: "119.00", trailing_floor: null, roi_target_price: "115.00", horizon_ends_at: "2026-10-01T00:00:00Z", nearest_floor: "119.00", nearest_rule: "volatility_stop", peak_price: "125.00", atr: "2.00", horizon: "weeks", reason: null}` and assert the row shows `119.00` with the text `volatility` and `92.00`; add a second position with `exit: {managed: false, reason: "no StockBrain buy behind it", ...nulls}` and assert "not managed" appears.
- [ ] **Step 2: `npm test -- Portfolio` → fail**
- [ ] **Step 3: Implement** — add `exit: PortfolioPositionExit | null` to the type; in the table add two columns after "Current": **"Nearest exit"** (`nearest_floor` formatted via the page's existing `formatDecimal`, with the rule name as a muted label, e.g. `119.00 · volatility`) and **"Floors"** (a compact stacked list: `stop 92.00`, `vol 119.00`, `trail —`, `target 115.00`, `horizon 1 Oct`), and for unmanaged positions a single muted cell "not managed — no StockBrain buy behind it". Use the page's existing classes (`num`, `tight`, muted text); do not add a stylesheet. Dates via the page's existing date helper.
- [ ] **Step 4: `cd frontend && npm test -- Portfolio && npm run lint && npm run typecheck` → clean.**
- [ ] **Step 5: Commit** — `feat(web): show each position's exit floors on the portfolio page`

---

### Task 6: `/positions` shows the floors

**Files:**
- Modify: `backend/stockbrain/telegram/service.py` (`PositionView.exit`, `positions()`), `backend/stockbrain/telegram/messages.py:201` (`render_positions`)
- Test: `backend/tests/unit/test_telegram_messages.py`

- [ ] **Step 1: Failing test** — `render_positions([_position_view(exit=PositionExitStatus(managed=True, floors=ExitFloors(...), ...))])` contains `stop 92.00`, `vol 119.00`, `nearest`; an unmanaged view renders `not managed`.
- [ ] **Step 2: Implement** — `PositionView.exit: PositionExitStatus | None = None`; `TelegramService.positions()` calls `self._exits.status(session)` when the sweep service was injected (add an optional ctor kwarg `exits=` and wire it in `services.py`/`telegram/runtime.py` — read how the Telegram service gets its other dependencies); `render_positions` appends per position `  exit: stop 92.00 · vol 119.00 · trail — · target 115.00 · horizon 1 Oct (nearest: vol 119.00)` or `  exit: not managed — no StockBrain buy behind it`.
- [ ] **Step 3: Tests → PASS**; mypy; ruff.
- [ ] **Step 4: Commit** — `feat(telegram): show exit floors in /positions`

---

### Task 7: Daily Telegram summary

**Files:**
- Modify: `backend/stockbrain/config.py`, `backend/stockbrain/api/settings_model.py` (`TELEGRAM_DAILY_SUMMARY_TIME`)
- Modify: `backend/stockbrain/telegram/preferences.py`, `notifier.py`, `messages.py`, `service.py`
- Modify: `backend/stockbrain/services.py` (scheduled task `telegram_daily_summary`)
- Test: `backend/tests/integration/test_daily_summary.py`, `backend/tests/unit/test_telegram_messages.py`

**Interfaces:**
- `Settings.telegram_daily_summary_time: str | None = "08:00"` (UTC `HH:MM`; empty/None disables), validated by regex.
- `PipelineEvent.PORTFOLIO_SUMMARY` / `NotificationCategory.DAILY_SUMMARY` (default True); `_PIPELINE_ENTITY[...] = "portfolio"`; `DAILY_SUMMARY_NAMESPACE = uuid.UUID("6f1b6b3e-0d7e-4d6b-9b0a-2b6f1c0d9e11")`; `summary_entity_id(date) = uuid.uuid5(DAILY_SUMMARY_NAMESPACE, date.isoformat())`.
- `TelegramService.daily_summary(now) -> DailySummaryView` (total/invested/result/cash from the latest snapshot; positions with `PositionExitStatus`; open proposals count; events promoted to candidate in the last 24h; research runs completed in the last 24h with their action; the exit sweep's last tallies if available).
- `ServiceContainer._telegram_daily_summary()` runs every 60s: if the setting is set and `now.time() >= configured` and `summary_entity_id(today)` has no notification row yet, enqueue `SEND_NOTIFICATION` with `{"entity_id": ..., "pipeline_event": "PORTFOLIO_SUMMARY"}` via `enqueue_pipeline_notification`.

- [ ] **Step 1: Failing tests** — renderer test: `render_daily_summary(view)` contains the total value, one line per position with its nearest floor, "Open proposals: 1", "Research (24h): 3", and no MarkdownV2 escaping errors (reuse the file's existing escape-validation helper if one exists). Integration: with `telegram_daily_summary_time="00:00"`, calling `services._telegram_daily_summary()` twice on the same day enqueues exactly one job; with the setting `None`, none.
- [ ] **Step 2: Run → fail**
- [ ] **Step 3: Implement** — setting + validator (`^\d{2}:\d{2}$`, hours < 24, minutes < 60) + catalogue row; category/event/title ("Daily summary"); `_render_pipeline` special-cases `entity_type == "portfolio"` → `messages.render_daily_summary(await self._service.daily_summary(now))` (body = first line); the scheduled task in `_register_schedules` guarded on `self.telegram_service is not None and settings.telegram_daily_summary_time`, `interval_seconds=60`, `jitter_ratio=0.05`. "Already sent today" = a `notifications` row exists with `dedupe = pipeline_dedupe_key(summary_entity_id(today), PORTFOLIO_SUMMARY)` — query the same table `_claim_row` writes.
- [ ] **Step 4: Tests → PASS**; mypy; ruff.
- [ ] **Step 5: Commit** — `feat(telegram): send a daily portfolio summary with exit floors`

---

### Task 8: Docs, `.env.example`, full gate

- [ ] **Step 1:** `docs/operations.md`: the three new notification categories and what each message means; `PROPOSAL_DEFERRAL_MAX_HOURS`; `TELEGRAM_DAILY_SUMMARY_TIME`; how to read the floors on the Portfolio page and in `/positions`. `.env.example`: `PROPOSAL_DEFERRAL_MAX_HOURS=72`, `TELEGRAM_DAILY_SUMMARY_TIME=08:00` beside their neighbours.
- [ ] **Step 2:** `<WS>/alembic.sh check && <WS>/alembic.sh heads` (one head); backend gates from `backend/` (ruff format/check, mypy --strict); frontend `npm run lint && npm run typecheck && npm test` from `frontend/`; full backend suite `<WS>/pytest.sh` → > 2122 passed, 0 failed.
- [ ] **Step 3: Commit** — `docs: describe blocked/deferred trade notifications, exit floors, and the daily summary`

---

## Self-review

**Spec coverage.** §21 notification policy — Tasks 1, 2, 7 add categories with defaults stated and `EXECUTION_CRITICAL` untouched. §15 lifecycle — Task 2 changes only which evaluations `enqueue_pending` treats as terminal. §20 — Task 6 extends `/positions`; no new command. §22 — Task 7's task only enqueues.

**Placeholders.** Read-and-match instructions name their files: `tests/proposal_helpers.py` (Tasks 1, 2), the route's service-container access (Task 4), the Telegram service's dependency wiring (Task 6), the notifier's access to settings (Task 2 Step 7), the `notifications` table `_claim_row` writes (Task 7).

**Type consistency.** `ExitFloors`/`exit_floors` (Task 3) → `PositionExitStatus.floors` (Task 4) → `PositionExitResponse` (Task 4) → `PortfolioPositionExit` TS type (Task 5) → `PositionView.exit` (Task 6) → `DailySummaryView` positions (Task 7). `TRANSIENT_RULE_IDS` (Task 2) is the only consumer of rule-id strings outside `rules.py`. `PipelineEvent` members added: `PROPOSAL_BLOCKED` (T1), `PROPOSAL_DEFERRED` (T2), `PORTFOLIO_SUMMARY` (T7).
