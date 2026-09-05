"""Pause, resume and the emergency kill switch.

The property that matters is *durability*: a safety control that a restart
clears is not a safety control.  Every test here therefore builds a **new**
service object against the same database to stand in for a restarted process,
and asserts the halt is still in force.

The second property is *reach*: the halt is enforced inside
``ProposalService`` rather than in each client, so web, Telegram and the
automatic path are all covered by one check and cannot diverge.

The third is what a halt does **not** do.  It closes no position and cancels no
order -- and that is structural rather than promised: no order, cancel or amend
method exists anywhere in the process for it to call.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.models.portfolio import Position
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.models.system import AppSetting, AuditLog
from stockbrain.db.session import Database
from stockbrain.enums import (
    AuthorizationSource,
    ControlFlag,
    ExecutionPolicy,
    ProposalStatus,
)
from stockbrain.errors import AuthorizationNotPermitted
from stockbrain.proposals.service import ProposalService
from tests import proposal_helpers as helpers

pytestmark = pytest.mark.integration

OWNER = 4242


def _service(database: Database, **overrides: object) -> ProposalService:
    """A fresh service object, as a restarted process would build."""
    settings: Settings = helpers.settings(**overrides)
    return helpers.service_with(
        database,
        settings,
        market_data=helpers.StubMarketData(),
        control=ControlStateService(database),
    )


async def _ready_proposal(database: Database, **overrides: object) -> uuid.UUID:
    service = _service(database, **overrides)
    await helpers.seed(database)
    await helpers.fund(database)
    result = await service.generate(helpers.THESIS_ID)
    assert result.proposal_id is not None, result.reason
    return result.proposal_id


# ---------------------------------------------------------------------------
# Durability
# ---------------------------------------------------------------------------
async def test_a_pause_survives_a_restart(clean_tables: Database) -> None:
    await ControlStateService(clean_tables).pause(
        actor=f"telegram:{OWNER}", source="HUMAN_TELEGRAM", reason="stepping away"
    )
    # A brand new service object, as a restarted process would build.
    snapshot = await ControlStateService(clean_tables).snapshot()
    assert snapshot.trading_halted
    assert snapshot.paused.active
    assert snapshot.paused.actor == f"telegram:{OWNER}"
    assert snapshot.paused.source == "HUMAN_TELEGRAM"
    assert snapshot.paused.reason == "stepping away"
    assert snapshot.paused.changed_at is not None
    assert "trading is paused" in " ".join(snapshot.blockers)


async def test_resume_lifts_a_pause(clean_tables: Database) -> None:
    control = ControlStateService(clean_tables)
    await control.pause(actor="web:local-operator", source="HUMAN_WEB")
    await control.resume(actor="web:local-operator", source="HUMAN_WEB")
    snapshot = await ControlStateService(clean_tables).snapshot()
    assert not snapshot.trading_halted
    assert snapshot.blockers == []


async def test_the_kill_switch_survives_a_restart(clean_tables: Database) -> None:
    await ControlStateService(clean_tables).engage_kill_switch(
        actor=f"telegram:{OWNER}", source="HUMAN_TELEGRAM", reason="emergency stop from Telegram"
    )
    snapshot = await ControlStateService(clean_tables).snapshot()
    assert snapshot.kill_switch.active
    assert snapshot.trading_halted
    assert "emergency kill switch is engaged" in " ".join(snapshot.blockers)


async def test_resume_does_not_release_the_kill_switch(clean_tables: Database) -> None:
    """The routine control must not undo the emergency one.

    If ``/resume`` cleared a kill switch, the emergency stop would be one
    habitual tap away from being lifted by somebody who only meant to restart
    normal work.
    """
    control = ControlStateService(clean_tables)
    await control.engage_kill_switch(actor="a", source="HUMAN_TELEGRAM")
    await control.pause(actor="a", source="HUMAN_TELEGRAM")
    snapshot = await control.resume(actor="a", source="HUMAN_TELEGRAM")
    assert not snapshot.paused.active
    assert snapshot.kill_switch.active
    assert snapshot.trading_halted

    released = await control.release_kill_switch(actor="a", source="HUMAN_WEB", reason="all clear")
    assert not released.trading_halted


async def test_the_state_lives_in_postgres_not_in_memory(clean_tables: Database) -> None:
    """One row per flag in ``app_settings``, readable with plain SQL."""
    await ControlStateService(clean_tables).engage_kill_switch(
        actor="web:local-operator", source="HUMAN_WEB", reason="drill"
    )
    async with clean_tables.session() as session:
        row = await session.get(AppSetting, ControlFlag.KILL_SWITCH.value)
    assert row is not None
    assert row.value["active"] is True
    assert row.value["actor"] == "web:local-operator"
    assert row.value["reason"] == "drill"
    assert row.updated_by == "web:local-operator"


async def test_a_control_change_is_audited_with_its_actor_and_source(
    clean_tables: Database,
) -> None:
    await ControlStateService(clean_tables).engage_kill_switch(
        actor=f"telegram:{OWNER}", source="HUMAN_TELEGRAM", reason="panic"
    )
    async with clean_tables.session() as session:
        entry = (
            await session.execute(
                sa.select(AuditLog).where(AuditLog.action == "control.kill_switch.engaged")
            )
        ).scalar_one()
    assert entry.actor_id == f"telegram:{OWNER}"
    assert entry.details["source"] == "HUMAN_TELEGRAM"
    assert entry.details["reason"] == "panic"
    # Recorded explicitly, because it is the question somebody will ask later.
    assert entry.details["broker_positions_touched"] is False
    assert entry.details["broker_orders_touched"] is False


async def test_an_unreadable_flag_row_does_not_silently_halt_the_system(
    clean_tables: Database,
) -> None:
    """A malformed row reports inactive and logs, rather than halting.

    Failing *open* is right only here: the absence of a row is the normal case
    for a deployment that never paused, so a shape nobody wrote must not be
    read as an emergency nobody declared.
    """
    async with clean_tables.transaction() as session:
        session.add(AppSetting(key=ControlFlag.TRADING_PAUSED.value, value={"garbage": True}))
    snapshot = await ControlStateService(clean_tables).snapshot()
    assert not snapshot.paused.active
    assert not snapshot.trading_halted


# ---------------------------------------------------------------------------
# Reach: what a halt actually stops
# ---------------------------------------------------------------------------
async def test_a_pause_stops_new_proposal_generation(clean_tables: Database) -> None:
    await helpers.seed(clean_tables)
    await helpers.fund(clean_tables)
    await ControlStateService(clean_tables).pause(actor="a", source="HUMAN_TELEGRAM")

    result = await _service(clean_tables).generate(helpers.THESIS_ID)
    assert not result.created
    assert "halted" in result.reason
    async with clean_tables.session() as session:
        count = int(
            (
                await session.execute(sa.select(sa.func.count()).select_from(TradeProposal))
            ).scalar_one()
        )
    assert count == 0


async def test_a_pause_stops_every_authorization_path(clean_tables: Database) -> None:
    """Web, Telegram and the system are all refused by the same check.

    Enforced inside ``ProposalService.authorize`` rather than in each client,
    so there is exactly one place for it to be right.
    """
    proposal_id = await _ready_proposal(clean_tables)
    await ControlStateService(clean_tables).pause(actor="a", source="HUMAN_TELEGRAM")

    for source in (
        AuthorizationSource.HUMAN_WEB,
        AuthorizationSource.HUMAN_TELEGRAM,
        AuthorizationSource.SYSTEM_AUTOMATIC,
    ):
        with pytest.raises(AuthorizationNotPermitted) as excinfo:
            await _service(clean_tables).authorize(proposal_id, source=source, actor="a")
        assert "halted" in str(excinfo.value)
    assert await _status(clean_tables, proposal_id) is not ProposalStatus.APPROVED


async def test_the_kill_switch_stops_automatic_authorization(clean_tables: Database) -> None:
    """Automatic mode is not a way around the emergency stop."""
    await helpers.seed(clean_tables)
    await helpers.fund(clean_tables)
    await ControlStateService(clean_tables).engage_kill_switch(
        actor=f"telegram:{OWNER}", source="HUMAN_TELEGRAM"
    )
    service = _service(clean_tables, execution_policy=ExecutionPolicy.AUTOMATIC)
    assert service.settings.automatic_authorization_permitted

    result = await service.generate(helpers.THESIS_ID)
    assert not result.created
    assert not result.authorized
    assert "kill switch" in result.reason


async def test_a_halt_does_not_touch_positions_or_orders(clean_tables: Database) -> None:
    """The kill switch must never liquidate.

    Asserted twice: the stored positions are byte-for-byte unchanged, and the
    control module contains no broker verb at all -- so there is nothing for a
    future change to start calling.
    """
    await helpers.seed(clean_tables)
    await helpers.fund(clean_tables, positions={"AAPL_US_EQ": (Decimal("10"), Decimal("10"))})

    async def snapshot_positions() -> list[tuple[str, Decimal]]:
        async with clean_tables.session() as session:
            rows = (
                await session.execute(
                    sa.select(Position.broker_ticker, Position.quantity).order_by(
                        Position.broker_ticker
                    )
                )
            ).all()
        return [(ticker, quantity) for ticker, quantity in rows]

    before = await snapshot_positions()
    await ControlStateService(clean_tables).engage_kill_switch(
        actor="a", source="HUMAN_TELEGRAM", reason="drill"
    )
    assert await snapshot_positions() == before

    from stockbrain.control import state as control_state

    # The module's own prose says "never liquidates a position" repeatedly, so a
    # substring scan of the source would fail on its documentation. What matters
    # is that no *identifier* it calls or references names a broker action, so
    # the scan runs over the names in the syntax tree.
    called = _referenced_names(inspect.getsource(control_state))
    for verb in (
        "place_order",
        "place_market_order",
        "cancel_order",
        "modify_order",
        "amend_order",
        "liquidate",
        "close_position",
        "sync",
        "fetch_positions",
    ):
        assert verb not in called, f"the control module must not reference {verb}"
    assert not any("trading212" in name for name in called)


def _referenced_names(source: str) -> set[str]:
    """Every identifier the module names, ignoring its prose entirely."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            names.add(node.attr.lower())
        elif isinstance(node, ast.alias):
            names.add(node.name.lower())
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.lower())
    return names


