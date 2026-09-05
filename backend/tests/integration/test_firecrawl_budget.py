"""The durable Firecrawl budget, against a real PostgreSQL.

Phase 2's credit accounting was an integer on a client object and a Prometheus
counter.  Neither survives a restart and neither can refuse a call, which is why
twenty-nine searches emptied an allowance with nothing in the system able to
stop them.  Every test here asserts a property that only a *table* can have.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.db.models.sources import Source
from stockbrain.db.models.system import FirecrawlCall
from stockbrain.db.session import Database
from stockbrain.enums import FirecrawlCallKind, FirecrawlCallOutcome, SourceProvider
from stockbrain.ingestion.firecrawl_budget import FirecrawlBudget

pytestmark = pytest.mark.integration


def budget(
    database: Database,
    *,
    enabled: bool = True,
    searches: int = 3,
    scrapes: int = 2,
    daily: int = 100,
    monthly: int = 1000,
) -> FirecrawlBudget:
    return FirecrawlBudget(
        database,
        enabled=enabled,
        blockers=() if enabled else ("FIRECRAWL_ENABLED is false",),
        max_searches_per_day=searches,
        max_scrapes_per_day=scrapes,
        daily_credit_cap=daily,
        monthly_credit_cap=monthly,
    )


async def _rows(database: Database) -> list[FirecrawlCall]:
    async with database.session() as session:
        return list(
            (
                await session.execute(sa.select(FirecrawlCall).order_by(FirecrawlCall.reserved_at))
            ).scalars()
        )


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------
async def test_a_reservation_is_committed_before_the_call(clean_tables: Database) -> None:
    """The whole point of the design, asserted directly.

    ``reserve`` returns only after the row is committed, so a process that dies
    during the HTTP request leaves an over-estimate rather than an unaccounted
    spend. The same ordering as ``execution_attempts.sent_to_broker``, for the
    same reason: the honest question is not "what did we spend" but "what may we
    already have spent".
    """
    guard = budget(clean_tables)
    reservation = await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=2)
    assert reservation is not None

    rows = await _rows(clean_tables)
    assert len(rows) == 1
    assert rows[0].outcome is FirecrawlCallOutcome.RESERVED
    assert rows[0].credits_reserved == 2
    # Charged at the reservation before any answer arrives.
    assert rows[0].credits_charged == 2
    # And the budget already counts it, so a second caller cannot spend it too.
    state = await guard.state()
    assert state.today.searches == 1
    assert state.today.credits == 2


async def test_an_unreconciled_reservation_is_charged_not_forgiven(
    clean_tables: Database,
) -> None:
    """A crash between the POST and the response must not free the credits.

    Simulated by never calling ``record_success`` -- which is exactly the state
    a killed worker leaves behind.
    """
    guard = budget(clean_tables)
    await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=4)
    state = await guard.state()
    assert state.today.credits == 4
    assert state.today.reported_credits == 0


async def test_the_providers_own_number_wins_when_it_is_higher(
    clean_tables: Database,
) -> None:
    """An estimate that is too low is the one failure a cost control cannot
    tolerate, so a larger ``creditsUsed`` is believed outright."""
    guard = budget(clean_tables)
    reservation = await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=2)
    assert reservation is not None
    await guard.record_success(reservation, credits_reported=6, results_returned=30)
    rows = await _rows(clean_tables)
    assert rows[0].credits_charged == 6
    assert rows[0].credits_reported == 6


async def test_a_cheaper_call_is_honoured_down_to_the_floor(
    clean_tables: Database,
) -> None:
    """A search returning three results costs one block, not four.

    But never zero: the request was processed, and processing is what is billed.
    """
    guard = budget(clean_tables)
    reservation = await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=4)
    assert reservation is not None
    await guard.record_success(reservation, credits_reported=2, results_returned=3)
    assert (await _rows(clean_tables))[0].credits_charged == 2

    second = await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=4)
    assert second is not None
    await guard.record_success(second, credits_reported=0, results_returned=0)
    assert (await _rows(clean_tables))[1].credits_charged == 2


async def test_a_failed_call_still_costs_what_it_claimed(clean_tables: Database) -> None:
    """Firecrawl documents that credits are charged whenever its infrastructure
    processed the request, "even if the target site returns an HTTP error status
    code". StockBrain cannot tell from a timeout whether that happened, so a
    failure costs what it reserved -- and that is also what stops a failing
    query from being retried into a second incident."""
    guard = budget(clean_tables)
    reservation = await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=2)
    assert reservation is not None
    await guard.record_failure(reservation, error_category="ProviderRateLimited", http_status=429)

    rows = await _rows(clean_tables)
    assert rows[0].outcome is FirecrawlCallOutcome.FAILED
    assert rows[0].credits_charged == 2
    assert rows[0].error_category == "ProviderRateLimited"
    assert rows[0].http_status == 429
    assert (await guard.state()).today.credits == 2


async def test_only_a_class_name_is_stored_never_a_provider_body(
    clean_tables: Database,
) -> None:
    """A Firecrawl error body can echo the request, and the request carries an
    ``Authorization`` header. The category is what an operator triages on."""
    guard = budget(clean_tables)
    reservation = await guard.reserve(FirecrawlCallKind.SCRAPE, credits_needed=1)
    assert reservation is not None
    await guard.record_failure(reservation, error_category="ProviderAuthError")
    row = (await _rows(clean_tables))[0]
    assert row.error_category == "ProviderAuthError"
    assert "Bearer" not in (row.error_category or "")
    assert "fc-" not in (row.error_category or "")


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------
async def test_the_search_cap_refuses_the_next_search(clean_tables: Database) -> None:
    guard = budget(clean_tables, searches=2, daily=1000)
    assert await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=2) is not None
    assert await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=2) is not None
    assert await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=2) is None
    assert len(await _rows(clean_tables)) == 2


async def test_a_full_scrape_cap_does_not_stop_a_search(clean_tables: Database) -> None:
    """Per-call, not per-summary.

    The two stages have separate budgets because they answer separate
    questions, and a full content-fetch allowance says nothing about whether a
    metadata search may run.
    """
    guard = budget(clean_tables, searches=5, scrapes=1, daily=1000)
    assert await guard.reserve(FirecrawlCallKind.SCRAPE, credits_needed=1) is not None
    assert await guard.reserve(FirecrawlCallKind.SCRAPE, credits_needed=1) is None
    assert await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=2) is not None


async def test_the_cost_of_this_call_is_checked_not_the_current_total(
    clean_tables: Database,
) -> None:
    """A five-credit call cannot slip through two credits of headroom."""
    guard = budget(clean_tables, searches=99, daily=6)
    assert await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=4) is not None
    assert await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=4) is None
    assert await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=2) is not None
    assert (await guard.state()).today.credits == 6


async def test_the_monthly_cap_refuses_even_with_daily_headroom(
    clean_tables: Database,
) -> None:
    """Firecrawl's allowance is monthly, so a daily cap alone cannot protect it."""
    guard = budget(clean_tables, searches=99, daily=1000, monthly=4)
    assert await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=4) is not None
    assert await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=2) is None
    state = await guard.state()
    assert state.exhausted
    assert any("monthly cap" in reason for reason in state.exhausted_reasons)


