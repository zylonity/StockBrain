"""Company -> broker instrument resolution.

This is the correctness boundary of the whole system.  The classifier produces a
*hint* -- a name, sometimes a ticker, sometimes an exchange -- and a hint is
never an executable identity.  The only thing that may ever reach an order
request is a ``broker_instruments`` row that Trading 212 itself supplied.

The resolver walks a ladder of evidence, strongest first, and stops at the first
rung that yields exactly one instrument:

===  ====================================  ==========================
 #   Evidence                              Confidence
===  ====================================  ==========================
 1   Exact ISIN                            0.99
 2   Curated authoritative alias           0.97
 3   Exact ticker + exchange               0.92
 4   Name + exchange + currency            0.82
 5   Weaker heuristics                     candidates only, never resolves
===  ====================================  ==========================

Rung 5 exists to explain a refusal, not to make one fewer refusal happen.  If a
rung matches more than one instrument the result is ``AMBIGUOUS`` and carries
every alternative, because "GOOGL or GOOG?" is a question a human or a curated
alias answers -- picking one silently is the failure mode this module exists to
prevent.

Ambiguity is a *general* property of the evidence, not a list of special cases.
Alphabet's share classes, Berkshire's A/B, an ADR against its ordinary line, a
UK line against a US line, a reused ticker and a renamed company all reach it by
the same route: more than one verified listing fits the evidence supplied.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.models.companies import BrokerInstrument, Company, CompanyAlias
from stockbrain.enums import Broker, ResolutionMethod, ResolutionStatus
from stockbrain.instruments.normalize import (
    instrument_name_key,
    normalize_currency,
    normalize_exchange,
    normalize_isin,
    normalize_ticker,
)
from stockbrain.logging import get_logger

__all__ = [
    "CONFIDENCE_BY_METHOD",
    "SUPPORTED_INSTRUMENT_TYPES",
    "InstrumentCandidate",
    "InstrumentResolver",
    "ResolutionRequest",
    "ResolutionResult",
]

log = get_logger(__name__)

#: Instrument types StockBrain is willing to price and (later) size.  Everything
#: else resolves to UNSUPPORTED rather than being silently treated as equity:
#: the risk engine has no model for a warrant, a future or a corporate action.
SUPPORTED_INSTRUMENT_TYPES = frozenset({"STOCK", "ETF"})

#: Confidence attached to each rung.  These are evidence strengths, not
#: probabilities, and nothing downstream may treat them as calibrated.
CONFIDENCE_BY_METHOD: dict[ResolutionMethod, float] = {
    ResolutionMethod.ISIN_EXACT: 0.99,
    ResolutionMethod.MANUAL_ALIAS: 0.97,
    ResolutionMethod.TICKER_EXCHANGE: 0.92,
    ResolutionMethod.NAME_EXCHANGE_CURRENCY: 0.82,
    ResolutionMethod.HEURISTIC_CANDIDATE: 0.0,
    ResolutionMethod.NONE: 0.0,
}

#: Never generate more candidates than a human would read.
_MAX_CANDIDATES = 10


@dataclass(slots=True, frozen=True)
class ResolutionRequest:
    """Everything known about the company to be resolved.

    ``ticker_hint`` and ``exchange_hint`` come from the LLM classifier and are
    treated as search *keys*, never as identity: a hint that matches no synced
    instrument produces NOT_FOUND, not an instrument built from the hint.
    """

    name_hint: str
    ticker_hint: str | None = None
    exchange_hint: str | None = None
    currency_hint: str | None = None
    isin_hint: str | None = None
    broker: Broker = Broker.TRADING212


@dataclass(slots=True)
class InstrumentCandidate:
    """One listing that matched, with enough context to choose between them."""

    broker_instrument_id: uuid.UUID
    broker_ticker: str
    name: str | None
    market_symbol: str | None
    exchange: str | None
    currency: str | None
    isin: str | None
    instrument_type: str | None
    matched_by: ResolutionMethod

    def as_dict(self) -> dict[str, Any]:
        return {
            "broker_instrument_id": str(self.broker_instrument_id),
            "broker_ticker": self.broker_ticker,
            "name": self.name,
            "market_symbol": self.market_symbol,
            "exchange": self.exchange,
            "currency": self.currency,
            "isin": self.isin,
            "instrument_type": self.instrument_type,
            "matched_by": self.matched_by.value,
        }


@dataclass(slots=True)
class ResolutionResult:
    """The resolver's verdict.  Only ``RESOLVED`` may progress."""

    status: ResolutionStatus
    method: ResolutionMethod = ResolutionMethod.NONE
    confidence: float = 0.0
    broker_instrument_id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None
    reasons: list[str] = field(default_factory=list)
    alternatives: list[InstrumentCandidate] = field(default_factory=list)

    @property
    def is_resolved(self) -> bool:
        return self.status is ResolutionStatus.RESOLVED

    @property
    def notes(self) -> str:
        return "; ".join(self.reasons)[:2000]

    def alternatives_as_json(self) -> list[dict[str, Any]]:
        return [candidate.as_dict() for candidate in self.alternatives]


