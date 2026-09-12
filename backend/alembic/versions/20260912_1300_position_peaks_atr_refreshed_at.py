"""The rate-discipline clock for the ATR refresh.

Revision ID: cd8d1b706008
Revises: c0398cea8629

The refresh's old freshness gate keyed off ``atr_as_of`` -- the date of the last
bar that fed the ATR.  That date advances only on success, so on weekends, on
holidays, and permanently for every mismatch, insufficient or failed fetch, the
symbol was refetched on every interval: four times a day at the default cadence
and more at the minimum.

``atr_refreshed_at`` records when the last fetch was *attempted*, whatever its
outcome.  It is the clock the refresh gates on; ``atr_as_of`` keeps describing
the data itself, which is what the rule's staleness check reads.

One nullable column on ``position_peaks``.  Nothing is backfilled: ``NULL``
means "no attempt recorded yet", so the first refresh after this migration runs
exactly as before.

No application code is imported.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cd8d1b706008"
down_revision: str | None = "c0398cea8629"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "position_peaks", sa.Column("atr_refreshed_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("position_peaks", "atr_refreshed_at")