async def test_a_disabled_budget_refuses_everything_and_writes_nothing(
    clean_tables: Database,
) -> None:
    """Constructing the budget for a disabled provider is deliberate -- the GUI
    still shows yesterday's usage -- but it grants nothing."""
    guard = budget(clean_tables, enabled=False)
    assert await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=2) is None
    assert len(await _rows(clean_tables)) == 0


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
async def test_two_workers_cannot_spend_the_last_credit_twice(
    clean_tables: Database,
) -> None:
    """The property an in-process counter cannot have.

    Eight concurrent reservations against a three-search cap: exactly three
    win. The advisory lock is transaction-scoped, so it holds across worker
    tasks, across processes and across a restart -- an ``asyncio.Lock`` holds
    across none of them.
    """
    guard = budget(clean_tables, searches=3, daily=1000)
    results = await asyncio.gather(
        *(guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=2) for _ in range(8))
    )
    granted = [item for item in results if item is not None]
    assert len(granted) == 3
    assert len(await _rows(clean_tables)) == 3
    assert (await guard.state()).today.searches == 3


async def test_concurrent_reservations_respect_a_credit_cap_too(
    clean_tables: Database,
) -> None:
    """Not just the call count: the credit total is the binding limit when the
    calls are not all the same size."""
    guard = budget(clean_tables, searches=99, daily=10)
    results = await asyncio.gather(
        *(guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=4) for _ in range(6))
    )
    granted = [item for item in results if item is not None]
    assert len(granted) == 2
    assert (await guard.state()).today.credits == 8


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------
async def test_the_window_resets_on_the_utc_day_from_the_database_clock(
    clean_tables: Database,
) -> None:
    """Yesterday's spend does not count against today's cap.

    Backdated by writing ``reserved_at`` directly, which is the only way to
    simulate a day boundary without waiting for one. The boundary itself is
    computed by PostgreSQL, so a container with a drifting system clock cannot
    hand itself a fresh day.
    """
    guard = budget(clean_tables, searches=2, daily=4)
    reservation = await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=4)
    assert reservation is not None
    assert await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=2) is None

    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(FirecrawlCall).values(
                reserved_at=sa.func.now() - dt.timedelta(days=1, hours=1)
            )
        )

    state = await guard.state()
    assert state.today.searches == 0
    assert state.today.credits == 0
    # Still inside the month, so the monthly total remembers it.
    assert state.month.credits == 4
    assert await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=2) is not None


