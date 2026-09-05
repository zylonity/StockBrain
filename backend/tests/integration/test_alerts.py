"""Operational alerts: what fires, what does not, and how often.

Phase 6's bug 12 is the reference case: 144 events sat unclassified for hours,
the container reported healthy, ``/api/health`` was green, and the queue
recorded every job as *succeeded*.  Nothing told anybody.  These tests assert
that each condition of that shape now reaches the channel -- and, just as
importantly, that the conditions which are *supposed* to be true do not, because
an alert stream nobody reads is worse than no alerts.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.api.auth import hash_password
from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.models.proposals import ExecutionAttempt, TradeProposal
from stockbrain.db.models.system import Job, Notification
from stockbrain.db.session import Database
from stockbrain.enums import (
    ExecutionOutcome,
    JobStatus,
    NotificationClass,
    NotificationStatus,
    OrderSide,
    OrderType,
    PriceSource,
    ProposalStatus,
    ProviderStatus,
)
from stockbrain.ingestion.firecrawl_budget import FirecrawlBudget
from stockbrain.observability.alerts import AlertCondition, OperationalAlerts
from stockbrain.observability.health import ProviderHealthRegistry, ProviderName

pytestmark = pytest.mark.integration


def _settings(database: Database, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "log_level": "CRITICAL",
        "web_auth_enabled": False,
        "database_url": database.engine.url.render_as_string(hide_password=False),
        "stockbrain_secret_key": "test-key",
        "web_owner_password_hash": hash_password("a-long-enough-password"),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _alerts(
    database: Database,
    *,
    health: ProviderHealthRegistry | None = None,
    firecrawl: FirecrawlBudget | None = None,
    **overrides: object,
) -> OperationalAlerts:
    return OperationalAlerts(
        database,
        _settings(database, **overrides),
        health=health or ProviderHealthRegistry(),
        firecrawl_budget=firecrawl,
    )


async def _notifications(database: Database) -> list[Notification]:
    async with database.session() as session:
        return list(
            (
                await session.execute(
                    sa.select(Notification)
                    .where(Notification.entity_type == "alert")
                    .order_by(Notification.created_at)
                )
            ).scalars()
        )


async def _proposal_with_attempt(
    database: Database, *, outcome: ExecutionOutcome, reconciliation_attempts: int = 0
) -> uuid.UUID:
    """A minimal transmitted attempt, built directly.

    Deliberately not driven through the full pipeline: the condition under test
    is "an ambiguous attempt exists", and reaching that state through generation
    and authorization would test the pipeline instead.
    """
    proposal_id = uuid.uuid4()
    async with database.transaction() as session:
        session.add(
            TradeProposal(
                id=proposal_id,
                broker="TRADING212",
                broker_ticker="AAPL_US_EQ",
                account_id="12345",
                broker_environment="demo",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                proposed_quantity=Decimal("1"),
                reference_price=Decimal("200"),
                reference_currency="USD",
                price_source=PriceSource.ALPACA_IEX,
                quote_timestamp=dt.datetime.now(dt.UTC),
                quote_age_ms=100,
                estimated_notional=Decimal("200"),
                account_currency="USD",
                status=ProposalStatus.EXECUTING,
                expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=30),
            )
        )
        await session.flush()
        session.add(
            ExecutionAttempt(
                proposal_id=proposal_id,
                attempt_number=1,
                started_at=dt.datetime.now(dt.UTC),
                broker_environment="demo",
                request_payload={},
                request_fingerprint=uuid.uuid4().hex,
                sent_to_broker=True,
                sent_at=dt.datetime.now(dt.UTC),
                outcome=outcome,
                ambiguous=outcome is ExecutionOutcome.AMBIGUOUS,
                reconciliation_attempts=reconciliation_attempts,
            )
        )
    return proposal_id


# ---------------------------------------------------------------------------
# A quiet system
# ---------------------------------------------------------------------------
async def test_a_healthy_system_fires_nothing(clean_tables: Database) -> None:
    """The baseline that makes every other assertion here meaningful."""
    scan = await _alerts(clean_tables).scan()
    assert scan.active == ()
    assert scan.fired == ()
    assert await _notifications(clean_tables) == []


async def test_a_disabled_provider_is_never_an_alert(clean_tables: Database) -> None:
    """``DISABLED`` is a configuration statement, not a fault.

    Alerting on it would train the operator to ignore the channel -- which is
    the failure mode that matters most, because it looks like coverage.
    """
    registry = ProviderHealthRegistry()
    for name in ProviderName:
        registry.record(name, ProviderStatus.DISABLED, detail="not configured")
    scan = await _alerts(clean_tables, health=registry).scan()
    assert AlertCondition.PROVIDER_DOWN not in scan.active
    assert AlertCondition.DATABASE_UNHEALTHY not in scan.active


async def test_a_degraded_provider_is_not_an_alert(clean_tables: Database) -> None:
    """Degradation is the designed response to a provider having a bad minute.

    Alerting on it would fill the channel with things that fix themselves.
    """
    registry = ProviderHealthRegistry()
    registry.record(ProviderName.ALPACA_MARKET_DATA, ProviderStatus.DEGRADED, detail="slow")
    scan = await _alerts(clean_tables, health=registry).scan()
    assert scan.active == ()


async def test_a_disabled_firecrawl_budget_is_never_an_alert(
    clean_tables: Database,
) -> None:
    """Firecrawl is *expected* to be off.

    Its budget looks exhausted the moment the caps are zero, and a message
    about it every twelve hours would be pure noise.
    """
    budget = FirecrawlBudget(
        clean_tables,
        enabled=False,
        blockers=("FIRECRAWL_ENABLED is false",),
        max_searches_per_day=0,
        max_scrapes_per_day=0,
        daily_credit_cap=0,
        monthly_credit_cap=0,
    )
    scan = await _alerts(clean_tables, firecrawl=budget).scan()
    assert AlertCondition.FIRECRAWL_BUDGET_EXHAUSTED not in scan.active


async def test_fx_is_not_an_alert_when_no_provider_is_configured(
    clean_tables: Database,
) -> None:
    """``FX_PROVIDER=none`` is the default and is a safe, deliberate setting.

    There is no rate to be missing, so there is nothing to say.
    """
    scan = await _alerts(clean_tables).scan()
    assert AlertCondition.FX_UNAVAILABLE not in scan.active


# ---------------------------------------------------------------------------
# Conditions that must fire
# ---------------------------------------------------------------------------
async def test_a_down_database_is_critical(clean_tables: Database) -> None:
    registry = ProviderHealthRegistry()
    registry.record(ProviderName.POSTGRES, ProviderStatus.DOWN, detail="connection refused")
    scan = await _alerts(clean_tables, health=registry).scan()
    assert AlertCondition.DATABASE_UNHEALTHY in scan.active

    notifications = await _notifications(clean_tables)
    assert len(notifications) == 1
    assert notifications[0].notification_class is NotificationClass.CRITICAL
    assert "connection refused" in notifications[0].body


async def test_a_down_broker_fires_and_names_the_provider(
    clean_tables: Database,
) -> None:
    registry = ProviderHealthRegistry()
    registry.record(ProviderName.TRADING212, ProviderStatus.DOWN, detail="503 from broker")
    scan = await _alerts(clean_tables, health=registry).scan()
    assert AlertCondition.PROVIDER_DOWN in scan.active
    assert "trading212" in (await _notifications(clean_tables))[0].body


async def test_repeated_broker_auth_failure_is_its_own_condition(
    clean_tables: Database,
) -> None:
    """The remedy is different from every other outage.

    Nothing recovers on its own, and a live key pointed at the demo host looks
    exactly like this -- which is the Phase 4 finding, now alerted on.
    """
    registry = ProviderHealthRegistry()
    for _ in range(4):
        registry.record(
            ProviderName.TRADING212,
            ProviderStatus.DOWN,
            detail="ProviderAuthError: auth rejected (HTTP 401)",
        )
    scan = await _alerts(clean_tables, health=registry).scan()
    assert AlertCondition.BROKER_AUTH_FAILING in scan.active
    bodies = " ".join(n.body for n in await _notifications(clean_tables))
    assert "consecutive" in bodies


async def test_a_stuck_job_fires(clean_tables: Database) -> None:
    """The signal that would have caught bug 12 in one glance.

    A job RUNNING past the claim timeout is a worker that died or a handler
    that hangs, and the queue records neither as a failure.
    """
    async with clean_tables.transaction() as session:
        session.add(
            Job(
                job_type="CLASSIFY_EVENT",
                payload={},
                status=JobStatus.RUNNING,
                locked_by="worker#0",
                locked_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
                attempts=1,
            )
        )
    scan = await _alerts(clean_tables).scan()
    assert AlertCondition.QUEUE_STUCK in scan.active
    assert "CLASSIFY_EVENT" in (await _notifications(clean_tables))[0].body


async def test_a_dead_job_fires(clean_tables: Database) -> None:
    """Terminal, and it will never run again without an operator.

    Counted apart from a job that is merely between retries, because only one
    of the two needs a human.
    """
    async with clean_tables.transaction() as session:
        session.add(
            Job(
                job_type="RUN_RESEARCH",
                payload={},
                status=JobStatus.FAILED,
                attempts=3,
                max_attempts=3,
                last_error="boom",
            )
        )
    scan = await _alerts(clean_tables).scan()
    assert AlertCondition.DEAD_JOBS in scan.active


async def test_a_stale_backlog_fires(clean_tables: Database) -> None:
    async with clean_tables.transaction() as session:
        session.add(
            Job(
                job_type="CLASSIFY_EVENT",
                payload={},
                status=JobStatus.PENDING,
                created_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=3),
            )
        )
    scan = await _alerts(clean_tables, alert_queue_backlog_seconds=600.0).scan()
    assert AlertCondition.QUEUE_BACKLOG in scan.active


async def test_an_ambiguous_execution_is_critical_and_says_not_to_resend(
    clean_tables: Database,
) -> None:
    """The most important condition in the system.

    "The order may or may not exist" is the one state where doing nothing and
    doing something are both wrong, so the message carries the instruction
    rather than leaving it to the runbook.
    """
    await _proposal_with_attempt(clean_tables, outcome=ExecutionOutcome.AMBIGUOUS)
    scan = await _alerts(clean_tables).scan()
    assert AlertCondition.EXECUTION_AMBIGUOUS in scan.active

    notification = (await _notifications(clean_tables))[0]
    assert notification.notification_class is NotificationClass.CRITICAL
    assert "Never resend" in notification.body


async def test_an_attempt_out_of_reconciliation_passes_fires(
    clean_tables: Database,
) -> None:
    """An ambiguous order is not a thing to poll forever.

    Past the ceiling it stops being swept and waits for a human, and this is
    what tells the human.
    """
    await _proposal_with_attempt(
        clean_tables, outcome=ExecutionOutcome.PENDING, reconciliation_attempts=25
    )
    scan = await _alerts(clean_tables, execution_reconcile_max_attempts=20).scan()
    assert AlertCondition.RECONCILIATION_UNRESOLVED in scan.active


async def test_a_reconciled_attempt_no_longer_fires(clean_tables: Database) -> None:
    """Resolution is what clears it, not time."""
    proposal_id = await _proposal_with_attempt(clean_tables, outcome=ExecutionOutcome.AMBIGUOUS)
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(ExecutionAttempt)
            .where(ExecutionAttempt.proposal_id == proposal_id)
            .values(reconciled_at=dt.datetime.now(dt.UTC))
        )
    scan = await _alerts(clean_tables).scan()
    assert AlertCondition.EXECUTION_AMBIGUOUS not in scan.active


async def test_an_engaged_kill_switch_fires(clean_tables: Database) -> None:
    """Not because the operator forgot they engaged it.

    A kill switch left on is indistinguishable from a system that is quietly
    not trading, and the second is what a stale halt becomes after a week.
    """
    await ControlStateService(clean_tables).engage_kill_switch(
        actor="test", source="test", reason="drill"
    )
    scan = await _alerts(clean_tables).scan()
    assert AlertCondition.TRADING_HALTED in scan.active
    assert "drill" in (await _notifications(clean_tables))[0].body


async def test_an_exhausted_firecrawl_budget_fires_and_says_what_still_works(
    clean_tables: Database,
) -> None:
    """Budget exhaustion is not a global failure.

    The message says so explicitly, because the operator's first question on
    reading it is "has discovery stopped".
    """
    from stockbrain.enums import FirecrawlCallKind

    budget = FirecrawlBudget(
        clean_tables,
        enabled=True,
        max_searches_per_day=1,
        max_scrapes_per_day=1,
        daily_credit_cap=100,
        monthly_credit_cap=100,
    )
    assert await budget.reserve(FirecrawlCallKind.SEARCH, credits_needed=2) is not None

    alerts = _alerts(
        clean_tables,
        firecrawl=budget,
        firecrawl_enabled=True,
        firecrawl_api_key="fc-test",
    )
    scan = await alerts.scan()
    assert AlertCondition.FIRECRAWL_BUDGET_EXHAUSTED in scan.active
    body = (await _notifications(clean_tables))[0].body
    assert "Alpaca news and SEC EDGAR are unaffected" in body


async def test_a_configured_but_unusable_fx_source_fires(clean_tables: Database) -> None:
    """A rate that cannot be obtained blocks every cross-currency proposal.

    Worth knowing *before* wondering why nothing is being proposed.
    """
    alerts = _alerts(
        clean_tables,
        fx_provider="alpaca",
        alpaca_api_key="",
        alpaca_api_secret="",
    )
    scan = await alerts.scan()
    assert AlertCondition.FX_UNAVAILABLE in scan.active


# ---------------------------------------------------------------------------
# Suppression and recovery
# ---------------------------------------------------------------------------
async def test_a_condition_that_persists_produces_one_message_per_window(
    clean_tables: Database,
) -> None:
    """A provider down for six hours must produce one message, not 360.

    The suppression is the ``notifications.dedupe_key`` unique index, not a
    timestamp comparison in Python -- so two workers scanning at once cannot
    both announce, and a restart cannot re-announce.
    """
    registry = ProviderHealthRegistry()
    registry.record(ProviderName.TRADING212, ProviderStatus.DOWN, detail="503")
    alerts = _alerts(clean_tables, health=registry)

    first = await alerts.scan()
    second = await alerts.scan()
    third = await alerts.scan()

    assert AlertCondition.PROVIDER_DOWN in first.fired
    assert second.fired == () and third.fired == ()
    assert len(await _notifications(clean_tables)) == 1


async def test_the_window_rolls_over_and_the_condition_repeats(
    clean_tables: Database,
) -> None:
    """A persistent condition is re-announced eventually.

    Six hours for a down provider, one hour for an ambiguous execution: the
    urgent ones repeat sooner, because "your order may or may not exist"
    deserves a second message and "the news feed is down" does not.
    """
    registry = ProviderHealthRegistry()
    registry.record(ProviderName.TRADING212, ProviderStatus.DOWN, detail="503")
    alerts = _alerts(clean_tables, health=registry)

    now = dt.datetime(2026, 9, 5, 0, 0, tzinfo=dt.UTC)
    await alerts.scan(now=now)
    await alerts.scan(now=now + dt.timedelta(hours=1))
    later = await alerts.scan(now=now + dt.timedelta(hours=7))
    assert AlertCondition.PROVIDER_DOWN in later.fired
    assert len(await _notifications(clean_tables)) == 2


async def test_a_cleared_condition_announces_its_recovery_once(
    clean_tables: Database,
) -> None:
    """So the last thing in the channel is the current state.

    A channel whose most recent message is six hours old and says "the broker
    is down" is a channel that misinforms.
    """
    registry = ProviderHealthRegistry()
    registry.record(ProviderName.TRADING212, ProviderStatus.DOWN, detail="503")
    alerts = _alerts(clean_tables, health=registry)
    await alerts.scan()

    registry.record(ProviderName.TRADING212, ProviderStatus.HEALTHY)
    recovered = await alerts.scan()
    assert AlertCondition.PROVIDER_DOWN in recovered.resolved
    assert recovered.active == ()

    bodies = [n.title for n in await _notifications(clean_tables)]
    assert any("resolved" in title for title in bodies)

    # And once, not on every subsequent tick.
    quiet = await alerts.scan()
    assert quiet.resolved == ()


async def test_the_active_set_survives_a_new_alerts_object(
    clean_tables: Database,
) -> None:
    """A restart must not re-announce everything, or announce a recovery that
    never happened. The active set is a durable row for the same reason the
    Firecrawl budget is a table."""
    registry = ProviderHealthRegistry()
    registry.record(ProviderName.TRADING212, ProviderStatus.DOWN, detail="503")
    await _alerts(clean_tables, health=registry).scan()

    restarted = _alerts(clean_tables, health=registry)
    scan = await restarted.scan()
    assert scan.fired == ()
    assert AlertCondition.PROVIDER_DOWN in scan.active


async def test_every_alert_is_a_pending_row_for_the_delivery_job(
    clean_tables: Database,
) -> None:
    """Delivery is a job, not an inline send.

    An alert that already happened must not fail because Telegram is
    unreachable, and the row is the system of record either way.
    """
    registry = ProviderHealthRegistry()
    registry.record(ProviderName.POSTGRES, ProviderStatus.DOWN, detail="gone")
    await _alerts(clean_tables, health=registry).scan()

    notifications = await _notifications(clean_tables)
    assert notifications
    assert all(n.status is NotificationStatus.PENDING for n in notifications)
    assert all(n.channel == "telegram" for n in notifications)
    assert all(n.dedupe_key and n.dedupe_key.startswith("alert") for n in notifications)


async def test_alerting_never_touches_the_broker_or_the_risk_engine() -> None:
    """Structural, not aspirational.

    The alert scanner reads state and writes rows. If it could pause, halt,
    cancel or send, an alerting bug would become a trading bug.
    """
    import inspect

    from stockbrain.observability import alerts as module

    # Import lines only: the prose in the docstring names these modules in
    # order to say it does not use them, and a scan of the whole file would
    # trip over its own explanation.
    imports = [
        line
        for line in inspect.getsource(module).splitlines()
        if line.startswith(("import ", "from ")) or line.strip().startswith(("import ", "from "))
    ]
    joined = "\n".join(imports)
    assert "stockbrain.risk" not in joined
    assert "stockbrain.execution.service" not in joined
    assert "stockbrain.execution.reconciliation" not in joined
    assert "stockbrain.broker" not in joined

    source = inspect.getsource(module)
    for verb in ("submit", "place_order", "cancel_order", "engage_kill_switch", "pause_trading"):
        assert f".{verb}(" not in source, verb