def _candidate(instrument: BrokerInstrument, method: ResolutionMethod) -> InstrumentCandidate:
    return InstrumentCandidate(
        broker_instrument_id=instrument.id,
        broker_ticker=instrument.broker_ticker,
        name=instrument.name,
        market_symbol=instrument.market_symbol,
        exchange=instrument.exchange,
        currency=instrument.currency,
        isin=instrument.isin,
        instrument_type=instrument.instrument_type,
        matched_by=method,
    )


class InstrumentResolver:
    """Resolves one company hint against synced broker metadata.

    Stateless and read-only: it never writes, so it can be called speculatively
    from the API to explain why something did not resolve.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(self, request: ResolutionRequest) -> ResolutionResult:
        reasons: list[str] = []

        if not await self._has_any_metadata(request.broker):
            return ResolutionResult(
                status=ResolutionStatus.NOT_FOUND,
                reasons=[
                    f"no {request.broker.value} instrument metadata has been synced; "
                    "run INSTRUMENT_REFRESH before resolving"
                ],
            )

        # --- rung 1: ISIN ------------------------------------------------
        isin = normalize_isin(request.isin_hint)
        if not isin:
            isin = await self._isin_from_company(request)
        if isin:
            reasons.append(f"ISIN {isin} available as strongest evidence")
            matches = await self._by_isin(request.broker, isin)
            outcome = self._decide(request, matches, ResolutionMethod.ISIN_EXACT, reasons)
            if outcome is not None:
                return outcome
            reasons.append("no active instrument carries that ISIN")

        # --- rung 2: curated alias ---------------------------------------
        alias_company, alias_reason = await self._company_from_alias(request)
        if alias_company is not None:
            reasons.append(alias_reason)
            matches = await self._for_company(request.broker, alias_company)
            outcome = self._decide(
                request, matches, ResolutionMethod.MANUAL_ALIAS, reasons, company=alias_company
            )
            if outcome is not None:
                return outcome
            reasons.append("the aliased company has no matching broker instrument")

        # --- rung 3: ticker + exchange -----------------------------------
        ticker = normalize_ticker(request.ticker_hint)
        if ticker:
            matches = await self._by_symbol(request.broker, ticker)
            reasons.append(f"ticker hint {ticker!r} matched {len(matches)} listing(s)")
            outcome = self._decide(request, matches, ResolutionMethod.TICKER_EXCHANGE, reasons)
            if outcome is not None:
                return outcome

        # --- rung 4: name + exchange + currency --------------------------
        name_key = instrument_name_key(request.name_hint)
        name_matches: list[BrokerInstrument] = []
        if name_key:
            name_matches = await self._by_name_key(request.broker, name_key)
            reasons.append(f"name key {name_key!r} matched {len(name_matches)} listing(s)")
            outcome = self._decide(
                request, name_matches, ResolutionMethod.NAME_EXCHANGE_CURRENCY, reasons
            )
            if outcome is not None:
                return outcome

        # --- rung 5: candidates only -------------------------------------
        heuristic = await self._heuristic_candidates(request, name_key, ticker)
        pool = _dedupe(name_matches + heuristic)
        if pool:
            reasons.append(
                f"{len(pool)} listing(s) plausibly match, none uniquely; "
                "resolution requires an ISIN, a curated alias, or an exchange"
            )
            return ResolutionResult(
                status=ResolutionStatus.AMBIGUOUS,
                method=ResolutionMethod.HEURISTIC_CANDIDATE,
                confidence=0.0,
                reasons=reasons,
                alternatives=[
                    _candidate(item, ResolutionMethod.HEURISTIC_CANDIDATE)
                    for item in pool[:_MAX_CANDIDATES]
                ],
            )

        reasons.append("no broker instrument matched any available evidence")
        return ResolutionResult(status=ResolutionStatus.NOT_FOUND, reasons=reasons)

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------
    def _decide(
        self,
        request: ResolutionRequest,
        matches: list[BrokerInstrument],
        method: ResolutionMethod,
        reasons: list[str],
        *,
        company: Company | None = None,
    ) -> ResolutionResult | None:
        """Turn a rung's matches into a verdict, or ``None`` to try the next rung.

        Narrowing happens only with evidence the *caller* supplied: an exchange
        hint, then a currency hint.  Nothing here prefers a listing for being
        larger, cheaper, first alphabetically or American.

        A hint that *contradicts* a single match does not reject it: the hint
        came from a language model and the match came from broker metadata, so
        the model losing that disagreement is the correct outcome.  A hint only
        ever chooses between listings the evidence already admits.
        """
        if not matches:
            return None

        narrowed, narrowing = self._narrow(request, matches)
        if narrowing:
            reasons.append(narrowing)

        if len(narrowed) == 1:
            instrument = narrowed[0]
            if not self._is_supported(instrument):
                reasons.append(
                    f"instrument type {instrument.instrument_type!r} is not supported for "
                    "pricing or sizing"
                )
                return ResolutionResult(
                    status=ResolutionStatus.UNSUPPORTED,
                    method=method,
                    confidence=CONFIDENCE_BY_METHOD[method],
                    broker_instrument_id=instrument.id,
                    company_id=company.id if company is not None else instrument.company_id,
                    reasons=reasons,
                    alternatives=[_candidate(instrument, method)],
                )
            reasons.append(f"resolved to {instrument.broker_ticker} via {method.value}")
            return ResolutionResult(
                status=ResolutionStatus.RESOLVED,
                method=method,
                confidence=CONFIDENCE_BY_METHOD[method],
                broker_instrument_id=instrument.id,
                company_id=company.id if company is not None else instrument.company_id,
                reasons=reasons,
                alternatives=[],
            )

        if len(narrowed) > 1:
            supported = [item for item in narrowed if self._is_supported(item)]
            if not supported:
                reasons.append("every matching listing is of an unsupported instrument type")
                return ResolutionResult(
                    status=ResolutionStatus.UNSUPPORTED,
                    method=method,
                    reasons=reasons,
                    alternatives=[_candidate(item, method) for item in narrowed[:_MAX_CANDIDATES]],
                )
            reasons.append(
                f"{len(narrowed)} listings match by {method.value}; refusing to choose one"
            )
            return ResolutionResult(
                status=ResolutionStatus.AMBIGUOUS,
                method=method,
                confidence=0.0,
                reasons=reasons,
                alternatives=[_candidate(item, method) for item in narrowed[:_MAX_CANDIDATES]],
            )
        return None

    def _narrow(
        self, request: ResolutionRequest, matches: list[BrokerInstrument]
    ) -> tuple[list[BrokerInstrument], str | None]:
        exchange = normalize_exchange(request.exchange_hint)
        if exchange and len(matches) > 1:
            by_exchange = [
                item
                for item in matches
                if normalize_exchange(item.exchange) == exchange
                or (item.market_code or "").lower() == exchange
            ]
            if by_exchange:
                matches = by_exchange
                if len(matches) == 1:
                    return matches, f"exchange hint {exchange!r} narrowed to one listing"

        currency = normalize_currency(request.currency_hint)
        if currency and len(matches) > 1:
            by_currency = [item for item in matches if (item.currency or "") == currency]
            if by_currency:
                matches = by_currency
                if len(matches) == 1:
                    return matches, f"currency hint {currency!r} narrowed to one listing"
        return matches, None

    @staticmethod
    def _is_supported(instrument: BrokerInstrument) -> bool:
        return (instrument.instrument_type or "").upper() in SUPPORTED_INSTRUMENT_TYPES

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------
    async def _has_any_metadata(self, broker: Broker) -> bool:
        found = await self._session.execute(
            sa.select(BrokerInstrument.id).where(BrokerInstrument.broker == broker).limit(1)
        )
        return found.scalar_one_or_none() is not None

    async def _by_isin(self, broker: Broker, isin: str) -> list[BrokerInstrument]:
        return list(
            (
                await self._session.execute(
                    sa.select(BrokerInstrument).where(
                        BrokerInstrument.broker == broker,
                        BrokerInstrument.isin == isin,
                        BrokerInstrument.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )

    async def _by_symbol(self, broker: Broker, symbol: str) -> list[BrokerInstrument]:
        return list(
            (
                await self._session.execute(
                    sa.select(BrokerInstrument).where(
                        BrokerInstrument.broker == broker,
                        BrokerInstrument.market_symbol == symbol,
                        BrokerInstrument.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )

    async def _by_name_key(self, broker: Broker, name_key: str) -> list[BrokerInstrument]:
        return list(
            (
                await self._session.execute(
                    sa.select(BrokerInstrument).where(
                        BrokerInstrument.broker == broker,
                        BrokerInstrument.name_key == name_key,
                        BrokerInstrument.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )

    async def _for_company(self, broker: Broker, company: Company) -> list[BrokerInstrument]:
        """Instruments belonging to a known company, by every identity it has."""
        clauses = [BrokerInstrument.company_id == company.id]
        if company.isin:
            clauses.append(BrokerInstrument.isin == normalize_isin(company.isin))
        if company.primary_symbol:
            clauses.append(
                BrokerInstrument.market_symbol == normalize_ticker(company.primary_symbol)
            )
        return list(
            (
                await self._session.execute(
                    sa.select(BrokerInstrument).where(
                        BrokerInstrument.broker == broker,
                        BrokerInstrument.is_active.is_(True),
                        sa.or_(*clauses),
                    )
                )
            )
            .scalars()
            .all()
        )

    async def _isin_from_company(self, request: ResolutionRequest) -> str:
        """Look up an ISIN from a company already known by *ticker*.

        Deliberately not by name.  A company row is created by an earlier
        successful resolution, so a name lookup here would let that resolution
        become the evidence that re-justifies itself: once "Alphabet Inc."
        resolved to the class A line, the company row created for it would keep
        answering "Alphabet Inc." with that ISIN even after a second class
        appeared and the name became genuinely ambiguous.  The name is the field
        ambiguity lives in, so the name-to-company edge is the one to cut; the
        ticker is a discriminator in its own right and rung 3 would use it anyway.
        """
        ticker = normalize_ticker(request.ticker_hint)
        if not ticker:
            return ""
        rows = (
            (
                await self._session.execute(
                    sa.select(Company.isin)
                    .where(Company.isin.is_not(None), Company.primary_symbol == ticker)
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        # Two companies claiming the ticker is not evidence; it is a reason to
        # keep going and let a later rung produce an explicit ambiguity.
        if len(rows) == 1:
            return normalize_isin(rows[0])
        return ""

    async def _company_from_alias(self, request: ResolutionRequest) -> tuple[Company | None, str]:
        """Resolve the hint through the curated alias table.

        Listing-scoped aliases are preferred when the request carries a matching
        exchange or currency, which is how "Alphabet" stays ambiguous while
        "Alphabet class A" does not.  Two authoritative aliases disagreeing is
        impossible by construction: ``uq_company_aliases_authoritative_scope``
        refuses to store the second one.
        """
        keys = {instrument_name_key(request.name_hint)}
        ticker = normalize_ticker(request.ticker_hint)
        if ticker:
            keys.add(ticker.lower())
        keys.discard("")
        if not keys:
            return None, ""

        rows = list(
            (
                await self._session.execute(
                    sa.select(CompanyAlias)
                    .where(
                        CompanyAlias.alias_normalized.in_(sorted(keys)),
                        CompanyAlias.is_authoritative.is_(True),
                    )
                    .order_by(CompanyAlias.confidence.desc())
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return None, ""

        exchange = normalize_exchange(request.exchange_hint)
        currency = normalize_currency(request.currency_hint)
        scoped = [
            row
            for row in rows
            if (row.exchange and normalize_exchange(row.exchange) == exchange)
            or (row.currency and row.currency == currency)
        ]
        chosen = scoped or [row for row in rows if not row.exchange and not row.currency]
        if len(chosen) != 1:
            return None, (f"{len(rows)} curated aliases match the hint; none is uniquely in scope")

        company = await self._session.get(Company, chosen[0].company_id)
        if company is None:  # pragma: no cover - FK cascade keeps these aligned
            return None, ""
        return company, f"curated {chosen[0].alias_type.value} alias {chosen[0].alias!r} applied"

    async def _heuristic_candidates(
        self, request: ResolutionRequest, name_key: str, ticker: str
    ) -> list[BrokerInstrument]:
        """Weak matches, for explaining a refusal.

        These never resolve anything.  A prefix match on a normalised name is
        exactly the kind of evidence that confuses an ADR with its ordinary line,
        so its only job is to put both in front of a human.
        """
        clauses = []
        if name_key:
            clauses.append(BrokerInstrument.name_key.like(f"{name_key}%"))
        if ticker:
            clauses.append(BrokerInstrument.market_symbol.like(f"{ticker}%"))
        if not clauses:
            return []
        return list(
            (
                await self._session.execute(
                    sa.select(BrokerInstrument)
                    .where(
                        BrokerInstrument.broker == request.broker,
                        BrokerInstrument.is_active.is_(True),
                        sa.or_(*clauses),
                    )
                    .order_by(BrokerInstrument.broker_ticker.asc())
                    .limit(_MAX_CANDIDATES)
                )
            )
            .scalars()
            .all()
        )


def _dedupe(items: list[BrokerInstrument]) -> list[BrokerInstrument]:
    seen: set[uuid.UUID] = set()
    unique: list[BrokerInstrument] = []
    for item in items:
        if item.id in seen:
            continue
        seen.add(item.id)
        unique.append(item)
    return unique
