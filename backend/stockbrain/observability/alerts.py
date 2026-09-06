"""Operational alerts: the conditions worth waking somebody for.

Phase 7 gave StockBrain a notification channel and used it for exactly one
thing: proposal transitions.  Phase 6's bug 12 shows why that is not enough --
144 events sat unclassified for hours, the container reported healthy, and
nothing told anybody.  The whole point of a self-hosted system on a NAS is that
nobody is watching it, so the conditions that need a human have to reach one.

Four rules shape this module, and each of them exists to keep the channel worth
reading:

**One row per condition, not per tick.**  The ``notifications.dedupe_key``
unique index carries a *window* stamp -- a day, or a shorter bucket for the
urgent ones -- so a provider that has been down for six hours produces one
message rather than three hundred and sixty.

**A deliberately disabled provider is never an alert.**  ``DISABLED`` is a
configuration statement, not a fault.  Alerting on it would train the operator
to ignore the channel, which is the failure mode that matters most: an alert
stream nobody reads is worse than no alerts, because it looks like coverage.

**Alerting never changes anything.**  This module reads state and writes
notification rows.  It cannot pause, cannot halt, cannot cancel and cannot
send an order -- it imports nothing from ``stockbrain.execution`` or
``stockbrain.risk``, and a test asserts it.

**Recovery is announced too.**  A condition that clears sends one "resolved"
message, so the last thing in the channel is the current state rather than the
worst state.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.models.proposals import ExecutionAttempt
from stockbrain.db.models.system import AppSetting, Notification
from stockbrain.db.session import Database
from stockbrain.enums import (
    ExecutionOutcome,
    NotificationClass,
    NotificationStatus,
    ProviderStatus,
)
from stockbrain.ingestion.provider_budget import ProviderCallBudget
from stockbrain.jobs.queue import JobQueue
from stockbrain.llm.budget import BudgetGuard, BudgetStatus
from stockbrain.logging import get_logger
from stockbrain.observability.health import ProviderHealthRegistry, ProviderName
from stockbrain.observability.metrics import METRICS

__all__ = ["ALERT_STATE_KEY", "AlertCondition", "AlertScan", "OperationalAlerts"]

log = get_logger(__name__)

#: Where the "which conditions were firing last time" record lives, so a
#: recovery message can be sent exactly once.
ALERT_STATE_KEY = "alerts.active"


class AlertCondition(StrEnum):
    """One member per condition, because the value is half the dedupe key.

    Names rather than sentences: an operator triages on the name and a metric
    aggregates on it, and a sentence would make both impossible.
    """

    PROVIDER_DOWN = "PROVIDER_DOWN"
    DATABASE_UNHEALTHY = "DATABASE_UNHEALTHY"
    QUEUE_STUCK = "QUEUE_STUCK"
    QUEUE_BACKLOG = "QUEUE_BACKLOG"
    DEAD_JOBS = "DEAD_JOBS"
    TRADING_HALTED = "TRADING_HALTED"
    EXECUTION_AMBIGUOUS = "EXECUTION_AMBIGUOUS"
    RECONCILIATION_UNRESOLVED = "RECONCILIATION_UNRESOLVED"
    BROKER_AUTH_FAILING = "BROKER_AUTH_FAILING"
    FX_UNAVAILABLE = "FX_UNAVAILABLE"
    LLM_BUDGET_EXHAUSTED = "LLM_BUDGET_EXHAUSTED"
    DISCOVERY_BUDGET_EXHAUSTED = "DISCOVERY_BUDGET_EXHAUSTED"


#: How long one firing of a condition suppresses the next.  Urgent conditions
#: repeat sooner, because "your order may or may not exist" deserves a second
#: message and "the news feed is down" does not.
_WINDOWS: dict[AlertCondition, dt.timedelta] = {
    AlertCondition.EXECUTION_AMBIGUOUS: dt.timedelta(hours=1),
    AlertCondition.DATABASE_UNHEALTHY: dt.timedelta(hours=1),
    AlertCondition.BROKER_AUTH_FAILING: dt.timedelta(hours=4),
    AlertCondition.TRADING_HALTED: dt.timedelta(hours=12),
    AlertCondition.RECONCILIATION_UNRESOLVED: dt.timedelta(hours=6),
    AlertCondition.QUEUE_STUCK: dt.timedelta(hours=2),
    AlertCondition.QUEUE_BACKLOG: dt.timedelta(hours=6),
    AlertCondition.DEAD_JOBS: dt.timedelta(hours=12),
    AlertCondition.PROVIDER_DOWN: dt.timedelta(hours=6),
    AlertCondition.FX_UNAVAILABLE: dt.timedelta(hours=12),
    AlertCondition.LLM_BUDGET_EXHAUSTED: dt.timedelta(hours=12),
    AlertCondition.DISCOVERY_BUDGET_EXHAUSTED: dt.timedelta(hours=12),
}

#: Conditions that mean money may already have moved, or is about to.
_CRITICAL: frozenset[AlertCondition] = frozenset(
    {
        AlertCondition.EXECUTION_AMBIGUOUS,
        AlertCondition.DATABASE_UNHEALTHY,
        AlertCondition.BROKER_AUTH_FAILING,
        AlertCondition.RECONCILIATION_UNRESOLVED,
    }
)

#: Providers whose absence is a genuine fault rather than a design choice.
#: Brave, Exa and Firecrawl are absent from this set on purpose: each is
#: *expected* to be off in a deployment that has not configured it, and their
#: budgets have their own condition.
_ALERTABLE_PROVIDERS: tuple[ProviderName, ...] = (
    ProviderName.POSTGRES,
    ProviderName.TRADING212,
    ProviderName.ALPACA_MARKET_DATA,
    ProviderName.LLM,
)


@dataclass(frozen=True, slots=True)
class AlertScan:
    """What one pass found.

    ``fired`` and ``resolved`` are what was *newly* written; ``active`` is
    everything currently true, which is what the GUI reads.
    """

    active: tuple[AlertCondition, ...] = ()
    fired: tuple[AlertCondition, ...] = ()
    resolved: tuple[AlertCondition, ...] = ()
    details: dict[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": [condition.value for condition in self.active],
            "fired": [condition.value for condition in self.fired],
            "resolved": [condition.value for condition in self.resolved],
            "details": dict(self.details or {}),
        }


class OperationalAlerts:
    """Scans for the conditions worth a message, and records them once each."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        health: ProviderHealthRegistry,
        llm_budget: BudgetGuard | None = None,
        budgets: dict[str, ProviderCallBudget] | None = None,
        queue: JobQueue | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._health = health
        self._llm_budget = llm_budget
        self._budgets = dict(budgets or {})
        self._queue = queue or JobQueue()

    async def scan(self, *, now: dt.datetime | None = None) -> AlertScan:
        """One pass.  Reads state, writes notification rows, changes nothing else."""
        moment = now or dt.datetime.now(dt.UTC)
        found: dict[AlertCondition, str] = {}

        found.update(await self._provider_conditions())
        found.update(await self._queue_conditions())
        found.update(await self._execution_conditions())
        found.update(await self._control_conditions())
        found.update(await self._budget_conditions())
        found.update(await self._fx_conditions())

        previous = await self._load_previous()
        fired: list[AlertCondition] = []
        for condition, detail in found.items():
            if await self._record(condition, detail, moment):
                fired.append(condition)

        resolved = [
            condition
            for condition in previous
            if condition not in found
            # A resolution is only interesting for something worth alerting on
            # in the first place.
            and condition in _WINDOWS
        ]
        for condition in resolved:
            await self._record_resolution(condition, moment)

        await self._store_active(tuple(found))
        for condition in found:
            METRICS.set(f"stockbrain_alert_active_{condition.value.lower()}", 1.0)
        for condition in resolved:
            METRICS.set(f"stockbrain_alert_active_{condition.value.lower()}", 0.0)

        if fired or resolved:
            log.warning(
                "operational_alerts",
                fired=[condition.value for condition in fired],
                resolved=[condition.value for condition in resolved],
                active=[condition.value for condition in found],
            )
        return AlertScan(
            active=tuple(found),
            fired=tuple(fired),
            resolved=tuple(resolved),
            details={key.value: value for key, value in found.items()},
        )

    # ------------------------------------------------------------------
    # Conditions
    # ------------------------------------------------------------------
    async def _provider_conditions(self) -> dict[AlertCondition, str]:
        """A ``DOWN`` provider that matters, and the database above all.

        ``DISABLED`` is never an alert: it is what the operator asked for.
        ``DEGRADED`` is not either -- degradation is the designed response to a
        provider having a bad minute, and alerting on it would fill the channel
        with things that fix themselves.
        """
        conditions: dict[AlertCondition, str] = {}
        snapshot = self._health.snapshot()
        down: list[str] = []
        for name in _ALERTABLE_PROVIDERS:
            state = snapshot.get(name)
            if state is None or state.status is not ProviderStatus.DOWN:
                continue
            if name is ProviderName.POSTGRES:
                conditions[AlertCondition.DATABASE_UNHEALTHY] = (
                    f"PostgreSQL is DOWN: {state.detail or 'no detail'}"
                )
                continue
            down.append(f"{name.value} ({state.detail or 'no detail'})")

        if down:
            conditions[AlertCondition.PROVIDER_DOWN] = "; ".join(down)

        # Repeated broker authentication failure is its own condition, because
        # the remedy is different from every other outage: nothing will recover
        # on its own and a live key against the demo host looks exactly like
        # this.
        broker = snapshot.get(ProviderName.TRADING212)
        if (
            broker is not None
            and broker.status is ProviderStatus.DOWN
            and broker.consecutive_failures >= 3
            and "auth" in (broker.detail or "").lower()
        ):
            conditions[AlertCondition.BROKER_AUTH_FAILING] = (
                f"Trading 212 has rejected {broker.consecutive_failures} consecutive "
                f"requests: {broker.detail or 'authentication failure'}"
            )
        return conditions

    async def _queue_conditions(self) -> dict[AlertCondition, str]:
        """The signals that would have caught Phase 6's bug 12 in one glance."""
        conditions: dict[AlertCondition, str] = {}
        async with self._database.session() as session:
            health = await self._queue.health(
                session, stuck_after_seconds=self._settings.job_claim_timeout_seconds
            )
        if health.stuck:
            conditions[AlertCondition.QUEUE_STUCK] = (
                f"{health.stuck} job(s) have been RUNNING longer than the "
                f"{self._settings.job_claim_timeout_seconds}s claim timeout: "
                f"{', '.join(sorted(set(health.stuck_job_types)))}"
            )
        if health.dead:
            conditions[AlertCondition.DEAD_JOBS] = (
                f"{health.dead} job(s) exhausted their retries and will not run again "
                f"without an operator"
            )
        oldest = health.oldest_pending_age_seconds
        if oldest is not None and oldest > self._settings.alert_queue_backlog_seconds:
            conditions[AlertCondition.QUEUE_BACKLOG] = (
                f"the oldest pending job ({health.oldest_pending_job_type}) has waited "
                f"{int(oldest)}s; {health.pending} job(s) are queued"
            )
        return conditions

    async def _execution_conditions(self) -> dict[AlertCondition, str]:
        """Ambiguity is the most important condition in the system.

        "The order may or may not exist" is the one state where doing nothing
        and doing something are both wrong, so it has the shortest repeat
        window and the CRITICAL class.
        """
        conditions: dict[AlertCondition, str] = {}
        async with self._database.session() as session:
            ambiguous = int(
                (
                    await session.execute(
                        sa.select(sa.func.count())
                        .select_from(ExecutionAttempt)
                        .where(
                            ExecutionAttempt.ambiguous.is_(True),
                            ExecutionAttempt.reconciled_at.is_(None),
                        )
                    )
                ).scalar_one()
            )
            stale_pending = int(
                (
                    await session.execute(
                        sa.select(sa.func.count())
                        .select_from(ExecutionAttempt)
                        .where(
                            ExecutionAttempt.sent_to_broker.is_(True),
                            ExecutionAttempt.outcome.in_(
                                (ExecutionOutcome.PENDING, ExecutionOutcome.AMBIGUOUS)
                            ),
                            ExecutionAttempt.reconciliation_attempts
                            >= self._settings.execution_reconcile_max_attempts,
                        )
                    )
                ).scalar_one()
            )
        if ambiguous:
            conditions[AlertCondition.EXECUTION_AMBIGUOUS] = (
                f"{ambiguous} execution attempt(s) are AMBIGUOUS and unreconciled. "
                f"Never resend: reconcile, or read the broker's own order list."
            )
        if stale_pending:
            conditions[AlertCondition.RECONCILIATION_UNRESOLVED] = (
                f"{stale_pending} attempt(s) have exhausted "
                f"{self._settings.execution_reconcile_max_attempts} reconciliation passes "
                f"and now need a human"
            )
        return conditions

    async def _control_conditions(self) -> dict[AlertCondition, str]:
        """A halt is worth a daily reminder.

        Not because the operator forgot they engaged it, but because a kill
        switch left on is indistinguishable from a system that is quietly not
        trading -- and the second is what a stale halt becomes after a week.
        """
        from stockbrain.control.state import ControlStateService

        snapshot = await ControlStateService(self._database).snapshot()
        if not snapshot.trading_halted:
            return {}
        return {
            AlertCondition.TRADING_HALTED: ("trading is halted: " + "; ".join(snapshot.blockers))
        }

    async def _budget_conditions(self) -> dict[AlertCondition, str]:
        conditions: dict[AlertCondition, str] = {}
        if self._llm_budget is not None:
            state = await self._llm_budget.state()
            if state.status is BudgetStatus.HARD_EXCEEDED:
                conditions[AlertCondition.LLM_BUDGET_EXHAUSTED] = (
                    f"{state.reason}. Ingestion, deterministic processing and broker "
                    f"reconciliation continue; new model work does not."
                )
        exhausted: list[str] = []
        for budget in self._budgets.values():
            # Only for providers that are actually switched on. A disabled
            # provider reporting "budget exhausted" every twelve hours is
            # exactly the noise that teaches an operator to stop reading the
            # channel.
            if not budget.enabled:
                continue
            state_provider = await budget.state()
            if state_provider.exhausted:
                exhausted.append(
                    f"{budget.provider}: " + "; ".join(state_provider.exhausted_reasons)
                )
        if exhausted:
            # One condition covering every metered discovery provider rather
            # than one per provider: an operator whose Brave and Exa allowances
            # both ran out has one thing to look at, not two messages.
            conditions[AlertCondition.DISCOVERY_BUDGET_EXHAUSTED] = (
                "; ".join(exhausted) + ". Alpaca news and SEC EDGAR are unaffected."
            )
        return conditions

    async def _fx_conditions(self) -> dict[AlertCondition, str]:
        """Only when FX is load-bearing.

        With ``FX_PROVIDER=none`` there is no rate to be missing and nothing to
        say -- that is the default and it is a deliberate, safe setting. With a
        provider *chosen* and unusable, every cross-currency proposal is blocked,
        and that is worth knowing before wondering why nothing is being
        proposed.
        """
        from stockbrain.config import FxProviderName

        if self._settings.fx_provider is FxProviderName.NONE:
            return {}
        blockers = self._settings.fx_blockers
        if not blockers:
            return {}
        return {
            AlertCondition.FX_UNAVAILABLE: (
                "cross-currency sizing is blocked: " + "; ".join(blockers)
            )
        }

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    async def _record(self, condition: AlertCondition, detail: str, now: dt.datetime) -> bool:
        """Write one notification, or return ``False`` because one already exists.

        The suppression is the ``notifications.dedupe_key`` unique index, not a
        timestamp comparison in Python: two workers scanning at once must not
        both announce, and a restart must not re-announce.
        """
        window = _WINDOWS.get(condition, dt.timedelta(hours=12))
        bucket = int(now.timestamp() // max(1, int(window.total_seconds())))
        key = f"alert:{condition.value}:{bucket}"
        notification_class = (
            NotificationClass.CRITICAL
            if condition in _CRITICAL
            else NotificationClass.SYSTEM_WARNING
        )
        try:
            async with self._database.transaction() as session:
                savepoint = await session.begin_nested()
                try:
                    session.add(
                        Notification(
                            notification_class=notification_class,
                            channel="telegram",
                            title=f"StockBrain: {condition.value}",
                            body=detail[:2000],
                            status=NotificationStatus.PENDING,
                            entity_type="alert",
                            dedupe_key=key,
                        )
                    )
                    await session.flush()
                except Exception:
                    await savepoint.rollback()
                    return False
                await savepoint.commit()
        except Exception:  # pragma: no cover - the savepoint handles the race
            return False
        return True

    async def _record_resolution(self, condition: AlertCondition, now: dt.datetime) -> None:
        """So the last thing in the channel is the current state.

        A channel whose most recent message is six hours old and says "the
        broker is down" is a channel that misinforms.
        """
        bucket = int(now.timestamp() // 60)
        async with self._database.transaction() as session:
            savepoint = await session.begin_nested()
            try:
                session.add(
                    Notification(
                        notification_class=NotificationClass.SYSTEM_WARNING,
                        channel="telegram",
                        title=f"StockBrain: {condition.value} resolved",
                        body=f"{condition.value} is no longer active.",
                        status=NotificationStatus.PENDING,
                        entity_type="alert",
                        dedupe_key=f"alert-resolved:{condition.value}:{bucket}",
                    )
                )
                await session.flush()
            except Exception:
                await savepoint.rollback()
                return
            await savepoint.commit()

    async def _load_previous(self) -> tuple[AlertCondition, ...]:
        async with self._database.session() as session:
            row = await session.get(AppSetting, ALERT_STATE_KEY)
        if row is None:
            return ()
        raw = row.value.get("active")
        if not isinstance(raw, list):
            return ()
        conditions: list[AlertCondition] = []
        for item in raw:
            try:
                conditions.append(AlertCondition(str(item)))
            except ValueError:
                # A condition removed in a later version. Ignored rather than
                # raised: an old row must not stop the scan.
                continue
        return tuple(conditions)

    async def _store_active(self, active: tuple[AlertCondition, ...]) -> None:
        payload = {"active": [condition.value for condition in active]}
        async with self._database.transaction() as session:
            row = await session.get(AppSetting, ALERT_STATE_KEY)
            if row is None:
                session.add(
                    AppSetting(
                        key=ALERT_STATE_KEY,
                        value=payload,
                        description="Operational alert conditions active at the last scan.",
                        updated_by="system:alerts",
                    )
                )
            else:
                row.value = payload
                row.updated_by = "system:alerts"
