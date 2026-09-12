"""A per-position high-water mark, so a trailing exit has a high to trail.

Revision ID: 2a109374b5c6
Revises: c3f28a1d6b45

Trailing exits need the highest price a position has reached since it was
opened, and nothing stores it today.  The account sync already reads every open
position's broker-supplied price, so ``position_peaks`` is filled from data the
system fetches anyway -- no new endpoint and no extra call.

Three choices are load-bearing:

* **The natural key is ``(broker, account_id, broker_ticker)``**, the same one
  ``positions`` uses, so the sync's upsert can address a peak without a lookup.
* **The price only ratchets up.**  The sync writes the greater of the stored
  peak and the newly observed price, so a pullback cannot erase the high a
  trailing rule is armed against.
* **``observations`` counts the syncs that contributed.**  A peak seen once is
  a peak a trailing rule should not yet trust, and the count is how it can tell
  a single print from a high confirmed twenty times.

The row is deleted when the broker stops reporting its position.  A name closed
and re-bought is a new position with a new thesis, and inheriting the old peak
would arm a stop against a high this position never saw.

No application code is imported.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2a109374b5c6"
down_revision: str | None = "c3f28a1d6b45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "position_peaks",
        sa.Column(
            "broker",
            postgresql.ENUM("TRADING212", name="broker", create_type=False),
            nullable=False,
        ),
        sa.Column("account_id", sa.Text(), server_default="default", nullable=False),
        sa.Column("broker_ticker", sa.Text(), nullable=False),
        sa.Column("peak_price", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("peak_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observations", sa.Integer(), server_default="1", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_position_peaks")),
        sa.UniqueConstraint(
            "broker",
            "account_id",
            "broker_ticker",
            name="uq_position_peaks_broker_account_id_broker_ticker",
        ),
    )


def downgrade() -> None:
    op.drop_table("position_peaks")
