"""add classification state llm telemetry and impact idempotency

Revision ID: 4e0854e62da5
Revises: 1ab50f38ad87
Create Date: 2026-09-04 19:14:41.701693+00:00
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4e0854e62da5"
down_revision: str | None = "1ab50f38ad87"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Frozen copy of stockbrain.intelligence.normalize.company_key as of this
# revision. Inlined on purpose: a migration must keep producing the same values
# forever, so it must not import application code that may later change.
_SUFFIXES = (
    "incorporated",
    "corporation",
    "company",
    "limited",
    "holdings",
    "holding",
    "group",
    "plc",
    "inc",
    "corp",
    "co",
    "ltd",
    "llc",
    "lp",
    "sa",
    "nv",
    "ag",
    "se",
    "spa",
    "oyj",
    "ab",
    "as",
    "asa",
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _company_key(name: str | None) -> str:
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = _NON_ALNUM.sub(" ", text.lower()).strip()
    if not text:
        return ""
    words = text.split()
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words)


def upgrade() -> None:
    connection = op.get_bind()

    # --- event_company_impacts.company_key -------------------------------
    # NOT NULL with a unique constraint, so it is added nullable, backfilled,
    # de-duplicated, and only then constrained.
    op.add_column("event_company_impacts", sa.Column("company_key", sa.Text(), nullable=True))
    op.add_column(
        "event_company_impacts",
        sa.Column("impact_path", sa.Text(), server_default="unknown", nullable=False),
    )

    impacts = sa.table(
        "event_company_impacts",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("event_id", sa.Uuid(as_uuid=True)),
        sa.column("company_name_hint", sa.Text),
        sa.column("company_key", sa.Text),
        sa.column("materiality_score", sa.Float),
    )

    rows = connection.execute(
        sa.select(impacts.c.id, impacts.c.company_name_hint, impacts.c.company_key).where(
            impacts.c.company_key.is_(None)
        )
    ).all()
    for row in rows:
        key = _company_key(row.company_name_hint) or str(row.id)
        connection.execute(sa.update(impacts).where(impacts.c.id == row.id).values(company_key=key))

    # Existing data predates the uniqueness rule, so collapse any duplicates
    # before the constraint is applied. The most material row wins.
    duplicates = (
        connection.execute(
            sa.text(
                """
            SELECT id FROM (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY event_id, company_key
                           ORDER BY materiality_score DESC NULLS LAST, created_at ASC
                       ) AS rank
                FROM event_company_impacts
            ) ranked
            WHERE ranked.rank > 1
            """
            )
        )
        .scalars()
        .all()
    )
    if duplicates:
        connection.execute(sa.delete(impacts).where(impacts.c.id.in_(list(duplicates))))

    op.alter_column("event_company_impacts", "company_key", existing_type=sa.Text(), nullable=False)
    op.create_unique_constraint(
        "uq_event_company_impacts_event_id_company_key",
        "event_company_impacts",
        ["event_id", "company_key"],
    )
    op.add_column("events", sa.Column("classified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("events", sa.Column("classifier_error", sa.Text(), nullable=True))
    op.add_column("events", sa.Column("merged_into_event_id", sa.Uuid(), nullable=True))
    op.add_column("events", sa.Column("relevant_to_public_equities", sa.Boolean(), nullable=True))
    op.add_column("events", sa.Column("needs_corroboration", sa.Boolean(), nullable=True))
    op.add_column("events", sa.Column("event_type_confidence", sa.Float(), nullable=True))
    # `topics` becomes a JSON array of classifier topics. Rows written by the
    # ingestion phase hold an object, which would fail list validation on read.
    op.alter_column(
        "events",
        "topics",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        existing_nullable=False,
    )
    connection.execute(
        sa.text("UPDATE events SET topics = '[]'::jsonb WHERE jsonb_typeof(topics) <> 'array'")
    )
    op.create_foreign_key(
        op.f("fk_events_merged_into_event_id_events"),
        "events",
        "events",
        ["merged_into_event_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("llm_calls", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("llm_calls", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("llm_calls", sa.Column("job_id", sa.Uuid(), nullable=True))
    op.add_column(
        "llm_calls", sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("llm_calls", sa.Column("cache_miss_input_tokens", sa.Integer(), nullable=True))
    op.add_column("llm_calls", sa.Column("reasoning_tokens", sa.Integer(), nullable=True))
    op.add_column("llm_calls", sa.Column("provider_request_id", sa.Text(), nullable=True))
    op.add_column("llm_calls", sa.Column("finish_reason", sa.Text(), nullable=True))
    op.add_column(
        "llm_calls",
        sa.Column(
            "had_reasoning_content", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column("llm_calls", sa.Column("error_class", sa.Text(), nullable=True))
    op.create_index("ix_llm_calls_event_id", "llm_calls", ["event_id"], unique=False)
    op.create_index("ix_llm_calls_job_id", "llm_calls", ["job_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_llm_calls_job_id_jobs"),
        "llm_calls",
        "jobs",
        ["job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    connection = op.get_bind()
    # Reverse the topics shape change so the older code can read the column.
    connection.execute(
        sa.text("UPDATE events SET topics = '{}'::jsonb WHERE jsonb_typeof(topics) = 'array'")
    )
    op.drop_constraint(op.f("fk_llm_calls_job_id_jobs"), "llm_calls", type_="foreignkey")
    op.drop_index("ix_llm_calls_job_id", table_name="llm_calls")
    op.drop_index("ix_llm_calls_event_id", table_name="llm_calls")
    op.drop_column("llm_calls", "error_class")
    op.drop_column("llm_calls", "had_reasoning_content")
    op.drop_column("llm_calls", "finish_reason")
    op.drop_column("llm_calls", "provider_request_id")
    op.drop_column("llm_calls", "reasoning_tokens")
    op.drop_column("llm_calls", "cache_miss_input_tokens")
    op.drop_column("llm_calls", "retry_count")
    op.drop_column("llm_calls", "job_id")
    op.drop_column("llm_calls", "completed_at")
    op.drop_column("llm_calls", "started_at")
    op.drop_constraint(op.f("fk_events_merged_into_event_id_events"), "events", type_="foreignkey")
    op.alter_column(
        "events",
        "topics",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        existing_nullable=False,
    )
    op.drop_column("events", "event_type_confidence")
    op.drop_column("events", "needs_corroboration")
    op.drop_column("events", "relevant_to_public_equities")
    op.drop_column("events", "merged_into_event_id")
    op.drop_column("events", "classifier_error")
    op.drop_column("events", "classified_at")
    op.drop_constraint(
        "uq_event_company_impacts_event_id_company_key", "event_company_impacts", type_="unique"
    )
    op.drop_column("event_company_impacts", "impact_path")
    op.drop_column("event_company_impacts", "company_key")
    # ### end Alembic commands ###
