"""Deterministic risk, proposal provenance and account state.

Revision ID: 36f53456be8a
Revises: 5a180cf497b2

Follows the project's migration convention: **add nullable, backfill, then
constrain.**  Three details are worth stating because they are the ones that
break against a populated table:

1. ``proposal_status`` gains ``INVALIDATED``.  PostgreSQL will not let a new
   enum value be *used* in the same transaction that added it, and the check
   constraint below compares against it, so the ``ALTER TYPE`` runs in an
   explicit autocommit block first.
2. A legacy ``APPROVED`` proposal predates the authorization-provenance columns
   and would violate the new check the moment it is added.  Provenance is
   therefore reconstructed from the existing ``approved_channel``, and rows with
   nothing to reconstruct from are marked ``legacy:unknown`` rather than
   attributed to a person who never clicked anything.
3. The downgrade cannot drop an enum value, so it rebuilds the type.  Rows
   sitting in ``INVALIDATED`` are moved to ``CANCELLED`` first -- the nearest
   truthful terminal state -- because otherwise the cast fails and the
   downgrade is not actually reversible.

No application code is imported here.  Normalisation logic is inlined and
frozen, so a later change to the risk engine cannot retroactively alter what
this migration did.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "36f53456be8a"
down_revision: str | None = "5a180cf497b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Native enum types this migration creates. Alembic does not emit DROP TYPE,
#: so the downgrade removes them explicitly or a re-upgrade fails with
#: "type already exists".
NEW_ENUM_TYPES: tuple[str, ...] = ("risk_outcome", "execution_policy", "authorization_source")

#: Frozen copy of the proposal statuses that occupy an instrument, as they stand
#: at this revision. Deliberately a literal rather than an import from
#: ``stockbrain.proposals.state_machine``: a later phase adding a status must
#: not silently change the predicate of an index this migration created.
ACTIVE_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "READY",
    "NOTIFIED",
    "APPROVAL_PENDING",
    "APPROVED",
    "EXECUTING",
    "EXECUTION_AMBIGUOUS",
)

_ACTIVE_SQL = ", ".join(f"'{status}'" for status in ACTIVE_STATUSES)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. The new proposal status, committed before anything compares to it.
    # ------------------------------------------------------------------
    with op.get_context().autocommit_block():
        op.execute(sa.text("ALTER TYPE proposal_status ADD VALUE IF NOT EXISTS 'INVALIDATED'"))

    connection = op.get_bind()
    risk_outcome = postgresql.ENUM(
        "ALLOW", "REDUCE_SIZE", "BLOCK", name="risk_outcome", create_type=False
    )
    execution_policy = postgresql.ENUM(
        "MANUAL", "AUTOMATIC", name="execution_policy", create_type=False
    )
    authorization_source = postgresql.ENUM(
        "HUMAN_WEB",
        "HUMAN_TELEGRAM",
        "SYSTEM_AUTOMATIC",
        name="authorization_source",
        create_type=False,
    )
    for enum_type in (risk_outcome, execution_policy, authorization_source):
        enum_type.create(connection, checkfirst=True)

    # ------------------------------------------------------------------
    # 2. Durable record of every risk evaluation, allowed or blocked.
    # ------------------------------------------------------------------
    op.create_table(
        "risk_evaluations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("thesis_id", sa.Uuid(), nullable=True),
        sa.Column("proposal_id", sa.Uuid(), nullable=True),
        sa.Column("broker_instrument_id", sa.Uuid(), nullable=True),
        sa.Column(
            "broker",
            postgresql.ENUM("TRADING212", name="broker", create_type=False),
            nullable=False,
        ),
        sa.Column("broker_ticker", sa.Text(), nullable=True),
        sa.Column("outcome", risk_outcome, nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=True),
        sa.Column(
            "rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["broker_instrument_id"],
            ["broker_instruments.id"],
            name=op.f("fk_risk_evaluations_broker_instrument_id_broker_instruments"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["trade_proposals.id"],
            name=op.f("fk_risk_evaluations_proposal_id_trade_proposals"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["thesis_id"],
            ["theses.id"],
            name=op.f("fk_risk_evaluations_thesis_id_theses"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_evaluations")),
    )
    op.create_index(
        "ix_risk_evaluations_created_at", "risk_evaluations", ["created_at"], unique=False
    )
    op.create_index(
        "ix_risk_evaluations_proposal_id", "risk_evaluations", ["proposal_id"], unique=False
    )
    op.create_index(
        "ix_risk_evaluations_thesis_id", "risk_evaluations", ["thesis_id"], unique=False
    )

    # ------------------------------------------------------------------
    # 3. Account snapshots gain the fields sizing actually needs.
    # ------------------------------------------------------------------
    op.add_column(
        "portfolio_snapshots",
        sa.Column("cash_reserved", sa.Numeric(precision=24, scale=4), nullable=True),
    )
    op.add_column(
        "portfolio_snapshots",
        sa.Column("cash_in_pies", sa.Numeric(precision=24, scale=4), nullable=True),
    )
    op.add_column("portfolio_snapshots", sa.Column("broker_environment", sa.Text(), nullable=True))

    # ------------------------------------------------------------------
    # 4. Proposals record the exact values the decision was made on.
    # ------------------------------------------------------------------
    for column in (
        sa.Column("risk_outcome", risk_outcome, nullable=True),
        sa.Column("risk_policy_version", sa.String(length=64), nullable=True),
        sa.Column(
            "risk_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("quote_provider", sa.Text(), nullable=True),
        sa.Column("quote_feed", sa.Text(), nullable=True),
        sa.Column("quote_bid", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("quote_ask", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("quote_mid", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("quote_spread", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("quote_spread_bps", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("quote_spread_status", sa.Text(), nullable=True),
        sa.Column("market_session", sa.Text(), nullable=True),
        sa.Column("market_session_source", sa.Text(), nullable=True),
        sa.Column("research_confidence", sa.Float(), nullable=True),
        sa.Column("research_action", sa.Text(), nullable=True),
        sa.Column("research_run_id", sa.Uuid(), nullable=True),
        sa.Column("execution_policy", execution_policy, server_default="MANUAL", nullable=False),
        sa.Column("authorization_source", authorization_source, nullable=True),
        sa.Column(
            "authorization_policy_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("dedupe_key", sa.String(length=64), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidation_reason", sa.Text(), nullable=True),
        sa.Column(
            "sizing_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("max_quantity", sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column("max_notional", sa.Numeric(precision=24, scale=4), nullable=True),
    ):
        op.add_column("trade_proposals", column)

    # ------------------------------------------------------------------
    # 5. Backfill authorization provenance for pre-existing APPROVED rows.
    #
    # These predate the columns, so the constraint added below would reject
    # them. The channel is the only evidence available; where there is none,
    # the row is marked legacy rather than attributed to a person.
    # ------------------------------------------------------------------
    op.execute(
        sa.text(
            """
            UPDATE trade_proposals
               SET authorization_source = CASE approved_channel
                       WHEN 'TELEGRAM' THEN 'HUMAN_TELEGRAM'::authorization_source
                       ELSE 'HUMAN_WEB'::authorization_source
                   END
             WHERE status = 'APPROVED'
               AND authorization_source IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE trade_proposals
               SET approved_by = COALESCE(approved_by, 'legacy:unknown'),
                   approved_at = COALESCE(approved_at, updated_at, created_at, now())
             WHERE status = 'APPROVED'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE trade_proposals
               SET risk_policy_version = COALESCE(
                       risk_policy_version, NULLIF(risk_snapshot_hash, '')
                   )
             WHERE risk_policy_version IS NULL
            """
        )
    )

    # ------------------------------------------------------------------
    # 6. Constrain.
    # ------------------------------------------------------------------
    op.create_index("ix_trade_proposals_thesis_id", "trade_proposals", ["thesis_id"], unique=False)
    op.create_unique_constraint(
        op.f("uq_trade_proposals_dedupe_key"), "trade_proposals", ["dedupe_key"]
    )
    op.create_index(
        "uq_trade_proposals_active_thesis",
        "trade_proposals",
        ["thesis_id"],
        unique=True,
        postgresql_where=sa.text(f"thesis_id IS NOT NULL AND status IN ({_ACTIVE_SQL})"),
    )
    op.create_foreign_key(
        op.f("fk_trade_proposals_research_run_id_research_runs"),
        "trade_proposals",
        "research_runs",
        ["research_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "approved_requires_authorization_provenance",
        "trade_proposals",
        "status <> 'APPROVED' OR (authorization_source IS NOT NULL "
        "AND approved_at IS NOT NULL AND approved_by IS NOT NULL)",
    )
    op.create_check_constraint(
        "system_auth_requires_automatic_policy",
        "trade_proposals",
        "authorization_source <> 'SYSTEM_AUTOMATIC' OR execution_policy = 'AUTOMATIC'",
    )
    op.create_check_constraint(
        "invalidated_requires_status",
        "trade_proposals",
        "invalidated_at IS NULL OR status = 'INVALIDATED'",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_trade_proposals_invalidated_requires_status"), "trade_proposals", type_="check"
    )
    op.drop_constraint(
        op.f("ck_trade_proposals_system_auth_requires_automatic_policy"),
        "trade_proposals",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_trade_proposals_approved_requires_authorization_provenance"),
        "trade_proposals",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_trade_proposals_research_run_id_research_runs"),
        "trade_proposals",
        type_="foreignkey",
    )
    op.drop_index(
        "uq_trade_proposals_active_thesis",
        table_name="trade_proposals",
        postgresql_where=sa.text(f"thesis_id IS NOT NULL AND status IN ({_ACTIVE_SQL})"),
    )
    op.drop_constraint(op.f("uq_trade_proposals_dedupe_key"), "trade_proposals", type_="unique")
    op.drop_index("ix_trade_proposals_thesis_id", table_name="trade_proposals")

    for name in (
        "max_notional",
        "max_quantity",
        "sizing_reasons",
        "invalidation_reason",
        "invalidated_at",
        "dedupe_key",
        "authorization_policy_snapshot",
        "authorization_source",
        "execution_policy",
        "research_run_id",
        "research_action",
        "research_confidence",
        "market_session_source",
        "market_session",
        "quote_spread_status",
        "quote_spread_bps",
        "quote_spread",
        "quote_mid",
        "quote_ask",
        "quote_bid",
        "quote_feed",
        "quote_provider",
        "risk_rules",
        "risk_policy_version",
        "risk_outcome",
    ):
        op.drop_column("trade_proposals", name)

    op.drop_column("portfolio_snapshots", "broker_environment")
    op.drop_column("portfolio_snapshots", "cash_in_pies")
    op.drop_column("portfolio_snapshots", "cash_reserved")

    op.drop_index("ix_risk_evaluations_thesis_id", table_name="risk_evaluations")
    op.drop_index("ix_risk_evaluations_proposal_id", table_name="risk_evaluations")
    op.drop_index("ix_risk_evaluations_created_at", table_name="risk_evaluations")
    op.drop_table("risk_evaluations")

    connection = op.get_bind()
    for enum_name in NEW_ENUM_TYPES:
        postgresql.ENUM(name=enum_name).drop(connection, checkfirst=True)

    # PostgreSQL cannot remove an enum value, so the type is rebuilt. Rows in
    # INVALIDATED move to CANCELLED first -- the nearest truthful terminal
    # state -- because otherwise the cast fails and this downgrade would not
    # actually reverse the upgrade.
    op.execute(
        sa.text(
            "UPDATE trade_proposals SET status = 'CANCELLED', "
            "status_reason = COALESCE(status_reason, 'invalidated before schema downgrade') "
            "WHERE status = 'INVALIDATED'"
        )
    )
    # Both indexes carry `status` in their definition, and a partial index's
    # predicate binds the *old* type's equality operator. Rebuilding the column
    # type underneath them fails with "operator does not exist"; they are
    # therefore dropped and recreated verbatim around the swap.
    op.drop_index(
        "uq_trade_proposals_active_instrument",
        table_name="trade_proposals",
        postgresql_where=sa.text(f"status IN ({_ACTIVE_SQL})"),
    )
    op.drop_index("ix_trade_proposals_status_expires", table_name="trade_proposals")
    op.execute(sa.text("ALTER TABLE trade_proposals ALTER COLUMN status DROP DEFAULT"))
    op.execute(sa.text("ALTER TYPE proposal_status RENAME TO proposal_status_old"))
    op.execute(
        sa.text(
            "CREATE TYPE proposal_status AS ENUM ("
            "'DRAFT', 'READY', 'NOTIFIED', 'APPROVAL_PENDING', 'APPROVED', 'REJECTED', "
            "'EXPIRED', 'EXECUTING', 'EXECUTED', 'EXECUTION_AMBIGUOUS', 'FAILED', 'CANCELLED')"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE trade_proposals ALTER COLUMN status TYPE proposal_status "
            "USING status::text::proposal_status"
        )
    )
    op.execute(sa.text("ALTER TABLE trade_proposals ALTER COLUMN status SET DEFAULT 'DRAFT'"))
    op.execute(sa.text("DROP TYPE proposal_status_old"))
    op.create_index(
        "ix_trade_proposals_status_expires",
        "trade_proposals",
        ["status", "expires_at"],
        unique=False,
    )
    op.create_index(
        "uq_trade_proposals_active_instrument",
        "trade_proposals",
        ["broker", "account_id", "broker_ticker"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({_ACTIVE_SQL})"),
    )
