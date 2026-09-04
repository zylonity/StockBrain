"""add event title hash for deterministic grouping

Adds ``events.title_hash``: a SHA-256 over the normalised event title, used to
attach a syndicated story to the event it belongs to with an indexed lookup
rather than a scan.

The column is NOT NULL, so it is added nullable, backfilled, and only then
constrained. The normalisation is **inlined here on purpose**: a migration must
keep producing the same bytes forever, so it must not import application code
that may later change.

Revision ID: 1ab50f38ad87
Revises: 1e4f3a52f540
Create Date: 2026-09-04 18:33:57.073366+00:00
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1ab50f38ad87"
down_revision: str | None = "1e4f3a52f540"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of the normalisation in stockbrain.ingestion.normalizer as of this
# revision. Includes NO-BREAK SPACE (U+00A0) and ZERO WIDTH SPACE (U+200B).
_WHITESPACE_RE = re.compile("[ \\t\\u00a0\\u200b]+")

_BACKFILL_BATCH = 1000


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = _WHITESPACE_RE.sub(" ", text.replace("\n", " "))
    return text.strip().casefold()


def _title_hash(title: str | None) -> str:
    digest = hashlib.sha256()
    digest.update(_normalize(title).encode("utf-8"))
    digest.update(b"\x1f")
    digest.update(b"")
    return digest.hexdigest()


def upgrade() -> None:
    op.add_column("events", sa.Column("title_hash", sa.String(length=64), nullable=True))

    connection = op.get_bind()
    events = sa.table(
        "events",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("title", sa.Text),
        sa.column("title_hash", sa.String(64)),
    )

    # Batched so a large table does not build one enormous UPDATE statement.
    while True:
        rows = connection.execute(
            sa.select(events.c.id, events.c.title)
            .where(events.c.title_hash.is_(None))
            .limit(_BACKFILL_BATCH)
        ).all()
        if not rows:
            break
        for row in rows:
            connection.execute(
                sa.update(events)
                .where(events.c.id == row.id)
                .values(title_hash=_title_hash(row.title))
            )

    op.alter_column("events", "title_hash", existing_type=sa.String(length=64), nullable=False)
    op.create_index(
        "ix_events_title_hash_first_seen",
        "events",
        ["title_hash", "first_seen_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_events_title_hash_first_seen", table_name="events")
    op.drop_column("events", "title_hash")
