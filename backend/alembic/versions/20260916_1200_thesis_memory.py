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
                "BUY",
                "HOLD",
                "REDUCE",
                "SELL",
                "NO_ACTION",
                name="thesis_action",
                create_type=False,
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
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_thesis_outcomes")),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["trade_proposals.id"],
            ondelete="CASCADE",
            name=op.f("fk_thesis_outcomes_proposal_id_trade_proposals"),
        ),
        sa.ForeignKeyConstraint(
            ["thesis_id"],
            ["theses.id"],
            ondelete="CASCADE",
            name=op.f("fk_thesis_outcomes_thesis_id_theses"),
        ),
        sa.ForeignKeyConstraint(
            ["research_run_id"],
            ["research_runs.id"],
            ondelete="CASCADE",
            name=op.f("fk_thesis_outcomes_research_run_id_research_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
            name=op.f("fk_thesis_outcomes_company_id_companies"),
        ),
        sa.ForeignKeyConstraint(
            ["broker_instrument_id"],
            ["broker_instruments.id"],
            ondelete="CASCADE",
            name=op.f("fk_thesis_outcomes_broker_instrument_id_broker_instruments"),
        ),
        sa.UniqueConstraint("proposal_id", name=op.f("uq_thesis_outcomes_proposal_id")),
    )
    op.create_index("ix_thesis_outcomes_status", "thesis_outcomes", ["status"])
    op.create_index(
        "ix_thesis_outcomes_company_action", "thesis_outcomes", ["company_id", "action"]
    )
    op.create_index(
        "ix_thesis_outcomes_event_type_action", "thesis_outcomes", ["event_type", "action"]
    )
    op.create_index(
        "ix_thesis_outcomes_broker_ticker", "thesis_outcomes", ["broker", "broker_ticker"]
    )

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
            ["outcome_id"],
            ["thesis_outcomes.id"],
            ondelete="CASCADE",
            name=op.f("fk_thesis_outcome_grades_outcome_id_thesis_outcomes"),
        ),
        sa.UniqueConstraint("outcome_id", "checkpoint", name="uq_thesis_outcome_grades_checkpoint"),
    )
    op.create_index("ix_thesis_outcome_grades_graded_at", "thesis_outcome_grades", ["graded_at"])


def downgrade() -> None:
    op.drop_table("thesis_outcome_grades")
    op.drop_table("thesis_outcomes")
    sa.Enum(name="outcome_status").drop(op.get_bind(), checkfirst=True)
