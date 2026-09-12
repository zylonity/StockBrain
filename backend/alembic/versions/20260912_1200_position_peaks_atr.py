"""Average true range per open position, so the volatility stop has a scale.

Revision ID: c0398cea8629
Revises: 2a109374b5c6

The deterministic exit rules are percentage floors: eight percent of average
cost is eight percent whether the instrument moves half a percent a day or five.
A volatility-scaled floor needs the instrument's own recent range, and
``average_true_range`` computes it from daily bars; this is where the result is
kept.

Five nullable columns on ``position_peaks``, the row the trailing rule already
reads, because the ATR is a property of an open position rather than a listing:
the same instrument held twice was bought at different times, and the stop must
be scaled to what this position actually experienced.

Nothing here is backfilled.  ``NULL`` means "no volatility reading yet", which
is the honest state on a database that has never run the refresh; the rule reads
it as "skip and let the flat hard stop stand", never as a default ATR.

No application code is imported.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c0398cea8629"
down_revision: str | None = "2a109374b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "position_peaks", sa.Column("atr", sa.Numeric(precision=24, scale=8), nullable=True)
    )
    op.add_column("position_peaks", sa.Column("atr_period", sa.Integer(), nullable=True))
    op.add_column("position_peaks", sa.Column("atr_currency", sa.String(length=3), nullable=True))
    op.add_column("position_peaks", sa.Column("atr_as_of", sa.Date(), nullable=True))
    op.add_column("position_peaks", sa.Column("atr_source", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("position_peaks", "atr_source")
    op.drop_column("position_peaks", "atr_as_of")
    op.drop_column("position_peaks", "atr_currency")
    op.drop_column("position_peaks", "atr_period")
    op.drop_column("position_peaks", "atr")
