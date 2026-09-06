"""Durable per-provider call and spend budget.

This is Phase 9's Firecrawl ledger generalised, not replaced.  The reason it
existed has not changed: Phase 2 shipped nine enabled queries on twenty-minute
intervals with ``scrapeOptions`` on every one of them and no ceiling of any
kind, and ``firecrawl_activity_logs.csv`` records twenty-nine searches in
sixty-two minutes before the API started answering HTTP 402.  The only
accounting the process had was an integer on a client object and a Prometheus
counter.  Neither survives a restart, and neither can refuse a call.

What is new is that there is now more than one metered provider, so the guard is
parameterised by provider instead of being written for one vendor:

* Every paid provider gets **its own** caps, its own window sums and its own
  advisory lock.  Brave hitting its ceiling must not serialise behind Firecrawl,
  and must not stop Exa.
* A row is inserted and **committed before the HTTP request**, carrying the
  worst-case cost.  A process that dies mid-call leaves an over-estimate rather
  than an unaccounted spend, and the same reservation is what makes two workers
  unable to spend the last unit twice.
* After the call the row is reconciled against whatever the provider itself
  reported.  ``units_charged`` is the single column the caps are compared
  against.
* Windows are computed with the **database clock**, so a container whose system
  time drifts cannot widen its own budget window.

"Units" are per-provider billing units, and the module never pretends they are
comparable:

============  ==================  ============================================
Provider      One unit is         Verified 2026-09-05
============  ==================  ============================================
``brave``     one request         $5 / 1,000 requests; only *successful*
                                  requests are billed
``exa``       one request         $7 / 1,000 requests for up to 10 results;
                                  ``costDollars`` reports the real price
``firecrawl`` one credit          search 2 / 10 results, scrape 1 / page;
                                  charged even when the target errors
============  ==================  ============================================

What budget exhaustion must **not** do is stop the application.  Alpaca news
continues, SEC EDGAR continues, already-ingested events keep being classified,
research keeps running and broker reconciliation keeps running.  That is
structural: this guard is consulted only at the paid call sites, and nothing
else in the process asks it anything.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
import zlib
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.models.system import ProviderCall
from stockbrain.db.session import Database
from stockbrain.enums import ProviderCallKind, ProviderCallOutcome
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = [
    "FIRECRAWL_SEARCH_CREDITS_PER_BLOCK",
    "FIRECRAWL_SEARCH_RESULTS_PER_BLOCK",
    "BudgetRefusal",
    "ProviderBudgetState",
    "ProviderCallBudget",
    "ProviderReservation",
    "ProviderUsage",
    "estimate_firecrawl_scrape_credits",
    "estimate_firecrawl_search_credits",
]

log = get_logger(__name__)

#: Firecrawl bills search in blocks of ten results at two credits a block.
#: Retained because the historical ledger rows were priced with it and because
#: the legacy search path may still be re-read; nothing schedules a search now.
FIRECRAWL_SEARCH_RESULTS_PER_BLOCK = 10
FIRECRAWL_SEARCH_CREDITS_PER_BLOCK = 2


class BudgetRefusal(StrEnum):
    """Why a paid call was refused.  A name, not a sentence, so it aggregates."""

    DISABLED = "DISABLED"
    SEARCH_LIMIT = "SEARCH_LIMIT"
    SCRAPE_LIMIT = "SCRAPE_LIMIT"
    DAILY_UNITS = "DAILY_UNITS"
    MONTHLY_UNITS = "MONTHLY_UNITS"


def estimate_firecrawl_search_credits(*, result_limit: int, source_count: int) -> int:
    """Worst-case credits for one Firecrawl ``/v2/search``.

    ``limit`` is per source, so the billed result count is ``limit *
    len(sources)``.  Rounded **up** to a whole ten-result block, because that is
    how the published model rounds and because rounding down would let the cap
    be crossed by a block.  A search that returns nothing still costs one block:
    the request was processed.

    Kept for the legacy path and for re-reading historical rows.  Firecrawl is
    no longer a search provider.
    """
    billed_results = max(1, result_limit) * max(1, source_count)
    blocks = math.ceil(billed_results / FIRECRAWL_SEARCH_RESULTS_PER_BLOCK)
    return max(1, blocks) * FIRECRAWL_SEARCH_CREDITS_PER_BLOCK


def estimate_firecrawl_scrape_credits(*, pages: int = 1) -> int:
    """Credits for a plain markdown Firecrawl scrape: one per page.

    StockBrain requests no JSON extraction, no prompt-injection check and no
    zero-data-retention, each of which is documented as an additional per-page
    charge.  If one of those is ever added, this function is where the cost has
    to change with it.
    """
    return max(1, pages)


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Durable usage inside one window, for one provider."""

    searches: int = 0
    scrapes: int = 0
    units: int = 0
    reported_units: int = 0
    """The part of ``units`` the provider itself accounted for.  Where this is
    below ``units`` the difference is StockBrain's own estimate for calls the
    provider gave no number for."""

    results_returned: int = 0
    pages_scraped: int = 0
    cost_usd: Decimal = Decimal("0")
    """Charged cost in USD where the provider's price is knowable.  Firecrawl
    bills credits against an allowance rather than dollars per call, so its rows
    contribute nothing here and the credit caps are what protect it."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "searches": self.searches,
            "scrapes": self.scrapes,
            "estimated_units": self.units,
            "provider_reported_units": self.reported_units,
            "results_returned": self.results_returned,
            "pages_scraped": self.pages_scraped,
            "estimated_cost_usd": str(self.cost_usd),
        }


@dataclass(frozen=True, slots=True)
class ProviderBudgetState:
    """Everything an operator or a health panel needs, with no secrets in it."""

    provider: str
    unit_label: str
    enabled: bool
    blockers: tuple[str, ...]
    today: ProviderUsage
    month: ProviderUsage
    max_searches_per_day: int
    max_scrapes_per_day: int
    daily_unit_cap: int
    monthly_unit_cap: int
    day_start: dt.datetime
    month_start: dt.datetime
    exhausted_reasons: tuple[str, ...] = ()

    @property
    def exhausted(self) -> bool:
        """Whether *any* cap is at or past its limit.

        Reported for observability.  It is deliberately not what
        :meth:`ProviderCallBudget.reserve` decides on: a scrape cap being full
        must not stop a search, so the per-call check looks at the cap that call
        would cross rather than at this summary.
        """
        return bool(self.exhausted_reasons)

    @property
    def search_exhausted(self) -> bool:
        return (
            self.searches_remaining <= 0
            or self.daily_units_remaining <= 0
            or self.monthly_units_remaining <= 0
        )

    @property
    def scrape_exhausted(self) -> bool:
        return (
            self.scrapes_remaining <= 0
            or self.daily_units_remaining <= 0
            or self.monthly_units_remaining <= 0
        )

    @property
    def searches_remaining(self) -> int:
        return max(0, self.max_searches_per_day - self.today.searches)

    @property
    def scrapes_remaining(self) -> int:
        return max(0, self.max_scrapes_per_day - self.today.scrapes)

    @property
    def daily_units_remaining(self) -> int:
        return max(0, self.daily_unit_cap - self.today.units)

    @property
    def monthly_units_remaining(self) -> int:
        return max(0, self.monthly_unit_cap - self.month.units)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "unit_label": self.unit_label,
            "enabled": self.enabled,
            "blockers": list(self.blockers),
            "exhausted": self.exhausted,
            "exhausted_reasons": list(self.exhausted_reasons),
            "today": self.today.as_dict(),
            "month": self.month.as_dict(),
            "limits": {
                "max_searches_per_day": self.max_searches_per_day,
                "max_scrapes_per_day": self.max_scrapes_per_day,
                "daily_unit_cap": self.daily_unit_cap,
                "monthly_unit_cap": self.monthly_unit_cap,
            },
            "remaining": {
                "searches_today": self.searches_remaining,
                "scrapes_today": self.scrapes_remaining,
                "daily_units": self.daily_units_remaining,
                "monthly_units": self.monthly_units_remaining,
            },
            "window": {
                "day_start": self.day_start.isoformat(),
                "month_start": self.month_start.isoformat(),
            },
        }


@dataclass(frozen=True, slots=True)
class ProviderReservation:
    """A committed claim on the budget, and the row that will be reconciled."""

    call_id: uuid.UUID
    provider: str
    kind: ProviderCallKind
    units_reserved: int
    cost_usd_reserved: Decimal | None = None


class ProviderCallBudget:
    """Grants, records and reconciles every paid call to one provider.

    There is deliberately no ``force`` parameter and no way to spend without a
    reservation: the guard is not advisory.
    """

    def __init__(
        self,
        database: Database,
        *,
        provider: str,
        unit_label: str = "requests",
        unit_cost_usd: Decimal | None = None,
        enabled: bool,
        blockers: tuple[str, ...] = (),
        max_searches_per_day: int,
        max_scrapes_per_day: int = 0,
        daily_unit_cap: int,
        monthly_unit_cap: int,
    ) -> None:
        self._database = database
        self._provider = provider
        self._unit_label = unit_label
        self._unit_cost_usd = unit_cost_usd
        self._enabled = enabled
        self._blockers = blockers
        self._max_searches = max_searches_per_day
        self._max_scrapes = max_scrapes_per_day
        self._daily_cap = daily_unit_cap
        self._monthly_cap = monthly_unit_cap

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    async def state(self) -> ProviderBudgetState:
        """Current usage and remaining headroom, straight from the ledger.

        Uncached on purpose.  The LLM guard caches for thirty seconds because it
        is consulted per classification; this one is consulted a handful of
        times a day, and a cache is the mechanism by which two workers both
        believe the last unit is theirs.
        """
        async with self._database.session() as session:
            return await self.state_in_session(session)

    async def state_in_session(self, session: AsyncSession) -> ProviderBudgetState:
        day_start, month_start = await _window_starts(session)
        today = await _usage_since(session, self._provider, day_start)
        month = await _usage_since(session, self._provider, month_start)

        reasons: list[str] = []
        if self._max_searches and today.searches >= self._max_searches:
            reasons.append(
                f"searches today {today.searches} reached the daily cap {self._max_searches}"
            )
        if self._max_scrapes and today.scrapes >= self._max_scrapes:
            reasons.append(
                f"scrapes today {today.scrapes} reached the daily cap {self._max_scrapes}"
            )
        if today.units >= self._daily_cap:
            reasons.append(
                f"estimated {self._unit_label} today {today.units} reached the daily cap "
                f"{self._daily_cap}"
            )
        if month.units >= self._monthly_cap:
            reasons.append(
                f"estimated {self._unit_label} this month {month.units} reached the monthly cap "
                f"{self._monthly_cap}"
            )

        state = ProviderBudgetState(
            provider=self._provider,
            unit_label=self._unit_label,
            enabled=self._enabled,
            blockers=self._blockers,
            today=today,
            month=month,
            max_searches_per_day=self._max_searches,
            max_scrapes_per_day=self._max_scrapes,
            daily_unit_cap=self._daily_cap,
            monthly_unit_cap=self._monthly_cap,
            day_start=day_start,
            month_start=month_start,
            exhausted_reasons=tuple(reasons),
        )
        labels = {"provider": self._provider}
        METRICS.set("stockbrain_provider_searches_today", float(today.searches), labels=labels)
        METRICS.set("stockbrain_provider_scrapes_today", float(today.scrapes), labels=labels)
        METRICS.set("stockbrain_provider_units_today", float(today.units), labels=labels)
        METRICS.set("stockbrain_provider_units_month", float(month.units), labels=labels)
        return state

    # ------------------------------------------------------------------
    # Reserving
    # ------------------------------------------------------------------
    async def reserve(
        self,
        kind: ProviderCallKind,
        *,
        units_needed: int,
        query_id: uuid.UUID | None = None,
        topic_slug: str | None = None,
        source_id: uuid.UUID | None = None,
        target_url: str | None = None,
        requested_limit: int | None = None,
        requested_sources: list[str] | None = None,
        scrape_requested: bool = False,
    ) -> ProviderReservation | None:
        """Claim budget for one call, or return ``None`` with the reason logged.

        The whole check-and-insert runs in one transaction that locks *this
        provider's* window, so "two workers both see eleven searches and both
        make the twelfth" is not a state this can reach.  The row is committed
        when this returns, before any socket is opened.
        """
        if not self._enabled:
            self._refused(kind, BudgetRefusal.DISABLED, "; ".join(self._blockers) or "disabled")
            return None
        if units_needed <= 0:  # pragma: no cover - the estimators never return this
            raise ValueError("a reservation must claim at least one unit")

        cost = (self._unit_cost_usd * units_needed) if self._unit_cost_usd is not None else None
        async with self._database.transaction() as session:
            # Serialise this provider's reservations across the deployment. A
            # transaction-scoped advisory lock is the only thing that holds
            # across worker tasks, processes and a restart; an asyncio.Lock
            # holds across none of them. The key is per provider so Brave does
            # not queue behind Exa.
            await session.execute(
                sa.select(sa.func.pg_advisory_xact_lock(_budget_lock_key(self._provider)))
            )
            state = await self.state_in_session(session)

            refusal = _refusal_for(kind, state, units_needed)
            if refusal is not None:
                self._refused(kind, refusal[0], refusal[1])
                return None

            call = ProviderCall(
                provider=self._provider,
                kind=kind,
                outcome=ProviderCallOutcome.RESERVED,
                query_id=query_id,
                topic_slug=topic_slug,
                source_id=source_id,
                target_url=target_url,
                requested_limit=requested_limit,
                requested_sources=list(requested_sources or []),
                scrape_requested=scrape_requested,
                units_reserved=units_needed,
                # Charged at the reservation until the provider tells us better.
                # A row that is never reconciled is charged, not forgiven.
                units_charged=units_needed,
                cost_usd_charged=cost,
                pages_scraped=0,
            )
            session.add(call)
            await session.flush()
            reservation = ProviderReservation(
                call_id=call.id,
                provider=self._provider,
                kind=kind,
                units_reserved=units_needed,
                cost_usd_reserved=cost,
            )

        log.info(
            "provider_call_reserved",
            provider=self._provider,
            kind=kind.value,
            units_reserved=units_needed,
            topic=topic_slug,
            searches_today=state.today.searches,
            units_today=state.today.units,
        )
        METRICS.inc(
            "stockbrain_provider_calls_total",
            labels={"provider": self._provider, "kind": kind.value, "phase": "reserved"},
        )
        return reservation

    # ------------------------------------------------------------------
    # Reconciling
    # ------------------------------------------------------------------
    async def record_success(
        self,
        reservation: ProviderReservation,
        *,
        units_reported: int | None = None,
        cost_usd_reported: Decimal | None = None,
        results_returned: int | None = None,
        pages_scraped: int = 0,
        http_status: int | None = 200,
    ) -> None:
        """Close a reservation with what actually happened.

        ``units_charged`` becomes the provider's own number where it gives one,
        and the reservation otherwise -- but never below one unit, because the
        request was processed and processing is what is billed.

        A provider that billed *more* than the published model predicted is
        believed outright: an estimate that is too low is the one failure mode a
        cost control cannot tolerate.  A genuinely cheaper call -- a search that
        returned three results where the limit allowed thirty -- is honoured,
        because a ledger that systematically over-counts is a ledger an operator
        stops reconciling against the invoice.  The same rule applies to the
        dollar figure.
        """
        units = reservation.units_reserved if units_reported is None else max(1, units_reported)

        cost = reservation.cost_usd_reserved
        if cost_usd_reported is not None:
            cost = cost_usd_reported if cost is None else max(cost, cost_usd_reported)

        await self._close(
            reservation,
            outcome=ProviderCallOutcome.SUCCEEDED,
            units_reported=units_reported,
            units_charged=units,
            cost_usd_reported=cost_usd_reported,
            cost_usd_charged=cost,
            results_returned=results_returned,
            pages_scraped=pages_scraped,
            http_status=http_status,
            error_category=None,
        )

    async def record_failure(
        self,
        reservation: ProviderReservation,
        *,
        error_category: str,
        http_status: int | None = None,
        refund: bool = False,
    ) -> None:
        """Close a reservation that failed.

        The reservation stands by default.  Firecrawl documents that credits are
        charged whenever its infrastructure processed the request, and
        StockBrain cannot tell from a timeout whether that happened -- so a
        failure costs what it claimed.  This is also what stops a failing query
        from being retried into a second incident.

        ``refund=True`` is for the one provider that publishes the opposite
        guarantee: Brave documents that "only successful requests (non-error
        responses) are counted against your quota and billed".  Even there the
        refund is applied only to a *classified* provider error, never to a
        timeout, because a timeout is precisely the case where nobody knows
        whether the request succeeded on the far side.
        """
        await self._close(
            reservation,
            outcome=ProviderCallOutcome.FAILED,
            units_reported=None,
            units_charged=0 if refund else reservation.units_reserved,
            cost_usd_reported=None,
            cost_usd_charged=(Decimal("0") if refund else reservation.cost_usd_reserved),
            results_returned=None,
            pages_scraped=0,
            http_status=http_status,
            error_category=error_category[:200],
        )

    async def _close(
        self,
        reservation: ProviderReservation,
        *,
        outcome: ProviderCallOutcome,
        units_reported: int | None,
        units_charged: int,
        cost_usd_reported: Decimal | None,
        cost_usd_charged: Decimal | None,
        results_returned: int | None,
        pages_scraped: int,
        http_status: int | None,
        error_category: str | None,
    ) -> None:
        async with self._database.transaction() as session:
            await session.execute(
                sa.update(ProviderCall)
                .where(ProviderCall.id == reservation.call_id)
                .values(
                    outcome=outcome,
                    completed_at=sa.func.now(),
                    units_reported=units_reported,
                    units_charged=units_charged,
                    cost_usd_reported=cost_usd_reported,
                    cost_usd_charged=cost_usd_charged,
                    results_returned=results_returned,
                    pages_scraped=pages_scraped,
                    http_status=http_status,
                    error_category=error_category,
                )
            )
        log.info(
            "provider_call_recorded",
            provider=reservation.provider,
            kind=reservation.kind.value,
            outcome=outcome.value,
            units_charged=units_charged,
            units_reported=units_reported,
            results_returned=results_returned,
            pages_scraped=pages_scraped,
            http_status=http_status,
            error_category=error_category,
        )
        METRICS.inc(
            "stockbrain_provider_calls_total",
            labels={
                "provider": reservation.provider,
                "kind": reservation.kind.value,
                "phase": outcome.value.lower(),
            },
        )
        METRICS.inc(
            "stockbrain_provider_units_total",
            float(units_charged),
            labels={"provider": reservation.provider},
        )

    def _refused(self, kind: ProviderCallKind, refusal: BudgetRefusal, detail: str) -> None:
        log.warning(
            "provider_call_refused",
            provider=self._provider,
            kind=kind.value,
            refusal=refusal.value,
            detail=detail,
        )
        METRICS.inc(
            "stockbrain_provider_calls_refused_total",
            labels={"provider": self._provider, "kind": kind.value, "refusal": refusal.value},
        )


def _budget_lock_key(provider: str) -> int:
    """A stable per-provider advisory-lock key.

    Derived from the provider name so that adding a provider needs no constant,
    and so that two providers never share a lock.  CRC32 is not a security
    choice here -- it is a deterministic 32-bit fold of a short ASCII string
    that PostgreSQL will accept as a ``bigint``.
    """
    return zlib.crc32(f"stockbrain-budget:{provider}".encode())


def _refusal_for(
    kind: ProviderCallKind, state: ProviderBudgetState, units_needed: int
) -> tuple[BudgetRefusal, str] | None:
    """Which cap, if any, refuses this specific call.

    Checked against the cost of *this* call rather than against the current
    total, so a five-unit call cannot slip through two units of headroom.
    """
    if kind is ProviderCallKind.SEARCH and state.today.searches + 1 > state.max_searches_per_day:
        return (
            BudgetRefusal.SEARCH_LIMIT,
            f"searches today {state.today.searches} of {state.max_searches_per_day}",
        )
    if kind is ProviderCallKind.SCRAPE and state.today.scrapes + 1 > state.max_scrapes_per_day:
        return (
            BudgetRefusal.SCRAPE_LIMIT,
            f"scrapes today {state.today.scrapes} of {state.max_scrapes_per_day}",
        )
    if state.today.units + units_needed > state.daily_unit_cap:
        return (
            BudgetRefusal.DAILY_UNITS,
            f"estimated {state.unit_label} today {state.today.units} + {units_needed} would pass "
            f"the daily cap {state.daily_unit_cap}",
        )
    if state.month.units + units_needed > state.monthly_unit_cap:
        return (
            BudgetRefusal.MONTHLY_UNITS,
            f"estimated {state.unit_label} this month {state.month.units} + {units_needed} would "
            f"pass the monthly cap {state.monthly_unit_cap}",
        )
    return None


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


async def _usage_since(session: AsyncSession, provider: str, since: dt.datetime) -> ProviderUsage:
    """Sum one provider's ledger from ``since``.

    ``RESERVED`` rows are included: a call in flight, or a call whose process
    died before it could be reconciled, has already committed the operator's
    money as far as anyone here can tell.
    """
    row = (
        await session.execute(
            sa.select(
                sa.func.count().filter(ProviderCall.kind == ProviderCallKind.SEARCH),
                sa.func.count().filter(ProviderCall.kind == ProviderCallKind.SCRAPE),
                sa.func.coalesce(sa.func.sum(ProviderCall.units_charged), 0),
                sa.func.coalesce(sa.func.sum(ProviderCall.units_reported), 0),
                sa.func.coalesce(sa.func.sum(ProviderCall.results_returned), 0),
                sa.func.coalesce(sa.func.sum(ProviderCall.pages_scraped), 0),
                sa.func.coalesce(sa.func.sum(ProviderCall.cost_usd_charged), 0),
            ).where(ProviderCall.provider == provider, ProviderCall.reserved_at >= since)
        )
    ).one()
    return ProviderUsage(
        searches=int(row[0]),
        scrapes=int(row[1]),
        units=int(row[2]),
        reported_units=int(row[3]),
        results_returned=int(row[4]),
        pages_scraped=int(row[5]),
        cost_usd=Decimal(str(row[6])),
    )
