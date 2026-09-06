"""Provider health tracking.

Health is deliberately *per provider*.  A dead news feed must not make the
application report itself unhealthy, because research, reconciliation and the
approval path all keep working without it.  Only PostgreSQL is load-bearing
enough that losing it makes the application not-ready.

Statuses follow the specification: HEALTHY, DEGRADED, DOWN, DISABLED, UNKNOWN,
plus BUDGET_EXHAUSTED.  ``DISABLED`` means "deliberately switched off or not
configured" and never counts as a fault; ``UNKNOWN`` means "configured but not
yet checked"; ``BUDGET_EXHAUSTED`` means "switched on, working, and out of
allowance", which is a spending limit doing its job rather than a fault to
investigate.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from stockbrain.db.base import utcnow
from stockbrain.db.models.system import ProviderHealthRecord
from stockbrain.db.session import Database
from stockbrain.enums import ProviderStatus
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = [
    "ProviderHealth",
    "ProviderHealthRegistry",
    "ProviderName",
    "Subsystem",
    "aggregate_status",
]

log = get_logger(__name__)


class ProviderName(StrEnum):
    POSTGRES = "postgres"
    DEEPSEEK = "deepseek"
    ALPACA_NEWS = "alpaca_news"
    ALPACA_MARKET_DATA = "alpaca_market_data"
    BRAVE = "brave"
    EXA = "exa"
    FIRECRAWL = "firecrawl"
    CONTENT_EXTRACTION = "content_extraction"
    SEC = "sec"
    FRED = "fred"
    TRADING212 = "trading212"
    TELEGRAM = "telegram"
    TRADINGAGENTS = "tradingagents"


class Subsystem(StrEnum):
    """Functional areas whose health is derived from provider health."""

    DATABASE = "database"
    DISCOVERY = "discovery"
    RESEARCH = "research"
    EXECUTION = "execution"
    NOTIFICATIONS = "notifications"


#: Which providers contribute to which subsystem, and whether the subsystem can
#: survive on a subset of them.  Discovery, for example, degrades rather than
#: fails when Alpaca news is down but Brave and SEC still work.
#:
#: ``CONTENT_EXTRACTION`` is deliberately absent: failing to read one publisher's
#: page does not mean discovery stopped working, and folding it in here would
#: make a paywall look like an outage.
SUBSYSTEM_PROVIDERS: dict[Subsystem, tuple[ProviderName, ...]] = {
    Subsystem.DATABASE: (ProviderName.POSTGRES,),
    Subsystem.DISCOVERY: (
        ProviderName.ALPACA_NEWS,
        ProviderName.BRAVE,
        ProviderName.EXA,
        ProviderName.FIRECRAWL,
        ProviderName.SEC,
    ),
    Subsystem.RESEARCH: (ProviderName.DEEPSEEK, ProviderName.TRADINGAGENTS),
    Subsystem.EXECUTION: (ProviderName.TRADING212,),
    Subsystem.NOTIFICATIONS: (ProviderName.TELEGRAM,),
}

_SEVERITY: dict[ProviderStatus, int] = {
    ProviderStatus.HEALTHY: 0,
    ProviderStatus.DISABLED: 0,
    ProviderStatus.UNKNOWN: 1,
    # A spending limit doing its job sits below a fault, and above "not yet
    # checked": the provider is definitely not working, and definitely not
    # broken.
    ProviderStatus.BUDGET_EXHAUSTED: 2,
    ProviderStatus.DEGRADED: 3,
    ProviderStatus.DOWN: 4,
}


@dataclass(slots=True)
class ProviderHealth:
    provider: ProviderName
    status: ProviderStatus = ProviderStatus.UNKNOWN
    detail: str | None = None
    last_ok_at: dt.datetime | None = None
    last_checked_at: dt.datetime | None = None
    consecutive_failures: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)


def aggregate_status(statuses: list[ProviderStatus], *, require_all: bool) -> ProviderStatus:
    """Combine provider statuses into a subsystem status.

    ``require_all=True`` -- every provider is load-bearing, so the worst status
    wins (used for the database).

    ``require_all=False`` -- the subsystem is redundant: it is HEALTHY while at
    least one provider is healthy, DEGRADED if some but not all are usable, and
    DOWN only when every non-disabled provider has failed.
    """
    considered = [s for s in statuses if s is not ProviderStatus.DISABLED]
    if not considered:
        return ProviderStatus.DISABLED
    if require_all:
        return max(considered, key=lambda s: _SEVERITY[s])

    healthy = sum(1 for s in considered if s is ProviderStatus.HEALTHY)
    down = sum(1 for s in considered if s is ProviderStatus.DOWN)
    if healthy == len(considered):
        return ProviderStatus.HEALTHY
    if healthy > 0:
        return ProviderStatus.DEGRADED
    if down == len(considered):
        return ProviderStatus.DOWN
    if any(
        s in (ProviderStatus.DOWN, ProviderStatus.DEGRADED, ProviderStatus.BUDGET_EXHAUSTED)
        for s in considered
    ):
        # No provider is healthy and at least one has definitely stopped
        # working. That is a degraded subsystem, not an unknown one -- reporting
        # UNKNOWN here would hide a fully budget-exhausted discovery layer.
        return ProviderStatus.DEGRADED
    return ProviderStatus.UNKNOWN


class ProviderHealthRegistry:
    """In-memory current health, mirrored into PostgreSQL for the GUI and audit.

    The in-memory copy is authoritative for request handling so that a health
    endpoint never depends on a database round trip per provider.
    """

    def __init__(self, providers: dict[ProviderName, ProviderHealth] | None = None) -> None:
        self._states: dict[ProviderName, ProviderHealth] = providers or {
            name: ProviderHealth(provider=name) for name in ProviderName
        }
        self._lock = asyncio.Lock()
        self._schema_current = False
        self._schema_detail: str | None = "migration state not yet checked"

    @property
    def schema_state(self) -> tuple[bool, str | None]:
        """Whether the applied migration matches the code's head revision."""
        return self._schema_current, self._schema_detail

    def set_schema_state(self, current: bool, detail: str | None) -> None:
        self._schema_current = current
        self._schema_detail = detail

    def get_database_status(self) -> ProviderStatus:
        return self._states[ProviderName.POSTGRES].status

    def get(self, provider: ProviderName) -> ProviderHealth:
        return self._states[provider]

    def seconds_since_check(self, provider: ProviderName) -> float | None:
        """Age of the last check, or ``None`` if the provider has never been checked."""
        last = self._states[provider].last_checked_at
        if last is None:
            return None
        return (utcnow() - last).total_seconds()

    def snapshot(self) -> dict[ProviderName, ProviderHealth]:
        return dict(self._states)

    def set_disabled(self, provider: ProviderName, reason: str) -> None:
        state = self._states[provider]
        state.status = ProviderStatus.DISABLED
        state.detail = reason
        state.consecutive_failures = 0
        METRICS.set(
            "stockbrain_provider_status",
            float(_SEVERITY[ProviderStatus.DISABLED]),
            labels={"provider": provider.value},
        )

    def record(
        self,
        provider: ProviderName,
        status: ProviderStatus,
        *,
        detail: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> ProviderHealth:
        state = self._states[provider]
        now = utcnow()
        previous = state.status

        state.status = status
        state.detail = detail
        state.last_checked_at = now
        if metrics:
            state.metrics = metrics
        if status is ProviderStatus.HEALTHY:
            state.last_ok_at = now
            state.consecutive_failures = 0
        elif status in (ProviderStatus.DEGRADED, ProviderStatus.DOWN):
            state.consecutive_failures += 1
        elif status is ProviderStatus.BUDGET_EXHAUSTED:
            # Not a failure. Counting it as one would make a provider that
            # simply spent its allowance look like a provider that is broken,
            # and would eventually trip the failure-count alerting.
            state.consecutive_failures = 0

        METRICS.set(
            "stockbrain_provider_status",
            float(_SEVERITY[status]),
            labels={"provider": provider.value},
        )

        if previous is not status:
            log.info(
                "provider_health_changed",
                provider=provider.value,
                previous=previous.value,
                status=status.value,
                detail=detail,
            )
        return state

    def subsystem_status(self, subsystem: Subsystem) -> ProviderStatus:
        providers = SUBSYSTEM_PROVIDERS[subsystem]
        statuses = [self._states[p].status for p in providers]
        return aggregate_status(statuses, require_all=subsystem is Subsystem.DATABASE)

    def subsystems(self) -> dict[Subsystem, ProviderStatus]:
        return {subsystem: self.subsystem_status(subsystem) for subsystem in Subsystem}

    def overall_status(self) -> ProviderStatus:
        """Overall application status.

        Only the database can force DOWN.  Everything else can at worst degrade
        the application, which is the whole point of the subsystem model.
        """
        if self.subsystem_status(Subsystem.DATABASE) is not ProviderStatus.HEALTHY:
            return ProviderStatus.DOWN
        others = [
            self.subsystem_status(subsystem)
            for subsystem in Subsystem
            if subsystem is not Subsystem.DATABASE
        ]
        if any(status in (ProviderStatus.DOWN, ProviderStatus.DEGRADED) for status in others):
            return ProviderStatus.DEGRADED
        if any(status is ProviderStatus.UNKNOWN for status in others):
            return ProviderStatus.DEGRADED
        return ProviderStatus.HEALTHY

    async def persist(self, database: Database) -> None:
        """Upsert the current state into ``provider_health``.

        Failures here are logged and swallowed: health reporting must never take
        down the caller.
        """
        rows = [
            {
                "provider": state.provider.value,
                "status": state.status,
                "detail": state.detail,
                "last_ok_at": state.last_ok_at,
                "last_checked_at": state.last_checked_at,
                "consecutive_failures": state.consecutive_failures,
                "metrics": state.metrics,
            }
            for state in self._states.values()
        ]
        try:
            async with self._lock, database.transaction() as session:
                statement = pg_insert(ProviderHealthRecord).values(rows)
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[ProviderHealthRecord.provider],
                        set_={
                            "status": statement.excluded.status,
                            "detail": statement.excluded.detail,
                            "last_ok_at": statement.excluded.last_ok_at,
                            "last_checked_at": statement.excluded.last_checked_at,
                            "consecutive_failures": statement.excluded.consecutive_failures,
                            "metrics": statement.excluded.metrics,
                            "updated_at": utcnow(),
                        },
                    )
                )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("provider_health_persist_failed", error=str(exc))
