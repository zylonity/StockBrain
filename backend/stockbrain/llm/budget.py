"""LLM spend budgets.

Two thresholds, two behaviours:

* **Soft limit** — warn and degrade the *research* subsystem's health, and stop
  spending on optional low-value work. Essential classification continues.
* **Hard limit** — stop starting new non-essential LLM analysis entirely.

What a hard limit must never stop: ingestion, deterministic deduplication,
broker reconciliation, portfolio safety alerts. Running out of model budget is
not a reason to stop knowing what the broker thinks your account holds. That is
enforced structurally -- the budget guard is consulted only by LLM callers, and
nothing in the ingestion or broker paths asks it anything.

Spend is summed from ``llm_calls``, so the number always matches the recorded
history and needs no reconciliation after a restart.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from stockbrain.db.session import Database
from stockbrain.llm.telemetry import LlmTelemetry
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = ["BudgetDecision", "BudgetGuard", "BudgetState", "BudgetStatus", "WorkPriority"]

log = get_logger(__name__)


class BudgetStatus(StrEnum):
    OK = "OK"
    SOFT_EXCEEDED = "SOFT_EXCEEDED"
    HARD_EXCEEDED = "HARD_EXCEEDED"


class WorkPriority(StrEnum):
    """How important a piece of LLM work is.

    ``ESSENTIAL`` covers classification of already-ingested events: skipping it
    strands data. ``OPTIONAL`` covers work that improves quality but whose
    absence loses nothing -- semantic deduplication falls here, because the
    deterministic layers still run and the worst outcome is a duplicate event.
    """

    ESSENTIAL = "ESSENTIAL"
    OPTIONAL = "OPTIONAL"


@dataclass(frozen=True, slots=True)
class BudgetState:
    status: BudgetStatus
    daily_spend: Decimal
    monthly_spend: Decimal
    daily_soft: Decimal
    daily_hard: Decimal
    monthly_soft: Decimal
    monthly_hard: Decimal
    reason: str | None = None

    @property
    def degraded(self) -> bool:
        return self.status is not BudgetStatus.OK


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    allowed: bool
    state: BudgetState
    reason: str | None = None


class BudgetGuard:
    """Decides whether a piece of LLM work may start."""

    def __init__(
        self,
        database: Database,
        *,
        daily_soft_usd: Decimal,
        daily_hard_usd: Decimal,
        monthly_soft_usd: Decimal,
        monthly_hard_usd: Decimal,
        telemetry: LlmTelemetry | None = None,
        cache_seconds: float = 30.0,
    ) -> None:
        self._database = database
        self._daily_soft = daily_soft_usd
        self._daily_hard = daily_hard_usd
        self._monthly_soft = monthly_soft_usd
        self._monthly_hard = monthly_hard_usd
        self._telemetry = telemetry or LlmTelemetry()
        self._cache_seconds = cache_seconds
        self._cached: BudgetState | None = None
        self._cached_at: dt.datetime | None = None

    @staticmethod
    def _day_start(now: dt.datetime) -> dt.datetime:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _month_start(now: dt.datetime) -> dt.datetime:
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async def state(self, *, now: dt.datetime | None = None, refresh: bool = False) -> BudgetState:
        """Current budget state, cached briefly.

        Summing spend on every single call would add a query per classification;
        a short cache keeps the guard cheap without letting it go far out of date.
        """
        moment = now or dt.datetime.now(dt.UTC)
        if (
            not refresh
            and self._cached is not None
            and self._cached_at is not None
            and (moment - self._cached_at).total_seconds() < self._cache_seconds
        ):
            return self._cached

        async with self._database.session() as session:
            daily = await self._telemetry.spend_since(session, self._day_start(moment))
            monthly = await self._telemetry.spend_since(session, self._month_start(moment))

        status = BudgetStatus.OK
        reason: str | None = None
        if daily >= self._daily_hard:
            status, reason = (
                BudgetStatus.HARD_EXCEEDED,
                (f"daily LLM spend ${daily:.4f} reached the hard limit ${self._daily_hard:.2f}"),
            )
        elif monthly >= self._monthly_hard:
            status, reason = (
                BudgetStatus.HARD_EXCEEDED,
                (
                    f"monthly LLM spend ${monthly:.4f} reached the hard limit "
                    f"${self._monthly_hard:.2f}"
                ),
            )
        elif daily >= self._daily_soft:
            status, reason = (
                BudgetStatus.SOFT_EXCEEDED,
                (f"daily LLM spend ${daily:.4f} passed the soft limit ${self._daily_soft:.2f}"),
            )
        elif monthly >= self._monthly_soft:
            status, reason = (
                BudgetStatus.SOFT_EXCEEDED,
                (
                    f"monthly LLM spend ${monthly:.4f} passed the soft limit "
                    f"${self._monthly_soft:.2f}"
                ),
            )

        state = BudgetState(
            status=status,
            daily_spend=daily,
            monthly_spend=monthly,
            daily_soft=self._daily_soft,
            daily_hard=self._daily_hard,
            monthly_soft=self._monthly_soft,
            monthly_hard=self._monthly_hard,
            reason=reason,
        )

        if self._cached is None or self._cached.status is not status:
            log.info(
                "llm_budget_status",
                status=status.value,
                daily_spend=str(daily),
                monthly_spend=str(monthly),
                reason=reason,
            )

        METRICS.set("stockbrain_llm_spend_daily_usd", float(daily))
        METRICS.set("stockbrain_llm_spend_monthly_usd", float(monthly))
        self._cached = state
        self._cached_at = moment
        return state

    async def check(
        self, priority: WorkPriority, *, now: dt.datetime | None = None
    ) -> BudgetDecision:
        """Whether work of this priority may start."""
        state = await self.state(now=now)

        if state.status is BudgetStatus.HARD_EXCEEDED:
            # Even essential analysis stops: continuing would spend money the
            # operator has explicitly capped. Ingestion and deduplication keep
            # running, so nothing is lost -- classification resumes when the
            # budget rolls over or the limit is raised.
            return BudgetDecision(allowed=False, state=state, reason=state.reason)

        if state.status is BudgetStatus.SOFT_EXCEEDED and priority is WorkPriority.OPTIONAL:
            return BudgetDecision(
                allowed=False,
                state=state,
                reason=f"optional work suppressed: {state.reason}",
            )

        return BudgetDecision(allowed=True, state=state)

    def invalidate(self) -> None:
        """Drop the cache, so the next check re-reads spend.

        Called after a call that spent money, so crossing a limit takes effect
        immediately rather than up to ``cache_seconds`` later.
        """
        self._cached_at = None
