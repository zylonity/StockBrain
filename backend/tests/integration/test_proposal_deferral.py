"""A thesis blocked only by a closed market waits; one blocked by judgment does not.

The distinction the deferral rests on is between a refusal that says something
about *this moment* -- a stale quote, a shut session, an account snapshot that
has not been taken -- and one that says something about the trade.  Only the
first is worth retrying: the market reopens, a quote refreshes, and the thesis
is judged again.  A confidence below the floor or a currency that cannot be
reconciled will not fix itself, and retrying it forever would be a loop wearing
a deferral's name.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from stockbrain.db.models.proposals import RiskEvaluation
from stockbrain.db.models.research import Thesis
from stockbrain.db.models.system import Job
from stockbrain.db.session import Database
from stockbrain.enums import ThesisAction
from stockbrain.telegram.preferences import NotificationPreferences
from tests import proposal_helpers as ph

pytestmark = pytest.mark.integration


async def test_a_closed_market_block_is_recorded_as_deferred(clean_tables: Database) -> None:
    await ph.seed(clean_tables, action=ThesisAction.BUY, confidence=0.9)
    # Funded, so the only rule refusing the trade is the stale market:
    # ``currency_alignment`` is a judgment rule and would otherwise block too.
    await ph.fund(clean_tables)
    service = ph.service(clean_tables, market_data=ph.StubMarketData(age_ms=60_000))
    result = await service.generate(ph.THESIS_ID)
    assert not result.created
    async with clean_tables.session() as session:
        evaluation = (await session.execute(sa.select(RiskEvaluation))).scalar_one()
    assert evaluation.deferred is True


async def test_a_deferred_thesis_is_retried_after_the_ttl_and_not_before(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables, action=ThesisAction.BUY, confidence=0.9)
    await ph.fund(clean_tables)
    service = ph.service(clean_tables, market_data=ph.StubMarketData(age_ms=60_000))
    await service.generate(ph.THESIS_ID)
    assert await service.enqueue_pending() == 0  # just deferred: not yet
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(RiskEvaluation).values(
                created_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=31)
            )
        )
    assert await service.enqueue_pending() == 1  # TTL (30 min) elapsed: retried


async def test_a_judgment_block_is_terminal(clean_tables: Database) -> None:
    await ph.seed(
        clean_tables, action=ThesisAction.BUY, confidence=0.10
    )  # below the confidence floor
    await ph.fund(clean_tables)
    service = ph.service(clean_tables, market_data=ph.StubMarketData(age_ms=60_000))
    await service.generate(ph.THESIS_ID)
    async with clean_tables.session() as session:
        evaluation = (await session.execute(sa.select(RiskEvaluation))).scalar_one()
    assert evaluation.deferred is False
    assert await service.enqueue_pending() == 0


async def test_deferral_gives_up_after_the_age_limit(clean_tables: Database) -> None:
    await ph.seed(clean_tables, action=ThesisAction.BUY, confidence=0.9)
    await ph.fund(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(Thesis).values(created_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=73))
        )
    service = ph.service(clean_tables, market_data=ph.StubMarketData(age_ms=60_000))
    await service.generate(ph.THESIS_ID)
    async with clean_tables.session() as session:
        evaluation = (await session.execute(sa.select(RiskEvaluation))).scalar_one()
    assert evaluation.deferred is False


async def test_a_deferred_block_announces_deferred_and_not_blocked(
    clean_tables: Database,
) -> None:
    """One message per thing: the wait is announced, not the refusal it is not."""
    await ph.seed(clean_tables, action=ThesisAction.BUY, confidence=0.9)
    await ph.fund(clean_tables)
    service = ph.service(
        clean_tables,
        market_data=ph.StubMarketData(age_ms=60_000),
        preferences=NotificationPreferences(clean_tables),
    )
    await service.generate(ph.THESIS_ID)
    async with clean_tables.session() as session:
        jobs = list(
            (
                await session.execute(sa.select(Job).where(Job.job_type == "SEND_NOTIFICATION"))
            ).scalars()
        )
    assert [job.payload["pipeline_event"] for job in jobs] == ["PROPOSAL_DEFERRED"]
    assert jobs[0].payload["entity_id"] == str(ph.RUN_ID)


async def test_a_terminal_block_announces_blocked_and_not_deferred(
    clean_tables: Database,
) -> None:
    await ph.seed(clean_tables, action=ThesisAction.BUY, confidence=0.10)
    await ph.fund(clean_tables)
    service = ph.service(
        clean_tables,
        market_data=ph.StubMarketData(age_ms=60_000),
        preferences=NotificationPreferences(clean_tables),
    )
    await service.generate(ph.THESIS_ID)
    async with clean_tables.session() as session:
        jobs = list(
            (
                await session.execute(sa.select(Job).where(Job.job_type == "SEND_NOTIFICATION"))
            ).scalars()
        )
    assert [job.payload["pipeline_event"] for job in jobs] == ["PROPOSAL_BLOCKED"]
