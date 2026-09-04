"""Helpers for mapping Python enums onto native PostgreSQL enum types."""

from __future__ import annotations

from enum import StrEnum

import sqlalchemy as sa


def pg_enum[E: StrEnum](enum_cls: type[E], name: str) -> sa.Enum:
    """Return a native PostgreSQL ``ENUM`` bound to ``enum_cls``.

    ``values_callable`` makes PostgreSQL store the enum *value* rather than the
    Python member name, so the database is readable with plain SQL and the two
    never drift apart.
    """
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=True,
        create_constraint=False,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )
