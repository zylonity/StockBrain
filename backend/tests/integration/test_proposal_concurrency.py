"""Races, and the database guarantees that decide them.

Every mechanism exercised here lives in PostgreSQL, because the guarantee has to
hold across worker tasks, across processes and across a restart -- and an
``asyncio.Lock`` holds across none of those. Each test names the race it settles
and asserts the *count*, because "one winner" is the whole property.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa

from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument, Company, EventCompanyImpact
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.models.research import ResearchRun, Thesis
from stockbrain.db.session import Database
from stockbrain.enums import (
    AuthorizationSource,
    Broker,
    ImpactDirection,
    ProposalStatus,
    ResearchStatus,
    ResolutionStatus,
    ThesisAction,
    TimeHorizon,
)
from stockbrain.errors import ProposalAlreadyConsumed, ProposalExpired
from tests import proposal_helpers as ph

pytestmark = pytest.mark.integration

SECOND_INSTRUMENT = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
SECOND_IMPACT = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000004")
SECOND_RUN = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000005")
SECOND_THESIS = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000006")
SECOND_COMPANY = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")


async def _count(database: Database, **filters: object) -> int:
    query = sa.select(sa.func.count()).select_from(TradeProposal)
    if "status" in filters:
        query = query.where(TradeProposal.status == filters["status"])
    async with database.session() as session:
        return int((await session.execute(query)).scalar_one())


async def _seed_second_listing(database: Database) -> None:
    """A second, independent thesis on a different listing.

    Needed to prove that the exposure lock serialises trades on *different*
    instruments too: the one-live-proposal-per-listing index would otherwise
    hide the cash race behind a uniqueness violation.
    """
    async with database.transaction() as session:
        session.add(Company(id=SECOND_COMPANY, name="Microsoft", isin="US5949181045"))
        await session.flush()
        session.add(
            BrokerInstrument(
                id=SECOND_INSTRUMENT,
                company_id=SECOND_COMPANY,
                broker=Broker.TRADING212,
                broker_ticker="MSFT_US_EQ",
                market_symbol="MSFT",
                market_code="US",
                name="Microsoft",
                exchange="NASDAQ",
                currency="USD",
                isin="US5949181045",
                instrument_type="STOCK",
                max_open_quantity=Decimal("55000"),
            )
        )
        await session.flush()
        session.add(
            EventCompanyImpact(
                id=SECOND_IMPACT,
                event_id=ph.EVENT_ID,
                company_id=SECOND_COMPANY,
                company_name_hint="Microsoft",
                company_key="microsoft",
                direction=ImpactDirection.POSITIVE,
                materiality_score=0.8,
                confidence=0.85,
                impact_path="direct",
                broker_instrument_id=SECOND_INSTRUMENT,
                resolution_status=ResolutionStatus.RESOLVED,
            )
        )
        session.add(
            ResearchRun(
                id=SECOND_RUN,
                event_id=ph.EVENT_ID,
                company_id=SECOND_COMPANY,
                impact_id=SECOND_IMPACT,
                broker_instrument_id=SECOND_INSTRUMENT,
                status=ResearchStatus.SUCCEEDED,
                completed_at=utcnow(),
            )
        )
        await session.flush()
        session.add(
            Thesis(
                id=SECOND_THESIS,
                research_run_id=SECOND_RUN,
                action=ThesisAction.BUY,
                confidence=0.9,
                time_horizon=TimeHorizon.DAYS,
            )
        )


# ---------------------------------------------------------------------------
# Duplicate generation
# ---------------------------------------------------------------------------
async def test_a_redelivered_generation_job_creates_exactly_one_proposal(
    clean_tables: Database,
) -> None:
    """At-least-once delivery guarantees this happens eventually."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)

    first = await service.generate(ph.THESIS_ID)
    second = await service.generate(ph.THESIS_ID)

    assert first.created and not second.created
    assert "already exist" in second.reason
    assert await _count(clean_tables) == 1


