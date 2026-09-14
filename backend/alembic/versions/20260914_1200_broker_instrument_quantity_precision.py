"""The quantity precision each instrument's broker will accept.

Revision ID: e5b1c9d47a02
Revises: 9f1c3a7e2b04

Trading 212's instrument metadata carries no precision, yet it refuses an order
whose quantity has too many decimal places with
``api-errors/quantity-precision-mismatch``.  The precision is per instrument
(CRM: 4, GSK: 3), so a single process-wide step cannot be right for all of them.

``quantity_precision`` stores what the broker has revealed, per instrument.  It
is nullable precisely because the honest starting value is "unknown": the
refusal is the only source of the number, and until one happens sizing must fall
back on the configured default rather than invent a per-instrument value.
Nothing is backfilled -- every existing row genuinely has no learned precision.

No application code is imported.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5b1c9d47a02"
down_revision: str | None = "9f1c3a7e2b04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "broker_instruments",
        sa.Column("quantity_precision", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("broker_instruments", "quantity_precision")
