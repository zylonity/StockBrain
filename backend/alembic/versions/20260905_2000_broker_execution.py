"""Broker execution: attempt snapshots, reconciliation state, environment binding.

Revision ID: 5f2a7c93e410
Revises: 8c41d0f7ab92

Additive to ``execution_attempts`` and ``broker_orders``, plus the constraints
that make Phase 8's safety claims database facts rather than service-layer
intentions.  Three are worth stating because they are the ones that would break
against a populated table:

1. **The composite foreign key** ``(proposal_id, broker_environment)`` into
   ``trade_proposals (id, broker_environment)`` is what makes "an execution
   attempt always runs in its proposal's environment" impossible to violate --
   a worker started under a different ``T212_ENV`` cannot write a row claiming
   otherwise.  It needs a unique constraint on the parent side, which is added
   first, and it needs existing rows to already agree: the upgrade backfills
   ``execution_attempts.broker_environment`` from the parent before adding the
   key, and deletes nothing.

2. **``ck_execution_attempts_ambiguous_matches_outcome``** pins ``ambiguous`` to
   ``outcome = 'AMBIGUOUS'``.  Legacy rows could in principle disagree, so the
   upgrade normalises the flag from the enum -- the enum is the authority --
   before constraining.

3. **The partial index on unresolved attempts** is the predicate the
   reconciliation sweep actually runs, so it is created with the same
   ``WHERE`` clause rather than a broader one.

No application code is imported here.  The status tuples are frozen literals so
a later phase adding a status cannot retroactively change what this migration
did.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5f2a7c93e410"
down_revision: str | None = "8c41d0f7ab92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen copy of the outcomes that mean "still unresolved", as they stand at
#: this revision.  Deliberately a literal: the sweep's predicate and this index
#: must not drift apart because a later phase added an enum member.
_UNRESOLVED_OUTCOMES: tuple[str, ...] = ("PENDING", "AMBIGUOUS")

_UNRESOLVED_SQL = ", ".join(f"'{value}'" for value in _UNRESOLVED_OUTCOMES)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # execution_attempts: the immutable snapshot and reconciliation state
    # ------------------------------------------------------------------
    op.add_column(
        "execution_attempts",
        sa.Column(
            "execution_snapshot",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "execution_attempts", sa.Column("preflight_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("execution_attempts", sa.Column("error_category", sa.Text(), nullable=True))
    op.add_column(
        "execution_attempts", sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "execution_attempts", sa.Column("reconciliation_result", sa.Text(), nullable=True)
    )
    op.add_column(
        "execution_attempts",
        sa.Column("reconciliation_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "execution_attempts",
        sa.Column(
            "reconciliation_detail",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    op.create_index(
        "ix_execution_attempts_unresolved",
        "execution_attempts",
        ["sent_at"],
        unique=False,
        postgresql_where=sa.text(f"outcome IN ({_UNRESOLVED_SQL})"),
    )
    op.create_index(
        "ix_execution_attempts_broker_order_id",
        "execution_attempts",
        ["broker_order_id"],
        unique=False,
    )

    # ------------------------------------------------------------------
    # broker_orders: which environment, and who created the order
    # ------------------------------------------------------------------
    op.add_column("broker_orders", sa.Column("broker_environment", sa.Text(), nullable=True))
    op.add_column("broker_orders", sa.Column("initiated_from", sa.Text(), nullable=True))

    # ------------------------------------------------------------------
    # Normalise before constraining
    # ------------------------------------------------------------------
    # The enum is the authority; the boolean is a convenience spelling of it.
    op.execute(
        sa.text(
            "UPDATE execution_attempts SET ambiguous = (outcome = 'AMBIGUOUS') "
            "WHERE ambiguous <> (outcome = 'AMBIGUOUS')"
        )
    )
    # An attempt inherits its environment from its proposal. Any pre-existing row
    # that disagreed was wrong; the parent is the source of truth.
    op.execute(
        sa.text(
            "UPDATE execution_attempts a SET broker_environment = p.broker_environment "
            "FROM trade_proposals p "
            "WHERE a.proposal_id = p.id AND a.broker_environment <> p.broker_environment"
        )
    )

    op.create_check_constraint(
        "ambiguous_matches_outcome",
        "execution_attempts",
        "ambiguous = (outcome = 'AMBIGUOUS')",
    )
    op.create_check_constraint(
        "broker_order_requires_send",
        "execution_attempts",
        "broker_order_id IS NULL OR sent_to_broker",
    )
    op.create_check_constraint(
        "broker_outcome_requires_send",
        "execution_attempts",
        "outcome NOT IN ('SUBMITTED', 'REJECTED_BY_BROKER') OR sent_to_broker",
    )

    # ------------------------------------------------------------------
    # Environment isolation, as a database guarantee
    # ------------------------------------------------------------------
    op.create_unique_constraint(
        "uq_trade_proposals_id_environment", "trade_proposals", ["id", "broker_environment"]
    )
    op.create_foreign_key(
        "fk_execution_attempts_proposal_environment",
        "execution_attempts",
        "trade_proposals",
        ["proposal_id", "broker_environment"],
        ["id", "broker_environment"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_execution_attempts_proposal_environment", "execution_attempts", type_="foreignkey"
    )
    op.drop_constraint("uq_trade_proposals_id_environment", "trade_proposals", type_="unique")

    # Bare names, not the rendered ones: the metadata naming convention
    # (`ck_%(table_name)s_%(constraint_name)s`) is applied by `drop_constraint`
    # too, so passing an already-prefixed name gets it prefixed twice -- and
    # then truncated to PostgreSQL's 63-character limit with a hash suffix,
    # which is Phase 6's bug #13 wearing a different hat.
    op.drop_constraint("broker_outcome_requires_send", "execution_attempts", type_="check")
    op.drop_constraint("broker_order_requires_send", "execution_attempts", type_="check")
    op.drop_constraint("ambiguous_matches_outcome", "execution_attempts", type_="check")

    op.drop_column("broker_orders", "initiated_from")
    op.drop_column("broker_orders", "broker_environment")

    op.drop_index("ix_execution_attempts_broker_order_id", table_name="execution_attempts")
    op.drop_index(
        "ix_execution_attempts_unresolved",
        table_name="execution_attempts",
        postgresql_where=sa.text(f"outcome IN ({_UNRESOLVED_SQL})"),
    )

    op.drop_column("execution_attempts", "reconciliation_detail")
    op.drop_column("execution_attempts", "reconciliation_attempts")
    op.drop_column("execution_attempts", "reconciliation_result")
    op.drop_column("execution_attempts", "reconciled_at")
    op.drop_column("execution_attempts", "error_category")
    op.drop_column("execution_attempts", "preflight_at")
    op.drop_column("execution_attempts", "execution_snapshot")