async def test_concurrent_generation_of_the_same_thesis_creates_one_proposal(
    clean_tables: Database,
) -> None:
    """Two workers claim the same job; the database decides, not a check."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    services = [ph.service(clean_tables) for _ in range(4)]

    results = await asyncio.gather(
        *(service.generate(ph.THESIS_ID) for service in services), return_exceptions=True
    )
    created = [item for item in results if getattr(item, "created", False)]
    assert len(created) == 1, results
    assert await _count(clean_tables) == 1


async def test_two_theses_on_the_same_listing_produce_one_live_proposal(
    clean_tables: Database,
) -> None:
    """``uq_trade_proposals_active_instrument``, exercised for real."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    async with clean_tables.transaction() as session:
        session.add(
            Thesis(
                id=SECOND_THESIS,
                research_run_id=ph.RUN_ID,
                action=ThesisAction.BUY,
                confidence=0.95,
                time_horizon=TimeHorizon.DAYS,
            )
        )
    service = ph.service(clean_tables)

    first = await service.generate(ph.THESIS_ID)
    second = await service.generate(SECOND_THESIS)
    assert first.created
    assert not second.created
    assert await _count(clean_tables) == 1


async def test_concurrent_generation_on_different_listings_respects_one_cash_budget(
    clean_tables: Database,
) -> None:
    """The race the advisory lock exists for.

    Two proposals on *different* instruments, generated at the same instant,
    against a cash balance that only funds one. Without the lock both read the
    same headroom and both are created; with it the second sees the first's
    reservation.
    """
    await ph.seed(clean_tables)
    await _seed_second_listing(clean_tables)
    # 1,000 total, 10% reserve, so ~900 spendable -- one 800-notional trade fits.
    await ph.fund(clean_tables, cash=Decimal("1000"), total=Decimal("1000"), invested=Decimal("0"))
    services = [
        ph.service(
            clean_tables,
            risk_max_notional_per_trade=Decimal("800"),
            risk_max_trade_pct=Decimal("1"),
            risk_max_position_pct=Decimal("1"),
            risk_max_aggregate_exposure_pct=Decimal("1"),
            risk_max_active_proposal_exposure_pct=Decimal("1"),
            risk_min_research_confidence=Decimal("0.5"),
            risk_confidence_modulates_size=False,
        )
        for _ in range(2)
    ]
    results = await asyncio.gather(
        services[0].generate(ph.THESIS_ID),
        services[1].generate(SECOND_THESIS),
        return_exceptions=True,
    )
    assert not any(isinstance(item, BaseException) for item in results), results

    async with clean_tables.session() as session:
        committed = Decimal(
            (
                await session.execute(
                    sa.select(sa.func.coalesce(sa.func.sum(TradeProposal.estimated_notional), 0))
                )
            ).scalar_one()
        )
    # Without the advisory lock both evaluations read the same 900 of headroom
    # and each commits its full 800, for ~1,600 against a 1,000 account. The
    # lock is what makes this assertion possible to state at all.
    assert committed <= Decimal("900"), f"the pair committed {committed} against a 900 buffer"
    assert committed > Decimal("0")