async def test_the_reported_window_boundaries_are_utc_midnight(
    clean_tables: Database,
) -> None:
    state = await budget(clean_tables).state()
    assert state.day_start.tzinfo is not None
    assert (state.day_start.hour, state.day_start.minute, state.day_start.second) == (0, 0, 0)
    assert state.month_start.day == 1


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
async def test_the_state_payload_carries_no_secret(clean_tables: Database) -> None:
    """Reported to the GUI and to the health endpoint.

    The API key is never part of the budget's state -- the budget does not have
    one, which is the structural version of "do not expose the key".
    """
    payload = (await budget(clean_tables).state()).as_dict()
    rendered = str(payload)
    assert "fc-" not in rendered
    assert "Bearer" not in rendered
    assert "api_key" not in rendered
    for key in ("enabled", "exhausted", "today", "month", "limits", "remaining", "window"):
        assert key in payload, key


async def test_remaining_headroom_is_reported_and_never_negative(
    clean_tables: Database,
) -> None:
    """A negative "remaining" would render as a nonsense number in the GUI and
    would invert the ``< limit`` comparisons a caller might build on it."""
    guard = budget(clean_tables, searches=1, scrapes=1, daily=2, monthly=2)
    reservation = await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=2)
    assert reservation is not None
    await guard.record_success(reservation, credits_reported=4)

    state = await guard.state()
    assert state.searches_remaining == 0
    assert state.daily_credits_remaining == 0
    assert state.monthly_credits_remaining == 0
    assert state.search_exhausted
    assert state.scrape_exhausted


async def test_a_row_can_be_attributed_to_its_query_and_topic(
    clean_tables: Database,
) -> None:
    """ "Which topic spent the allowance" has to be answerable afterwards."""
    guard = budget(clean_tables)
    reservation = await guard.reserve(
        FirecrawlCallKind.SEARCH,
        credits_needed=2,
        topic_slug="ai_infrastructure",
        requested_limit=5,
        requested_sources=["web", "news"],
    )
    assert reservation is not None
    row = (await _rows(clean_tables))[0]
    assert row.topic_slug == "ai_infrastructure"
    assert row.requested_limit == 5
    assert row.requested_sources == ["web", "news"]
    assert row.scrape_requested is False


