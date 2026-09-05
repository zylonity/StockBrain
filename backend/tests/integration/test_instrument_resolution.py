"""Company -> broker instrument resolution against a real database.

The invariant every test here protects: **an LLM hint is a search key, never an
identity.** Only a ``broker_instruments`` row that Trading 212 supplied may ever
become an executable instrument, and when the evidence fits more than one
listing the correct answer is a refusal that explains itself.

The ambiguity cases are drawn from real classes of confusion -- share classes,
ADRs against ordinary lines, dual listings, reused tickers, renamed companies --
but the resolver has no special case for any of them.  They all reach AMBIGUOUS
by the same route: more than one verified listing fits the evidence supplied.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import (
    BrokerInstrument,
    Company,
    CompanyAlias,
    EventCompanyImpact,
)
from stockbrain.db.models.sources import Event
from stockbrain.db.session import Database
from stockbrain.enums import (
    AliasType,
    Broker,
    EventStatus,
    ImpactDirection,
    ResolutionMethod,
    ResolutionStatus,
)
from stockbrain.instruments.aliases import AliasSpec, upsert_alias
from stockbrain.instruments.normalize import instrument_name_key, normalize_ticker
from stockbrain.instruments.resolver import InstrumentResolver, ResolutionRequest
from stockbrain.instruments.service import ResolutionService

pytestmark = pytest.mark.integration


async def _instrument(
    database: Database,
    ticker: str,
    *,
    name: str,
    short_name: str | None = None,
    isin: str | None = None,
    currency: str = "USD",
    exchange: str | None = "NASDAQ",
    instrument_type: str = "STOCK",
    market_code: str | None = None,
    is_active: bool = True,
) -> uuid.UUID:
    symbol = normalize_ticker(short_name) or normalize_ticker(ticker.split("_")[0])
    parts = ticker.split("_")
    row = BrokerInstrument(
        broker=Broker.TRADING212,
        broker_ticker=ticker,
        name=name,
        short_name=short_name,
        isin=isin,
        currency=currency,
        instrument_type=instrument_type,
        exchange=exchange,
        market_symbol=symbol,
        market_code=market_code
        if market_code is not None
        else (parts[-2] if len(parts) >= 3 else None),
        name_key=instrument_name_key(name),
        is_active=is_active,
        last_refreshed_at=utcnow(),
        last_seen_at=utcnow(),
    )
    async with database.transaction() as session:
        session.add(row)
        await session.flush()
        return row.id


async def _event_with_impact(
    database: Database,
    *,
    name_hint: str,
    ticker_hint: str | None = None,
    exchange_hint: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with database.transaction() as session:
        event = Event(
            title=f"news about {name_hint}",
            status=EventStatus.CLASSIFIED,
            first_seen_at=utcnow(),
            title_hash="x" * 64,
        )
        session.add(event)
        await session.flush()
        impact = EventCompanyImpact(
            event_id=event.id,
            company_name_hint=name_hint,
            company_key=instrument_name_key(name_hint),
            ticker_hint=ticker_hint,
            exchange_hint=exchange_hint,
            direction=ImpactDirection.POSITIVE,
            impact_path="direct",
            materiality_score=0.8,
            confidence=0.9,
        )
        session.add(impact)
        await session.flush()
        return event.id, impact.id


async def _resolve(database: Database, **kwargs: Any) -> Any:
    async with database.session() as session:
        return await InstrumentResolver(session).resolve(ResolutionRequest(**kwargs))


# ---------------------------------------------------------------------------
# Rung 1 -- ISIN
# ---------------------------------------------------------------------------
async def test_isin_resolves_exactly(clean_tables: Database) -> None:
    """The strongest evidence: an ISIN identifies one security globally."""
    instrument_id = await _instrument(
        clean_tables, "AAPL_US_EQ", name="Apple Inc.", short_name="AAPL", isin="US0378331005"
    )
    result = await _resolve(clean_tables, name_hint="Apple", isin_hint="us0378331005")

    assert result.status is ResolutionStatus.RESOLVED
    assert result.method is ResolutionMethod.ISIN_EXACT
    assert result.broker_instrument_id == instrument_id
    assert result.confidence == pytest.approx(0.99)


async def test_one_isin_on_two_venues_is_ambiguous_without_an_exchange(
    clean_tables: Database,
) -> None:
    """A dual listing shares an ISIN. Two venues is a question, not an answer."""
    await _instrument(
        clean_tables, "SHEL_US_EQ", name="Shell plc", short_name="SHEL", isin="GB00BP6MXD84"
    )
    await _instrument(
        clean_tables,
        "SHELl_EQ",
        name="Shell plc",
        short_name="SHEL",
        isin="GB00BP6MXD84",
        currency="GBX",
        exchange="London Stock Exchange",
        market_code="l",
    )
    result = await _resolve(clean_tables, name_hint="Shell", isin_hint="GB00BP6MXD84")

    assert result.status is ResolutionStatus.AMBIGUOUS
    assert len(result.alternatives) == 2
    assert result.broker_instrument_id is None


async def test_an_exchange_hint_narrows_a_dual_listing(clean_tables: Database) -> None:
    us_id = await _instrument(
        clean_tables, "SHEL_US_EQ", name="Shell plc", short_name="SHEL", isin="GB00BP6MXD84"
    )
    await _instrument(
        clean_tables,
        "SHELl_EQ",
        name="Shell plc",
        short_name="SHEL",
        isin="GB00BP6MXD84",
        currency="GBX",
        exchange="London Stock Exchange",
        market_code="l",
    )
    result = await _resolve(
        clean_tables, name_hint="Shell", isin_hint="GB00BP6MXD84", exchange_hint="NASDAQ"
    )
    assert result.status is ResolutionStatus.RESOLVED
    assert result.broker_instrument_id == us_id


# ---------------------------------------------------------------------------
# Rung 2 -- curated aliases
# ---------------------------------------------------------------------------
async def test_a_listing_alias_resolves_a_share_class(clean_tables: Database) -> None:
    """Alphabet's classes are the canonical case for a curated mapping.

    The bare name "Alphabet" deliberately gets *no* alias: it genuinely refers to
    two listings, and an alias for it would be a decision disguised as data.
    """
    googl = await _instrument(
        clean_tables,
        "GOOGL_US_EQ",
        name="Alphabet Inc. Class A",
        short_name="GOOGL",
        isin="US02079K3059",
    )
    await _instrument(
        clean_tables,
        "GOOG_US_EQ",
        name="Alphabet Inc. Class C",
        short_name="GOOG",
        isin="US02079K1079",
    )
    async with clean_tables.transaction() as session:
        company = Company(
            name="Alphabet Inc. Class A",
            name_key=instrument_name_key("Alphabet Inc. Class A"),
            primary_symbol="GOOGL",
            exchange="NASDAQ",
            isin="US02079K3059",
        )
        session.add(company)
        await session.flush()
        await upsert_alias(
            session,
            company,
            AliasSpec(
                alias="Alphabet class A",
                alias_type=AliasType.LISTING,
                exchange="NASDAQ",
                notes="voting shares",
            ),
        )

    result = await _resolve(clean_tables, name_hint="Alphabet class A", exchange_hint="NASDAQ")
    assert result.status is ResolutionStatus.RESOLVED
    assert result.method is ResolutionMethod.MANUAL_ALIAS
    assert result.broker_instrument_id == googl


async def test_a_historical_name_alias_resolves_a_renamed_company(
    clean_tables: Database,
) -> None:
    """A model trained before a rename still says "Facebook"."""
    meta = await _instrument(
        clean_tables,
        "META_US_EQ",
        name="Meta Platforms Inc.",
        short_name="META",
        isin="US30303M1027",
    )
    async with clean_tables.transaction() as session:
        company = Company(
            name="Meta Platforms Inc.",
            name_key=instrument_name_key("Meta Platforms Inc."),
            primary_symbol="META",
            isin="US30303M1027",
        )
        session.add(company)
        await session.flush()
        await upsert_alias(
            session, company, AliasSpec(alias="Facebook", alias_type=AliasType.HISTORICAL)
        )

    result = await _resolve(clean_tables, name_hint="Facebook, Inc.")
    assert result.status is ResolutionStatus.RESOLVED
    assert result.broker_instrument_id == meta


async def test_two_authoritative_aliases_cannot_claim_the_same_scope(
    clean_tables: Database,
) -> None:
    """A contradiction is a curation mistake, refused at the database.

    Last-write-wins here would let one careless row silently redirect a name to
    a different security.
    """
    async with clean_tables.transaction() as session:
        first = Company(name="First Corp", name_key="first")
        second = Company(name="Second Corp", name_key="second")
        session.add_all([first, second])
        await session.flush()
        await upsert_alias(session, first, AliasSpec(alias="Ambiguous Name"))

    async with clean_tables.transaction() as session:
        other = (
            await session.execute(sa.select(Company).where(Company.name == "Second Corp"))
        ).scalar_one()
        with pytest.raises(IntegrityError):
            await upsert_alias(session, other, AliasSpec(alias="Ambiguous Name"))
        # The savepoint rolled back, so the transaction is still usable: the
        # rest of a seeding batch can continue after one bad row.
        surviving = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(CompanyAlias)
                .where(CompanyAlias.alias_normalized == "ambiguous name")
            )
        ).scalar_one()
        assert surviving == 1


async def test_a_non_authoritative_alias_may_coexist(clean_tables: Database) -> None:
    """Only *authoritative* claims are exclusive; a suggestion is not a claim."""
    async with clean_tables.transaction() as session:
        first = Company(name="First Corp", name_key="first")
        second = Company(name="Second Corp", name_key="second")
        session.add_all([first, second])
        await session.flush()
        await upsert_alias(session, first, AliasSpec(alias="Shared Name"))
        await upsert_alias(session, second, AliasSpec(alias="Shared Name", is_authoritative=False))

    async with clean_tables.session() as session:
        count = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(CompanyAlias)
                .where(CompanyAlias.alias_normalized == "shared name")
            )
        ).scalar_one()
    assert count == 2


# ---------------------------------------------------------------------------
# Rung 3 -- ticker + exchange
# ---------------------------------------------------------------------------
async def test_ticker_and_exchange_resolve_when_unique(clean_tables: Database) -> None:
    instrument_id = await _instrument(
        clean_tables, "VRT_US_EQ", name="Vertiv Holdings Co", short_name="VRT", isin="US92537N1081"
    )
    result = await _resolve(clean_tables, name_hint="Vertiv", ticker_hint="vrt")
    assert result.status is ResolutionStatus.RESOLVED
    assert result.method is ResolutionMethod.TICKER_EXCHANGE
    assert result.broker_instrument_id == instrument_id


async def test_a_reused_ticker_on_two_venues_is_ambiguous(clean_tables: Database) -> None:
    """The same three letters mean different companies on different exchanges."""
    await _instrument(
        clean_tables, "ABC_US_EQ", name="Alpha Beta Corp", short_name="ABC", isin="US0000000001"
    )
    await _instrument(
        clean_tables,
        "ABCl_EQ",
        name="Anglo Broadcasting",
        short_name="ABC",
        isin="GB0000000001",
        currency="GBX",
        exchange="London Stock Exchange",
        market_code="l",
    )
    result = await _resolve(clean_tables, name_hint="ABC", ticker_hint="ABC")
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert {c.broker_ticker for c in result.alternatives} == {"ABC_US_EQ", "ABCl_EQ"}


async def test_class_share_tickers_stay_distinct(clean_tables: Database) -> None:
    """``BRK.A`` must never normalise onto ``BRK.B``.

    Collapsing the class separator would map a $700k share onto a $470 one.
    """
    brk_a = await _instrument(
        clean_tables,
        "BRKa_US_EQ",
        name="Berkshire Hathaway Inc. Class A",
        short_name="BRK.A",
        isin="US0846701086",
    )
    await _instrument(
        clean_tables,
        "BRKb_US_EQ",
        name="Berkshire Hathaway Inc. Class B",
        short_name="BRK.B",
        isin="US0846707026",
    )
    result = await _resolve(clean_tables, name_hint="Berkshire Hathaway", ticker_hint="BRK.A")
    assert result.status is ResolutionStatus.RESOLVED
    assert result.broker_instrument_id == brk_a


async def test_the_bare_berkshire_name_is_ambiguous(clean_tables: Database) -> None:
    await _instrument(
        clean_tables,
        "BRKa_US_EQ",
        name="Berkshire Hathaway Inc.",
        short_name="BRK.A",
        isin="US0846701086",
    )
    await _instrument(
        clean_tables,
        "BRKb_US_EQ",
        name="Berkshire Hathaway Inc.",
        short_name="BRK.B",
        isin="US0846707026",
    )
    result = await _resolve(clean_tables, name_hint="Berkshire Hathaway")
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert len(result.alternatives) == 2


# ---------------------------------------------------------------------------
# Rung 4 -- name + exchange + currency
# ---------------------------------------------------------------------------
async def test_punctuation_and_legal_suffixes_do_not_prevent_a_name_match(
    clean_tables: Database,
) -> None:
    instrument_id = await _instrument(
        clean_tables, "VRT_US_EQ", name="Vertiv Holdings Co.", short_name="VRT", isin="US92537N1081"
    )
    result = await _resolve(clean_tables, name_hint="  VERTIV   holdings,  co ")
    assert result.status is ResolutionStatus.RESOLVED
    assert result.broker_instrument_id == instrument_id


async def test_an_adr_and_its_ordinary_line_are_ambiguous(clean_tables: Database) -> None:
    """Different ISINs, different currencies, the same company name."""
    await _instrument(
        clean_tables,
        "BABA_US_EQ",
        name="Alibaba Group",
        short_name="BABA",
        isin="US01609W1027",
    )
    await _instrument(
        clean_tables,
        "9988_HK_EQ",
        name="Alibaba Group",
        short_name="9988",
        isin="KYG017191142",
        currency="HKD",
        exchange="Hong Kong Stock Exchange",
    )
    result = await _resolve(clean_tables, name_hint="Alibaba Group")
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert len(result.alternatives) == 2


async def test_a_currency_hint_narrows_an_adr_ambiguity(clean_tables: Database) -> None:
    adr = await _instrument(
        clean_tables, "BABA_US_EQ", name="Alibaba Group", short_name="BABA", isin="US01609W1027"
    )
    await _instrument(
        clean_tables,
        "9988_HK_EQ",
        name="Alibaba Group",
        short_name="9988",
        isin="KYG017191142",
        currency="HKD",
        exchange="Hong Kong Stock Exchange",
    )
    result = await _resolve(clean_tables, name_hint="Alibaba Group", currency_hint="usd")
    assert result.status is ResolutionStatus.RESOLVED
    assert result.broker_instrument_id == adr


async def test_a_uk_and_a_us_listing_are_ambiguous_without_a_hint(
    clean_tables: Database,
) -> None:
    await _instrument(clean_tables, "BP_US_EQ", name="BP", short_name="BP", isin="US0556221044")
    await _instrument(
        clean_tables,
        "BPl_EQ",
        name="BP",
        short_name="BP.",
        isin="GB0007980591",
        currency="GBX",
        exchange="London Stock Exchange",
        market_code="l",
    )
    result = await _resolve(clean_tables, name_hint="BP")
    assert result.status is ResolutionStatus.AMBIGUOUS


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
async def test_an_unknown_company_is_not_found(clean_tables: Database) -> None:
    await _instrument(
        clean_tables, "AAPL_US_EQ", name="Apple Inc.", short_name="AAPL", isin="US0378331005"
    )
    result = await _resolve(clean_tables, name_hint="Nonexistent Widgets", ticker_hint="ZZZZ")
    assert result.status is ResolutionStatus.NOT_FOUND
    assert result.broker_instrument_id is None


async def test_an_empty_universe_says_so_rather_than_guessing(clean_tables: Database) -> None:
    result = await _resolve(clean_tables, name_hint="Apple", ticker_hint="AAPL")
    assert result.status is ResolutionStatus.NOT_FOUND
    assert any("has been synced" in reason for reason in result.reasons)


async def test_an_unsupported_instrument_type_does_not_resolve_as_equity(
    clean_tables: Database,
) -> None:
    """The risk engine has no model for a warrant; pretending it is a share is worse
    than refusing."""
    await _instrument(
        clean_tables,
        "SPCEW_US_EQ",
        name="Widget Warrants",
        short_name="WIDGW",
        isin="US9999999999",
        instrument_type="WARRANT",
    )
    result = await _resolve(clean_tables, name_hint="Widget Warrants", ticker_hint="WIDGW")
    # UNSUPPORTED, and therefore not RESOLVED: nothing downstream may size it.
    assert result.status is ResolutionStatus.UNSUPPORTED


async def test_an_inactive_instrument_is_never_resolved(clean_tables: Database) -> None:
    """A delisted row is kept for audit, not offered for trading."""
    await _instrument(
        clean_tables,
        "GONE_US_EQ",
        name="Delisted Corp",
        short_name="GONE",
        isin="US1111111111",
        is_active=False,
    )
    await _instrument(
        clean_tables, "AAPL_US_EQ", name="Apple Inc.", short_name="AAPL", isin="US0378331005"
    )
    result = await _resolve(clean_tables, name_hint="Delisted Corp", ticker_hint="GONE")
    assert result.status is ResolutionStatus.NOT_FOUND


async def test_an_llm_ticker_hint_can_never_become_an_instrument(
    clean_tables: Database,
) -> None:
    """The invariant of the whole phase.

    A hint that matches nothing in synced broker metadata must produce a refusal.
    It must not create an instrument, and it must not appear as an executable
    identity anywhere.
    """
    await _instrument(
        clean_tables, "AAPL_US_EQ", name="Apple Inc.", short_name="AAPL", isin="US0378331005"
    )
    _event_id, impact_id = await _event_with_impact(
        clean_tables,
        name_hint="Totally Made Up Holdings",
        ticker_hint="FAKE",
        exchange_hint="NASDAQ",
    )
    await ResolutionService(clean_tables).resolve_impact(impact_id)

    async with clean_tables.session() as session:
        impact = await session.get(EventCompanyImpact, impact_id)
        assert impact is not None
        assert impact.resolution_status is ResolutionStatus.NOT_FOUND
        assert impact.broker_instrument_id is None
        instruments = (
            (await session.execute(sa.select(BrokerInstrument.broker_ticker))).scalars().all()
        )
    assert list(instruments) == ["AAPL_US_EQ"], "no instrument may be created from a hint"


# ---------------------------------------------------------------------------
# The service: persistence, idempotency, concurrency
# ---------------------------------------------------------------------------
async def test_resolution_persists_the_verdict_and_links_a_company(
    clean_tables: Database,
) -> None:
    instrument_id = await _instrument(
        clean_tables, "AAPL_US_EQ", name="Apple Inc.", short_name="AAPL", isin="US0378331005"
    )
    event_id, impact_id = await _event_with_impact(
        clean_tables, name_hint="Apple Inc.", ticker_hint="AAPL", exchange_hint="NASDAQ"
    )
    result = await ResolutionService(clean_tables).resolve_event(event_id)
    assert result.resolved == 1

    async with clean_tables.session() as session:
        impact = await session.get(EventCompanyImpact, impact_id)
        assert impact is not None
        assert impact.resolution_status is ResolutionStatus.RESOLVED
        assert impact.broker_instrument_id == instrument_id
        assert impact.company_id is not None
        assert impact.resolution_confidence is not None
        assert impact.resolved_at is not None

        company = await session.get(Company, impact.company_id)
        assert company is not None
        assert company.isin == "US0378331005"


async def test_a_company_is_only_ever_created_from_an_isin(clean_tables: Database) -> None:
    """ISIN is the one globally unique identifier, and a unique index on it is
    what makes "no duplicate companies" a guarantee rather than a hope.  An
    instrument without one resolves, but stays company-less."""
    await _instrument(
        clean_tables, "NOISIN_US_EQ", name="No Isin Corp", short_name="NOISIN", isin=None
    )
    event_id, impact_id = await _event_with_impact(
        clean_tables, name_hint="No Isin Corp", ticker_hint="NOISIN"
    )
    await ResolutionService(clean_tables).resolve_event(event_id)

    async with clean_tables.session() as session:
        impact = await session.get(EventCompanyImpact, impact_id)
        assert impact is not None
        assert impact.resolution_status is ResolutionStatus.RESOLVED
        assert impact.company_id is None
        companies = (
            await session.execute(sa.select(sa.func.count()).select_from(Company))
        ).scalar_one()
    assert companies == 0


async def test_running_the_same_resolution_twice_changes_nothing(
    clean_tables: Database,
) -> None:
    """At-least-once delivery guarantees this job runs twice eventually."""
    await _instrument(
        clean_tables, "AAPL_US_EQ", name="Apple Inc.", short_name="AAPL", isin="US0378331005"
    )
    event_id, impact_id = await _event_with_impact(
        clean_tables, name_hint="Apple Inc.", ticker_hint="AAPL"
    )
    service = ResolutionService(clean_tables)
    await service.resolve_event(event_id)
    async with clean_tables.session() as session:
        impact = await session.get(EventCompanyImpact, impact_id)
        assert impact is not None
        first_instrument = impact.broker_instrument_id
        first_company = impact.company_id

    await service.resolve_event(event_id)
    async with clean_tables.session() as session:
        impact = await session.get(EventCompanyImpact, impact_id)
        assert impact is not None
        assert impact.broker_instrument_id == first_instrument
        assert impact.company_id == first_company
        impacts = (
            await session.execute(sa.select(sa.func.count()).select_from(EventCompanyImpact))
        ).scalar_one()
        companies = (
            await session.execute(sa.select(sa.func.count()).select_from(Company))
        ).scalar_one()
    assert impacts == 1
    assert companies == 1, "a second run must not create a second company"


async def test_concurrent_resolution_of_the_same_event_is_safe(
    clean_tables: Database,
) -> None:
    """Two workers claiming the same redelivered job must not race.

    The impact row is locked FOR UPDATE, and the company upsert is
    ``ON CONFLICT (isin)``, so the loser writes the identical verdict rather
    than a duplicate.
    """
    await _instrument(
        clean_tables, "AAPL_US_EQ", name="Apple Inc.", short_name="AAPL", isin="US0378331005"
    )
    event_id, impact_id = await _event_with_impact(
        clean_tables, name_hint="Apple Inc.", ticker_hint="AAPL"
    )
    service = ResolutionService(clean_tables)
    await asyncio.gather(*(service.resolve_event(event_id) for _ in range(4)))

    async with clean_tables.session() as session:
        companies = (
            await session.execute(sa.select(sa.func.count()).select_from(Company))
        ).scalar_one()
        impact = await session.get(EventCompanyImpact, impact_id)
    assert companies == 1
    assert impact is not None
    assert impact.resolution_status is ResolutionStatus.RESOLVED


async def test_an_ambiguous_result_records_its_alternatives_and_blocks(
    clean_tables: Database,
) -> None:
    """A refusal a human cannot inspect is indistinguishable from a bug."""
    await _instrument(
        clean_tables,
        "GOOGL_US_EQ",
        name="Alphabet Inc.",
        short_name="GOOGL",
        isin="US02079K3059",
    )
    await _instrument(
        clean_tables, "GOOG_US_EQ", name="Alphabet Inc.", short_name="GOOG", isin="US02079K1079"
    )
    event_id, impact_id = await _event_with_impact(clean_tables, name_hint="Alphabet")
    await ResolutionService(clean_tables).resolve_event(event_id)

    async with clean_tables.session() as session:
        impact = await session.get(EventCompanyImpact, impact_id)
    assert impact is not None
    assert impact.resolution_status is ResolutionStatus.AMBIGUOUS
    assert impact.broker_instrument_id is None
    assert impact.company_id is None
    assert len(impact.resolution_alternatives) == 2
    assert {alt["broker_ticker"] for alt in impact.resolution_alternatives} == {
        "GOOGL_US_EQ",
        "GOOG_US_EQ",
    }
    assert impact.resolution_notes


async def test_a_previously_resolved_impact_is_cleared_when_it_becomes_ambiguous(
    clean_tables: Database,
) -> None:
    """A newly synced second listing must un-resolve, not silently keep the old pick."""
    await _instrument(
        clean_tables, "GOOGL_US_EQ", name="Alphabet Inc.", short_name="GOOGL", isin="US02079K3059"
    )
    event_id, impact_id = await _event_with_impact(clean_tables, name_hint="Alphabet Inc.")
    service = ResolutionService(clean_tables)
    await service.resolve_event(event_id)
    async with clean_tables.session() as session:
        impact = await session.get(EventCompanyImpact, impact_id)
        assert impact is not None and impact.resolution_status is ResolutionStatus.RESOLVED

    await _instrument(
        clean_tables, "GOOG_US_EQ", name="Alphabet Inc.", short_name="GOOG", isin="US02079K1079"
    )
    await service.resolve_event(event_id)

    async with clean_tables.session() as session:
        impact = await session.get(EventCompanyImpact, impact_id)
    assert impact is not None
    assert impact.resolution_status is ResolutionStatus.AMBIGUOUS
    assert impact.broker_instrument_id is None


async def test_impacts_start_pending_so_the_sweeper_can_find_them(
    clean_tables: Database,
) -> None:
    _event_id, impact_id = await _event_with_impact(clean_tables, name_hint="Apple")
    async with clean_tables.session() as session:
        impact = await session.get(EventCompanyImpact, impact_id)
    assert impact is not None
    assert impact.resolution_status is ResolutionStatus.PENDING
    assert impact.resolution_alternatives == []


async def test_normalisation_keeps_share_classes_apart() -> None:
    """The normaliser must not be the thing that merges two securities."""
    assert instrument_name_key("Berkshire Hathaway Inc. Class A") != instrument_name_key(
        "Berkshire Hathaway Inc. Class B"
    )
    assert normalize_ticker("brk.a") == "BRK.A"
    assert normalize_ticker("BRK-B") == "BRK-B"
    assert normalize_ticker("brk.a") != normalize_ticker("BRK")
    # Legal suffixes and punctuation do collapse -- that is the point.
    assert instrument_name_key("Vertiv Holdings Co.") == instrument_name_key("VERTIV holdings")


def test_utcnow_is_timezone_aware() -> None:
    assert utcnow().tzinfo is dt.UTC
