"""Budget enforcement against real recorded spend.

Spend is summed from ``llm_calls`` rather than a running counter, so these tests
drive the real aggregation query with real rows: that is what production does,
and it is what must survive a restart without reconciliation.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from stockbrain.db.models.research import LlmCall
from stockbrain.db.session import Database
from stockbrain.llm.budget import BudgetGuard, BudgetStatus, WorkPriority
from stockbrain.llm.telemetry import LlmTelemetry

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)


def _guard(database: Database, **overrides: Decimal) -> BudgetGuard:
    limits: dict[str, Decimal] = {
        "daily_soft_usd": Decimal("2.00"),
        "daily_hard_usd": Decimal("5.00"),
        "monthly_soft_usd": Decimal("30.00"),
        "monthly_hard_usd": Decimal("75.00"),
    }
    limits.update(overrides)
    return BudgetGuard(database, cache_seconds=0.0, **limits)  # type: ignore[arg-type]


async def _spend(database: Database, amount: str, *, at: dt.datetime) -> None:
    async with database.transaction() as session:
        session.add(
            LlmCall(
                created_at=at,
                purpose="CLASSIFY_EVENT",
                provider="deepseek",
                model="deepseek-v4-flash",
                succeeded=True,
                used=True,
                estimated_cost_usd=Decimal(amount),
            )
        )


async def test_no_spend_is_under_budget(clean_llm_calls: Database) -> None:
    state = await _guard(clean_llm_calls).state(now=NOW, refresh=True)
    assert state.status is BudgetStatus.OK
    assert state.daily_spend == Decimal("0")
    assert state.degraded is False


async def test_spend_below_the_soft_limit_is_ok(clean_llm_calls: Database) -> None:
    await _spend(clean_llm_calls, "1.99", at=NOW)
    state = await _guard(clean_llm_calls).state(now=NOW, refresh=True)
    assert state.status is BudgetStatus.OK
    assert state.daily_spend == Decimal("1.99")


async def test_the_soft_limit_is_inclusive(clean_llm_calls: Database) -> None:
    await _spend(clean_llm_calls, "2.00", at=NOW)
    state = await _guard(clean_llm_calls).state(now=NOW, refresh=True)
    assert state.status is BudgetStatus.SOFT_EXCEEDED
    assert state.reason is not None and "soft limit" in state.reason


async def test_the_hard_limit_is_inclusive(clean_llm_calls: Database) -> None:
    await _spend(clean_llm_calls, "5.00", at=NOW)
    state = await _guard(clean_llm_calls).state(now=NOW, refresh=True)
    assert state.status is BudgetStatus.HARD_EXCEEDED
    assert state.reason is not None and "hard limit" in state.reason


async def test_the_monthly_limit_applies_independently(clean_llm_calls: Database) -> None:
    """A small daily spend can still breach a month's budget."""
    for day in range(1, 15):
        await _spend(clean_llm_calls, "2.20", at=NOW.replace(day=day, hour=3))
    await _spend(clean_llm_calls, "0.10", at=NOW)

    state = await _guard(clean_llm_calls).state(now=NOW, refresh=True)
    assert state.daily_spend == Decimal("0.10")
    assert state.monthly_spend > Decimal("30")
    assert state.status is BudgetStatus.SOFT_EXCEEDED


async def test_yesterdays_spend_does_not_count_against_today(
    clean_llm_calls: Database,
) -> None:
    await _spend(clean_llm_calls, "4.99", at=NOW - dt.timedelta(days=1))
    state = await _guard(clean_llm_calls).state(now=NOW, refresh=True)
    assert state.daily_spend == Decimal("0")
    assert state.status is BudgetStatus.OK


async def test_soft_limit_suppresses_optional_work_only(clean_llm_calls: Database) -> None:
    """Semantic deduplication is quality; classification is not."""
    await _spend(clean_llm_calls, "2.50", at=NOW)
    guard = _guard(clean_llm_calls)

    essential = await guard.check(WorkPriority.ESSENTIAL, now=NOW)
    optional = await guard.check(WorkPriority.OPTIONAL, now=NOW)

    assert essential.allowed is True
    assert optional.allowed is False
    assert optional.reason is not None and "optional work suppressed" in optional.reason


async def test_hard_limit_stops_every_new_llm_call(clean_llm_calls: Database) -> None:
    await _spend(clean_llm_calls, "5.50", at=NOW)
    guard = _guard(clean_llm_calls)
    assert (await guard.check(WorkPriority.ESSENTIAL, now=NOW)).allowed is False
    assert (await guard.check(WorkPriority.OPTIONAL, now=NOW)).allowed is False


async def test_state_is_cached_then_invalidated(clean_llm_calls: Database) -> None:
    """A short cache keeps the guard cheap; spending invalidates it immediately."""
    guard = BudgetGuard(
        clean_llm_calls,
        daily_soft_usd=Decimal("2.00"),
        daily_hard_usd=Decimal("5.00"),
        monthly_soft_usd=Decimal("30.00"),
        monthly_hard_usd=Decimal("75.00"),
        cache_seconds=3600.0,
    )
    assert (await guard.state(now=NOW)).status is BudgetStatus.OK

    await _spend(clean_llm_calls, "9.00", at=NOW)
    # Still cached, so the stale answer stands...
    assert (await guard.state(now=NOW)).status is BudgetStatus.OK
    # ...until the caller that spent the money says so.
    guard.invalidate()
    assert (await guard.state(now=NOW)).status is BudgetStatus.HARD_EXCEEDED


async def test_spend_aggregation_ignores_unpriced_calls(clean_llm_calls: Database) -> None:
    """A call with no cost estimate contributes nothing rather than crashing."""
    async with clean_llm_calls.transaction() as session:
        session.add(
            LlmCall(
                created_at=NOW,
                purpose="CLASSIFY_EVENT",
                provider="deepseek",
                model="unknown-model",
                succeeded=True,
                estimated_cost_usd=None,
            )
        )
    async with clean_llm_calls.session() as session:
        total = await LlmTelemetry().spend_since(session, NOW - dt.timedelta(days=1))
    assert total == Decimal("0")