async def test_a_scrape_row_records_its_target_and_page_count(
    clean_tables: Database,
) -> None:
    guard = budget(clean_tables)
    reservation = await guard.reserve(
        FirecrawlCallKind.SCRAPE,
        credits_needed=1,
        target_url="https://example.com/article",
        scrape_requested=True,
        source_id=None,
    )
    assert reservation is not None
    await guard.record_success(reservation, credits_reported=None, pages_scraped=1)
    row = (await _rows(clean_tables))[0]
    assert row.kind is FirecrawlCallKind.SCRAPE
    assert row.target_url == "https://example.com/article"
    assert row.pages_scraped == 1
    # /v2/scrape reports no creditsUsed, so the reservation stands.
    assert row.credits_reported is None
    assert row.credits_charged == 1


async def test_a_zero_credit_reservation_is_a_programming_error(
    clean_tables: Database,
) -> None:
    """There is no such thing as a free paid call.

    Raised rather than tolerated: a caller that computed zero has a broken
    estimator, and silently letting it through would uncap that path.
    """
    with pytest.raises(ValueError, match="at least one credit"):
        await budget(clean_tables).reserve(FirecrawlCallKind.SEARCH, credits_needed=0)


async def test_the_ledger_survives_a_new_budget_object(clean_tables: Database) -> None:
    """A restart is a new process with new objects and the same table.

    This is the regression test for the incident's root cause: the counter used
    to live on the client.
    """
    first = budget(clean_tables, searches=2, daily=1000)
    assert await first.reserve(FirecrawlCallKind.SEARCH, credits_needed=2) is not None
    assert await first.reserve(FirecrawlCallKind.SEARCH, credits_needed=2) is not None

    restarted = budget(clean_tables, searches=2, daily=1000)
    assert await restarted.reserve(FirecrawlCallKind.SEARCH, credits_needed=2) is None
    assert (await restarted.state()).today.searches == 2


async def test_a_source_reference_survives_the_source_being_deleted(
    clean_tables: Database,
) -> None:
    """``ON DELETE SET NULL``: the spend record outlives the thing it fetched.

    Deleting an old source must not delete the evidence of what it cost, and it
    must not fail either. The row is what the monthly total is computed from, so
    losing it would silently reopen the budget.
    """
    source_id = uuid.uuid4()
    async with clean_tables.transaction() as session:
        session.add(
            Source(
                id=source_id,
                provider=SourceProvider.FIRECRAWL,
                canonical_url="https://example.com/article",
                original_url="https://example.com/article",
                headline="A thing happened",
                content_hash="d" * 64,
            )
        )

    guard = budget(clean_tables)
    reservation = await guard.reserve(
        FirecrawlCallKind.SCRAPE,
        credits_needed=1,
        source_id=source_id,
        target_url="https://example.com/article",
        scrape_requested=True,
    )
    assert reservation is not None
    await guard.record_success(reservation, credits_reported=None, pages_scraped=1)

    async with clean_tables.transaction() as session:
        await session.execute(sa.delete(Source).where(Source.id == source_id))

    rows = await _rows(clean_tables)
    assert len(rows) == 1
    assert rows[0].source_id is None
    assert rows[0].credits_charged == 1
    assert rows[0].target_url == "https://example.com/article"
    assert (await guard.state()).month.credits == 1


async def test_estimated_and_reported_credits_are_reported_separately(
    clean_tables: Database,
) -> None:
    """So an operator can see how good the estimate is.

    A large gap between the two means the published model has changed and the
    estimator needs revisiting -- which is invisible if only one number is kept.
    """
    guard = budget(clean_tables)
    first = await guard.reserve(FirecrawlCallKind.SEARCH, credits_needed=4)
    second = await guard.reserve(FirecrawlCallKind.SCRAPE, credits_needed=1)
    assert first is not None and second is not None
    await guard.record_success(first, credits_reported=4, results_returned=20)
    await guard.record_success(second, credits_reported=None, pages_scraped=1)

    state = await guard.state()
    assert state.today.credits == 5
    assert state.today.reported_credits == 4
    assert state.today.pages_scraped == 1
    assert Decimal(state.today.credits) >= Decimal(state.today.reported_credits)
