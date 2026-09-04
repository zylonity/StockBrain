"""Declarative base, naming conventions and shared column types."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any, ClassVar

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "Base",
    "JSONDict",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "created_at_column",
    "money",
    "quantity",
    "updated_at_column",
    "utcnow",
]

# Deterministic constraint names make Alembic autogenerate diffs stable and
# allow migrations to drop constraints by name on any environment.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JSONDict = dict[str, Any]

#: Cash amounts in an account currency. 4 decimal places is enough for every
#: currency StockBrain handles while leaving headroom for intermediate maths.
money = Annotated[Decimal, mapped_column(sa.Numeric(24, 4))]

#: Share quantities. Trading 212 supports fractional shares, so this needs far
#: more precision than a currency amount.
quantity = Annotated[Decimal, mapped_column(sa.Numeric(28, 10))]


def utcnow() -> dt.datetime:
    """Timezone-aware current UTC time.

    Used for client-side defaults; column server defaults use ``now()`` so that
    the database clock is authoritative for persisted rows.
    """
    return dt.datetime.now(dt.UTC)


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dt.datetime: sa.DateTime(timezone=True),
        uuid.UUID: sa.Uuid(as_uuid=True),
        JSONDict: pg.JSONB,
        dict[str, Any]: pg.JSONB,
        Decimal: sa.Numeric(24, 8),
        str: sa.Text,
    }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        identifier = getattr(self, "id", None)
        return f"<{type(self).__name__} id={identifier}>"


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )


def created_at_column() -> Mapped[dt.datetime]:
    return mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=sa.func.now(),
    )


def updated_at_column() -> Mapped[dt.datetime]:
    return mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=sa.func.now(),
    )


class TimestampMixin:
    created_at: Mapped[dt.datetime] = created_at_column()
    updated_at: Mapped[dt.datetime] = updated_at_column()
