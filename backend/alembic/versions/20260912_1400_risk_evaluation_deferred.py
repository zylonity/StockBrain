"""A blocked evaluation that has not happened yet.

Revision ID: 9f1c3a7e2b04
Revises: cd8d1b706008

Some refusals are about the market's state rather than the trade's merits: the
session is shut, the quote is stale, the account snapshot has not been taken.
Those are worth retrying -- the next open or the next quote can change the
answer -- while a refusal over confidence, currency or a concentration cap is
final.

``deferred`` marks the first kind.  It defaults to false, so every evaluation
written before this migration -- and every blocked evaluation that does not set
it -- is terminal, which is the safe reading: the retry loop only ever includes
rows that explicitly claimed to be a deferral.

One not-null column with a server default on ``risk_evaluations``.  Nothing is
backfilled.

No application code is imported.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9f1c3a7e2b04"
down_revision: str | None = "cd8d1b706008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "risk_evaluations",
        sa.Column("deferred", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("risk_evaluations", "deferred")
