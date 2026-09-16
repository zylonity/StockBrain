# Thesis Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every research run the company's standing thesis and live position, grade every executed thesis-backed trade against SPY at horizon-relative checkpoints with zero LLM spend, and let a calibration record shrink size in the deterministic risk engine.

**Architecture:** A `MemoryService` (`stockbrain/intelligence/memory.py`) sweeps EXECUTED proposals into `thesis_outcomes`, grades them from the Yahoo `DailyBars` source the ATR refresh already uses into `thesis_outcome_grades`, and answers two read queries: `research_memory()` for the packet and `calibration()` for the risk engine. `ResearchPacket.memory` is behind `MEMORY_PACKET_ENABLED` (off by default); the new `calibration_size_modulation` rule only ever shrinks and stays dormant until a bucket reaches `RISK_CALIBRATION_MIN_SAMPLES`. Recording is a sweep over proposal state, never a hook on execution.

**Tech Stack:** Python 3.12, SQLAlchemy async, Alembic, Pydantic v2, pytest (`tests/conftest.py` fixtures `clean_tables`, `migrated_database`), `yfinance` via the existing `YahooDailyBars`. No new dependencies. No LLM calls anywhere on this path.

**Spec:** `docs/superpowers/specs/2026-09-16-thesis-memory-design.md` (commit `c136892`). Read it first; every section number below refers to it.

---

## Global Constraints

- **Zero LLM tokens.** Nothing in this plan may import or call `stockbrain.llm`, the classifier, or research engines. Grading is arithmetic.
- **Yahoo is never a `PriceSource`.** No Yahoo close may become a `reference_price`; nothing touches `EXECUTION_GRADE_PRICE_SOURCES`.
- **Never link supersession automatically.** The automatic path must not set `ResearchPacket.previous_thesis_id` or `Thesis.supersedes_thesis_id` from the standing thesis (spec §7.1: `thesis_is_superseded` fires the `thesis_superseded` exit rule and would sell the position).
- **The rule only shrinks.** `calibration_size_factor` returns `size_factor ∈ [min_calibration_size_factor, 1]`, outcome `REDUCE` or `PASS`, never `BLOCK`.
- **Every threshold lives on `RiskConfig`** (hashed into `version`); memory and risk settings are `RESTART_REQUIRED`; no write routes.
- **No change under `execution/`.** Recording is a sweep.
- **Layering:** `risk/` never imports from `intelligence/`. `CalibrationBucket` therefore lives in `stockbrain/risk/models.py`; `intelligence/memory.py` imports it from there.
- **`Decimal` for prices and returns.** Convert at the boundary; never `float` arithmetic.
- **Rate discipline:** one Yahoo request per distinct symbol per tick (pending tickers + the benchmark), `days=120`, and a symbol is not re-fetched within 20 hours (`_MIN_REFRESH_INTERVAL` pattern from `market_data/volatility.py`).
- **Alembic head before this work: `e5b1c9d47a02`.** New file `backend/alembic/versions/20260916_1200_thesis_memory.py`, revision `b7d2e4f1a9c3`.
- **`PROMPT_VERSION` becomes `"research-v3"`** (Task 4). This cancels PENDING research runs at deploy by existing design.
- **Quality gate per task:** from `backend/`: `.venv/bin/ruff format stockbrain tests && .venv/bin/ruff check stockbrain tests && .venv/bin/mypy stockbrain tests`, then the task's tests. Full suite before the final commit: `cd <repo root> && make test` (needs the `stockbrain_test` database; `TEST_DATABASE_URL` is set by the Makefile from `POSTGRES_PASSWORD` in `.env`).
- **Baseline:** `make test` at `cf69e87`: **2182 passed, 24 deselected** (~3.5 min). Every task must leave that passing plus its own new tests.
- **Every commit message ends with:**
  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_0167HHBUFNQXd6Rigj3wPp6h
  ```

## File map

| File | Responsibility |
|---|---|
| `backend/stockbrain/enums.py` | `OutcomeStatus`, `OutcomeCheckpoint` (Task 1) |
| `backend/stockbrain/db/models/memory.py` | `ThesisOutcome`, `ThesisOutcomeGrade` ORM (Task 1) |
| `backend/stockbrain/db/models/__init__.py` | export the two models (Task 1) |
| `backend/alembic/versions/20260916_1200_thesis_memory.py` | schema (Task 1) |
| `backend/stockbrain/risk/models.py` | `CalibrationBucket`, `RiskInputs.calibration` (Task 2) |
| `backend/stockbrain/intelligence/memory.py` | pure grading functions + `MemoryService` (Tasks 2, 3, 4) |
| `backend/stockbrain/intelligence/research.py` | `ResearchMemory` & friends, `ResearchPacket.memory`, `PROMPT_VERSION` (Task 4) |
| `backend/stockbrain/intelligence/research_service.py` | populate `memory` in `packet()` (Task 4) |
| `backend/stockbrain/intelligence/tradingagents_adapter.py` | `SYSTEM_POLICY` paragraph (Task 4) |
| `backend/stockbrain/risk/config.py` | three calibration fields (Task 5) |
| `backend/stockbrain/risk/rules.py` | `calibration_size_factor` (Task 5) |
| `backend/stockbrain/risk/engine.py` | append the rule (Task 5) |
| `backend/stockbrain/proposals/evaluation.py` | `EvaluationContext.event_type`, load the bucket (Task 6) |
| `backend/stockbrain/proposals/service.py` | supply `event_type`, pass `memory` (Task 6) |
| `backend/stockbrain/config.py` | eight settings + validation (Tasks 3, 5) |
| `backend/stockbrain/api/settings_model.py` | catalogue rows (Tasks 3, 5) |
| `.env.example` | documentation (Tasks 3, 5) |
| `backend/stockbrain/services.py` | construct `MemoryService`, wire, schedule (Task 7) |
| `backend/stockbrain/api/routes/memory.py` + `main.py` | read-only endpoints (Task 7) |

---

### Task 1: Enums, ORM models and migration

**Files:**
- Modify: `backend/stockbrain/enums.py` (after `class TimeHorizon`, ~line 150)
- Create: `backend/stockbrain/db/models/memory.py`
- Modify: `backend/stockbrain/db/models/__init__.py`
- Create: `backend/alembic/versions/20260916_1200_thesis_memory.py`
- Test: `backend/tests/integration/test_thesis_memory_schema.py`

**Interfaces:**
- Produces: `stockbrain.enums.OutcomeStatus` (`PENDING`, `CLOSED`, `ABANDONED`), `stockbrain.enums.OutcomeCheckpoint` (`D1`, `D5`, `D20`, `D60`, `CLOSE`); ORM classes `ThesisOutcome`, `ThesisOutcomeGrade` with the columns in spec §4.1/§4.2.

- [ ] **Step 1: Add the enums**

In `backend/stockbrain/enums.py`, directly after `class TimeHorizon`:

```python
class OutcomeStatus(StrEnum):
    """Lifecycle of a graded thesis outcome (spec §4.1)."""

    PENDING = "PENDING"
    CLOSED = "CLOSED"
    ABANDONED = "ABANDONED"


class OutcomeCheckpoint(StrEnum):
    """When an outcome is graded: N trading days after entry, or on close."""

    D1 = "D1"
    D5 = "D5"
    D20 = "D20"
    D60 = "D60"
    CLOSE = "CLOSE"

    @property
    def trading_days(self) -> int | None:
        """The day count a checkpoint waits for; ``None`` for CLOSE."""
        return None if self is OutcomeCheckpoint.CLOSE else int(self.value[1:])
```

- [ ] **Step 2: Write the ORM models**

Create `backend/stockbrain/db/models/memory.py`:

```python
"""Thesis outcomes: the ground truth research is graded against.

One ``thesis_outcomes`` row per EXECUTED thesis-backed proposal, one
``thesis_outcome_grades`` row per (outcome, checkpoint).  Written only by the
memory sweep; read by the research packet and the risk engine.  No LLM output
is stored here -- every column is a number, a date or an identifier.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stockbrain.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from stockbrain.db.models._types import pg_enum
from stockbrain.enums import Broker, OutcomeStatus, ThesisAction, TimeHorizon

__all__ = ["ThesisOutcome", "ThesisOutcomeGrade"]


class ThesisOutcome(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "thesis_outcomes"

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("trade_proposals.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    thesis_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("theses.id", ondelete="CASCADE"), nullable=False
    )
    research_run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("research_runs.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    broker_instrument_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("broker_instruments.id", ondelete="CASCADE"), nullable=False
    )
    broker: Mapped[Broker] = mapped_column(pg_enum(Broker, "broker"), nullable=False)
    broker_ticker: Mapped[str] = mapped_column(sa.Text, nullable=False)
    event_type: Mapped[str | None] = mapped_column(sa.Text)
    action: Mapped[ThesisAction] = mapped_column(
        pg_enum(ThesisAction, "thesis_action"), nullable=False
    )
    horizon: Mapped[TimeHorizon] = mapped_column(
        pg_enum(TimeHorizon, "time_horizon"), nullable=False
    )
    confidence: Mapped[Decimal] = mapped_column(sa.Numeric(4, 3), nullable=False)
    is_exit: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    exit_rule_id: Mapped[str | None] = mapped_column(sa.Text)
    entry_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    entry_date: Mapped[dt.date] = mapped_column(sa.Date, nullable=False)
    reference_price: Mapped[Decimal | None] = mapped_column(sa.Numeric(24, 8))
    currency: Mapped[str | None] = mapped_column(sa.String(3))
    benchmark_symbol: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[OutcomeStatus] = mapped_column(
        pg_enum(OutcomeStatus, "outcome_status"),
        nullable=False,
        server_default=OutcomeStatus.PENDING.value,
    )
    closed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(sa.Text)
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    """When the sweep last fetched bars for this outcome; the 20-hour clock."""

    grades: Mapped[list[ThesisOutcomeGrade]] = relationship(
        back_populates="outcome", cascade="all, delete-orphan", order_by="ThesisOutcomeGrade.graded_at"
    )

    __table_args__ = (
        sa.Index("ix_thesis_outcomes_status", "status"),
        sa.Index("ix_thesis_outcomes_company_action", "company_id", "action"),
        sa.Index("ix_thesis_outcomes_event_type_action", "event_type", "action"),
        sa.Index("ix_thesis_outcomes_broker_ticker", "broker", "broker_ticker"),
    )


class ThesisOutcomeGrade(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "thesis_outcome_grades"

    outcome_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("thesis_outcomes.id", ondelete="CASCADE"), nullable=False
    )
    checkpoint: Mapped[str] = mapped_column(sa.Text, nullable=False)
    trading_days: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    entry_close: Mapped[Decimal] = mapped_column(sa.Numeric(24, 8), nullable=False)
    current_close: Mapped[Decimal] = mapped_column(sa.Numeric(24, 8), nullable=False)
    benchmark_entry_close: Mapped[Decimal] = mapped_column(sa.Numeric(24, 8), nullable=False)
    benchmark_current_close: Mapped[Decimal] = mapped_column(sa.Numeric(24, 8), nullable=False)
    instrument_return: Mapped[Decimal] = mapped_column(sa.Numeric(10, 6), nullable=False)
    benchmark_return: Mapped[Decimal] = mapped_column(sa.Numeric(10, 6), nullable=False)
    alpha: Mapped[Decimal] = mapped_column(sa.Numeric(10, 6), nullable=False)
    correct: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    graded_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    outcome: Mapped[ThesisOutcome] = relationship(back_populates="grades")

    __table_args__ = (
        sa.UniqueConstraint("outcome_id", "checkpoint", name="uq_thesis_outcome_grades_checkpoint"),
        sa.Index("ix_thesis_outcome_grades_graded_at", "graded_at"),
    )
```

Note `last_attempt_at` is an addition to spec §4.1 — it is the per-outcome rate-discipline clock the spec's §5.5 requires and has no other home.

- [ ] **Step 3: Export the models**

In `backend/stockbrain/db/models/__init__.py`, add next to the other imports:

```python
from stockbrain.db.models.memory import ThesisOutcome, ThesisOutcomeGrade
```

and add `"ThesisOutcome", "ThesisOutcomeGrade"` to `__all__` if the module defines one (check with `grep -n "__all__" backend/stockbrain/db/models/__init__.py`).

- [ ] **Step 4: Write the migration**

Create `backend/alembic/versions/20260916_1200_thesis_memory.py`:

```python
"""Thesis outcomes and their grades.

Revision ID: b7d2e4f1a9c3
Revises: e5b1c9d47a02

Ground truth for research: one row per executed thesis-backed proposal, graded
against a benchmark at horizon-relative checkpoints.  Written only by the
memory sweep.  The ``outcome_status`` enum is created explicitly and then
referenced with ``create_type=False`` so downgrade can drop it cleanly.

