"""Curated company aliases.

The alias table is the deterministic escape hatch for cases evidence alone
cannot settle.  It is *curated*, not learned: every row is a human statement
that a particular name, in a particular scope, means a particular company.

Two rules keep it from becoming a source of the ambiguity it is meant to remove:

1. An **authoritative** alias claims a (name, type, exchange, currency) scope.
   ``uq_company_aliases_authoritative_scope`` makes a second authoritative claim
   on the same scope by a different company impossible, so two aliases can never
   silently contradict each other.
2. A bare name that genuinely refers to several listings gets **no** alias at
   all.  "Alphabet" resolving to GOOGL would be a decision disguised as data;
   the correct behaviour is the ambiguity the resolver already reports.  Only
   the *listing-specific* names get aliases.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.models.companies import Company, CompanyAlias
from stockbrain.enums import AliasType
from stockbrain.instruments.normalize import (
    instrument_name_key,
    normalize_currency,
    normalize_isin,
    normalize_ticker,
)
from stockbrain.logging import get_logger

__all__ = ["AliasSpec", "alias_key", "upsert_alias"]

log = get_logger(__name__)


def alias_key(alias: str, alias_type: AliasType) -> str:
    """The lookup key for an alias.

    Tickers normalise as tickers (class separators preserved, then lower-cased
    to share one column with names); everything else normalises as a name.  The
    resolver builds its candidate keys the same way, so the two cannot drift.
    """
    if alias_type is AliasType.TICKER:
        return normalize_ticker(alias).lower()
    return instrument_name_key(alias)


@dataclass(slots=True, frozen=True)
class AliasSpec:
    """One curated mapping, before it becomes a row."""

    alias: str
    alias_type: AliasType = AliasType.COMMON
    exchange: str | None = None
    currency: str | None = None
    isin: str | None = None
    confidence: float = 1.0
    is_authoritative: bool = True
    source: str = "MANUAL"
    notes: str | None = None


async def upsert_alias(session: AsyncSession, company: Company, spec: AliasSpec) -> CompanyAlias:
    """Insert or update one alias for ``company``.

    Raises :class:`sqlalchemy.exc.IntegrityError` when the alias would contradict
    an existing authoritative one.  That is deliberate: a contradiction is a
    curation mistake to be fixed, not a conflict to be resolved by last-write-wins.
    """
    key = alias_key(spec.alias, spec.alias_type)
    if not key:
        raise ValueError(f"alias {spec.alias!r} normalises to an empty key")

    existing = (
        await session.execute(
            sa.select(CompanyAlias).where(
                CompanyAlias.company_id == company.id,
                CompanyAlias.alias_normalized == key,
            )
        )
    ).scalar_one_or_none()

    currency = normalize_currency(spec.currency) or None
    isin = normalize_isin(spec.isin) or None

    if existing is not None:
        existing.alias = spec.alias
        existing.alias_type = spec.alias_type
        existing.exchange = spec.exchange
        existing.currency = currency
        existing.isin = isin
        existing.confidence = spec.confidence
        existing.is_authoritative = spec.is_authoritative
        existing.source = spec.source
        existing.notes = spec.notes
        await session.flush()
        return existing

    row = CompanyAlias(
        company_id=company.id,
        alias=spec.alias,
        alias_normalized=key,
        alias_type=spec.alias_type,
        exchange=spec.exchange,
        currency=currency,
        isin=isin,
        confidence=spec.confidence,
        is_authoritative=spec.is_authoritative,
        source=spec.source,
        notes=spec.notes,
    )
    # Inside a savepoint so a contradiction rolls back only this row: seeding a
    # batch of aliases must be able to report the one that conflicts without
    # poisoning the transaction that carries the rest.
    savepoint = await session.begin_nested()
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await savepoint.rollback()
        # Surfaced rather than swallowed: the caller must decide whether the
        # existing authoritative claim or the new one is wrong.
        log.warning(
            "company_alias_contradiction",
            alias=spec.alias,
            alias_type=spec.alias_type.value,
            company_id=str(company.id),
        )
        raise
    await savepoint.commit()
    return row
