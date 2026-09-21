"""Disclosure-feed provenance labels.

Revision ID: d47f2a9c81e5
Revises: b7d2e4f1a9c3

``SourceProvider`` gains the five keyless non-US wires so a source row's
provenance says which wire carried it.  ``JobType`` and ``ProviderName`` are
free text in the database (``jobs.job_type``, ``provider_health.provider``), so
neither needs a PostgreSQL type change; only ``source_provider`` is a native
enum.

No application code is imported.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d47f2a9c81e5"
down_revision: str | None = "b7d2e4f1a9c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS so a database that has somehow seen a label already is not
    # stranded.  ADD VALUE cannot run inside a transaction before PostgreSQL 12;
    # this deployment is on 17.
    op.execute("ALTER TYPE source_provider ADD VALUE IF NOT EXISTS 'INVESTEGATE'")
    op.execute("ALTER TYPE source_provider ADD VALUE IF NOT EXISTS 'EQS'")
    op.execute("ALTER TYPE source_provider ADD VALUE IF NOT EXISTS 'CNMV'")
    op.execute("ALTER TYPE source_provider ADD VALUE IF NOT EXISTS 'GLOBENEWSWIRE'")
    op.execute("ALTER TYPE source_provider ADD VALUE IF NOT EXISTS 'ACTUSNEWS'")


def downgrade() -> None:
    # PostgreSQL cannot remove an enum value, and recreating the type would
    # rewrite every source row.  An unused label is inert; a downgrade that
    # destroys provenance is not.  Same posture as the Brave/Exa migration.
    pass