async def test_generation_backlog_is_not_enqueued_while_halted(
    clean_tables: Database,
) -> None:
    """A pause should not fill the queue with work it will refuse.

    The backlog is derived from the database rather than from an in-memory
    list, so nothing is lost: resuming picks the same theses up again.
    """
    await helpers.seed(clean_tables)
    await helpers.fund(clean_tables)
    service = _service(clean_tables)
    await ControlStateService(clean_tables).pause(actor="a", source="HUMAN_TELEGRAM")
    assert await service.enqueue_pending() == 0

    await ControlStateService(clean_tables).resume(actor="a", source="HUMAN_TELEGRAM")
    assert await service.enqueue_pending() == 1


async def test_authorization_works_again_after_a_resume(clean_tables: Database) -> None:
    proposal_id = await _ready_proposal(clean_tables)
    control = ControlStateService(clean_tables)
    await control.pause(actor="a", source="HUMAN_TELEGRAM")
    await control.resume(actor="a", source="HUMAN_TELEGRAM")

    await _service(clean_tables).authorize(
        proposal_id, source=AuthorizationSource.HUMAN_TELEGRAM, actor=f"telegram:{OWNER}"
    )
    assert await _status(clean_tables, proposal_id) is ProposalStatus.APPROVED


async def test_the_startup_report_reads_the_persisted_state(clean_tables: Database) -> None:
    """A process that comes back up halted has to say so."""
    await ControlStateService(clean_tables).engage_kill_switch(
        actor="a", source="HUMAN_TELEGRAM", reason="overnight"
    )
    restored = await ControlStateService(clean_tables).log_restored_state()
    assert restored.trading_halted
    assert restored.kill_switch.reason == "overnight"


async def _status(database: Database, proposal_id: uuid.UUID) -> ProposalStatus:
    async with database.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        return proposal.status
