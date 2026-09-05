"""Cross-currency sizing provenance on trade proposals.

Revision ID: b47e0c81f5a2
Revises: 9a1c4d2b7e31

Phase 9 makes a GBP account able to size a USD listing.  That is only safe if
the *rate it was sized with* is as auditable as the price it was priced with, so
every proposal now records the conversion the way it already records the quote:
the pair, the rate, which direction the rate was applied in, the source, the
source's grade, the source's own timestamp, when it arrived and how old it was.

Two columns deserve their reasons:

* **``estimated_notional_account_currency``** exists beside
  ``estimated_notional`` rather than replacing it.  Both numbers are true and
  they answer different questions: what the broker will trade (in the
  instrument's currency) and what it costs the portfolio (in the account's).
  Every existing row was created under the same-currency rule, so the backfill
  is a copy and is exactly correct for them.

* **``fx_required``** distinguishes "no conversion was needed" from "converted
  at parity".  Backfilled ``false`` for every existing row, which is what the
  same-currency rule guaranteed.  A ``NULL``-as-unknown would have made the two
  cases indistinguishable in precisely the audit this whole set of columns is
  for.

Nothing is dropped and nothing is made ``NOT NULL``: the FX columns are all
nullable because a same-currency proposal legitimately has no rate, and a
``NOT NULL`` column filled with a placeholder rate would be the invented number
this design exists to prevent.

No application code is imported.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b47e0c81f5a2"
down_revision: str | None = "9a1c4d2b7e31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trade_proposals",
        sa.Column("estimated_notional_account_currency", sa.Numeric(24, 4), nullable=True),
    )
    op.add_column(
        "trade_proposals",
        sa.Column("fx_required", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("trade_proposals", sa.Column("fx_rate", sa.Numeric(28, 12), nullable=True))
    op.add_column("trade_proposals", sa.Column("fx_base_currency", sa.String(3), nullable=True))
    op.add_column("trade_proposals", sa.Column("fx_quote_currency", sa.String(3), nullable=True))
    op.add_column("trade_proposals", sa.Column("fx_direction", sa.Text(), nullable=True))
    op.add_column("trade_proposals", sa.Column("fx_provider", sa.Text(), nullable=True))
    op.add_column("trade_proposals", sa.Column("fx_rate_grade", sa.Text(), nullable=True))
    op.add_column("trade_proposals", sa.Column("fx_rate_type", sa.Text(), nullable=True))
    op.add_column(
        "trade_proposals",
        sa.Column("fx_provider_timestamp", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "trade_proposals", sa.Column("fx_received_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("trade_proposals", sa.Column("fx_age_seconds", sa.Numeric(18, 3), nullable=True))

    # Every pre-Phase-9 proposal was produced under `RISK_REQUIRE_SAME_CURRENCY`
    # with `currency_alignment` blocking any mismatch, so the instrument and the
    # account currency were necessarily equal and the two notionals are the same
    # number. A copy, not a conversion.
    op.execute(
        sa.text(
            "UPDATE trade_proposals "
            "SET estimated_notional_account_currency = estimated_notional "
            "WHERE estimated_notional_account_currency IS NULL"
        )
    )

    # A rate is either present with its whole provenance or absent entirely.
    # Half a record -- a number with no source, or a source with no timestamp --
    # is worse than none, because it looks auditable.
    op.create_check_constraint(
        "fx_provenance_complete",
        "trade_proposals",
        "(fx_rate IS NULL AND fx_base_currency IS NULL AND fx_quote_currency IS NULL "
        " AND fx_provider IS NULL AND fx_provider_timestamp IS NULL) "
        "OR (fx_rate IS NOT NULL AND fx_base_currency IS NOT NULL "
        " AND fx_quote_currency IS NOT NULL AND fx_provider IS NOT NULL "
        " AND fx_provider_timestamp IS NOT NULL)",
    )
    # A positive rate or no rate. Zero and negative are parsing failures, and a
    # parsing failure that produces a plausible Numeric is the expensive kind.
    op.create_check_constraint(
        "fx_rate_positive", "trade_proposals", "fx_rate IS NULL OR fx_rate > 0"
    )
    # The direction that matters most: a proposal that says it needed no
    # conversion must not be carrying a rate, because that is the shape a
    # silently-applied 1.0 would take.
    op.create_check_constraint(
        "fx_rate_requires_fx_required",
        "trade_proposals",
        "fx_rate IS NULL OR fx_required",
    )
    # And the converse: a cross-currency proposal without a rate is a size
    # nobody can re-derive.
    op.create_check_constraint(
        "fx_required_requires_rate",
        "trade_proposals",
        "NOT fx_required OR fx_rate IS NOT NULL",
    )


def downgrade() -> None:
    # Bare constraint names. `op.drop_constraint` applies the naming convention
    # too, so passing an already-rendered `ck_<table>_<name>` prefixes it twice
    # and then truncates with a hash suffix -- Phase 6 bug 13 and Phase 8 bug 18.
    for name in (
        "fx_required_requires_rate",
        "fx_rate_requires_fx_required",
        "fx_rate_positive",
        "fx_provenance_complete",
    ):
        op.drop_constraint(name, "trade_proposals", type_="check")

    for column in (
        "fx_age_seconds",
        "fx_received_at",
        "fx_provider_timestamp",
        "fx_rate_type",
        "fx_rate_grade",
        "fx_provider",
        "fx_direction",
        "fx_quote_currency",
        "fx_base_currency",
        "fx_rate",
        "fx_required",
        "estimated_notional_account_currency",
    ):
        op.drop_column("trade_proposals", column)
