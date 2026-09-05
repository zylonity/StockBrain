"""Telegram approval bindings and the new approval stages.

Revision ID: 8c41d0f7ab92
Revises: 36f53456be8a

Three changes, all on ``approval_actions``:

1. ``chat_identifier`` -- the numeric Telegram chat a single-use token may be
   redeemed from.  Nullable, because the web channel has no chat and inventing
   one would be a binding that means nothing.  Existing rows keep NULL: a token
   issued before this revision was never bound to a chat, and back-filling one
   would claim a restriction that was not in force when it was issued.
2. ``approval_stage`` gains ``REJECT`` and ``DETAILS``.  PostgreSQL cannot add
   an enum value inside a transaction that then *uses* it, so the ``ALTER TYPE``
   runs in an explicit autocommit block, exactly as the Phase 6 migration does
   for ``proposal_status``.
3. A partial index on ``(proposal_id) WHERE consumed_at IS NULL`` -- the
   predicate the sweep that retires every outstanding token for a terminated
   proposal actually runs.

The downgrade rebuilds the enum type, because PostgreSQL cannot remove a value
from one.  Rows whose stage is ``REJECT`` or ``DETAILS`` are **deleted** first
rather than being recast into ``APPROVE``: an approval action is an ephemeral
single-use token, mostly already consumed, and mapping a "show me the details"
token onto "approve this trade" would be inventing an authorization record that
never existed.  The audit log keeps the actions that mattered.

No application code is imported here.  The value lists are frozen literals so a
later phase adding a stage cannot retroactively change what this migration did.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8c41d0f7ab92"
down_revision: str | None = "36f53456be8a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen copy of the approval stages as they stand *before* this revision.
_STAGES_BEFORE: tuple[str, ...] = ("APPROVE", "CONFIRM")

#: ...and after it.
_STAGES_ADDED: tuple[str, ...] = ("REJECT", "DETAILS")


def upgrade() -> None:
    # A new enum value cannot be used in the transaction that added it.
    with op.get_context().autocommit_block():
        for value in _STAGES_ADDED:
            op.execute(sa.text(f"ALTER TYPE approval_stage ADD VALUE IF NOT EXISTS '{value}'"))

    op.add_column("approval_actions", sa.Column("chat_identifier", sa.Text(), nullable=True))
    op.create_index(
        "ix_approval_actions_open",
        "approval_actions",
        ["proposal_id"],
        unique=False,
        postgresql_where=sa.text("consumed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_approval_actions_open",
        table_name="approval_actions",
        postgresql_where=sa.text("consumed_at IS NULL"),
    )
    op.drop_column("approval_actions", "chat_identifier")

    # PostgreSQL cannot drop an enum value, so the type is rebuilt. Rows using
    # a stage that will not exist afterwards are removed first, or the cast
    # fails and this downgrade would not actually reverse the upgrade.
    op.execute(
        sa.text("DELETE FROM approval_actions WHERE stage::text = ANY(:stages)").bindparams(
            stages=list(_STAGES_ADDED)
        )
    )

    kept = ", ".join(f"'{value}'" for value in _STAGES_BEFORE)
    op.execute(sa.text("ALTER TYPE approval_stage RENAME TO approval_stage_old"))
    op.execute(sa.text(f"CREATE TYPE approval_stage AS ENUM ({kept})"))
    op.execute(
        sa.text(
            "ALTER TABLE approval_actions ALTER COLUMN stage TYPE approval_stage "
            "USING stage::text::approval_stage"
        )
    )
    op.execute(sa.text("DROP TYPE approval_stage_old"))