# ---------------------------------------------------------------------------
# Authorization races
# ---------------------------------------------------------------------------
async def test_two_browser_tabs_approving_produce_one_winner(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    result = await service.generate(ph.THESIS_ID)
    assert result.proposal_id is not None

    outcomes = await asyncio.gather(
        *(
            ph.service(clean_tables).authorize(
                result.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor=f"tab-{index}"
            )
            for index in range(4)
        ),
        return_exceptions=True,
    )
    winners = [item for item in outcomes if not isinstance(item, BaseException)]
    losers = [item for item in outcomes if isinstance(item, ProposalAlreadyConsumed)]
    assert len(winners) == 1
    assert len(losers) == 3
    assert await _count(clean_tables, status=ProposalStatus.APPROVED) == 1


async def test_approve_racing_reject_leaves_exactly_one_terminal_answer(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    result = await service.generate(ph.THESIS_ID)
    assert result.proposal_id is not None

    outcomes = await asyncio.gather(
        ph.service(clean_tables).authorize(
            result.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="approver"
        ),
        ph.service(clean_tables).reject(result.proposal_id, actor="rejecter"),
        return_exceptions=True,
    )
    succeeded = [item for item in outcomes if not isinstance(item, BaseException)]
    assert len(succeeded) >= 1
    async with clean_tables.session() as session:
        status = (await session.execute(sa.select(TradeProposal.status))).scalar_one()
    assert status in {ProposalStatus.APPROVED, ProposalStatus.REJECTED}


async def test_two_automatic_authorizations_of_one_proposal_produce_one_winner(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    generator = ph.service(clean_tables, execution_policy="automatic")
    result = await generator.generate(ph.THESIS_ID)
    assert result.proposal_id is not None and result.authorized

    # A second automatic pass over an already-authorized proposal must refuse.
    with pytest.raises(ProposalAlreadyConsumed):
        await ph.service(clean_tables, execution_policy="automatic").authorize(
            result.proposal_id,
            source=AuthorizationSource.SYSTEM_AUTOMATIC,
            actor="system:automatic",
        )
    assert await _count(clean_tables, status=ProposalStatus.APPROVED) == 1


async def test_expiry_racing_authorization_never_authorizes_an_expired_proposal(
    clean_tables: Database,
) -> None:
    """Whoever wins, an expired proposal is never approved."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    result = await service.generate(ph.THESIS_ID)
    assert result.proposal_id is not None
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(TradeProposal).values(expires_at=utcnow() - dt.timedelta(seconds=1))
        )

    outcomes = await asyncio.gather(
        service.authorize(
            result.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:operator"
        ),
        ph.service(clean_tables).sweep(),
        return_exceptions=True,
    )
    assert any(isinstance(item, ProposalExpired) for item in outcomes)
    async with clean_tables.session() as session:
        status = (await session.execute(sa.select(TradeProposal.status))).scalar_one()
    assert status is not ProposalStatus.APPROVED


async def test_an_authorized_proposal_is_not_retracted_by_the_expiry_sweep(
    clean_tables: Database,
) -> None:
    """Otherwise "approve then expire" would depend on scheduler timing."""
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    result = await service.generate(ph.THESIS_ID)
    assert result.proposal_id is not None
    await service.authorize(
        result.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:operator"
    )
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(TradeProposal).values(expires_at=utcnow() - dt.timedelta(seconds=1))
        )
    counts = await service.sweep()
    assert counts["expired"] == 0
    async with clean_tables.session() as session:
        status = (await session.execute(sa.select(TradeProposal.status))).scalar_one()
    assert status is ProposalStatus.APPROVED


async def test_an_authorization_racing_an_invalidation_sweep_does_not_double_spend(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables)
    result = await service.generate(ph.THESIS_ID)
    assert result.proposal_id is not None

    async with clean_tables.transaction() as session:
        await session.execute(sa.update(BrokerInstrument).values(is_active=False))

    outcomes = await asyncio.gather(
        service.authorize(
            result.proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:operator"
        ),
        ph.service(clean_tables).sweep(),
        return_exceptions=True,
    )
    async with clean_tables.session() as session:
        status = (await session.execute(sa.select(TradeProposal.status))).scalar_one()
    assert status is ProposalStatus.INVALIDATED
    assert not any(
        getattr(item, "source", None) is AuthorizationSource.HUMAN_WEB for item in outcomes
    )


# ---------------------------------------------------------------------------
# Exposure reservation
# ---------------------------------------------------------------------------
async def test_a_live_proposal_reserves_its_notional_for_the_next_one(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables)
    await _seed_second_listing(clean_tables)
    # 700 of cash less a 10% reserve leaves 630 spendable -- enough for exactly
    # one three-share trade, and nothing at all afterwards.
    await ph.fund(clean_tables, cash=Decimal("700"), total=Decimal("700"), invested=Decimal("0"))
    service = ph.service(
        clean_tables,
        risk_max_notional_per_trade=Decimal("800"),
        risk_max_trade_pct=Decimal("1"),
        risk_max_position_pct=Decimal("1"),
        risk_max_aggregate_exposure_pct=Decimal("1"),
        risk_max_active_proposal_exposure_pct=Decimal("1"),
        risk_confidence_modulates_size=False,
    )
    first = await service.generate(ph.THESIS_ID)
    assert first.created

    second = await service.generate(SECOND_THESIS)
    assert not second.created, "the first proposal's notional is already spoken for"
    assert await _count(clean_tables) == 1


async def test_a_reservation_shrinks_the_next_proposal_when_headroom_remains(
    clean_tables: Database,
) -> None:
    """Reserving is not all-or-nothing: what is left is still usable."""
    await ph.seed(clean_tables)
    await _seed_second_listing(clean_tables)
    await ph.fund(clean_tables, cash=Decimal("1000"), total=Decimal("1000"), invested=Decimal("0"))
    service = ph.service(
        clean_tables,
        risk_max_notional_per_trade=Decimal("800"),
        risk_max_trade_pct=Decimal("1"),
        risk_max_position_pct=Decimal("1"),
        risk_max_aggregate_exposure_pct=Decimal("1"),
        risk_max_active_proposal_exposure_pct=Decimal("1"),
        risk_confidence_modulates_size=False,
    )
    first = await service.generate(ph.THESIS_ID)
    second = await service.generate(SECOND_THESIS)
    assert first.created and second.created

    async with clean_tables.session() as session:
        rows = {
            row.broker_ticker: row.proposed_quantity
            for row in (
                await session.execute(sa.select(TradeProposal).order_by(TradeProposal.created_at))
            ).scalars()
        }
        committed = Decimal(
            (
                await session.execute(
                    sa.select(sa.func.coalesce(sa.func.sum(TradeProposal.estimated_notional), 0))
                )
            ).scalar_one()
        )
    assert rows["MSFT_US_EQ"] < rows["AAPL_US_EQ"], "the second saw the first's reservation"
    assert committed <= Decimal("900")


async def test_a_terminal_proposal_releases_its_reservation(clean_tables: Database) -> None:
    await ph.seed(clean_tables)
    await _seed_second_listing(clean_tables)
    await ph.fund(clean_tables, cash=Decimal("1000"), total=Decimal("1000"), invested=Decimal("0"))
    overrides: dict[str, Any] = {
        "risk_max_notional_per_trade": Decimal("800"),
        "risk_max_trade_pct": Decimal("1"),
        "risk_max_position_pct": Decimal("1"),
        "risk_max_aggregate_exposure_pct": Decimal("1"),
        "risk_max_active_proposal_exposure_pct": Decimal("1"),
        "risk_confidence_modulates_size": False,
    }
    service = ph.service(clean_tables, **overrides)
    first = await service.generate(ph.THESIS_ID)
    assert first.created and first.proposal_id is not None

    await service.reject(first.proposal_id, actor="web:operator")
    second = await service.generate(SECOND_THESIS)
    assert second.created, "a rejected proposal must not keep holding the cash"


async def test_a_pending_sell_does_not_reserve_cash(clean_tables: Database) -> None:
    """A sell frees cash; counting it as reserved would shrink the very budget
    it is about to enlarge."""
    await ph.seed(clean_tables, action=ThesisAction.SELL)
    await _seed_second_listing(clean_tables)
    await ph.fund(
        clean_tables,
        cash=Decimal("1000"),
        total=Decimal("1000"),
        invested=Decimal("0"),
        positions={"AAPL_US_EQ": (Decimal("4"), Decimal("4"))},
    )
    overrides: dict[str, Any] = {
        "risk_max_notional_per_trade": Decimal("800"),
        "risk_max_trade_pct": Decimal("1"),
        "risk_max_position_pct": Decimal("1"),
        "risk_max_aggregate_exposure_pct": Decimal("1"),
        "risk_max_active_proposal_exposure_pct": Decimal("1"),
        "risk_confidence_modulates_size": False,
    }
    service = ph.service(clean_tables, **overrides)
    sell = await service.generate(ph.THESIS_ID)
    assert sell.created

    buy = await service.generate(SECOND_THESIS)
    assert buy.created, "the pending sell must not have reserved the buy's cash"
