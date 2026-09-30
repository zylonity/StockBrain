"""Persist a fixed catalyst expectation per news-driven BUY order.

Revision ID: b20e45a0d933
Revises: e93b7c14a6d2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b20e45a0d933"
down_revision: str | None = "e93b7c14a6d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "news_catalyst_deadlines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("broker_order_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("broker_ticker", sa.Text(), nullable=False),
        sa.Column("market_symbol", sa.Text(), nullable=False),
        sa.Column("entry_at", sa.DateTime(timezone=True)),
        sa.Column("entry_time_basis", sa.Text(), nullable=False),
        sa.Column("expected_threshold", sa.Numeric(12, 8)),
        sa.Column("supporting_sample_size", sa.Integer(), nullable=False),
        sa.Column("response_count", sa.Integer(), nullable=False),
        sa.Column("nonresponse_count", sa.Integer(), nullable=False),
        sa.Column("deadline_sessions_after_entry", sa.Integer()),
        sa.Column("buffer_sessions", sa.Integer(), nullable=False),
        sa.Column("measurement_status", sa.Text(), nullable=False),
        sa.Column(
            "evidence_snapshot",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("reassessment_requested_at", sa.DateTime(timezone=True)),
        sa.Column("reassessment_run_id", sa.Uuid()),
        sa.Column("observed_market_relative_move", sa.Numeric(12, 8)),
        sa.Column("review_reason", sa.Text()),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_catalyst_deadlines")),
        sa.ForeignKeyConstraint(["broker_order_id"], ["broker_orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["proposal_id"], ["trade_proposals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reassessment_run_id"], ["research_runs.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("broker_order_id", name="uq_news_catalyst_deadlines_broker_order_id"),
        sa.CheckConstraint("supporting_sample_size >= 0", name="news_catalyst_nonnegative_sample"),
        sa.CheckConstraint("response_count >= 0", name="news_catalyst_nonnegative_response"),
        sa.CheckConstraint("nonresponse_count >= 0", name="news_catalyst_nonnegative_nonresponse"),
        sa.CheckConstraint("buffer_sessions >= 0", name="news_catalyst_nonnegative_buffer"),
        sa.CheckConstraint(
            "deadline_sessions_after_entry IS NULL OR deadline_sessions_after_entry >= 0",
            name="news_catalyst_nonnegative_deadline",
        ),
    )
    op.create_index(
        "ix_news_catalyst_pending", "news_catalyst_deadlines", ["reassessment_requested_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_news_catalyst_pending", table_name="news_catalyst_deadlines")
    op.drop_table("news_catalyst_deadlines")
