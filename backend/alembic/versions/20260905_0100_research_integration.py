"""Research provenance and durable run ownership.

Revision ID: 5a180cf497b2
Revises: 9c31f4b70ad2
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "5a180cf497b2"
down_revision = "9c31f4b70ad2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy rows retain NULL keys; no old provenance is invented or erased.
    for name, kind in (
        ("dedupe_key", sa.String(64)),
        ("impact_id", sa.Uuid()),
        ("broker_instrument_id", sa.Uuid()),
        ("config_version", sa.String(64)),
        ("rerun_id", sa.Uuid()),
        ("lease_token", sa.Uuid()),
        ("lease_expires_at", sa.DateTime(timezone=True)),
        ("analyst_config", postgresql.JSONB()),
        ("provider_degradation", postgresql.JSONB()),
        ("error_class", sa.Text()),
    ):
        op.add_column("research_runs", sa.Column(name, kind, nullable=True))
    op.create_unique_constraint("uq_research_runs_dedupe_key", "research_runs", ["dedupe_key"])
    op.create_foreign_key(
        "fk_research_runs_impact_id_event_company_impacts",
        "research_runs",
        "event_company_impacts",
        ["impact_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_research_runs_broker_instrument_id_broker_instruments",
        "research_runs",
        "broker_instruments",
        ["broker_instrument_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_research_runs_broker_instrument_id_broker_instruments",
        "research_runs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_research_runs_impact_id_event_company_impacts", "research_runs", type_="foreignkey"
    )
    op.drop_constraint("uq_research_runs_dedupe_key", "research_runs", type_="unique")
    for name in (
        "error_class",
        "provider_degradation",
        "analyst_config",
        "lease_expires_at",
        "lease_token",
        "rerun_id",
        "config_version",
        "broker_instrument_id",
        "impact_id",
        "dedupe_key",
    ):
        op.drop_column("research_runs", name)