No application code is imported.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7d2e4f1a9c3"
down_revision: str | None = "e5b1c9d47a02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    sa.Enum("PENDING", "CLOSED", "ABANDONED", name="outcome_status").create(
        op.get_bind(), checkfirst=True
    )
    outcome_status = postgresql.ENUM(
        "PENDING", "CLOSED", "ABANDONED", name="outcome_status", create_type=False
    )
    op.create_table(
        "thesis_outcomes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("thesis_id", sa.Uuid(), nullable=False),
        sa.Column("research_run_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("broker_instrument_id", sa.Uuid(), nullable=False),
        sa.Column(
            "broker",
            postgresql.ENUM("TRADING212", name="broker", create_type=False),
            nullable=False,
        ),
        sa.Column("broker_ticker", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=True),
        sa.Column(
            "action",
            postgresql.ENUM(
                "BUY", "HOLD", "REDUCE", "SELL", "NO_ACTION", name="thesis_action", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "horizon",
            postgresql.ENUM(
                "intraday", "days", "weeks", "months", name="time_horizon", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("is_exit", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("exit_rule_id", sa.Text(), nullable=True),
        sa.Column("entry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("reference_price", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("benchmark_symbol", sa.Text(), nullable=False),
        sa.Column("status", outcome_status, server_default="PENDING", nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_thesis_outcomes")),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["trade_proposals.id"], ondelete="CASCADE",
            name=op.f("fk_thesis_outcomes_proposal_id_trade_proposals"),
        ),
        sa.ForeignKeyConstraint(
            ["thesis_id"], ["theses.id"], ondelete="CASCADE",
            name=op.f("fk_thesis_outcomes_thesis_id_theses"),
        ),
        sa.ForeignKeyConstraint(
            ["research_run_id"], ["research_runs.id"], ondelete="CASCADE",
            name=op.f("fk_thesis_outcomes_research_run_id_research_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE",
            name=op.f("fk_thesis_outcomes_company_id_companies"),
        ),
        sa.ForeignKeyConstraint(
            ["broker_instrument_id"], ["broker_instruments.id"], ondelete="CASCADE",
            name=op.f("fk_thesis_outcomes_broker_instrument_id_broker_instruments"),
        ),
        sa.UniqueConstraint("proposal_id", name=op.f("uq_thesis_outcomes_proposal_id")),
    )
    op.create_index("ix_thesis_outcomes_status", "thesis_outcomes", ["status"])
    op.create_index("ix_thesis_outcomes_company_action", "thesis_outcomes", ["company_id", "action"])
    op.create_index(
        "ix_thesis_outcomes_event_type_action", "thesis_outcomes", ["event_type", "action"]
    )
    op.create_index("ix_thesis_outcomes_broker_ticker", "thesis_outcomes", ["broker", "broker_ticker"])

    op.create_table(
        "thesis_outcome_grades",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("outcome_id", sa.Uuid(), nullable=False),
        sa.Column("checkpoint", sa.Text(), nullable=False),
        sa.Column("trading_days", sa.Integer(), nullable=False),
        sa.Column("entry_close", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("current_close", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("benchmark_entry_close", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("benchmark_current_close", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("instrument_return", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("benchmark_return", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("alpha", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("correct", sa.Boolean(), nullable=False),
        sa.Column("graded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_thesis_outcome_grades")),
        sa.ForeignKeyConstraint(
            ["outcome_id"], ["thesis_outcomes.id"], ondelete="CASCADE",
            name=op.f("fk_thesis_outcome_grades_outcome_id_thesis_outcomes"),
        ),
        sa.UniqueConstraint("outcome_id", "checkpoint", name="uq_thesis_outcome_grades_checkpoint"),
    )
    op.create_index("ix_thesis_outcome_grades_graded_at", "thesis_outcome_grades", ["graded_at"])


def downgrade() -> None:
    op.drop_table("thesis_outcome_grades")
    op.drop_table("thesis_outcomes")
    sa.Enum(name="outcome_status").drop(op.get_bind(), checkfirst=True)
```

If `alembic check` (Step 6) reports a drift between the ORM and this migration (index or constraint naming), fix the **migration** to match the ORM — the ORM is the source of truth.

- [ ] **Step 5: Write the schema test**

Create `backend/tests/integration/test_thesis_memory_schema.py`:

```python
"""The memory tables exist, round-trip an outcome with a grade, and truncate."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.db.base import utcnow
from stockbrain.db.models.memory import ThesisOutcome, ThesisOutcomeGrade
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.session import Database
from stockbrain.enums import (
    Broker,
    OrderSide,
    OrderType,
    OutcomeCheckpoint,
    OutcomeStatus,
    PriceSource,
    ProposalStatus,
    ThesisAction,
    TimeHorizon,
)
from tests import proposal_helpers as ph

pytestmark = pytest.mark.integration


async def _executed_buy(database: Database) -> TradeProposal:
    await ph.seed(database)
    moment = utcnow()
    proposal = TradeProposal(
        thesis_id=ph.THESIS_ID,
        research_run_id=ph.RUN_ID,
        broker=Broker.TRADING212,
        broker_ticker="AAPL_US_EQ",
        account_id=ph.ACCOUNT_ID,
        broker_environment="demo",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        proposed_quantity=Decimal("2"),
        reference_price=Decimal("180.50"),
        reference_currency="USD",
        price_source=PriceSource.ALPACA_IEX,
        quote_timestamp=moment,
        quote_age_ms=0,
        estimated_notional=Decimal("361"),
        account_currency="USD",
        status=ProposalStatus.EXECUTED,
        executed_at=moment,
        expires_at=moment + dt.timedelta(days=1),
    )
    async with database.transaction() as session:
        session.add(proposal)
    return proposal


async def test_an_outcome_and_its_grade_round_trip(clean_tables: Database) -> None:
    proposal = await _executed_buy(clean_tables)
    moment = utcnow()
    outcome_id = uuid.uuid4()
    async with clean_tables.transaction() as session:
        session.add(
            ThesisOutcome(
                id=outcome_id,
                proposal_id=proposal.id,
                thesis_id=ph.THESIS_ID,
                research_run_id=ph.RUN_ID,
                company_id=ph.COMPANY_ID,
                broker_instrument_id=ph.INSTRUMENT_ID,
                broker=Broker.TRADING212,
                broker_ticker="AAPL_US_EQ",
                event_type="EARNINGS",
                action=ThesisAction.BUY,
                horizon=TimeHorizon.WEEKS,
                confidence=Decimal("0.700"),
                entry_at=moment,
                entry_date=moment.date(),
                reference_price=Decimal("180.50"),
                currency="USD",
                benchmark_symbol="SPY",
            )
        )
        await session.flush()
        session.add(
            ThesisOutcomeGrade(
                outcome_id=outcome_id,
                checkpoint=OutcomeCheckpoint.D5.value,
                trading_days=5,
                entry_close=Decimal("180"),
                current_close=Decimal("189"),
                benchmark_entry_close=Decimal("500"),
                benchmark_current_close=Decimal("505"),
                instrument_return=Decimal("0.05"),
                benchmark_return=Decimal("0.01"),
                alpha=Decimal("0.04"),
                correct=True,
                graded_at=moment,
            )
        )
    async with clean_tables.session() as session:
        row = await session.get(ThesisOutcome, outcome_id)
        assert row is not None
        assert row.status is OutcomeStatus.PENDING
        grades = (
            await session.scalars(
                sa.select(ThesisOutcomeGrade).where(ThesisOutcomeGrade.outcome_id == outcome_id)
            )
        ).all()
        assert [grade.checkpoint for grade in grades] == ["D5"]
        assert grades[0].alpha == Decimal("0.040000")


async def test_a_second_grade_for_the_same_checkpoint_is_refused(clean_tables: Database) -> None:
    proposal = await _executed_buy(clean_tables)
    moment = utcnow()
    outcome_id = uuid.uuid4()

    def grade() -> ThesisOutcomeGrade:
        return ThesisOutcomeGrade(
            outcome_id=outcome_id,
            checkpoint="D1",
            trading_days=1,
            entry_close=Decimal(1),
            current_close=Decimal(1),
            benchmark_entry_close=Decimal(1),
            benchmark_current_close=Decimal(1),
            instrument_return=Decimal(0),
            benchmark_return=Decimal(0),
            alpha=Decimal(0),
            correct=False,
            graded_at=moment,
        )

    async with clean_tables.transaction() as session:
        session.add(
            ThesisOutcome(
                id=outcome_id,
                proposal_id=proposal.id,
                thesis_id=ph.THESIS_ID,
                research_run_id=ph.RUN_ID,
                company_id=ph.COMPANY_ID,
                broker_instrument_id=ph.INSTRUMENT_ID,
                broker=Broker.TRADING212,
                broker_ticker="AAPL_US_EQ",
                action=ThesisAction.BUY,
                horizon=TimeHorizon.DAYS,
                confidence=Decimal("0.7"),
                entry_at=moment,
                entry_date=moment.date(),
                benchmark_symbol="SPY",
            )
        )
        await session.flush()
        session.add(grade())
    with pytest.raises(sa.exc.IntegrityError):
        async with clean_tables.transaction() as session:
            session.add(grade())
```

Then add `"thesis_outcome_grades", "thesis_outcomes",` to the **top** of the `tables` tuple in `clean_tables` in `backend/tests/conftest.py` (they must be listed before `trade_proposals`; `CASCADE` makes order tolerant, but keep the file's convention of dependents first).

- [ ] **Step 6: Run the checks**

From `backend/`:

```
.venv/bin/alembic heads          # expect b7d2e4f1a9c3 (head)
.venv/bin/ruff format stockbrain tests alembic && .venv/bin/ruff check stockbrain tests alembic && .venv/bin/mypy stockbrain tests
DATABASE_URL_TEST=<TEST_DATABASE_URL> .venv/bin/python -m pytest tests/integration/test_thesis_memory_schema.py -v
```

Expected: 2 passed. If the enum `outcome_status` already exists from an earlier attempt, the `checkfirst=True` handles it.

- [ ] **Step 7: Commit**

```bash
git add backend/stockbrain/enums.py backend/stockbrain/db/models/memory.py backend/stockbrain/db/models/__init__.py backend/alembic/versions/20260916_1200_thesis_memory.py backend/tests/integration/test_thesis_memory_schema.py backend/tests/conftest.py
git commit -m "feat(memory): thesis outcome and grade tables"
```

---

### Task 2: Pure grading arithmetic and calibration

**Files:**
- Modify: `backend/stockbrain/risk/models.py` (add `CalibrationBucket`; add `calibration` field to `RiskInputs` at ~line 430-447)
- Create: `backend/stockbrain/intelligence/memory.py` (pure functions only in this task; the service class is Task 3)
- Test: `backend/tests/unit/test_memory_grading.py`, `backend/tests/unit/test_memory_calibration.py`

**Interfaces:**
- Produces (in `stockbrain.risk.models`):
  ```python
  @dataclass(frozen=True, slots=True)
  class CalibrationBucket:
      key: str
      samples: int
      correct: int
      hit_rate: Decimal
      mean_alpha: Decimal
      latest_graded_at: dt.datetime
  ```
  and `RiskInputs.calibration: CalibrationBucket | None = None`.
- Produces (in `stockbrain.intelligence.memory`):
  ```python
  CHECKPOINTS: Mapping[TimeHorizon, tuple[OutcomeCheckpoint, ...]]
  EXIT_RULE_IDS: frozenset[str]
  BARS_DAYS: int = 120
  def checkpoints_for(horizon: TimeHorizon) -> tuple[OutcomeCheckpoint, ...]
  def entry_bar(bars: Sequence[Bar], entry_date: dt.date) -> Bar | None
  def bar_on_or_before(bars: Sequence[Bar], day: dt.date) -> Bar | None
  def trading_days_after(bars: Sequence[Bar], entry_date: dt.date) -> int
  def returns(entry_close, current_close, benchmark_entry, benchmark_current) -> tuple[Decimal, Decimal, Decimal]  # instrument, benchmark, alpha
  def is_correct(action: ThesisAction, alpha: Decimal) -> bool
  @dataclass(frozen=True, slots=True) class EffectiveGrade: outcome_id, correct: bool, alpha: Decimal, graded_at: dt.datetime
  def calibrate(key: str, grades: Iterable[EffectiveGrade]) -> CalibrationBucket | None
  ```

- [ ] **Step 1: Write the failing grading tests**

Create `backend/tests/unit/test_memory_grading.py`:

```python
"""Grading is arithmetic on daily closes.  These tests pin the arithmetic."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from stockbrain.enums import OutcomeCheckpoint, ThesisAction, TimeHorizon
from stockbrain.intelligence.memory import (
    bar_on_or_before,
    checkpoints_for,
    entry_bar,
    is_correct,
    returns,
    trading_days_after,
)
from stockbrain.market_data.base import Bar


def bar(day: dt.date, close: str) -> Bar:
    return Bar(
        symbol="AAPL",
        timestamp=dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=1,
    )


MON = dt.date(2026, 9, 14)
BARS = [
    bar(dt.date(2026, 9, 11), "100"),  # Friday before entry
    bar(MON, "102"),
    bar(dt.date(2026, 9, 15), "104"),
    bar(dt.date(2026, 9, 16), "103"),
    bar(dt.date(2026, 9, 17), "108"),
]


@pytest.mark.parametrize(
    ("horizon", "expected"),
    [
        (TimeHorizon.INTRADAY, (OutcomeCheckpoint.D1, OutcomeCheckpoint.D5)),
        (TimeHorizon.DAYS, (OutcomeCheckpoint.D1, OutcomeCheckpoint.D5)),
        (TimeHorizon.WEEKS, (OutcomeCheckpoint.D5, OutcomeCheckpoint.D20)),
        (TimeHorizon.MONTHS, (OutcomeCheckpoint.D20, OutcomeCheckpoint.D60)),
    ],
)
def test_checkpoints_are_horizon_relative(
    horizon: TimeHorizon, expected: tuple[OutcomeCheckpoint, ...]
) -> None:
    assert checkpoints_for(horizon) == expected


def test_the_entry_bar_is_the_close_on_the_entry_date() -> None:
    assert entry_bar(BARS, MON) is BARS[1]


def test_the_entry_bar_falls_forward_to_the_next_session_when_the_entry_day_has_no_bar() -> None:
    saturday = dt.date(2026, 9, 12)
    assert entry_bar(BARS, saturday) is BARS[1]


def test_no_entry_bar_when_every_bar_predates_entry() -> None:
    assert entry_bar(BARS, dt.date(2026, 9, 18)) is None


def test_the_current_bar_is_the_last_close_on_or_before_the_day() -> None:
    assert bar_on_or_before(BARS, dt.date(2026, 9, 16)) is BARS[3]
    assert bar_on_or_before(BARS, dt.date(2026, 9, 19)) is BARS[4]
    assert bar_on_or_before(BARS, dt.date(2026, 9, 10)) is None


def test_trading_days_count_bars_strictly_after_entry() -> None:
    assert trading_days_after(BARS, MON) == 3
    assert trading_days_after(BARS, dt.date(2026, 9, 17)) == 0


def test_returns_are_decimal_ratios_and_alpha_is_their_difference() -> None:
    instrument, benchmark, alpha = returns(
        Decimal("100"), Decimal("110"), Decimal("500"), Decimal("505")
    )
    assert instrument == Decimal("0.1")
    assert benchmark == Decimal("0.01")
    assert alpha == Decimal("0.09")


def test_returns_refuse_a_zero_entry_close() -> None:
    with pytest.raises(ValueError, match="entry close"):
        returns(Decimal(0), Decimal(1), Decimal(1), Decimal(1))


@pytest.mark.parametrize(
    ("action", "alpha", "expected"),
    [
        (ThesisAction.BUY, Decimal("0.01"), True),
        (ThesisAction.BUY, Decimal("0"), False),
        (ThesisAction.BUY, Decimal("-0.01"), False),
        (ThesisAction.REDUCE, Decimal("-0.01"), True),
        (ThesisAction.REDUCE, Decimal("0"), False),
        (ThesisAction.SELL, Decimal("-0.02"), True),
        (ThesisAction.SELL, Decimal("0.02"), False),
    ],
)
def test_correctness_is_signed_by_action(
    action: ThesisAction, alpha: Decimal, expected: bool
) -> None:
    assert is_correct(action, alpha) is expected


@pytest.mark.parametrize("action", [ThesisAction.HOLD, ThesisAction.NO_ACTION])
def test_non_trading_actions_cannot_be_graded(action: ThesisAction) -> None:
    with pytest.raises(ValueError, match="cannot be graded"):
        is_correct(action, Decimal("0.1"))
```

- [ ] **Step 2: Write the failing calibration tests**

Create `backend/tests/unit/test_memory_calibration.py`:

```python
"""A calibration bucket is a hit rate and a mean alpha over effective grades."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from stockbrain.intelligence.memory import EffectiveGrade, calibrate

T0 = dt.datetime(2026, 9, 16, tzinfo=dt.UTC)


def grade(correct: bool, alpha: str, hours: int = 0) -> EffectiveGrade:
    return EffectiveGrade(
        outcome_id=uuid.uuid4(),
        correct=correct,
        alpha=Decimal(alpha),
        graded_at=T0 + dt.timedelta(hours=hours),
    )


def test_an_empty_bucket_is_none() -> None:
    assert calibrate("REGULATORY×REDUCE", []) is None


def test_hit_rate_and_mean_alpha_are_exact_decimals() -> None:
    bucket = calibrate(
        "REGULATORY×REDUCE",
        [grade(True, "0.02"), grade(False, "-0.01"), grade(False, "-0.04", hours=5)],
    )
    assert bucket is not None
    assert bucket.key == "REGULATORY×REDUCE"
    assert bucket.samples == 3
    assert bucket.correct == 1
    assert bucket.hit_rate == Decimal("0.333333")
    assert bucket.mean_alpha == Decimal("-0.01")
    assert bucket.latest_graded_at == T0 + dt.timedelta(hours=5)


def test_a_single_grade_is_a_bucket_of_one() -> None:
    bucket = calibrate("k", [grade(True, "0.1")])
    assert bucket is not None
    assert (bucket.samples, bucket.hit_rate) == (1, Decimal("1"))
```

- [ ] **Step 3: Run the tests to verify they fail**

From `backend/`: `.venv/bin/python -m pytest tests/unit/test_memory_grading.py tests/unit/test_memory_calibration.py -q`
Expected: ImportError on `stockbrain.intelligence.memory`.

- [ ] **Step 4: Add `CalibrationBucket` and `RiskInputs.calibration`**

In `backend/stockbrain/risk/models.py`, add before `class RiskInputs` (keep the file's `from __future__ import annotations` and existing imports; `dataclass`, `Decimal`, `dt` are already imported there — verify with `grep -n "^import\|^from" backend/stockbrain/risk/models.py`):

```python
@dataclass(frozen=True, slots=True)
class CalibrationBucket:
    """How this system's past calls in one situation performed.

    Lives in ``risk`` rather than ``intelligence`` so the engine's inputs never
    import from the research layer.  ``hit_rate`` is ``correct / samples``;
    ``mean_alpha`` is the mean benchmark-relative return of the effective grade
    of each outcome (spec §6).  Both are exact ``Decimal``s.
    """

    key: str
    samples: int
    correct: int
    hit_rate: Decimal
    mean_alpha: Decimal
    latest_graded_at: dt.datetime
```

In `class RiskInputs`, after `authorized_fx_rate: Decimal | None = None` (~line 447), add:

```python
    calibration: CalibrationBucket | None = None
    """This system's record for the proposal's (event_type, action) bucket.
    ``None`` when memory is off, nothing is graded yet, or the path is an exit."""
```

Add `"CalibrationBucket"` to the module's `__all__` if one exists.

- [ ] **Step 5: Write the pure functions**

Create `backend/stockbrain/intelligence/memory.py` (Task 3 appends the service class to this same file):

```python
"""Thesis memory: ground truth for research, with no model in the loop.

Every executed thesis-backed trade is graded against a benchmark from daily
closes at horizon-relative checkpoints (spec §5).  Grades aggregate into a
calibration bucket (spec §6) that the research packet reports and the risk
engine may act on.  Everything here is arithmetic on ``Decimal``; the only
network call is the daily-bars fetch, and it is research-grade data that never
becomes a reference price.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from stockbrain.enums import OutcomeCheckpoint, ThesisAction, TimeHorizon
from stockbrain.market_data.base import Bar
from stockbrain.risk.exits import EXIT_PRECEDENCE
from stockbrain.risk.models import CalibrationBucket

__all__ = [
    "BARS_DAYS",
    "CHECKPOINTS",
    "EXIT_RULE_IDS",
    "EffectiveGrade",
    "bar_on_or_before",
    "calibrate",
    "checkpoints_for",
    "entry_bar",
    "is_correct",
    "returns",
    "trading_days_after",
]

#: Enough daily bars to cover the longest checkpoint (D60) with a margin for
#: holidays, fetched once per symbol per tick.
BARS_DAYS = 120

#: Horizon-relative checkpoints (spec §5.2).  Not a setting: a checkpoint
#: schedule that changed under stored grades would make them incomparable.
CHECKPOINTS: Mapping[TimeHorizon, tuple[OutcomeCheckpoint, ...]] = {
    TimeHorizon.INTRADAY: (OutcomeCheckpoint.D1, OutcomeCheckpoint.D5),
    TimeHorizon.DAYS: (OutcomeCheckpoint.D1, OutcomeCheckpoint.D5),
    TimeHorizon.WEEKS: (OutcomeCheckpoint.D5, OutcomeCheckpoint.D20),
    TimeHorizon.MONTHS: (OutcomeCheckpoint.D20, OutcomeCheckpoint.D60),
}

#: A proposal whose recorded rules include one of these was generated by the
#: exit sweep, not by research.
EXIT_RULE_IDS: frozenset[str] = frozenset(EXIT_PRECEDENCE)

_SIX_PLACES = Decimal("0.000001")
_RISK_REDUCING = frozenset({ThesisAction.REDUCE, ThesisAction.SELL})


def checkpoints_for(horizon: TimeHorizon) -> tuple[OutcomeCheckpoint, ...]:
    return CHECKPOINTS[horizon]


def entry_bar(bars: Sequence[Bar], entry_date: dt.date) -> Bar | None:
    """The close on the entry date, or the first session after it.

    A fill on a weekend or holiday (a deferred proposal executed at the next
    open) has no bar of its own; the next session's close is the honest entry.
    """
    for candidate in bars:
        if candidate.timestamp.date() >= entry_date:
            return candidate
    return None


def bar_on_or_before(bars: Sequence[Bar], day: dt.date) -> Bar | None:
    """The last close on or before ``day``; bars are oldest first."""
    found: Bar | None = None
    for candidate in bars:
        if candidate.timestamp.date() > day:
            break
        found = candidate
    return found


def trading_days_after(bars: Sequence[Bar], entry_date: dt.date) -> int:
    """Sessions strictly after the entry date -- the checkpoint clock."""
    return sum(1 for candidate in bars if candidate.timestamp.date() > entry_date)


def returns(
    entry_close: Decimal,
    current_close: Decimal,
    benchmark_entry: Decimal,
    benchmark_current: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """``(instrument_return, benchmark_return, alpha)`` as exact ratios."""
    if entry_close <= 0 or benchmark_entry <= 0:
        raise ValueError("entry close must be positive")
    instrument = current_close / entry_close - Decimal(1)
    benchmark = benchmark_current / benchmark_entry - Decimal(1)
    return instrument, benchmark, instrument - benchmark


def is_correct(action: ThesisAction, alpha: Decimal) -> bool:
    """Signed by the action: a BUY wants alpha above zero, a trim wants it below."""
    if action is ThesisAction.BUY:
        return alpha > 0
    if action in _RISK_REDUCING:
        return alpha < 0
    raise ValueError(f"{action.value} cannot be graded: it never produces a trade")


@dataclass(frozen=True, slots=True)
class EffectiveGrade:
    """The one grade that stands for an outcome: CLOSE if graded, else the latest."""

    outcome_id: uuid.UUID
    correct: bool
    alpha: Decimal
    graded_at: dt.datetime


def calibrate(key: str, grades: Iterable[EffectiveGrade]) -> CalibrationBucket | None:
    rows = list(grades)
    if not rows:
        return None
    samples = len(rows)
    correct = sum(1 for row in rows if row.correct)
    hit_rate = (Decimal(correct) / Decimal(samples)).quantize(_SIX_PLACES, ROUND_HALF_EVEN)
    mean_alpha = (sum((row.alpha for row in rows), Decimal(0)) / Decimal(samples)).quantize(
        _SIX_PLACES, ROUND_HALF_EVEN
    )
    return CalibrationBucket(
        key=key,
        samples=samples,
        correct=correct,
        hit_rate=hit_rate.normalize() if hit_rate == hit_rate.to_integral() else hit_rate,
        mean_alpha=mean_alpha.normalize() if mean_alpha == mean_alpha.to_integral() else mean_alpha,
        latest_graded_at=max(row.graded_at for row in rows),
    )
```

Note on the `normalize()` calls: `Decimal("1.000000")` must compare equal to `Decimal("1")` (it does) but the tests also compare `Decimal("-0.01")` to a six-place mean; `Decimal("-0.010000") == Decimal("-0.01")` is `True`, so the normalisation is only cosmetic. If `mypy` or `ruff` object to the conditional expressions, simplify to `hit_rate=hit_rate, mean_alpha=mean_alpha` — the equality tests still pass.

- [ ] **Step 6: Run the tests to verify they pass**

From `backend/`: `.venv/bin/python -m pytest tests/unit/test_memory_grading.py tests/unit/test_memory_calibration.py -v`
Expected: all pass. Then the quality gate: `.venv/bin/ruff format stockbrain tests && .venv/bin/ruff check stockbrain tests && .venv/bin/mypy stockbrain tests`.

Also run `tests/unit/test_risk_engine.py` and `tests/unit/test_risk_config.py` to confirm the `RiskInputs` field addition broke nothing.

- [ ] **Step 7: Commit**

```bash
git add backend/stockbrain/risk/models.py backend/stockbrain/intelligence/memory.py backend/tests/unit/test_memory_grading.py backend/tests/unit/test_memory_calibration.py
git commit -m "feat(memory): grading arithmetic and calibration buckets"
```

---

### Task 3: `MemoryService` — record and grade sweep, with settings

**Files:**
- Modify: `backend/stockbrain/intelligence/memory.py` (append the service)
- Modify: `backend/stockbrain/config.py` (five `memory_*` settings after `volatility_bars_days` ~line 840; validation in `_validate_volatility_policy` ~line 1214)
- Modify: `backend/stockbrain/api/settings_model.py` (five `_s(...)` rows after `volatility_bars_days` ~line 1030)
- Modify: `.env.example` (after `VOLATILITY_BARS_DAYS`, ~line 795)
- Test: `backend/tests/integration/test_thesis_memory_sweep.py`

**Interfaces:**
- Consumes: Task 1 models and enums; Task 2 functions; `stockbrain.market_data.yahoo.DailyBars`, `yahoo_symbol`; `stockbrain.db.models.portfolio.Position`; `stockbrain.db.models.proposals.TradeProposal`; `stockbrain.db.models.research.ResearchRun, Thesis`; `stockbrain.db.models.sources.Event`; `stockbrain.db.models.companies.BrokerInstrument`.
- Produces:
  ```python
  class MemoryService:
      def __init__(self, database: Database, settings: Settings, *, bars: DailyBars, broker: Broker = Broker.TRADING212) -> None
      async def record(self, *, now: dt.datetime | None = None) -> int          # new outcome rows
      async def grade(self, *, now: dt.datetime | None = None) -> dict[str, int]  # tally
      async def tick(self) -> None                                               # record then grade; the scheduler entry point
  ```
  Settings: `memory_grade_enabled: bool = True`, `memory_packet_enabled: bool = False`, `memory_grade_interval_seconds: float = 21600.0`, `memory_benchmark_symbol: str = "SPY"`, `memory_standing_thesis_max_age_days: int = 14`.

- [ ] **Step 1: Add the settings**

In `backend/stockbrain/config.py`, after `volatility_bars_days: int = 90`:

```python
    memory_grade_enabled: bool = True
    """The thesis-memory sweep: record executed thesis trades and grade them
    against the benchmark from daily bars.  Additive -- it writes only the two
    memory tables and never touches a proposal."""
    memory_packet_enabled: bool = False
    """Whether research packets carry the memory section (standing thesis,
    position, calibration).  This is the behavioural change; off until the
    operator turns it on."""
    memory_grade_interval_seconds: float = 21600.0
    memory_benchmark_symbol: str = "SPY"
    memory_standing_thesis_max_age_days: int = 14
```

In `_validate_volatility_policy` (the validator that checks `volatility_refresh_interval_seconds`), add alongside the existing checks:

```python
        if not 3600.0 <= self.memory_grade_interval_seconds <= 86400.0:
            problems.append(
                "MEMORY_GRADE_INTERVAL_SECONDS must be between 3600 and 86400 "
                f"(got {self.memory_grade_interval_seconds})"
            )
        if not 1 <= self.memory_standing_thesis_max_age_days <= 90:
            problems.append(
                "MEMORY_STANDING_THESIS_MAX_AGE_DAYS must be between 1 and 90 "
                f"(got {self.memory_standing_thesis_max_age_days})"
            )
        if not self.memory_benchmark_symbol.strip():
            problems.append("MEMORY_BENCHMARK_SYMBOL must not be empty")
```

(Read the validator first: it collects `problems` and raises at the end; match that shape exactly.)

In `backend/stockbrain/api/settings_model.py`, after the `volatility_bars_days` row:

```python
            _s(
                "memory_grade_enabled",
                "Thesis memory grading",
                RESTART,
                "Whether the sweep that records executed thesis trades and grades them "
                "against the benchmark runs. Additive: it writes only the memory tables.",
            ),
            _s(
                "memory_packet_enabled",
                "Memory in research packets",
                RESTART,
                "Whether every research packet carries the standing thesis, the live "
                "position and the calibration record. Changes what research sees; off "
                "until turned on deliberately.",
            ),
            _s(
                "memory_grade_interval_seconds",
                "Memory grading interval",
                RESTART,
                "How often the grading sweep runs. Daily bars change once a day.",
                unit="seconds",
            ),
            _s(
                "memory_benchmark_symbol",
                "Memory benchmark",
                RESTART,
                "Yahoo symbol every outcome's return is measured against.",
            ),
            _s(
                "memory_standing_thesis_max_age_days",
                "Standing thesis maximum age",
                RESTART,
                "A published thesis older than this is no longer shown to research as "
                "the company's standing view.",
                unit="days",
            ),
```

In `.env.example`, after `VOLATILITY_BARS_DAYS=90`:

```
# Thesis memory. The grading sweep records every executed thesis-backed trade and
# grades it against the benchmark from Yahoo daily closes at horizon-relative
# checkpoints (5/20/60 trading days) and on close. Zero LLM calls; writes only
# the thesis_outcomes tables. Safe to leave on.
MEMORY_GRADE_ENABLED=true
# The memory section in research packets (standing thesis, live position,
# calibration record). This changes what research sees and is the one switch
# here that affects trading. Off by default; turn on once the grades have data.
MEMORY_PACKET_ENABLED=false
MEMORY_GRADE_INTERVAL_SECONDS=21600
MEMORY_BENCHMARK_SYMBOL=SPY
MEMORY_STANDING_THESIS_MAX_AGE_DAYS=14
```

Run `.venv/bin/python -m pytest tests/unit/test_settings_model.py tests/unit/test_environment_isolation.py -q` from `backend/` — expected pass (the catalogue test checks each attribute exists on `Settings`).

- [ ] **Step 2: Write the failing sweep tests**

Create `backend/tests/integration/test_thesis_memory_sweep.py`:

```python
"""The memory sweep: record executed thesis trades, grade them from bars."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.base import utcnow
from stockbrain.db.models.memory import ThesisOutcome, ThesisOutcomeGrade
from stockbrain.db.models.portfolio import Position
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.session import Database
from stockbrain.enums import (
    Broker,
    OrderSide,
    OrderType,
    OutcomeStatus,
    PriceSource,
    ProposalStatus,
    ThesisAction,
    TimeHorizon,
)
from stockbrain.errors import ProviderResponseError
from stockbrain.intelligence.memory import MemoryService
from stockbrain.market_data.base import Bar
from tests import proposal_helpers as ph

pytestmark = pytest.mark.integration

ENTRY = dt.datetime(2026, 9, 1, 15, tzinfo=dt.UTC)


def _bar(symbol: str, day: dt.date, close: str) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=1,
    )


def _series(symbol: str, start: dt.date, closes: Sequence[str]) -> list[Bar]:
    """One bar per weekday from ``start``."""
    bars: list[Bar] = []
    day = start
    for close in closes:
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        bars.append(_bar(symbol, day, close))
        day += dt.timedelta(days=1)
    return bars


class FakeBars:
    def __init__(self, series: dict[str, tuple[list[Bar], str]]) -> None:
        self.series = series
        self.calls: list[str] = []

    async def daily_bars(self, symbol: str, *, days: int) -> tuple[Sequence[Bar], str]:
        self.calls.append(symbol)
        if symbol not in self.series:
            raise ProviderResponseError(f"no bars for {symbol}")
        return self.series[symbol]


def _settings(**overrides: object) -> Settings:
    return ph.settings(memory_grade_enabled=True, **overrides)


async def _executed(
    database: Database,
    *,
    action: ThesisAction = ThesisAction.BUY,
    horizon: TimeHorizon = TimeHorizon.WEEKS,
    executed_at: dt.datetime = ENTRY,
    risk_rules: list[dict[str, object]] | None = None,
) -> uuid.UUID:
    await ph.seed(database, action=action)
    async with database.transaction() as session:
        thesis = await session.get(ph.Thesis, ph.THESIS_ID)  # type: ignore[attr-defined]
        assert thesis is not None
        thesis.time_horizon = horizon
    proposal = TradeProposal(
        thesis_id=ph.THESIS_ID,
        research_run_id=ph.RUN_ID,
        broker=Broker.TRADING212,
        broker_ticker="AAPL_US_EQ",
        account_id=ph.ACCOUNT_ID,
        broker_environment="demo",
        side=OrderSide.BUY if action is ThesisAction.BUY else OrderSide.SELL,
        order_type=OrderType.MARKET,
        proposed_quantity=Decimal("2"),
        reference_price=Decimal("100"),
        reference_currency="USD",
        price_source=PriceSource.ALPACA_IEX,
        quote_timestamp=executed_at,
        quote_age_ms=0,
        estimated_notional=Decimal("200"),
        account_currency="USD",
        status=ProposalStatus.EXECUTED,
        executed_at=executed_at,
        expires_at=executed_at + dt.timedelta(days=1),
        research_action=action.value,
        research_confidence=0.7,
        risk_rules=risk_rules or [],
    )
    async with database.transaction() as session:
        session.add(proposal)
    return proposal.id


async def _hold(database: Database, quantity: Decimal = Decimal("2")) -> None:
    await ph.fund(database, positions={"AAPL_US_EQ": (quantity, quantity)})


async def _outcomes(database: Database) -> list[ThesisOutcome]:
    async with database.session() as session:
        return list((await session.scalars(sa.select(ThesisOutcome))).all())


async def _grades(database: Database) -> list[ThesisOutcomeGrade]:
    async with database.session() as session:
        return list(
            (
                await session.scalars(
                    sa.select(ThesisOutcomeGrade).order_by(ThesisOutcomeGrade.trading_days)
                )
            ).all()
        )


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
async def test_an_executed_thesis_trade_is_recorded_once(clean_tables: Database) -> None:
    proposal_id = await _executed(clean_tables)
    service = MemoryService(clean_tables, _settings(), bars=FakeBars({}))
    assert await service.record() == 1
    assert await service.record() == 0
    (outcome,) = await _outcomes(clean_tables)
    assert outcome.proposal_id == proposal_id
    assert outcome.action is ThesisAction.BUY
    assert outcome.horizon is TimeHorizon.WEEKS
    assert outcome.entry_date == ENTRY.date()
    assert outcome.benchmark_symbol == "SPY"
    assert outcome.status is OutcomeStatus.PENDING
    assert outcome.is_exit is False


async def test_an_exit_sweep_proposal_is_recorded_as_an_exit(clean_tables: Database) -> None:
    await _executed(
        clean_tables,
        action=ThesisAction.SELL,
        risk_rules=[{"rule_id": "hard_stop", "outcome": "BLOCK"}],
    )
    service = MemoryService(clean_tables, _settings(), bars=FakeBars({}))
    await service.record()
    (outcome,) = await _outcomes(clean_tables)
    assert outcome.is_exit is True
    assert outcome.exit_rule_id == "hard_stop"


async def test_a_proposal_without_a_thesis_is_not_recorded(clean_tables: Database) -> None:
    await _executed(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(sa.update(TradeProposal).values(thesis_id=None))
    service = MemoryService(clean_tables, _settings(), bars=FakeBars({}))
    assert await service.record() == 0


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------
def _world(*, instrument: Sequence[str], benchmark: Sequence[str]) -> FakeBars:
    start = dt.date(2026, 8, 31)
    return FakeBars(
        {
            "AAPL": (_series("AAPL", start, instrument), "USD"),
            "SPY": (_series("SPY", start, benchmark), "USD"),
        }
    )


async def test_a_weeks_buy_is_graded_at_d5_once_five_sessions_have_passed(
    clean_tables: Database,
) -> None:
    await _executed(clean_tables)
    await _hold(clean_tables)
    # Entry Tue 1 Sep at 100; D5 is Tue 8 Sep.  Instrument +10%, SPY +2%.
    bars = _world(
        instrument=["99", "100", "101", "102", "103", "104", "110", "111"],
        benchmark=["500", "500", "501", "502", "503", "504", "510", "511"],
    )
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    tally = await service.grade(now=dt.datetime(2026, 9, 9, 22, tzinfo=dt.UTC))
    assert tally["graded"] == 1
    (grade,) = await _grades(clean_tables)
    assert grade.checkpoint == "D5"
    assert grade.entry_close == Decimal("100")
    assert grade.current_close == Decimal("111")
    assert grade.instrument_return == Decimal("0.11")
    assert grade.benchmark_return == Decimal("0.022")
    assert grade.alpha == Decimal("0.088")
    assert grade.correct is True
    assert sorted(bars.calls) == ["AAPL", "SPY"]


async def test_too_few_sessions_means_no_grade_yet(clean_tables: Database) -> None:
    await _executed(clean_tables)
    await _hold(clean_tables)
    bars = _world(instrument=["99", "100", "101"], benchmark=["500", "500", "501"])
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    tally = await service.grade(now=dt.datetime(2026, 9, 3, 22, tzinfo=dt.UTC))
    assert tally["graded"] == 0
    assert await _grades(clean_tables) == []


async def test_a_checkpoint_is_graded_once(clean_tables: Database) -> None:
    await _executed(clean_tables)
    await _hold(clean_tables)
    bars = _world(
        instrument=["99", "100", "101", "102", "103", "104", "110", "111"],
        benchmark=["500", "500", "501", "502", "503", "504", "510", "511"],
    )
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    first = dt.datetime(2026, 9, 9, 22, tzinfo=dt.UTC)
    await service.grade(now=first)
    # A second tick a day later re-fetches (the 20h clock has elapsed) but
    # finds D5 already graded and D20 not yet due.
    tally = await service.grade(now=first + dt.timedelta(days=1))
    assert tally["graded"] == 0
    assert len(await _grades(clean_tables)) == 1


async def test_a_reduce_is_correct_when_the_instrument_then_lags(clean_tables: Database) -> None:
    await _executed(clean_tables, action=ThesisAction.REDUCE, horizon=TimeHorizon.DAYS)
    await _hold(clean_tables)
    # D1 for a DAYS horizon.  Instrument -5%, SPY +1% -> alpha -6% -> correct trim.
    bars = _world(instrument=["99", "100", "95"], benchmark=["500", "500", "505"])
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    await service.grade(now=dt.datetime(2026, 9, 2, 22, tzinfo=dt.UTC))
    (grade,) = await _grades(clean_tables)
    assert (grade.checkpoint, grade.correct) == ("D1", True)


async def test_a_buy_whose_position_is_gone_is_closed_and_graded(clean_tables: Database) -> None:
    await _executed(clean_tables)
    await ph.fund(clean_tables)  # no positions: the holding is gone
    bars = _world(instrument=["99", "100", "90"], benchmark=["500", "500", "500"])
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    await service.grade(now=dt.datetime(2026, 9, 2, 22, tzinfo=dt.UTC))
    (outcome,) = await _outcomes(clean_tables)
    assert outcome.status is OutcomeStatus.CLOSED
    assert outcome.close_reason == "position_gone"
    grades = await _grades(clean_tables)
    assert [grade.checkpoint for grade in grades] == ["CLOSE"]
    assert grades[0].correct is False


async def test_a_close_takes_its_reason_and_date_from_the_exit_sell(clean_tables: Database) -> None:
    await _executed(clean_tables)
    await ph.fund(clean_tables)
    sold_at = dt.datetime(2026, 9, 3, 15, tzinfo=dt.UTC)
    async with clean_tables.transaction() as session:
        session.add(
            TradeProposal(
                thesis_id=ph.THESIS_ID,
                research_run_id=ph.RUN_ID,
                broker=Broker.TRADING212,
                broker_ticker="AAPL_US_EQ",
                account_id=ph.ACCOUNT_ID,
                broker_environment="demo",
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                proposed_quantity=Decimal("2"),
                reference_price=Decimal("92"),
                reference_currency="USD",
                price_source=PriceSource.ALPACA_IEX,
                quote_timestamp=sold_at,
                quote_age_ms=0,
                estimated_notional=Decimal("184"),
                account_currency="USD",
                status=ProposalStatus.EXECUTED,
                executed_at=sold_at,
                expires_at=sold_at + dt.timedelta(days=1),
                research_action="SELL",
                research_confidence=0.7,
                risk_rules=[{"rule_id": "trailing_stop", "outcome": "BLOCK"}],
            )
        )
    bars = _world(instrument=["99", "100", "95", "92", "120"], benchmark=["500"] * 5)
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    await service.grade(now=dt.datetime(2026, 9, 5, 22, tzinfo=dt.UTC))
    async with clean_tables.session() as session:
        outcome = (
            await session.scalars(
                sa.select(ThesisOutcome).where(ThesisOutcome.action == ThesisAction.BUY)
            )
        ).one()
    assert outcome.status is OutcomeStatus.CLOSED
    assert outcome.close_reason == "trailing_stop"
    assert outcome.closed_at == sold_at
    grades = [g for g in await _grades(clean_tables) if g.outcome_id == outcome.id]
    assert [g.checkpoint for g in grades] == ["CLOSE"]
    assert grades[0].current_close == Decimal("92")  # the close on the sell date, not the rebound


async def test_an_unknown_symbol_abandons_the_outcome(clean_tables: Database) -> None:
    await _executed(clean_tables)
    await _hold(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(ph.BrokerInstrument).values(exchange="Nowhere Exchange")  # type: ignore[attr-defined]
        )
    service = MemoryService(clean_tables, _settings(), bars=FakeBars({}))
    await service.record()
    tally = await service.grade()
    assert tally["abandoned"] == 1
    (outcome,) = await _outcomes(clean_tables)
    assert outcome.status is OutcomeStatus.ABANDONED
    assert outcome.close_reason == "no_yahoo_symbol"


async def test_a_provider_failure_degrades_one_symbol_and_the_tick_survives(
    clean_tables: Database,
) -> None:
    await _executed(clean_tables)
    await _hold(clean_tables)
    bars = FakeBars({"SPY": (_series("SPY", dt.date(2026, 8, 31), ["500"] * 8), "USD")})
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    tally = await service.grade(now=dt.datetime(2026, 9, 9, 22, tzinfo=dt.UTC))
    assert tally["failed"] == 1
    (outcome,) = await _outcomes(clean_tables)
    assert outcome.status is OutcomeStatus.PENDING
    assert outcome.last_attempt_at is not None


async def test_a_symbol_is_not_refetched_within_twenty_hours(clean_tables: Database) -> None:
    await _executed(clean_tables)
    await _hold(clean_tables)
    bars = _world(instrument=["99", "100", "101"], benchmark=["500", "500", "501"])
    service = MemoryService(clean_tables, _settings(), bars=bars)
    await service.record()
    first = dt.datetime(2026, 9, 3, 22, tzinfo=dt.UTC)
    await service.grade(now=first)
    calls = len(bars.calls)
    await service.grade(now=first + dt.timedelta(hours=2))
    assert len(bars.calls) == calls
```

The two `# type: ignore[attr-defined]` lines assume `tests/proposal_helpers.py` re-exports `Thesis` and `BrokerInstrument`; if it does not, import them directly from `stockbrain.db.models.research` and `stockbrain.db.models.companies` and drop the ignores.

- [ ] **Step 3: Run the tests to verify they fail**

From `backend/`: `DATABASE_URL_TEST=<url> .venv/bin/python -m pytest tests/integration/test_thesis_memory_sweep.py -q`
Expected: ImportError `MemoryService`.

- [ ] **Step 4: Write the service**

Append to `backend/stockbrain/intelligence/memory.py` (extend the imports at the top: `sqlalchemy as sa`, `Counter`, `Settings`, `Database`, `utcnow`, `BrokerInstrument`, `ThesisOutcome`, `ThesisOutcomeGrade`, `Position`, `TradeProposal`, `ResearchRun`, `Thesis`, `Event`, `Broker`, `OutcomeStatus`, `ProposalStatus`, `ProviderError`, `get_logger`, `DailyBars`, `yahoo_symbol`; add `"MemoryService"` to `__all__`):

```python
log = get_logger(__name__)

#: A symbol fetched more recently than this is not fetched again (spec §5.5).
_MIN_REFRESH_INTERVAL = dt.timedelta(hours=20)

_TALLY_KEYS = (
    "considered",
    "graded",
    "closed",
    "abandoned",
    "skipped_fresh",
    "skipped_not_due",
    "failed",
)


@dataclass(frozen=True, slots=True)
class _Pending:
    outcome: ThesisOutcome
    symbol: str | None
    exchange: str | None
    held: bool
    sold_at: dt.datetime | None
    sold_rule: str | None


class MemoryService:
    """Records thesis outcomes and grades them; the scheduler's entry point is ``tick``."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        bars: DailyBars,
        broker: Broker = Broker.TRADING212,
    ) -> None:
        self._database = database
        self._settings = settings
        self._bars = bars
        self._broker = broker

    # ------------------------------------------------------------------
    # Record (spec §5.1)
    # ------------------------------------------------------------------
    async def record(self, *, now: dt.datetime | None = None) -> int:
        """One outcome per EXECUTED thesis-backed proposal; idempotent."""
        moment = now or utcnow()
        async with self._database.transaction() as session:
            rows = (
                await session.execute(
                    sa.select(TradeProposal, Thesis, ResearchRun, Event.event_type, BrokerInstrument)
                    .join(Thesis, Thesis.id == TradeProposal.thesis_id)
                    .join(ResearchRun, ResearchRun.id == Thesis.research_run_id)
                    .join(Event, Event.id == ResearchRun.event_id, isouter=True)
                    # The run's resolved listing, always set; the proposal's own
                    # ``broker_instrument_id`` is nullable and the test seeds omit it.
                    .join(
                        BrokerInstrument,
                        BrokerInstrument.id == ResearchRun.broker_instrument_id,
                    )
                    .where(
                        TradeProposal.status == ProposalStatus.EXECUTED,
                        TradeProposal.thesis_id.is_not(None),
                        TradeProposal.executed_at.is_not(None),
                        TradeProposal.broker == self._broker,
                        ~sa.exists(
                            sa.select(ThesisOutcome.id).where(
                                ThesisOutcome.proposal_id == TradeProposal.id
                            )
                        ),
                    )
                    .order_by(TradeProposal.executed_at)
                    .limit(200)
                )
            ).all()
            created = 0
            for proposal, thesis, run, event_type, instrument in rows:
                action = ThesisAction(proposal.research_action or proposal.side.value)
                if action not in {ThesisAction.BUY, ThesisAction.REDUCE, ThesisAction.SELL}:
                    continue
                exit_rule = _exit_rule_of(proposal.risk_rules)
                assert proposal.executed_at is not None
                session.add(
                    ThesisOutcome(
                        proposal_id=proposal.id,
                        thesis_id=thesis.id,
                        research_run_id=run.id,
                        company_id=run.company_id,
                        broker_instrument_id=instrument.id,
                        broker=proposal.broker,
                        broker_ticker=proposal.broker_ticker,
                        event_type=event_type,
                        action=action,
                        horizon=thesis.time_horizon,
                        confidence=Decimal(str(proposal.research_confidence or 0)),
                        is_exit=exit_rule is not None,
                        exit_rule_id=exit_rule,
                        entry_at=proposal.executed_at,
                        entry_date=proposal.executed_at.astimezone(dt.UTC).date(),
                        reference_price=proposal.reference_price,
                        currency=instrument.currency,
                        benchmark_symbol=self._settings.memory_benchmark_symbol,
                        created_at=moment,
                        updated_at=moment,
                    )
                )
                created += 1
        if created:
            log.info("thesis_memory_recorded", outcomes=created)
        return created

    # ------------------------------------------------------------------
    # Grade (spec §5.2-§5.5)
    # ------------------------------------------------------------------
    async def grade(self, *, now: dt.datetime | None = None) -> dict[str, int]:
        moment = now or utcnow()
        counts: Counter[str] = Counter(dict.fromkeys(_TALLY_KEYS, 0))
        pending = await self._load_pending(moment, counts)
        if not pending:
            return dict(counts)

        symbols = {item.symbol for item in pending if item.symbol} | {
            self._settings.memory_benchmark_symbol
        }
        series: dict[str, tuple[Sequence[Bar], str]] = {}
        failed_symbols: set[str] = set()
        for symbol in sorted(symbols):
            try:
                series[symbol] = await self._bars.daily_bars(symbol, days=BARS_DAYS)
            except ProviderError as exc:
                failed_symbols.add(symbol)
                log.warning("thesis_memory_bars_failed", symbol=symbol, error=str(exc)[:200])

        benchmark = series.get(self._settings.memory_benchmark_symbol)
        for item in pending:
            outcome = item.outcome
            try:
                if item.symbol is None:
                    await self._abandon(outcome.id, "no_yahoo_symbol", moment)
                    counts["abandoned"] += 1
                    continue
                if item.symbol in failed_symbols or benchmark is None:
                    await self._stamp_attempt(outcome.id, moment)
                    counts["failed"] += 1
                    continue
                bars, currency = series[item.symbol]
                if outcome.currency and currency != outcome.currency:
                    await self._abandon(outcome.id, f"currency_mismatch:{currency}", moment)
                    counts["abandoned"] += 1
                    continue
                graded, closed = await self._grade_one(outcome, item, bars, benchmark[0], moment)
                counts["graded"] += graded
                counts["closed"] += closed
                if not graded and not closed:
                    counts["skipped_not_due"] += 1
            except Exception as exc:  # noqa: BLE001 - one outcome never sinks the tick
                counts["failed"] += 1
                log.exception(
                    "thesis_memory_grade_failed", outcome_id=str(outcome.id), error=str(exc)[:200]
                )
                await self._stamp_attempt(outcome.id, moment)
        log.info("thesis_memory_grade_completed", **counts)
        return dict(counts)

    async def tick(self) -> None:
        await self.record()
        await self.grade()

    # ------------------------------------------------------------------
    async def _load_pending(self, moment: dt.datetime, counts: Counter[str]) -> list[_Pending]:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    sa.select(ThesisOutcome, BrokerInstrument.exchange)
                    .join(BrokerInstrument, BrokerInstrument.id == ThesisOutcome.broker_instrument_id)
                    .where(
                        ThesisOutcome.status == OutcomeStatus.PENDING,
                        ThesisOutcome.broker == self._broker,
                    )
                    .order_by(ThesisOutcome.entry_at)
                    .limit(500)
                )
            ).all()
            pending: list[_Pending] = []
            for outcome, exchange in rows:
                counts["considered"] += 1
                if (
                    outcome.last_attempt_at is not None
                    and moment - outcome.last_attempt_at < _MIN_REFRESH_INTERVAL
                ):
                    counts["skipped_fresh"] += 1
                    continue
                held = (
                    await session.scalar(
                        sa.select(sa.func.count())
                        .select_from(Position)
                        .where(
                            Position.broker == self._broker,
                            Position.broker_ticker == outcome.broker_ticker,
                            Position.quantity > 0,
                        )
                    )
                    or 0
                ) > 0
                sold = (
                    await session.execute(
                        sa.select(TradeProposal.executed_at, TradeProposal.risk_rules)
                        .where(
                            TradeProposal.broker == self._broker,
                            TradeProposal.broker_ticker == outcome.broker_ticker,
                            TradeProposal.status == ProposalStatus.EXECUTED,
                            TradeProposal.side == OrderSide.SELL,
                            TradeProposal.executed_at > outcome.entry_at,
                        )
                        .order_by(TradeProposal.executed_at.desc())
                        .limit(1)
                    )
                ).first()
                pending.append(
                    _Pending(
                        outcome=outcome,
                        symbol=yahoo_symbol(outcome.broker_ticker, exchange),
                        exchange=exchange,
                        held=held,
                        sold_at=sold[0] if sold else None,
                        sold_rule=_exit_rule_of(sold[1]) if sold else None,
                    )
                )
            return pending

    async def _grade_one(
        self,
        outcome: ThesisOutcome,
        item: _Pending,
        bars: Sequence[Bar],
        benchmark: Sequence[Bar],
        moment: dt.datetime,
    ) -> tuple[int, int]:
        """Grade every due checkpoint (and CLOSE) for one outcome. Returns (graded, closed)."""
        entry = entry_bar(bars, outcome.entry_date)
        bench_entry = entry_bar(benchmark, outcome.entry_date)
        if entry is None or bench_entry is None:
            await self._stamp_attempt(outcome.id, moment)
            return 0, 0
        days = trading_days_after(bars, outcome.entry_date)
        async with self._database.transaction() as session:
            row = await session.get(ThesisOutcome, outcome.id, with_for_update=True)
            assert row is not None
            existing = set(
                await session.scalars(
                    sa.select(ThesisOutcomeGrade.checkpoint).where(
                        ThesisOutcomeGrade.outcome_id == row.id
                    )
                )
            )
            graded = 0
            closed = 0
            # Checkpoints are graded against the latest close.
            latest = bars[-1]
            bench_latest = bench_entry if len(benchmark) == 0 else benchmark[-1]
            schedule = checkpoints_for(row.horizon)
            for checkpoint in schedule:
                needed = checkpoint.trading_days
                assert needed is not None
                if checkpoint.value in existing or days < needed:
                    continue
                session.add(
                    _grade_row(row, checkpoint, days, entry, latest, bench_entry, bench_latest, moment)
                )
                existing.add(checkpoint.value)
                graded += 1

            # Close (spec §5.4).
            if row.action is ThesisAction.BUY and not row.is_exit and not item.held:
                closed_at = item.sold_at or moment
                close_day = closed_at.astimezone(dt.UTC).date()
                at_close = bar_on_or_before(bars, close_day)
                bench_close = bar_on_or_before(benchmark, close_day)
                if at_close is not None and bench_close is not None:
                    if OutcomeCheckpoint.CLOSE.value not in existing:
                        session.add(
                            _grade_row(
                                row,
                                OutcomeCheckpoint.CLOSE,
                                trading_days_after(bars, row.entry_date)
                                - trading_days_after(bars, close_day),
                                entry,
                                at_close,
                                bench_entry,
                                bench_close,
                                moment,
                            )
                        )
                        graded += 1
                    row.status = OutcomeStatus.CLOSED
                    row.closed_at = closed_at
                    row.close_reason = item.sold_rule or (
                        "sell_executed" if item.sold_at else "position_gone"
                    )
                    closed = 1
            elif row.action is not ThesisAction.BUY or row.is_exit:
                # Trims, sells and exits never close; they retire after their last checkpoint.
                if all(checkpoint.value in existing for checkpoint in schedule):
                    row.status = OutcomeStatus.CLOSED
                    row.closed_at = moment
                    row.close_reason = "checkpoints_complete"
                    closed = 1
            row.last_attempt_at = moment
            row.updated_at = moment
        return graded, closed

    async def _stamp_attempt(self, outcome_id: uuid.UUID, moment: dt.datetime) -> None:
        async with self._database.transaction() as session:
            await session.execute(
                sa.update(ThesisOutcome)
                .where(ThesisOutcome.id == outcome_id)
                .values(last_attempt_at=moment, updated_at=moment)
            )

    async def _abandon(self, outcome_id: uuid.UUID, reason: str, moment: dt.datetime) -> None:
        async with self._database.transaction() as session:
            await session.execute(
                sa.update(ThesisOutcome)
                .where(ThesisOutcome.id == outcome_id)
                .values(
                    status=OutcomeStatus.ABANDONED,
                    closed_at=moment,
                    close_reason=reason,
                    last_attempt_at=moment,
                    updated_at=moment,
                )
            )


def _exit_rule_of(rules: object) -> str | None:
    """The exit rule id recorded on a proposal's ``risk_rules``, if any."""
    if not isinstance(rules, list):
        return None
    for rule in rules:
        if isinstance(rule, dict) and rule.get("rule_id") in EXIT_RULE_IDS:
            return str(rule["rule_id"])
    return None


def _grade_row(
    outcome: ThesisOutcome,
    checkpoint: OutcomeCheckpoint,
    trading_days: int,
    entry: Bar,
    current: Bar,
    bench_entry: Bar,
    bench_current: Bar,
    moment: dt.datetime,
) -> ThesisOutcomeGrade:
    instrument, benchmark, alpha = returns(
        entry.close, current.close, bench_entry.close, bench_current.close
    )
    return ThesisOutcomeGrade(
        outcome_id=outcome.id,
        checkpoint=checkpoint.value,
        trading_days=trading_days,
        entry_close=entry.close,
        current_close=current.close,
        benchmark_entry_close=bench_entry.close,
        benchmark_current_close=bench_current.close,
        instrument_return=instrument.quantize(_SIX_PLACES, ROUND_HALF_EVEN),
        benchmark_return=benchmark.quantize(_SIX_PLACES, ROUND_HALF_EVEN),
        alpha=alpha.quantize(_SIX_PLACES, ROUND_HALF_EVEN),
        correct=is_correct(outcome.action, alpha),
        graded_at=moment,
    )
```

Two things to check while implementing:
1. `session.get(..., with_for_update=True)` is the SQLAlchemy 2 spelling; if the project's version rejects it, use `(await session.execute(sa.select(ThesisOutcome).where(ThesisOutcome.id == outcome.id).with_for_update())).scalar_one()`.
2. The `sorted(bars.calls) == ["AAPL", "SPY"]` test asserts each symbol is fetched once per tick; keep the `symbols` set.

- [ ] **Step 5: Run the tests to verify they pass**

From `backend/`: `DATABASE_URL_TEST=<url> .venv/bin/python -m pytest tests/integration/test_thesis_memory_sweep.py -v`
Expected: all pass. Then the quality gate.

- [ ] **Step 6: Commit**

```bash
git add backend/stockbrain/intelligence/memory.py backend/stockbrain/config.py backend/stockbrain/api/settings_model.py .env.example backend/tests/integration/test_thesis_memory_sweep.py
git commit -m "feat(memory): record and grade thesis outcomes from daily closes"
```

---

### Task 4: Memory in the research packet

**Files:**
- Modify: `backend/stockbrain/intelligence/memory.py` (read queries)
- Modify: `backend/stockbrain/intelligence/research.py` (`PROMPT_VERSION`, four models, `ResearchPacket.memory`)
- Modify: `backend/stockbrain/intelligence/research_service.py` (`__init__` kwargs, `packet()` populates memory, `config` includes the flag)
- Modify: `backend/stockbrain/intelligence/tradingagents_adapter.py` (`SYSTEM_POLICY`)
- Test: `backend/tests/integration/test_research_packet_memory.py`; update `backend/tests/unit/test_research_prompt.py` if it pins `PROMPT_VERSION` (check with `grep -rn "research-v2" backend/tests`)

**Interfaces:**
- Consumes: Task 2 `calibrate`, `EffectiveGrade`; Task 3 `MemoryService`.
- Produces (in `stockbrain.intelligence.research`): `CalibrationRecord`, `StandingThesis`, `PositionMemory`, `ResearchMemory` (spec §7.1 field lists); `ResearchPacket.memory: ResearchMemory | None = None`; `PROMPT_VERSION = "research-v3"`.
- Produces (on `MemoryService`):
  ```python
  async def effective_grades(self, session, *, as_of: dt.datetime, event_type: str | None = None, company_id: uuid.UUID | None = None, action: ThesisAction | None = None, exits: bool = False) -> list[EffectiveGrade]
  async def calibration(self, session, *, event_type: str | None, action: ThesisAction, as_of: dt.datetime) -> CalibrationBucket | None
  async def company_calibration(self, session, *, company_id: uuid.UUID, action: ThesisAction, as_of: dt.datetime) -> CalibrationBucket | None
  async def research_memory(self, session, *, company_id: uuid.UUID, broker_instrument_id: uuid.UUID, broker_ticker: str, event_type: str | None, as_of: dt.datetime) -> ResearchMemory
  ```
- `ResearchService.__init__` gains `memory: MemoryService | None = None, memory_packet_enabled: bool = False`.

- [ ] **Step 1: Write the failing packet tests**

Create `backend/tests/integration/test_research_packet_memory.py`:

```python
"""What research is told about its own prior state (spec §7.1)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.db.base import utcnow
from stockbrain.db.models.memory import ThesisOutcome, ThesisOutcomeGrade
from stockbrain.db.models.portfolio import Position
from stockbrain.db.models.research import ResearchRun, Thesis
from stockbrain.db.models.sources import Event
from stockbrain.db.session import Database
from stockbrain.enums import Broker, ResearchStatus, ThesisAction, TimeHorizon
from stockbrain.intelligence.memory import MemoryService
from stockbrain.intelligence.research import PROMPT_VERSION, ResearchPacket
from stockbrain.intelligence.research_service import ResearchService
from tests.integration.test_research import FakeEngine, seed, service
from tests.integration.test_thesis_memory_sweep import FakeBars, _settings

pytestmark = pytest.mark.integration


def _with_memory(db: Database, *, enabled: bool = True) -> ResearchService:
    research = service(db, FakeEngine())
    research.memory = MemoryService(db, _settings(), bars=FakeBars({}))
    research.memory_packet_enabled = enabled
    return research


async def _publish(
    db: Database,
    value: ResearchPacket,
    *,
    action: ThesisAction = ThesisAction.BUY,
    completed_at: dt.datetime,
    text: str = "Buy on the capacity story.",
) -> uuid.UUID:
    run_id, thesis_id = uuid.uuid4(), uuid.uuid4()
    async with db.transaction() as session:
        session.add(
            ResearchRun(
                id=run_id,
                event_id=value.event_id,
                impact_id=value.impact_id,
                company_id=value.company.company_id,
                broker_instrument_id=value.company.broker_instrument_id,
                status=ResearchStatus.SUCCEEDED,
                completed_at=completed_at,
                structured_decision={
                    "action": action.value,
                    "confidence": 0.72,
                    "horizon": "weeks",
                    "thesis": text,
                    "bull_case": "b",
                    "bear_case": "r",
                    "catalysts": [],
                    "risks": [],
                    "invalidation_conditions": ["Plant cancelled"],
                    "evidence_ids": [str(value.evidence[0].source_id)],
                },
            )
        )
        await session.flush()
        session.add(
            Thesis(
                id=thesis_id,
                research_run_id=run_id,
                action=action,
                confidence=0.72,
                time_horizon=TimeHorizon.WEEKS,
                summary=text,
                invalidation_conditions={"items": ["Plant cancelled"]},
            )
        )
    return thesis_id


async def _packet(db: Database, research: ResearchService, value: ResearchPacket) -> ResearchPacket:
    async with db.session() as session:
        return await research.packet(session, value.impact_id, value.as_of)


def test_the_prompt_version_moved_with_the_packet_shape() -> None:
    assert PROMPT_VERSION == "research-v3"


async def test_memory_is_absent_when_the_packet_flag_is_off(clean_tables: Database) -> None:
    value = await seed(clean_tables)
    built = await _packet(clean_tables, _with_memory(clean_tables, enabled=False), value)
    assert built.memory is None


async def test_memory_is_present_but_empty_when_nothing_is_known(clean_tables: Database) -> None:
    value = await seed(clean_tables)
    built = await _packet(clean_tables, _with_memory(clean_tables), value)
    assert built.memory is not None
    assert built.memory.standing_thesis is None
    assert built.memory.position is None
    assert built.memory.company_record is None
    assert built.memory.event_type_record == ()


async def test_the_latest_thesis_within_the_age_window_is_the_standing_one(
    clean_tables: Database,
) -> None:
    value = await seed(clean_tables)
    old = await _publish(
        clean_tables, value, completed_at=value.as_of - dt.timedelta(days=3), text="old"
    )
    newest = await _publish(
        clean_tables, value, completed_at=value.as_of - dt.timedelta(hours=2), text="newest"
    )
    await _publish(
        clean_tables, value, completed_at=value.as_of + dt.timedelta(hours=1), text="future"
    )
    built = await _packet(clean_tables, _with_memory(clean_tables), value)
    assert built.memory is not None
    standing = built.memory.standing_thesis
    assert standing is not None
    assert standing.thesis_id == newest
    assert standing.thesis_id != old
    assert standing.thesis == "newest"
    assert standing.invalidation_conditions == ("Plant cancelled",)
    assert 1.9 < standing.age_hours < 2.1
    # The chain is never linked automatically (spec §7.1).
    assert built.previous_thesis_id is None
    assert built.previous_thesis is None


async def test_a_thesis_older_than_the_window_is_not_standing(clean_tables: Database) -> None:
    value = await seed(clean_tables)
    await _publish(clean_tables, value, completed_at=value.as_of - dt.timedelta(days=15))
    built = await _packet(clean_tables, _with_memory(clean_tables), value)
    assert built.memory is not None
    assert built.memory.standing_thesis is None


async def test_the_live_position_is_reported_when_synced_before_as_of(
    clean_tables: Database,
) -> None:
    value = await seed(clean_tables)
    async with clean_tables.transaction() as session:
        session.add(
            Position(
                broker=Broker.TRADING212,
                account_id="4242",
                broker_ticker=value.company.broker_ticker,
                quantity=Decimal("3"),
                quantity_available=Decimal("3"),
                average_price=Decimal("100"),
                current_price=Decimal("110"),
                currency="USD",
                initial_fill_date=value.as_of - dt.timedelta(days=1),
                last_synced_at=value.as_of - dt.timedelta(minutes=5),
            )
        )
    built = await _packet(clean_tables, _with_memory(clean_tables), value)
    assert built.memory is not None
    position = built.memory.position
    assert position is not None
    assert position.quantity == Decimal("3")
    assert position.unrealised_pct == Decimal("0.1")


async def test_a_position_synced_after_as_of_is_unknown_to_a_historical_run(
    clean_tables: Database,
) -> None:
    value = await seed(clean_tables)
    async with clean_tables.transaction() as session:
        session.add(
            Position(
                broker=Broker.TRADING212,
                account_id="4242",
                broker_ticker=value.company.broker_ticker,
                quantity=Decimal("3"),
                currency="USD",
                last_synced_at=value.as_of + dt.timedelta(minutes=5),
            )
        )
    built = await _packet(clean_tables, _with_memory(clean_tables), value)
    assert built.memory is not None
    assert built.memory.position is None


async def _graded_outcome(
    db: Database,
    value: ResearchPacket,
    *,
    action: ThesisAction,
    event_type: str,
    correct: bool,
    alpha: str,
    graded_at: dt.datetime,
    checkpoints: tuple[str, ...] = ("D5",),
) -> None:
    """A finished outcome with one grade per checkpoint, no proposal needed for the read path."""
    from stockbrain.db.models.proposals import TradeProposal
    from stockbrain.enums import OrderSide, OrderType, PriceSource, ProposalStatus

    thesis_id = await _publish(db, value, action=action, completed_at=graded_at - dt.timedelta(days=9))
    async with db.session() as session:
        run_id = await session.scalar(sa.select(Thesis.research_run_id).where(Thesis.id == thesis_id))
    assert run_id is not None
    entry = graded_at - dt.timedelta(days=8)
    proposal = TradeProposal(
        thesis_id=thesis_id,
        research_run_id=run_id,
        broker=Broker.TRADING212,
        broker_ticker=value.company.broker_ticker,
        account_id="4242",
        broker_environment="demo",
        side=OrderSide.BUY if action is ThesisAction.BUY else OrderSide.SELL,
        order_type=OrderType.MARKET,
        proposed_quantity=Decimal("1"),
        reference_price=Decimal("100"),
        reference_currency="USD",
        price_source=PriceSource.ALPACA_IEX,
        quote_timestamp=entry,
        quote_age_ms=0,
        estimated_notional=Decimal("100"),
        account_currency="USD",
        status=ProposalStatus.EXECUTED,
        executed_at=entry,
        expires_at=entry + dt.timedelta(days=1),
        research_action=action.value,
        research_confidence=0.72,
    )
    outcome_id = uuid.uuid4()
    async with db.transaction() as session:
        session.add(proposal)
        await session.flush()
        session.add(
            ThesisOutcome(
                id=outcome_id,
                proposal_id=proposal.id,
                thesis_id=thesis_id,
                research_run_id=run_id,
                company_id=value.company.company_id,
                broker_instrument_id=value.company.broker_instrument_id,
                broker=Broker.TRADING212,
                broker_ticker=value.company.broker_ticker,
                event_type=event_type,
                action=action,
                horizon=TimeHorizon.WEEKS,
                confidence=Decimal("0.72"),
                entry_at=entry,
                entry_date=entry.date(),
                currency="USD",
                benchmark_symbol="SPY",
            )
        )
        await session.flush()
        for index, checkpoint in enumerate(checkpoints):
            session.add(
                ThesisOutcomeGrade(
                    outcome_id=outcome_id,
                    checkpoint=checkpoint,
                    trading_days=5 * (index + 1),
                    entry_close=Decimal("100"),
                    current_close=Decimal("100"),
                    benchmark_entry_close=Decimal("100"),
                    benchmark_current_close=Decimal("100"),
                    instrument_return=Decimal(0),
                    benchmark_return=Decimal(0),
                    alpha=Decimal(alpha),
                    correct=correct,
                    graded_at=graded_at + dt.timedelta(minutes=index),
                )
            )


async def test_calibration_rows_report_the_company_and_the_event_type(
    clean_tables: Database,
) -> None:
    value = await seed(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(Event).where(Event.id == value.event_id).values(event_type="REGULATORY")
        )
    before = value.as_of - dt.timedelta(days=1)
    await _graded_outcome(
        clean_tables, value, action=ThesisAction.BUY, event_type="REGULATORY",
        correct=True, alpha="0.02", graded_at=before,
    )
    await _graded_outcome(
        clean_tables, value, action=ThesisAction.REDUCE, event_type="REGULATORY",
        correct=False, alpha="0.01", graded_at=before,
    )
    # Graded after as_of: invisible to this run.
    await _graded_outcome(
        clean_tables, value, action=ThesisAction.BUY, event_type="REGULATORY",
        correct=False, alpha="-0.5", graded_at=value.as_of + dt.timedelta(days=1),
    )
    built = await _packet(clean_tables, _with_memory(clean_tables), value)
    assert built.memory is not None
    company = built.memory.company_record
    assert company is not None
    assert (company.samples, company.correct) == (1, 1)
    by_key = {row.key: row for row in built.memory.event_type_record}
    assert by_key["REGULATORY×BUY"].samples == 1
    assert by_key["REGULATORY×REDUCE"].hit_rate == Decimal("0")
    assert "REGULATORY×SELL" not in by_key


async def test_the_close_grade_stands_for_an_outcome_over_its_checkpoints(
    clean_tables: Database,
) -> None:
    value = await seed(clean_tables)
    before = value.as_of - dt.timedelta(days=1)
    await _graded_outcome(
        clean_tables, value, action=ThesisAction.BUY, event_type="EARNINGS",
        correct=True, alpha="0.03", graded_at=before, checkpoints=("D5", "CLOSE"),
    )
    # Both grades were written with the same values; make CLOSE the incorrect one.
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(ThesisOutcomeGrade)
            .where(ThesisOutcomeGrade.checkpoint == "CLOSE")
            .values(correct=False, alpha=Decimal("-0.02"))
        )
    built = await _packet(clean_tables, _with_memory(clean_tables), value)
    assert built.memory is not None
    company = built.memory.company_record
    assert company is not None
    assert (company.samples, company.correct, company.mean_alpha) == (1, 0, Decimal("-0.02"))
```

`tests/integration/test_research.py` must expose `FakeEngine`, `seed` and `service` at module level for the import above — check the class name with `grep -n "^class Fake\|^def service\|^async def seed" backend/tests/integration/test_research.py` and adjust the import.

- [ ] **Step 2: Run the tests to verify they fail**

From `backend/`: `DATABASE_URL_TEST=<url> .venv/bin/python -m pytest tests/integration/test_research_packet_memory.py -q`
Expected: failures on `PROMPT_VERSION`, `memory` attribute, `ResearchMemory`.

- [ ] **Step 3: Extend `research.py`**

In `backend/stockbrain/intelligence/research.py`:

1. `PROMPT_VERSION = "research-v3"`.
2. Add before `class ResearchPacket` (imports: `Decimal` from `decimal`; `AwareDatetime`, `BaseModel`, `Field` are already imported — verify):

```python
class CalibrationRecord(BaseModel):
    """Pydantic mirror of ``stockbrain.risk.models.CalibrationBucket`` (spec §6)."""

    model_config = ConfigDict(frozen=True)

    key: str
    samples: int
    correct: int
    hit_rate: Decimal
    mean_alpha: Decimal
    latest_graded_at: AwareDatetime


class StandingThesis(BaseModel):
    """The company's latest published thesis, shown to research, never linked."""

    model_config = ConfigDict(frozen=True)

    thesis_id: uuid.UUID
    published_at: AwareDatetime
    action: ThesisAction
    confidence: float
    horizon: TimeHorizon
    thesis: str = Field(max_length=600)
    invalidation_conditions: tuple[str, ...] = ()
    age_hours: float


class PositionMemory(BaseModel):
    model_config = ConfigDict(frozen=True)

    quantity: Decimal
    average_price: Decimal | None
    current_price: Decimal | None
    unrealised_pct: Decimal | None
    opened_at: AwareDatetime | None
    synced_at: AwareDatetime


class ResearchMemory(BaseModel):
    """This system's own prior state.  Context, never evidence (spec §7.1)."""

    model_config = ConfigDict(frozen=True)

    standing_thesis: StandingThesis | None = None
    position: PositionMemory | None = None
    company_record: CalibrationRecord | None = None
    event_type_record: tuple[CalibrationRecord, ...] = ()
```

3. On `ResearchPacket`, after `previous_thesis: ResearchDecision | None = None`:

```python
    memory: ResearchMemory | None = None
```

`ConfigDict` import: `from pydantic import ConfigDict` if not already present. Check `ThesisAction`, `TimeHorizon` are imported from `stockbrain.enums` in this module (they are, for `ResearchDecision`).

- [ ] **Step 4: Add the read queries to `MemoryService`**

Append to the `MemoryService` class in `backend/stockbrain/intelligence/memory.py` (imports: `AsyncSession` from `sqlalchemy.ext.asyncio`; `CalibrationRecord`, `PositionMemory`, `ResearchMemory`, `StandingThesis` from `stockbrain.intelligence.research` — this direction, `memory → research`, is fine; `research.py` must never import `memory`):

```python
    # ------------------------------------------------------------------
    # Read side (spec §6, §7.1)
    # ------------------------------------------------------------------
    async def effective_grades(
        self,
        session: AsyncSession,
        *,
        as_of: dt.datetime,
        event_type: str | None = None,
        company_id: uuid.UUID | None = None,
        action: ThesisAction | None = None,
        exits: bool = False,
    ) -> list[EffectiveGrade]:
        """One grade per outcome: CLOSE if graded by ``as_of``, else the latest."""
        stmt = (
            sa.select(
                ThesisOutcomeGrade.outcome_id,
                ThesisOutcomeGrade.checkpoint,
                ThesisOutcomeGrade.correct,
                ThesisOutcomeGrade.alpha,
                ThesisOutcomeGrade.graded_at,
            )
            .join(ThesisOutcome, ThesisOutcome.id == ThesisOutcomeGrade.outcome_id)
            .where(
                ThesisOutcomeGrade.graded_at <= as_of,
                ThesisOutcome.broker == self._broker,
                ThesisOutcome.is_exit.is_(exits),
            )
        )
        if event_type is not None:
            stmt = stmt.where(ThesisOutcome.event_type == event_type)
        if company_id is not None:
            stmt = stmt.where(ThesisOutcome.company_id == company_id)
        if action is not None:
            stmt = stmt.where(ThesisOutcome.action == action)
        chosen: dict[uuid.UUID, tuple[bool, EffectiveGrade]] = {}
        for outcome_id, checkpoint, correct, alpha, graded_at in await session.execute(stmt):
            grade = EffectiveGrade(
                outcome_id=outcome_id, correct=correct, alpha=alpha, graded_at=graded_at
            )
            is_close = checkpoint == OutcomeCheckpoint.CLOSE.value
            current = chosen.get(outcome_id)
            if current is None or (is_close and not current[0]) or (
                is_close == current[0] and graded_at > current[1].graded_at
            ):
                chosen[outcome_id] = (is_close, grade)
        return [grade for _, grade in chosen.values()]

    async def calibration(
        self,
        session: AsyncSession,
        *,
        event_type: str | None,
        action: ThesisAction,
        as_of: dt.datetime,
    ) -> CalibrationBucket | None:
        if event_type is None:
            return None
        grades = await self.effective_grades(
            session, as_of=as_of, event_type=event_type, action=action
        )
        return calibrate(f"{event_type}×{action.value}", grades)

    async def company_calibration(
        self,
        session: AsyncSession,
        *,
        company_id: uuid.UUID,
        action: ThesisAction,
        as_of: dt.datetime,
    ) -> CalibrationBucket | None:
        grades = await self.effective_grades(
            session, as_of=as_of, company_id=company_id, action=action
        )
        return calibrate(f"company:{company_id}×{action.value}", grades)

    async def research_memory(
        self,
        session: AsyncSession,
        *,
        company_id: uuid.UUID,
        broker_instrument_id: uuid.UUID,
        broker_ticker: str,
        event_type: str | None,
        as_of: dt.datetime,
    ) -> ResearchMemory:
        standing = await self._standing_thesis(session, company_id, broker_instrument_id, as_of)
        position = await self._position_memory(session, broker_ticker, as_of)
        company = await self.company_calibration(
            session, company_id=company_id, action=ThesisAction.BUY, as_of=as_of
        )
        by_type: list[CalibrationRecord] = []
        for action in (ThesisAction.BUY, ThesisAction.REDUCE, ThesisAction.SELL):
            bucket = await self.calibration(
                session, event_type=event_type, action=action, as_of=as_of
            )
            if bucket is not None:
                by_type.append(_record(bucket))
        return ResearchMemory(
            standing_thesis=standing,
            position=position,
            company_record=_record(company) if company else None,
            event_type_record=tuple(by_type),
        )

    async def _standing_thesis(
        self,
        session: AsyncSession,
        company_id: uuid.UUID,
        broker_instrument_id: uuid.UUID,
        as_of: dt.datetime,
    ) -> StandingThesis | None:
        floor = as_of - dt.timedelta(days=self._settings.memory_standing_thesis_max_age_days)
        row = (
            await session.execute(
                sa.select(Thesis, ResearchRun.completed_at)
                .join(ResearchRun, ResearchRun.id == Thesis.research_run_id)
                .where(
                    ResearchRun.company_id == company_id,
                    ResearchRun.broker_instrument_id == broker_instrument_id,
                    ResearchRun.status == ResearchStatus.SUCCEEDED,
                    ResearchRun.completed_at <= as_of,
                    ResearchRun.completed_at >= floor,
                )
                .order_by(ResearchRun.completed_at.desc(), Thesis.id)
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        thesis, completed_at = row
        assert completed_at is not None
        conditions = thesis.invalidation_conditions.get("items", [])
        return StandingThesis(
            thesis_id=thesis.id,
            published_at=completed_at,
            action=thesis.action,
            confidence=thesis.confidence,
            horizon=thesis.time_horizon,
            thesis=(thesis.summary or "")[:600],
            invalidation_conditions=tuple(str(item) for item in conditions),
            age_hours=(as_of - completed_at).total_seconds() / 3600,
        )

    async def _position_memory(
        self, session: AsyncSession, broker_ticker: str, as_of: dt.datetime
    ) -> PositionMemory | None:
        position = (
            await session.execute(
                sa.select(Position)
                .where(
                    Position.broker == self._broker,
                    Position.broker_ticker == broker_ticker,
                    Position.quantity > 0,
                    Position.last_synced_at <= as_of,
                )
                .order_by(Position.last_synced_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if position is None:
            return None
        unrealised: Decimal | None = None
        if position.average_price and position.current_price:
            unrealised = (
                (position.current_price - position.average_price) / position.average_price
            ).quantize(_SIX_PLACES, ROUND_HALF_EVEN)
        return PositionMemory(
            quantity=position.quantity,
            average_price=position.average_price,
            current_price=position.current_price,
            unrealised_pct=unrealised,
            opened_at=position.initial_fill_date,
            synced_at=position.last_synced_at,
        )


def _record(bucket: CalibrationBucket) -> CalibrationRecord:
    return CalibrationRecord(
        key=bucket.key,
        samples=bucket.samples,
        correct=bucket.correct,
        hit_rate=bucket.hit_rate,
        mean_alpha=bucket.mean_alpha,
        latest_graded_at=bucket.latest_graded_at,
    )
```

Add `ResearchStatus` to the enums import.

- [ ] **Step 5: Populate memory in `ResearchService.packet()`**

In `backend/stockbrain/intelligence/research_service.py`:

1. `__init__` signature gains, after `macro_markets`: `memory: MemoryService | None = None, memory_packet_enabled: bool = False`; store `self.memory = memory` and `self.memory_packet_enabled = memory_packet_enabled`. Import `MemoryService` under `TYPE_CHECKING` to avoid a cycle (`memory.py` imports `research.py`, not `research_service.py`, so a plain import also works — prefer the plain import if `ruff` is happy).
2. In `self.config`, add `"memory": memory_packet_enabled,` — a packet with memory is a different analysis from one without, so it must fork `config_version`.
3. In `packet()`, before the `try: return ResearchPacket(` block, add:

```python
        memory: ResearchMemory | None = None
        if self.memory_packet_enabled and self.memory is not None:
            memory = await self.memory.research_memory(
                session,
                company_id=company.id,
                broker_instrument_id=instrument.id,
                broker_ticker=instrument.broker_ticker,
                event_type=event.event_type,
                as_of=as_of,
            )
```

and pass `memory=memory,` into the `ResearchPacket(...)` constructor after `previous_thesis=previous,`. Import `ResearchMemory` from `stockbrain.intelligence.research`.

Do **not** touch `previous_thesis_id` handling.

- [ ] **Step 6: Add the policy paragraph**

In `backend/stockbrain/intelligence/tradingagents_adapter.py`, append to `SYSTEM_POLICY` after the final sentence (`...do not compress it toward the middle to hedge a defensible answer.`):

```
The packet's memory section is this system's own prior state, not evidence, and
must never be cited as a source. When a standing thesis is present you must do one
of three things and say which: reaffirm it, supersede it with a stated reason
grounded in the triggering event, or return NO_ACTION because the event is already
priced into it -- a story the standing thesis was itself a reaction to is already
priced in. A position's unrealised move is the tape, not the event. Calibration
records describe how this system's past calls in the same situation performed; a
poor record is a reason for lower confidence, never a reason to invert a direction
the evidence supports.
```

- [ ] **Step 7: Run the tests**

From `backend/`:
```
grep -rn "research-v2" tests   # update any pinned version to research-v3
DATABASE_URL_TEST=<url> .venv/bin/python -m pytest tests/integration/test_research_packet_memory.py tests/integration/test_research.py tests/unit -q
```
Expected: all pass. Then the quality gate.

- [ ] **Step 8: Commit**

```bash
git add backend/stockbrain/intelligence/memory.py backend/stockbrain/intelligence/research.py backend/stockbrain/intelligence/research_service.py backend/stockbrain/intelligence/tradingagents_adapter.py backend/tests/integration/test_research_packet_memory.py backend/tests
git commit -m "feat(research): memory section in the packet, behind MEMORY_PACKET_ENABLED"
```

---

### Task 5: Calibration size modulation in the risk engine

**Files:**
- Modify: `backend/stockbrain/risk/config.py` (three fields after `min_confidence_size_factor` ~line 128; `risk_config_from_settings` ~line 236)
- Modify: `backend/stockbrain/risk/rules.py` (new function after `confidence_size_factor` ~line 1010)
- Modify: `backend/stockbrain/risk/engine.py` (~line 58)
- Modify: `backend/stockbrain/config.py` (three `risk_calibration_*` settings after `risk_min_confidence_size_factor` ~line 825; validation in `_validate_risk_limits` ~line 1117)
- Modify: `backend/stockbrain/api/settings_model.py` (three rows after `risk_min_confidence_size_factor`)
- Modify: `.env.example` (after `RISK_MIN_CONFIDENCE_SIZE_FACTOR`)
- Test: `backend/tests/unit/test_risk_calibration_rule.py`; extend the parametrised list in `backend/tests/unit/test_risk_config.py::test_changing_any_threshold_changes_the_version` with the three new fields.

**Interfaces:**
- Consumes: `CalibrationBucket`, `RiskInputs.calibration` (Task 2).
- Produces: `calibration_size_factor(inputs: RiskInputs) -> RuleResult | None` with `rule_id="calibration_size_modulation"`, `rule_version=1`; `RiskConfig.calibration_modulates_size: bool = True`, `calibration_min_samples: int = 10`, `min_calibration_size_factor: Decimal = Decimal("0.5")`.

- [ ] **Step 1: Write the failing rule tests**

Create `backend/tests/unit/test_risk_calibration_rule.py`:

```python
"""The calibration rule only ever shrinks, and only with enough evidence."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from stockbrain.enums import RuleOutcome
from stockbrain.risk.engine import RiskEngine
from stockbrain.risk.models import CalibrationBucket
from stockbrain.risk.rules import calibration_size_factor
from tests import risk_helpers as h

ENGINE = RiskEngine()
T0 = dt.datetime(2026, 9, 16, tzinfo=dt.UTC)


def bucket(samples: int, correct: int, mean_alpha: str = "-0.01") -> CalibrationBucket:
    return CalibrationBucket(
        key="REGULATORY×REDUCE",
        samples=samples,
        correct=correct,
        hit_rate=Decimal(correct) / Decimal(samples),
        mean_alpha=Decimal(mean_alpha),
        latest_graded_at=T0,
    )


def test_no_rule_when_modulation_is_disabled() -> None:
    inputs = h.inputs(
        risk_config=h.config(calibration_modulates_size=False), calibration=bucket(20, 2)
    )
    assert calibration_size_factor(inputs) is None


def test_no_rule_without_a_bucket() -> None:
    assert calibration_size_factor(h.inputs()) is None


def test_below_the_sample_floor_the_rule_is_skipped_not_applied() -> None:
    result = calibration_size_factor(h.inputs(calibration=bucket(9, 0)))
    assert result is not None
    assert result.outcome is RuleOutcome.WARN
    assert result.size_factor is None
    assert "9 of 10" in result.reason


@pytest.mark.parametrize(
    ("correct", "expected"),
    [
        (20, Decimal("1")),      # hit rate 1.0 -> no reduction
        (10, Decimal("1")),      # 0.5 -> neutral
        (5, Decimal("0.75")),    # 0.25 -> 0.75
        (0, Decimal("0.5")),     # 0 -> floor
    ],
)
def test_the_factor_is_half_plus_hit_rate_clamped_to_the_floor(
    correct: int, expected: Decimal
) -> None:
    result = calibration_size_factor(h.inputs(calibration=bucket(20, correct)))
    assert result is not None
    assert result.rule_id == "calibration_size_modulation"
    assert result.size_factor == expected
    assert result.outcome is (RuleOutcome.PASS if expected == 1 else RuleOutcome.REDUCE)
    assert "REGULATORY×REDUCE" in result.reason
    assert f"{correct}/20" in result.reason


def test_a_higher_floor_is_respected() -> None:
    result = calibration_size_factor(
        h.inputs(
            risk_config=h.config(min_calibration_size_factor=Decimal("0.8")),
            calibration=bucket(20, 0),
        )
    )
    assert result is not None
    assert result.size_factor == Decimal("0.8")


def test_the_rule_never_blocks_and_combines_with_the_confidence_factor() -> None:
    decision = ENGINE.evaluate(
        h.inputs(confidence=Decimal("0.7"), calibration=bucket(20, 0))
    )
    assert decision.allowed
    rule_ids = [rule.rule_id for rule in decision.rules]
    assert "calibration_size_modulation" in rule_ids
    assert "confidence_size_modulation" in rule_ids
    assert decision.blocks == ()
    baseline = ENGINE.evaluate(h.inputs(confidence=Decimal("0.7")))
    assert decision.sizing.quantity < baseline.sizing.quantity


def test_a_healthy_record_changes_nothing() -> None:
    with_record = ENGINE.evaluate(h.inputs(calibration=bucket(20, 15)))
    without = ENGINE.evaluate(h.inputs())
    assert with_record.sizing.quantity == without.sizing.quantity
```

`tests/risk_helpers.py::inputs` must accept `calibration: CalibrationBucket | None = None` and pass it to `RiskInputs`; add that parameter (default `None`) as part of this task.

- [ ] **Step 2: Run the tests to verify they fail**

From `backend/`: `.venv/bin/python -m pytest tests/unit/test_risk_calibration_rule.py -q`
Expected: ImportError `calibration_size_factor`.

- [ ] **Step 3: `RiskConfig` fields and settings**

In `backend/stockbrain/risk/config.py`, after `min_confidence_size_factor`:

```python
    calibration_modulates_size: bool = True
    """Whether a poor calibration record for the proposal's bucket shrinks the size."""
    calibration_min_samples: int = 10
    """Graded outcomes a bucket needs before its record is acted on at all."""
    min_calibration_size_factor: Decimal = Decimal("0.5")
    """The floor the calibration factor is clamped to. It can only ever reduce."""
```

In `risk_config_from_settings`, after `min_confidence_size_factor=...`:

```python
        calibration_modulates_size=settings.risk_calibration_modulates_size,
        calibration_min_samples=settings.risk_calibration_min_samples,
        min_calibration_size_factor=settings.risk_min_calibration_size_factor,
```

In `backend/stockbrain/config.py`, after `risk_min_confidence_size_factor`:

```python
    risk_calibration_modulates_size: bool = True
    risk_calibration_min_samples: int = 10
    risk_min_calibration_size_factor: Decimal = Decimal("0.5")
```

In `_validate_risk_limits`, add alongside its existing checks (read the method; match the `problems` shape):

```python
        if self.risk_calibration_min_samples < 3:
            problems.append(
                "RISK_CALIBRATION_MIN_SAMPLES must be at least 3 "
                f"(got {self.risk_calibration_min_samples})"
            )
        if not Decimal(0) < self.risk_min_calibration_size_factor <= Decimal(1):
            problems.append(
                "RISK_MIN_CALIBRATION_SIZE_FACTOR must be in (0, 1] "
                f"(got {self.risk_min_calibration_size_factor})"
            )
```

In `backend/stockbrain/api/settings_model.py`, after the `risk_min_confidence_size_factor` row (find it with `grep -n risk_min_confidence_size_factor`):

```python
            _s(
                "risk_calibration_modulates_size",
                "Calibration modulates size",
                RESTART,
                "Whether a poor graded record for the proposal's event-type and action "
                "bucket shrinks the size. Only ever reduces; never blocks.",
            ),
            _s(
                "risk_calibration_min_samples",
                "Calibration sample floor",
                RESTART,
                "Graded outcomes a bucket needs before its record is acted on.",
            ),
            _s(
                "risk_min_calibration_size_factor",
                "Calibration size floor",
                RESTART,
                "The smallest fraction of the deterministic maximum a poor record can "
                "scale a trade to.",
            ),
```

In `.env.example`, after `RISK_MIN_CONFIDENCE_SIZE_FACTOR=...`:

```
# Thesis-memory calibration. Once a (event type × action) bucket has this many
# graded outcomes, a hit rate below 50% scales the size by 0.5 + hit_rate, never
# below the floor. Dormant until the memory tables have data; only ever reduces.
RISK_CALIBRATION_MODULATES_SIZE=true
RISK_CALIBRATION_MIN_SAMPLES=10
RISK_MIN_CALIBRATION_SIZE_FACTOR=0.5
```

- [ ] **Step 4: Write the rule and wire it**

In `backend/stockbrain/risk/rules.py`, after `confidence_size_factor`:

```python
def calibration_size_factor(inputs: RiskInputs) -> RuleResult | None:
    """Shrink the size when this system's record in the same situation is poor.

    ``None`` when modulation is off or nothing is known.  Below the sample floor
    the rule is reported as not evaluated rather than silently neutral, so a
    reviewer can see the record was too thin to act on.  The factor is
    ``0.5 + hit_rate`` clamped to ``[floor, 1]``: a coin-flip record is neutral,
    a perfect record earns nothing, and no record can lift a cap or a block.
    """
    config = inputs.config
    bucket = inputs.calibration
    if not config.calibration_modulates_size or bucket is None:
        return None
    if bucket.samples < config.calibration_min_samples:
        return _skipped(
            "calibration_size_modulation",
            1,
            f"{bucket.key} has {bucket.samples} of {config.calibration_min_samples} "
            "graded outcomes needed",
            threshold=str(config.calibration_min_samples),
        )
    floor = config.min_calibration_size_factor
    factor = min(Decimal(1), max(floor, Decimal("0.5") + bucket.hit_rate))
    return RuleResult(
        rule_id="calibration_size_modulation",
        rule_version=1,
        outcome=RuleOutcome.REDUCE if factor < Decimal(1) else RuleOutcome.PASS,
        reason=(
            f"{bucket.key}: {bucket.correct}/{bucket.samples} correct, mean alpha "
            f"{bucket.mean_alpha:+.2%}, scales the size to {factor} of the deterministic "
            "maximum (it can only reduce, never authorise)"
        ),
        observed=str(bucket.hit_rate),
        threshold=f"[{floor}, 1]",
        size_factor=factor,
    )
```

Add `"calibration_size_factor"` to the module `__all__` (there is one near line 48 listing `"confidence_size_factor"`).

In `backend/stockbrain/risk/engine.py`, import `calibration_size_factor` next to `confidence_size_factor` and, right after the `confidence_rule` block in `evaluate`:

```python
        calibration_rule = calibration_size_factor(inputs)
        if calibration_rule is not None:
            rules.append(calibration_rule)
```

In `tests/risk_helpers.py::inputs`, add the `calibration: CalibrationBucket | None = None` parameter and `calibration=calibration,` in the `RiskInputs(...)` call; import `CalibrationBucket` from `stockbrain.risk.models`.

In `tests/unit/test_risk_config.py`, add three entries to the parametrised list of `test_changing_any_threshold_changes_the_version`: `("calibration_modulates_size", False)`, `("calibration_min_samples", 11)`, `("min_calibration_size_factor", Decimal("0.6"))`. Also confirm `test_the_defaults_are_conservative` / `test_settings_build_the_same_defaults` still pass (they compare `RiskConfig()` to `risk_config_from_settings(Settings())`).

- [ ] **Step 5: Run the tests**

From `backend/`: `.venv/bin/python -m pytest tests/unit/test_risk_calibration_rule.py tests/unit/test_risk_engine.py tests/unit/test_risk_config.py tests/unit/test_settings_model.py -q`
Expected: all pass. Then the quality gate.

- [ ] **Step 6: Commit**

```bash
git add backend/stockbrain/risk backend/stockbrain/config.py backend/stockbrain/api/settings_model.py .env.example backend/tests/unit/test_risk_calibration_rule.py backend/tests/unit/test_risk_config.py backend/tests/risk_helpers.py
git commit -m "feat(risk): calibration size modulation, dormant until a bucket has samples"
```

---

### Task 6: Feed the calibration bucket from proposals

**Files:**
- Modify: `backend/stockbrain/proposals/evaluation.py` (`EvaluationContext` ~line 115; `ProposalEvaluator.__init__` ~line 140; `evaluate` ~line 225)
- Modify: `backend/stockbrain/proposals/service.py` (`__init__` ~line 178; `_evaluator` ~line 214; the four `EvaluationContext(` sites at ~344, ~648, ~868, ~1049; a new `_event_type_for` helper)
- Test: `backend/tests/integration/test_proposal_calibration_input.py`

**Interfaces:**
- Consumes: `MemoryService.calibration` (Task 4), rule (Task 5).
- Produces: `EvaluationContext.event_type: str | None = None`; `ProposalEvaluator(..., memory: MemoryService | None = None)`; `ProposalService(..., memory: MemoryService | None = None)`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/integration/test_proposal_calibration_input.py`:

```python
"""Generation feeds the engine the bucket for the thesis's (event_type, action)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.db.models.memory import ThesisOutcome, ThesisOutcomeGrade
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.models.sources import Event
from stockbrain.db.session import Database
from stockbrain.enums import Broker, OrderSide, OrderType, PriceSource, ProposalStatus, ThesisAction, TimeHorizon
from stockbrain.intelligence.memory import MemoryService
from tests import proposal_helpers as ph
from tests.integration.test_thesis_memory_sweep import FakeBars, _settings

pytestmark = pytest.mark.integration


async def _poor_record(db: Database, *, event_type: str, samples: int) -> None:
    """``samples`` graded BUY outcomes for ``event_type``, all wrong, on other listings."""
    from stockbrain.db.models.companies import BrokerInstrument, Company
    from stockbrain.db.models.research import ResearchRun, Thesis
    from stockbrain.enums import ResearchStatus

    moment = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    async with db.transaction() as session:
        for index in range(samples):
            company_id, instrument_id = uuid.uuid4(), uuid.uuid4()
            run_id, thesis_id, event_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            ticker = f"X{index}_US_EQ"
            session.add(Company(id=company_id, name=f"X{index}", isin=f"US000000000{index}"))
            session.add(
                Event(
                    id=event_id, title=f"e{index}", title_hash=f"{index:064d}", summary="",
                    first_seen_at=moment, event_type=event_type,
                )
            )
            await session.flush()
            session.add(
                BrokerInstrument(
                    id=instrument_id, company_id=company_id, broker=Broker.TRADING212,
                    broker_ticker=ticker, market_symbol=f"X{index}", name=f"X{index}",
                    exchange="NASDAQ", currency="USD", isin=f"US000000000{index}",
                    instrument_type="STOCK", is_active=True,
                )
            )
            session.add(
                ResearchRun(
                    id=run_id, event_id=event_id, company_id=company_id,
                    broker_instrument_id=instrument_id, status=ResearchStatus.SUCCEEDED,
                    completed_at=moment,
                )
            )
            await session.flush()
            session.add(
                Thesis(
                    id=thesis_id, research_run_id=run_id, action=ThesisAction.BUY,
                    confidence=0.8, time_horizon=TimeHorizon.WEEKS, summary="s",
                )
            )
            proposal = TradeProposal(
                thesis_id=thesis_id, research_run_id=run_id, broker=Broker.TRADING212,
                broker_ticker=ticker, account_id=ph.ACCOUNT_ID, broker_environment="demo",
                side=OrderSide.BUY, order_type=OrderType.MARKET, proposed_quantity=Decimal(1),
                reference_price=Decimal(10), reference_currency="USD",
                price_source=PriceSource.ALPACA_IEX, quote_timestamp=moment, quote_age_ms=0,
                estimated_notional=Decimal(10), account_currency="USD",
                status=ProposalStatus.EXECUTED, executed_at=moment,
                expires_at=moment + dt.timedelta(days=1), research_action="BUY",
                research_confidence=0.8,
            )
            session.add(proposal)
            await session.flush()
            outcome_id = uuid.uuid4()
            session.add(
                ThesisOutcome(
                    id=outcome_id, proposal_id=proposal.id, thesis_id=thesis_id,
                    research_run_id=run_id, company_id=company_id,
                    broker_instrument_id=instrument_id, broker=Broker.TRADING212,
                    broker_ticker=ticker, event_type=event_type, action=ThesisAction.BUY,
                    horizon=TimeHorizon.WEEKS, confidence=Decimal("0.8"), entry_at=moment,
                    entry_date=moment.date(), currency="USD", benchmark_symbol="SPY",
                )
            )
            await session.flush()
            session.add(
                ThesisOutcomeGrade(
                    outcome_id=outcome_id, checkpoint="D5", trading_days=5,
                    entry_close=Decimal(10), current_close=Decimal(9),
                    benchmark_entry_close=Decimal(10), benchmark_current_close=Decimal(10),
                    instrument_return=Decimal("-0.1"), benchmark_return=Decimal(0),
                    alpha=Decimal("-0.1"), correct=False,
                    graded_at=moment + dt.timedelta(days=7),
                )
            )


def _service(db: Database) -> ph.ProposalService:  # type: ignore[name-defined]
    memory = MemoryService(db, _settings(), bars=FakeBars({}))
    return ph.service(db, memory=memory)


async def test_generation_records_the_calibration_rule_on_the_proposal(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(Event).where(Event.id == ph.EVENT_ID).values(event_type="REGULATORY")
        )
    await _poor_record(clean_tables, event_type="REGULATORY", samples=10)
    service = _service(clean_tables)
    await service.generate(ph.THESIS_ID)
    async with clean_tables.session() as session:
        proposal = (
            await session.scalars(
                sa.select(TradeProposal).where(TradeProposal.thesis_id == ph.THESIS_ID)
            )
        ).one()
    rules = {rule["rule_id"]: rule for rule in proposal.risk_rules}
    assert rules["calibration_size_modulation"]["outcome"] == "REDUCE"
    assert rules["calibration_size_modulation"]["size_factor"] == "0.5"


async def test_a_thin_record_leaves_the_size_alone(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(Event).where(Event.id == ph.EVENT_ID).values(event_type="REGULATORY")
        )
    await _poor_record(clean_tables, event_type="REGULATORY", samples=4)
    service = _service(clean_tables)
    await service.generate(ph.THESIS_ID)
    async with clean_tables.session() as session:
        proposal = (
            await session.scalars(
                sa.select(TradeProposal).where(TradeProposal.thesis_id == ph.THESIS_ID)
            )
        ).one()
    rules = {rule["rule_id"]: rule for rule in proposal.risk_rules}
    assert rules["calibration_size_modulation"]["outcome"] == "WARN"


async def test_no_memory_service_means_no_rule(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    await service.generate(ph.THESIS_ID)
    async with clean_tables.session() as session:
        proposal = (
            await session.scalars(
                sa.select(TradeProposal).where(TradeProposal.thesis_id == ph.THESIS_ID)
            )
        ).one()
    assert "calibration_size_modulation" not in {rule["rule_id"] for rule in proposal.risk_rules}
```

Before writing this test, read `tests/proposal_helpers.py::service` and `::service_with` to learn the exact way a `ProposalService` is built there and whether `generate(thesis_id)` is the method name (`grep -n "async def generate" backend/stockbrain/proposals/service.py`). Adjust `_service` and the calls to match; `ph.service` must be extended to forward a `memory=` kwarg. Also check how `risk_rules` serialises `size_factor` (`RuleResult.as_dict`) — the `"0.5"` assertion assumes a string; if it is a number, compare to `Decimal("0.5")` or `0.5` accordingly.

- [ ] **Step 2: Run the test to verify it fails**

From `backend/`: `DATABASE_URL_TEST=<url> .venv/bin/python -m pytest tests/integration/test_proposal_calibration_input.py -q`
Expected: TypeError on the `memory=` kwarg.

- [ ] **Step 3: Thread `event_type` and `memory` through**

In `backend/stockbrain/proposals/evaluation.py`:

1. `EvaluationContext` gains `event_type: str | None = None` as its last field.
2. `ProposalEvaluator.__init__` gains `memory: MemoryService | None = None` (keyword-only, after `engine`) and stores `self.memory = memory`. Import: `from stockbrain.intelligence.memory import MemoryService` — `memory.py` does not import `proposals`, so there is no cycle; if `ruff` flags an import-order cycle anyway, use `TYPE_CHECKING`.
3. In `evaluate`, before `decision = self.engine.evaluate(`:

```python
        calibration = None
        if self.memory is not None and context.event_type is not None:
            calibration = await self.memory.calibration(
                session, event_type=context.event_type, action=context.action, as_of=now
            )
```

and pass `calibration=calibration,` into `RiskInputs(...)`.

In `backend/stockbrain/proposals/service.py`:

1. `__init__` gains `memory: MemoryService | None = None` (keyword-only) and stores `self.memory = memory`.
2. `_evaluator` passes `memory=self.memory`.
3. Add a helper near `_load_candidate`:

```python
    @staticmethod
    async def _event_type_for(session: AsyncSession, event_id: uuid.UUID | None) -> str | None:
        if event_id is None:
            return None
        return await session.scalar(sa.select(Event.event_type).where(Event.id == event_id))
```

(import `Event` from `stockbrain.db.models.sources` if not already imported).

4. At the two generation sites (~344 and ~648) the candidate is in scope: build the context as

```python
                EvaluationContext(
                    candidate.action,
                    candidate.confidence,
                    fresh_identity,
                    account_id,
                    event_type=await self._event_type_for(session, candidate.run.event_id),
                ),
```

5. At the two authorization/revalidation sites (~868 and ~1049) the proposal row is in scope (`locked` / `fresh`): read the event through the run —

```python
            event_type = await session.scalar(
                sa.select(Event.event_type)
                .join(ResearchRun, ResearchRun.event_id == Event.id)
                .where(ResearchRun.id == locked.research_run_id)
            ) if locked.research_run_id else None
```

and pass `event_type=event_type` to the `EvaluationContext(...)`. For the exit-sweep generation path (the one that builds a SELL from an `ExitSignal`, ~line 700) leave `event_type` unset so `calibration` stays `None` — exits are never modulated (spec §7.2).

6. `tests/proposal_helpers.py::service` (and `service_with` if that is the builder) forward `memory: MemoryService | None = None` to `ProposalService`.

- [ ] **Step 4: Run the tests**

From `backend/`: `DATABASE_URL_TEST=<url> .venv/bin/python -m pytest tests/integration/test_proposal_calibration_input.py tests/integration/test_proposals*.py tests/integration/test_exit_sweep.py -q`
Expected: all pass. Then the quality gate.

- [ ] **Step 5: Commit**

```bash
git add backend/stockbrain/proposals backend/tests/proposal_helpers.py backend/tests/integration/test_proposal_calibration_input.py
git commit -m "feat(proposals): feed the engine the calibration bucket for the thesis's event type"
```

---

### Task 7: Wiring, scheduler task and read-only endpoints

**Files:**
- Modify: `backend/stockbrain/services.py` (field ~line 176; construction after `self.risk_config` ~line 205; pass to `ProposalService` ~line 226 and `ResearchService` ~line 550; scheduler after `volatility_refresh` ~line 862; a `_memory_tick` method next to `_volatility_refresh` ~line 1257)
- Create: `backend/stockbrain/api/routes/memory.py`
- Modify: `backend/stockbrain/main.py` (~line 405, `include_router`)
- Test: `backend/tests/integration/test_memory_api.py`; extend `backend/tests/unit/test_services_wiring.py` if such a file exists (`ls backend/tests/unit | grep -i wiring`), else skip.

**Interfaces:**
- Consumes: everything above.
- Produces: `ServiceContainer.memory: MemoryService | None`; scheduler task `thesis_memory_grade`; `GET /api/v1/memory/calibration`, `GET /api/v1/memory/outcomes?limit=`.

- [ ] **Step 1: Write the failing API test**

Create `backend/tests/integration/test_memory_api.py`:

```python
"""Read-only memory endpoints: every bucket, and outcomes with their grades."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from asgi_lifespan import LifespanManager

from stockbrain.config import Settings
from stockbrain.db.session import Database
from stockbrain.enums import ThesisAction
from stockbrain.main import create_app
from tests.integration.test_research import seed
from tests.integration.test_research_packet_memory import _graded_outcome

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(migrated_database: str, clean_tables: Database) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        app_env="test",
        web_auth_enabled=False,
        log_level="WARNING",
        database_url=migrated_database,
        stockbrain_secret_key="test-key",
        memory_grade_enabled=True,
    )
    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def test_calibration_lists_every_bucket_with_counts(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    value = await seed(clean_tables)
    when = dt.datetime(2026, 9, 10, tzinfo=dt.UTC)
    await _graded_outcome(
        clean_tables, value, action=ThesisAction.BUY, event_type="EARNINGS",
        correct=True, alpha="0.05", graded_at=when,
    )
    await _graded_outcome(
        clean_tables, value, action=ThesisAction.BUY, event_type="EARNINGS",
        correct=False, alpha="-0.01", graded_at=when,
    )
    response = await client.get("/api/v1/memory/calibration")
    assert response.status_code == 200
    body = response.json()
    assert body["graded_outcomes"] == 2
    assert body["pending_outcomes"] == 2
    buckets = {row["key"]: row for row in body["buckets"]}
    assert buckets["EARNINGS×BUY"]["samples"] == 2
    assert buckets["EARNINGS×BUY"]["correct"] == 1
    assert Decimal(buckets["EARNINGS×BUY"]["hit_rate"]) == Decimal("0.5")
    company_keys = [key for key in buckets if key.startswith("company:")]
    assert len(company_keys) == 1


async def test_outcomes_list_newest_first_with_grades(
    client: httpx.AsyncClient, clean_tables: Database
) -> None:
    value = await seed(clean_tables)
    await _graded_outcome(
        clean_tables, value, action=ThesisAction.BUY, event_type="EARNINGS",
        correct=True, alpha="0.05", graded_at=dt.datetime(2026, 9, 10, tzinfo=dt.UTC),
    )
    response = await client.get("/api/v1/memory/outcomes", params={"limit": 10})
    assert response.status_code == 200
    (row,) = response.json()["outcomes"]
    assert row["broker_ticker"] == value.company.broker_ticker
    assert row["action"] == "BUY"
    assert row["status"] == "PENDING"
    assert [grade["checkpoint"] for grade in row["grades"]] == ["D5"]


async def test_the_limit_is_bounded(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/memory/outcomes", params={"limit": 10000})
    assert response.status_code == 422
```

Match the `client` fixture's `Settings(...)` kwargs to the one in `tests/integration/test_operator_api.py` (it may need `telegram_bot_token`).

- [ ] **Step 2: Run the test to verify it fails**

From `backend/`: `DATABASE_URL_TEST=<url> .venv/bin/python -m pytest tests/integration/test_memory_api.py -q`
Expected: 404s.

- [ ] **Step 3: Wire the service**

In `backend/stockbrain/services.py`:

1. Field, next to `volatility`: `memory: MemoryService | None = field(default=None, init=False)`; import `MemoryService` from `stockbrain.intelligence.memory`.
2. In `__post_init__`, immediately after `self.risk_config = risk_config_from_settings(self.settings)`:

```python
        # Constructed unconditionally: the read side (packet memory, calibration)
        # is a query and costs nothing; only the grading sweep is gated.
        self.memory = MemoryService(self.database, self.settings, bars=YahooDailyBars())
```

3. Pass `memory=self.memory` to `ProposalService(...)` and `memory=self.memory, memory_packet_enabled=settings.memory_packet_enabled` to `ResearchService(...)`.
4. Scheduler, after the `volatility_refresh` block:

```python
        if self.memory is not None and self.settings.memory_grade_enabled:
            scheduler.add(
                ScheduledTask(
                    name="thesis_memory_grade",
                    interval_seconds=self.settings.memory_grade_interval_seconds,
                    run=self._memory_tick,
                    initial_delay_seconds=180.0,
                    jitter_ratio=0.1,
                )
            )
```

5. Next to `_volatility_refresh`:

```python
    async def _memory_tick(self) -> None:
        if self.memory is not None:
            await self.memory.tick()
```

- [ ] **Step 4: Write the routes**

Create `backend/stockbrain/api/routes/memory.py`:

```python
"""Read-only thesis memory: calibration buckets and graded outcomes.

Alpha here ignores FX (an instrument's ratio in its own currency against a USD
benchmark) and uses one benchmark for every venue -- accepted limitations of a
hit-rate signal, spelled out in the design's §9.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import asdict
from decimal import Decimal

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from stockbrain.api.dependencies import DbSession, ServicesDep
from stockbrain.db.base import utcnow
from stockbrain.db.models.memory import ThesisOutcome, ThesisOutcomeGrade
from stockbrain.enums import OutcomeStatus, ThesisAction
from stockbrain.intelligence.memory import calibrate

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])


class BucketView(BaseModel):
    key: str
    samples: int
    correct: int
    hit_rate: Decimal
    mean_alpha: Decimal
    latest_graded_at: dt.datetime


class CalibrationView(BaseModel):
    as_of: dt.datetime
    pending_outcomes: int
    graded_outcomes: int
    buckets: list[BucketView]


class GradeView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    checkpoint: str
    trading_days: int
    instrument_return: Decimal
    benchmark_return: Decimal
    alpha: Decimal
    correct: bool
    graded_at: dt.datetime


class OutcomeView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    broker_ticker: str
    event_type: str | None
    action: ThesisAction
    horizon: str
    confidence: Decimal
    is_exit: bool
    exit_rule_id: str | None
    entry_at: dt.datetime
    status: OutcomeStatus
    closed_at: dt.datetime | None
    close_reason: str | None
    grades: list[GradeView]


class OutcomesView(BaseModel):
    outcomes: list[OutcomeView]


@router.get("/calibration", response_model=CalibrationView)
async def calibration(session: DbSession, services: ServicesDep) -> CalibrationView:
    if services is None or services.memory is None:
        raise HTTPException(status_code=503, detail="memory is not configured")
    now = utcnow()
    memory = services.memory
    pending = await session.scalar(
        sa.select(sa.func.count())
        .select_from(ThesisOutcome)
        .where(ThesisOutcome.status == OutcomeStatus.PENDING)
    )
    graded = await session.scalar(
        sa.select(sa.func.count(sa.distinct(ThesisOutcomeGrade.outcome_id)))
    )
    buckets: list[BucketView] = []
    keys = (
        await session.execute(
            sa.select(ThesisOutcome.event_type, ThesisOutcome.action)
            .where(ThesisOutcome.is_exit.is_(False), ThesisOutcome.event_type.is_not(None))
            .distinct()
        )
    ).all()
    for event_type, action in keys:
        bucket = await memory.calibration(session, event_type=event_type, action=action, as_of=now)
        if bucket is not None:
            buckets.append(BucketView(**asdict(bucket)))
    companies = (
        await session.execute(
            sa.select(ThesisOutcome.company_id)
            .where(ThesisOutcome.is_exit.is_(False), ThesisOutcome.action == ThesisAction.BUY)
            .distinct()
        )
    ).scalars()
    for company_id in companies:
        bucket = await memory.company_calibration(
            session, company_id=company_id, action=ThesisAction.BUY, as_of=now
        )
        if bucket is not None:
            buckets.append(BucketView(**asdict(bucket)))
    exit_rules = (
        await session.scalars(
            sa.select(ThesisOutcome.exit_rule_id)
            .where(ThesisOutcome.is_exit.is_(True), ThesisOutcome.exit_rule_id.is_not(None))
            .distinct()
        )
    ).all()
    for rule_id in exit_rules:
        grades = await memory.effective_grades(session, as_of=now, exits=True)
        rows = [
            grade
            for grade in grades
            if grade.outcome_id
            in set(
                await session.scalars(
                    sa.select(ThesisOutcome.id).where(ThesisOutcome.exit_rule_id == rule_id)
                )
            )
        ]
        bucket = calibrate(f"exits×{rule_id}", rows)
        if bucket is not None:
            buckets.append(BucketView(**asdict(bucket)))
    return CalibrationView(
        as_of=now,
        pending_outcomes=pending or 0,
        graded_outcomes=graded or 0,
        buckets=sorted(buckets, key=lambda row: row.key),
    )


@router.get("/outcomes", response_model=OutcomesView)
async def outcomes(
    session: DbSession, limit: int = Query(default=50, ge=1, le=500)
) -> OutcomesView:
    rows = (
        await session.scalars(
            sa.select(ThesisOutcome)
            .order_by(ThesisOutcome.entry_at.desc())
            .limit(limit)
            .options(sa.orm.selectinload(ThesisOutcome.grades))
        )
    ).all()
    return OutcomesView(
        outcomes=[
            OutcomeView(
                id=row.id,
                broker_ticker=row.broker_ticker,
                event_type=row.event_type,
                action=row.action,
                horizon=row.horizon.value,
                confidence=row.confidence,
                is_exit=row.is_exit,
                exit_rule_id=row.exit_rule_id,
                entry_at=row.entry_at,
                status=row.status,
                closed_at=row.closed_at,
                close_reason=row.close_reason,
                grades=[GradeView.model_validate(grade) for grade in row.grades],
            )
            for row in rows
        ]
    )
```

`sa.orm.selectinload` needs `from sqlalchemy.orm import selectinload`; use that import. Session-authentication is applied app-wide by the existing middleware (the `research` router declares no per-route dependency); confirm with `grep -n "auth" backend/stockbrain/api/routes/research.py` and mirror it.

In `backend/stockbrain/main.py`, import `memory as memory_routes` beside the other route modules and add `app.include_router(memory_routes.router)` after `research_routes`.

- [ ] **Step 5: Run the tests**

From `backend/`: `DATABASE_URL_TEST=<url> .venv/bin/python -m pytest tests/integration/test_memory_api.py tests/integration/test_health_endpoints.py tests/integration/test_operator_api.py -q`
Expected: all pass. Then the quality gate.

- [ ] **Step 6: Full suite and docs**

From the repo root: `make test`. Expected: 2182 + every new test passed, 24 deselected, 0 failures.

Add a short subsection to `docs/operations.md` under the volatility-refresh material (find it with `grep -n "VOLATILITY_REFRESH" docs/operations.md`):

```markdown
### Thesis memory

`MEMORY_GRADE_ENABLED` (default on) runs a daily sweep that records every
executed thesis-backed trade and grades it against `MEMORY_BENCHMARK_SYMBOL`
from Yahoo daily closes at horizon-relative checkpoints (5/20/60 trading days)
and on close. It writes only `thesis_outcomes` and `thesis_outcome_grades` and
makes no LLM calls. `GET /api/v1/memory/calibration` shows the hit rate and
mean alpha per event type × action, per company, and per exit rule.

`MEMORY_PACKET_ENABLED` (default off) adds the standing thesis, the live
position and the calibration record to every research packet. This is the
switch that changes what research sees; turn it on once the grades have data,
and compare outcomes by `research_runs.prompt_version` (v2 before, v3 after).
Turning it on or off changes the research `config_version`, which cancels
pending runs at restart by design.

`RISK_CALIBRATION_MODULATES_SIZE` (default on) lets a bucket with at least
`RISK_CALIBRATION_MIN_SAMPLES` graded outcomes and a hit rate under 50% scale
the size by `0.5 + hit_rate`, never below `RISK_MIN_CALIBRATION_SIZE_FACTOR`.
It never blocks and never applies to exit-sweep sells.
```

- [ ] **Step 7: Commit**

```bash
git add backend/stockbrain/services.py backend/stockbrain/api/routes/memory.py backend/stockbrain/main.py backend/tests/integration/test_memory_api.py docs/operations.md
git commit -m "feat(memory): wire the grading sweep and read-only calibration endpoints"
```

---

## Plan self-review

**Spec coverage.** §4 tables → Task 1. §5.1 record → Task 3. §5.2 checkpoints → Tasks 2, 3. §5.3 correctness → Task 2. §5.4 close detection → Task 3 (`_grade_one`). §5.5 discipline → Task 3 (`_MIN_REFRESH_INTERVAL`, one fetch per symbol, `ProviderError` isolation). §6 calibration → Tasks 2, 4. §7.1 packet + policy + no supersession → Task 4. §7.2 rule and evaluator → Tasks 5, 6. §7.3 endpoint → Task 7. §8 settings → Tasks 3, 5. §10 tests → each task. §11 must-nots → Global Constraints.

**Deliberate deviation from the spec:** `thesis_outcomes.last_attempt_at` (Task 1) is added as the per-outcome rate-discipline clock; spec §5.5 requires the behaviour without naming a column.

**Type consistency.** `CalibrationBucket` (risk.models) ↔ `CalibrationRecord` (research) converted by `_record` (Task 4). `EffectiveGrade` defined in Task 2, consumed in Tasks 4 and 7. `MemoryService.calibration(session, *, event_type, action, as_of)` is the signature used by both the evaluator (Task 6) and the endpoint (Task 7). `EvaluationContext.event_type` is keyword-only by position (last field with a default) at every call site.
