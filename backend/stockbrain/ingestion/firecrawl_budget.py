"""Durable Firecrawl call and credit budget.

Why this module exists, stated plainly: Phase 2 shipped Firecrawl with nine
enabled queries on twenty- and thirty-minute intervals, ``scrapeOptions`` set on
every one of them, and no ceiling of any kind.  ``firecrawl_activity_logs.csv``
records twenty-nine searches between 04:59 and 06:01 UTC on 2026-09-05 --
roughly seven hundred credits in sixty-two minutes -- after which the API
answered HTTP 402 and one hundred and twenty-seven jobs failed in a row.  The
only accounting the process had was an integer on the client object and a
Prometheus counter.  Neither survives a restart, and neither can refuse a call.

So the budget is a table, and it works the way the execution ledger works:

* A row is inserted and **committed before the HTTP request**, carrying the
  worst-case cost computed from the published billing model.  A process that
  dies mid-call therefore leaves an over-estimate rather than an unaccounted
  spend, and the same reservation is what makes two workers unable to spend the
  last credit twice.
* After the call the row is reconciled against the provider's own
  ``creditsUsed`` where the provider reports one.  ``credits_charged`` is the
  single column the caps are compared against.
* Windows are computed with the **database clock**, so a container whose system
  time drifts cannot widen its own budget window.

Firecrawl's published billing, verified 2026-09-05 against
<https://docs.firecrawl.dev/billing>:

* ``/v2/search`` -- **2 credits per 10 results, rounded up per 10.**  ``limit``
  is per *source*, so ``limit=10`` with ``web`` and ``news`` is twenty billed
  results and four credits.
* ``/v2/scrape`` -- **1 credit per page.**  Passing ``scrapeOptions`` to a
  search adds that per-page charge for every result, which is where a
  twenty-result search becomes a twenty-four-credit search.
* "Credits are charged whenever Firecrawl's infrastructure processes a request,
  even if the target site returns an HTTP error status code" -- so a failed
  call is not a free call, and a retried failure is not a free retry.

What budget exhaustion must **not** do is stop the application.  Alpaca news
continues, SEC EDGAR continues, already-ingested events keep being classified,
research keeps running and broker reconciliation keeps running.  That is
structural: this guard is consulted only by the two Firecrawl call sites, and
nothing else in the process asks it anything.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.models.system import FirecrawlCall
from stockbrain.db.session import Database
from stockbrain.enums import FirecrawlCallKind, FirecrawlCallOutcome
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = [
    "SEARCH_CREDITS_PER_BLOCK",
    "SEARCH_RESULTS_PER_BLOCK",
    "FirecrawlBudget",
    "FirecrawlBudgetState",
    "FirecrawlReservation",
    "FirecrawlUsage",
    "estimate_scrape_credits",
    "estimate_search_credits",
]

log = get_logger(__name__)

#: Firecrawl bills search in blocks of ten results at two credits a block.
SEARCH_RESULTS_PER_BLOCK = 10
SEARCH_CREDITS_PER_BLOCK = 2


class BudgetRefusal(StrEnum):
    """Why a paid call was refused.  A name, not a sentence, so it aggregates."""

    DISABLED = "DISABLED"
    SEARCH_LIMIT = "SEARCH_LIMIT"
    SCRAPE_LIMIT = "SCRAPE_LIMIT"
    DAILY_CREDITS = "DAILY_CREDITS"
    MONTHLY_CREDITS = "MONTHLY_CREDITS"


def estimate_search_credits(*, result_limit: int, source_count: int) -> int:
    """Worst-case credits for one ``/v2/search``.

    ``limit`` is per source, so the billed result count is ``limit *
    len(sources)``.  Rounded **up** to a whole ten-result block, because that is
    how the published model rounds and because rounding down would let the cap
    be crossed by a block.  A search that returns nothing still costs one block:
    the request was processed.
    """
    billed_results = max(1, result_limit) * max(1, source_count)
    blocks = math.ceil(billed_results / SEARCH_RESULTS_PER_BLOCK)
    return max(1, blocks) * SEARCH_CREDITS_PER_BLOCK


def estimate_scrape_credits(*, pages: int = 1) -> int:
    """Credits for a plain markdown scrape: one per page.

    StockBrain requests no JSON extraction, no prompt-injection check and no
    zero-data-retention, each of which is documented as an additional per-page
    charge.  If one of those is ever added, this function is where the cost
    has to change with it.
    """
    return max(1, pages)


@dataclass(frozen=True, slots=True)
class FirecrawlUsage:
    """Durable usage inside one window."""

    searches: int = 0
    scrapes: int = 0
    credits: int = 0
    reported_credits: int = 0
    """The part of ``credits`` the provider itself accounted for.  Where this is
    below ``credits`` the difference is StockBrain's own estimate for calls the
    provider gave no number for."""

    pages_scraped: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "searches": self.searches,
            "scrapes": self.scrapes,
            "estimated_credits": self.credits,
            "provider_reported_credits": self.reported_credits,
            "pages_scraped": self.pages_scraped,
        }


@dataclass(frozen=True, slots=True)
class FirecrawlBudgetState:
    """Everything an operator or a health panel needs, with no secrets in it."""

    enabled: bool
    blockers: tuple[str, ...]
    today: FirecrawlUsage
    month: FirecrawlUsage
    max_searches_per_day: int
    max_scrapes_per_day: int
    daily_credit_cap: int
    monthly_credit_cap: int
    day_start: dt.datetime
    month_start: dt.datetime
    exhausted_reasons: tuple[str, ...] = ()

    @property
    def exhausted(self) -> bool:
        """Whether *any* cap is at or past its limit.

        Reported for observability. It is deliberately not what
        :meth:`FirecrawlBudget.reserve` decides on: a scrape cap being full must
        not stop a search, so the per-call check looks at the cap that call
        would cross rather than at this summary.
        """
        return bool(self.exhausted_reasons)

    @property
    def search_exhausted(self) -> bool:
        return (
            self.searches_remaining <= 0
            or self.daily_credits_remaining < SEARCH_CREDITS_PER_BLOCK
            or self.monthly_credits_remaining < SEARCH_CREDITS_PER_BLOCK
        )

    @property
    def scrape_exhausted(self) -> bool:
        return (
            self.scrapes_remaining <= 0
            or self.daily_credits_remaining < 1
            or self.monthly_credits_remaining < 1
        )

    @property
    def searches_remaining(self) -> int:
        return max(0, self.max_searches_per_day - self.today.searches)

    @property
    def scrapes_remaining(self) -> int:
        return max(0, self.max_scrapes_per_day - self.today.scrapes)

    @property
    def daily_credits_remaining(self) -> int:
        return max(0, self.daily_credit_cap - self.today.credits)

    @property
    def monthly_credits_remaining(self) -> int:
        return max(0, self.monthly_credit_cap - self.month.credits)

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "blockers": list(self.blockers),
            "exhausted": self.exhausted,
            "exhausted_reasons": list(self.exhausted_reasons),
            "today": self.today.as_dict(),
            "month": self.month.as_dict(),
            "limits": {
                "max_searches_per_day": self.max_searches_per_day,
                "max_scrapes_per_day": self.max_scrapes_per_day,
                "daily_credit_cap": self.daily_credit_cap,
                "monthly_credit_cap": self.monthly_credit_cap,
            },
            "remaining": {
                "searches_today": self.searches_remaining,
                "scrapes_today": self.scrapes_remaining,
                "daily_credits": self.daily_credits_remaining,
                "monthly_credits": self.monthly_credits_remaining,
            },
            "window": {
                "day_start": self.day_start.isoformat(),
                "month_start": self.month_start.isoformat(),
            },
        }


@dataclass(frozen=True, slots=True)
class FirecrawlReservation:
    """A committed claim on the budget, and the row that will be reconciled."""

    call_id: uuid.UUID
    kind: FirecrawlCallKind
    credits_reserved: int


class FirecrawlBudget:
    """Grants, records and reconciles every paid Firecrawl call.

    There is deliberately no ``force`` parameter and no way to spend without a
    reservation: the guard is not advisory.
    """

    def __init__(
        self,
        database: Database,
        *,
        enabled: bool,
        blockers: tuple[str, ...] = (),
        max_searches_per_day: int,
        max_scrapes_per_day: int,
        daily_credit_cap: int,
        monthly_credit_cap: int,
    ) -> None:
        self._database = database
        self._enabled = enabled
        self._blockers = blockers
        self._max_searches = max_searches_per_day
        self._max_scrapes = max_scrapes_per_day
        self._daily_cap = daily_credit_cap
        self._monthly_cap = monthly_credit_cap

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    async def state(self) -> FirecrawlBudgetState:
        """Current usage and remaining headroom, straight from the ledger.

        Uncached on purpose.  The LLM guard caches for thirty seconds because it
        is consulted per classification; this one is consulted a handful of
        times a day, and a cache is the mechanism by which two workers both
        believe the last credit is theirs.
        """
        async with self._database.session() as session:
            return await self.state_in_session(session)

    async def state_in_session(self, session: AsyncSession) -> FirecrawlBudgetState:
        day_start, month_start = await _window_starts(session)
        today = await _usage_since(session, day_start)
        month = await _usage_since(session, month_start)

        reasons: list[str] = []
        if today.searches >= self._max_searches:
            reasons.append(
                f"searches today {today.searches} reached the daily cap {self._max_searches}"
            )
        if today.scrapes >= self._max_scrapes:
            reasons.append(
                f"scrapes today {today.scrapes} reached the daily cap {self._max_scrapes}"
            )
        if today.credits >= self._daily_cap:
            reasons.append(
                f"estimated credits today {today.credits} reached the daily cap {self._daily_cap}"
            )
        if month.credits >= self._monthly_cap:
            reasons.append(
                f"estimated credits this month {month.credits} reached the monthly cap "
                f"{self._monthly_cap}"
            )

        state = FirecrawlBudgetState(
            enabled=self._enabled,
            blockers=self._blockers,
            today=today,
            month=month,
            max_searches_per_day=self._max_searches,
            max_scrapes_per_day=self._max_scrapes,
            daily_credit_cap=self._daily_cap,
            monthly_credit_cap=self._monthly_cap,
            day_start=day_start,
            month_start=month_start,
            exhausted_reasons=tuple(reasons),
        )
        METRICS.set("stockbrain_firecrawl_searches_today", float(today.searches))
        METRICS.set("stockbrain_firecrawl_scrapes_today", float(today.scrapes))
        METRICS.set("stockbrain_firecrawl_credits_today", float(today.credits))
        METRICS.set("stockbrain_firecrawl_credits_month", float(month.credits))
        return state

    # ------------------------------------------------------------------
    # Reserving
    # ------------------------------------------------------------------
    async def reserve(
        self,
        kind: FirecrawlCallKind,
        *,
        credits_needed: int,
        query_id: uuid.UUID | None = None,
        topic_slug: str | None = None,
        source_id: uuid.UUID | None = None,
        target_url: str | None = None,
        requested_limit: int | None = None,
        requested_sources: list[str] | None = None,
        scrape_requested: bool = False,
    ) -> FirecrawlReservation | None:
        """Claim budget for one call, or return ``None`` with the reason logged.

        The whole check-and-insert runs in one transaction that locks the
        ledger's day window, so "two workers both see eleven searches and both
        make the twelfth" is not a state this can reach.  The row is committed
        when this returns, before any socket is opened.
        """
        if not self._enabled:
            _refused(kind, BudgetRefusal.DISABLED, "; ".join(self._blockers) or "disabled")
            return None
        if credits_needed <= 0:  # pragma: no cover - the estimators never return this
            raise ValueError("a reservation must claim at least one credit")

        async with self._database.transaction() as session:
            # Serialise every Firecrawl reservation in the deployment. A
            # transaction-scoped advisory lock is the only thing that holds
            # across worker tasks, processes and a restart; an asyncio.Lock
            # holds across none of them.
            await session.execute(sa.select(sa.func.pg_advisory_xact_lock(_FIRECRAWL_BUDGET_LOCK)))
            state = await self.state_in_session(session)

            refusal = _refusal_for(kind, state, credits_needed)
            if refusal is not None:
                _refused(kind, refusal[0], refusal[1])
                return None

            call = FirecrawlCall(
                kind=kind,
                outcome=FirecrawlCallOutcome.RESERVED,
                query_id=query_id,
                topic_slug=topic_slug,
                source_id=source_id,
                target_url=target_url,
                requested_limit=requested_limit,
                requested_sources=list(requested_sources or []),
                scrape_requested=scrape_requested,
                credits_reserved=credits_needed,
                # Charged at the reservation until the provider tells us better.
                # A row that is never reconciled is charged, not forgiven.
                credits_charged=credits_needed,
                pages_scraped=0,
            )
            session.add(call)
            await session.flush()
            reservation = FirecrawlReservation(
                call_id=call.id, kind=kind, credits_reserved=credits_needed
            )

        log.info(
            "firecrawl_call_reserved",
            kind=kind.value,
            credits_reserved=credits_needed,
            topic=topic_slug,
            searches_today=state.today.searches,
            credits_today=state.today.credits,
        )
        METRICS.inc(
            "stockbrain_firecrawl_calls_total",
            labels={"kind": kind.value, "phase": "reserved"},
        )
        return reservation

    # ------------------------------------------------------------------
    # Reconciling
    # ------------------------------------------------------------------
    async def record_success(
        self,
        reservation: FirecrawlReservation,
        *,
        credits_reported: int | None = None,
        results_returned: int | None = None,
        pages_scraped: int = 0,
        http_status: int | None = 200,
    ) -> None:
        """Close a reservation with what actually happened.

        ``credits_charged`` becomes the **larger** of the reservation and the
        provider's number.  Trusting a lower reported figure outright would let
        a provider bug -- or a response StockBrain misread -- quietly reopen the
        budget, and the conservative direction on a cost control is to
        over-count.
        """
        if credits_reported is None:
            charged = reservation.credits_reserved
        elif credits_reported > reservation.credits_reserved:
            # The provider billed more than the published model predicted. Trust
            # the provider: an estimate that is too low is the one failure mode
            # a cost control cannot tolerate.
            charged = credits_reported
        else:
            # A genuinely cheaper call -- fewer results than the limit allowed --
            # is the common case and is honoured, but never below the floor cost
            # of having the request processed at all.
            floor = SEARCH_CREDITS_PER_BLOCK if reservation.kind is FirecrawlCallKind.SEARCH else 1
            charged = max(credits_reported, floor)
        await self._close(
            reservation,
            outcome=FirecrawlCallOutcome.SUCCEEDED,
            credits_reported=credits_reported,
            credits_charged=charged,
            results_returned=results_returned,
            pages_scraped=pages_scraped,
            http_status=http_status,
            error_category=None,
        )

    async def record_failure(
        self,
        reservation: FirecrawlReservation,
        *,
        error_category: str,
        http_status: int | None = None,
    ) -> None:
        """Close a reservation that failed.

        The reservation stands.  Firecrawl documents that credits are charged
        whenever its infrastructure processed the request, and StockBrain cannot
        tell from a timeout whether that happened -- so a failure costs what it
        claimed.  This is also what stops a failing query from being retried
        into a second incident.
        """
        await self._close(
            reservation,
            outcome=FirecrawlCallOutcome.FAILED,
            credits_reported=None,
            credits_charged=reservation.credits_reserved,
            results_returned=None,
            pages_scraped=0,
            http_status=http_status,
            error_category=error_category[:200],
        )

    async def _close(
        self,
        reservation: FirecrawlReservation,
        *,
        outcome: FirecrawlCallOutcome,
        credits_reported: int | None,
        credits_charged: int,
        results_returned: int | None,
        pages_scraped: int,
        http_status: int | None,
        error_category: str | None,
    ) -> None:
        async with self._database.transaction() as session:
            await session.execute(
                sa.update(FirecrawlCall)
                .where(FirecrawlCall.id == reservation.call_id)
                .values(
                    outcome=outcome,
                    completed_at=sa.func.now(),
                    credits_reported=credits_reported,
                    credits_charged=credits_charged,
                    results_returned=results_returned,
                    pages_scraped=pages_scraped,
                    http_status=http_status,
                    error_category=error_category,
                )
            )
        log.info(
            "firecrawl_call_recorded",
            kind=reservation.kind.value,
            outcome=outcome.value,
            credits_charged=credits_charged,
            credits_reported=credits_reported,
            results_returned=results_returned,
            pages_scraped=pages_scraped,
            http_status=http_status,
            error_category=error_category,
        )
        METRICS.inc(
            "stockbrain_firecrawl_calls_total",
            labels={"kind": reservation.kind.value, "phase": outcome.value.lower()},
        )
        METRICS.inc("stockbrain_firecrawl_credits_total", float(credits_charged))


#: A fixed advisory-lock key for the Firecrawl budget.  Arbitrary but stable;
#: changing it would let an old and a new process spend concurrently.
_FIRECRAWL_BUDGET_LOCK = 0x46_43_42_47  # "FCBG"


def _refusal_for(
    kind: FirecrawlCallKind, state: FirecrawlBudgetState, credits_needed: int
) -> tuple[BudgetRefusal, str] | None:
    """Which cap, if any, refuses this specific call.

    Checked against the cost of *this* call rather than against the current
    total, so a five-credit call cannot slip through two credits of headroom.
    """
    if kind is FirecrawlCallKind.SEARCH and state.today.searches + 1 > state.max_searches_per_day:
        return (
            BudgetRefusal.SEARCH_LIMIT,
            f"searches today {state.today.searches} of {state.max_searches_per_day}",
        )
    if kind is FirecrawlCallKind.SCRAPE and state.today.scrapes + 1 > state.max_scrapes_per_day:
        return (
            BudgetRefusal.SCRAPE_LIMIT,
            f"scrapes today {state.today.scrapes} of {state.max_scrapes_per_day}",
        )
    if state.today.credits + credits_needed > state.daily_credit_cap:
        return (
            BudgetRefusal.DAILY_CREDITS,
            f"estimated credits today {state.today.credits} + {credits_needed} would pass "
            f"the daily cap {state.daily_credit_cap}",
        )
    if state.month.credits + credits_needed > state.monthly_credit_cap:
        return (
            BudgetRefusal.MONTHLY_CREDITS,
            f"estimated credits this month {state.month.credits} + {credits_needed} would pass "
            f"the monthly cap {state.monthly_credit_cap}",
        )
    return None


def _refused(kind: FirecrawlCallKind, refusal: BudgetRefusal, detail: str) -> None:
    log.warning(
        "firecrawl_call_refused",
        kind=kind.value,
        refusal=refusal.value,
        detail=detail,
    )
    METRICS.inc(
        "stockbrain_firecrawl_calls_refused_total",
        labels={"kind": kind.value, "refusal": refusal.value},
    )


async def _window_starts(session: AsyncSession) -> tuple[dt.datetime, dt.datetime]:
    """Start of the current UTC day and month, from the **database** clock.

    Computed by PostgreSQL rather than by Python so that the boundary the budget
    resets on is the same one the ledger's ``reserved_at`` defaults were written
    against.  A container with a drifting system clock would otherwise be able
    to hand itself a fresh day.
    """
    row = (
        await session.execute(
            sa.text(
                "SELECT (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') "
                "AS day_start, "
                "(date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') "
                "AS month_start"
            )
        )
    ).one()
    return _as_utc(row.day_start), _as_utc(row.month_start)


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


async def _usage_since(session: AsyncSession, since: dt.datetime) -> FirecrawlUsage:
    """Sum the ledger from ``since``.

    ``RESERVED`` rows are included: a call in flight, or a call whose process
    died before it could be reconciled, has already committed the operator's
    money as far as anyone here can tell.
    """
    row = (
        await session.execute(
            sa.select(
                sa.func.count().filter(FirecrawlCall.kind == FirecrawlCallKind.SEARCH),
                sa.func.count().filter(FirecrawlCall.kind == FirecrawlCallKind.SCRAPE),
                sa.func.coalesce(sa.func.sum(FirecrawlCall.credits_charged), 0),
                sa.func.coalesce(sa.func.sum(FirecrawlCall.credits_reported), 0),
                sa.func.coalesce(sa.func.sum(FirecrawlCall.pages_scraped), 0),
            ).where(FirecrawlCall.reserved_at >= since)
        )
    ).one()
    return FirecrawlUsage(
        searches=int(row[0]),
        scrapes=int(row[1]),
        credits=int(row[2]),
        reported_credits=int(row[3]),
        pages_scraped=int(row[4]),
    )
