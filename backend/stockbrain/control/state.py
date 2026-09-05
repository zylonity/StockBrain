"""Pause, resume and the emergency kill switch, persisted in PostgreSQL.

Why this is not a boolean on the service container: a safety control that a
restart clears is not a safety control.  If the process is killed while the
operator has stopped trading -- by a crash, by a NAS reboot, by ``docker
compose up`` -- it must come back stopped.  So both flags live in
``app_settings``, one row each, and every read goes to the database.  There is
deliberately no in-memory cache: the value is consulted a handful of times a
minute, and a cache is precisely the thing that would let two workers disagree
about whether trading is halted.

Two flags, on purpose, with different meanings:

``control.trading_paused`` (``/pause`` / ``/resume``)
    A reversible operational hold.  New proposals are not generated and no
    proposal may be authorized, by a human or by the system.  Ingestion,
    classification, research, broker reconciliation and the expiry sweep all
    keep running -- specification section 20 is explicit that a pause stops new
    trade proposals and *not* broker reconciliation, and stopping discovery as
    well would mean a pause silently costs the operator the news that happened
    during it.

``control.kill_switch`` (``/kill``)
    The emergency stop of specification section 20: "sets ``TRADING_ENABLED=false``
    in persistent settings.  It must not automatically liquidate positions."
    It does everything a pause does and additionally denies the Phase 8
    transmission path before it exists, and it is released only by an explicit
    second act rather than by ``/resume`` -- conflating the two would let a
    routine resume silently lift an emergency stop.

Neither flag closes a position, cancels an order or touches the broker at all.
There is no code path in this process that could: no order, cancel or amend
method exists on any client.  A kill switch that liquidated would turn a
precautionary tap into the largest trade of the day.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from stockbrain.db.base import utcnow
from stockbrain.db.models.system import AppSetting, AuditLog
from stockbrain.db.session import Database
from stockbrain.enums import ActorType, ControlFlag
from stockbrain.logging import get_logger

__all__ = [
    "ControlSnapshot",
    "ControlState",
    "ControlStateService",
    "control_blockers",
]

log = get_logger(__name__)

_DESCRIPTIONS: dict[ControlFlag, str] = {
    ControlFlag.TRADING_PAUSED: (
        "Operator pause. Suspends proposal generation and every authorization path. "
        "Ingestion, research and broker reconciliation are unaffected."
    ),
    ControlFlag.KILL_SWITCH: (
        "Emergency kill switch (spec section 20). Suspends proposal generation, every "
        "authorization path and any future order transmission. Never liquidates a "
        "position and never cancels a broker order."
    ),
}


@dataclass(frozen=True, slots=True)
class ControlState:
    """One control flag and the provenance of its current value."""

    flag: ControlFlag
    active: bool
    changed_at: dt.datetime | None = None
    actor: str | None = None
    source: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "flag": self.flag.value,
            "active": self.active,
            "changed_at": self.changed_at.isoformat() if self.changed_at else None,
            "actor": self.actor,
            "source": self.source,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ControlSnapshot:
    """Both flags, read in one transaction so they cannot disagree."""

    paused: ControlState
    kill_switch: ControlState

    @property
    def trading_halted(self) -> bool:
        """Whether *any* control flag currently stops proposal work."""
        return self.paused.active or self.kill_switch.active

    @property
    def blockers(self) -> list[str]:
        """Every reason proposal work is halted, in GUI- and Telegram-ready wording.

        ``trading_halted`` is defined as "there is at least one blocker", so the
        predicate and the explanation can never disagree -- the same shape the
        execution and automation gates in :mod:`stockbrain.config` use.
        """
        return control_blockers(self)

    def as_dict(self) -> dict[str, object]:
        return {
            "trading_halted": self.trading_halted,
            "paused": self.paused.as_dict(),
            "kill_switch": self.kill_switch.as_dict(),
            "blockers": self.blockers,
        }


def control_blockers(snapshot: ControlSnapshot) -> list[str]:
    blockers: list[str] = []
    if snapshot.kill_switch.active:
        detail = f" ({snapshot.kill_switch.reason})" if snapshot.kill_switch.reason else ""
        blockers.append(f"the emergency kill switch is engaged{detail}")
    if snapshot.paused.active:
        detail = f" ({snapshot.paused.reason})" if snapshot.paused.reason else ""
        blockers.append(f"trading is paused{detail}")
    return blockers


class ControlStateService:
    """Read and change the durable control flags."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    async def snapshot(self) -> ControlSnapshot:
        async with self._database.session() as session:
            rows = {
                row.key: row.value
                for row in (
                    await session.execute(
                        sa.select(AppSetting).where(
                            AppSetting.key.in_([flag.value for flag in ControlFlag])
                        )
                    )
                ).scalars()
            }
        return ControlSnapshot(
            paused=_state_from_row(
                ControlFlag.TRADING_PAUSED, rows.get(ControlFlag.TRADING_PAUSED)
            ),
            kill_switch=_state_from_row(ControlFlag.KILL_SWITCH, rows.get(ControlFlag.KILL_SWITCH)),
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    async def pause(self, *, actor: str, source: str, reason: str | None = None) -> ControlSnapshot:
        return await self._set(
            ControlFlag.TRADING_PAUSED, True, actor=actor, source=source, reason=reason
        )

    async def resume(
        self, *, actor: str, source: str, reason: str | None = None
    ) -> ControlSnapshot:
        return await self._set(
            ControlFlag.TRADING_PAUSED, False, actor=actor, source=source, reason=reason
        )

    async def engage_kill_switch(
        self, *, actor: str, source: str, reason: str | None = None
    ) -> ControlSnapshot:
        return await self._set(
            ControlFlag.KILL_SWITCH, True, actor=actor, source=source, reason=reason
        )

    async def release_kill_switch(
        self, *, actor: str, source: str, reason: str | None = None
    ) -> ControlSnapshot:
        """Lift the emergency stop.

        Deliberately a separate act from :meth:`resume`.  ``/resume`` is the
        routine control an operator uses often; if it also cleared the kill
        switch, the emergency stop would be one habitual tap away from being
        undone by someone who was only trying to restart normal work.
        """
        return await self._set(
            ControlFlag.KILL_SWITCH, False, actor=actor, source=source, reason=reason
        )

    async def _set(
        self,
        flag: ControlFlag,
        active: bool,
        *,
        actor: str,
        source: str,
        reason: str | None,
    ) -> ControlSnapshot:
        now = utcnow()
        payload: dict[str, object] = {
            "active": active,
            "changed_at": now.isoformat(),
            "actor": actor,
            "source": source,
            "reason": (reason or None) and reason[:500],
        }
        async with self._database.transaction() as session:
            statement = pg_insert(AppSetting).values(
                key=flag.value,
                value=payload,
                description=_DESCRIPTIONS[flag],
                updated_by=actor,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[AppSetting.key],
                    set_={
                        "value": statement.excluded.value,
                        "description": statement.excluded.description,
                        "updated_by": statement.excluded.updated_by,
                        "updated_at": now,
                    },
                )
            )
            session.add(
                AuditLog(
                    actor_type=ActorType.USER if source.startswith("HUMAN") else ActorType.SYSTEM,
                    actor_id=actor,
                    action=f"control.{flag.value.split('.', 1)[1]}."
                    + ("engaged" if active else "released"),
                    entity_type="app_setting",
                    entity_id=None,
                    details={
                        "flag": flag.value,
                        "active": active,
                        "source": source,
                        "reason": (reason or "")[:500] or None,
                        "broker_positions_touched": False,
                        "broker_orders_touched": False,
                    },
                )
            )
        log.warning(
            "control_flag_changed",
            flag=flag.value,
            active=active,
            actor=actor,
            source=source,
            reason=(reason or "")[:200] or None,
            broker_mutation="none: StockBrain has no order, cancel or amend path",
        )
        return await self.snapshot()

    # ------------------------------------------------------------------
    async def log_restored_state(self) -> ControlSnapshot:
        """Report the persisted safety state at startup.

        Called from the service container's ``start``.  An application that
        came back up while halted must say so loudly: silently resuming risky
        behaviour after a crash is the failure this table exists to prevent.
        """
        snapshot = await self.snapshot()
        if snapshot.trading_halted:
            log.warning(
                "control_state_restored_halted",
                blockers=snapshot.blockers,
                paused_since=snapshot.paused.changed_at.isoformat()
                if snapshot.paused.changed_at
                else None,
                killed_since=snapshot.kill_switch.changed_at.isoformat()
                if snapshot.kill_switch.changed_at
                else None,
            )
        else:
            log.info("control_state_restored", trading_halted=False)
        return snapshot


def _state_from_row(flag: ControlFlag, value: object) -> ControlState:
    """Build a state from a stored row, treating anything unreadable as *inactive*.

    Failing open here is the right direction only because the row's absence is
    the normal case: a deployment that never paused has no row at all.  A row
    that exists but is malformed is reported inactive *and* logged, rather than
    silently halting a system nobody asked to halt.
    """
    if not isinstance(value, dict):
        if value is not None:  # pragma: no cover - defensive
            log.error("control_flag_unreadable", flag=flag.value)
        return ControlState(flag=flag, active=False)
    changed_raw = value.get("changed_at")
    changed_at: dt.datetime | None = None
    if isinstance(changed_raw, str):
        try:
            changed_at = dt.datetime.fromisoformat(changed_raw)
        except ValueError:  # pragma: no cover - defensive
            changed_at = None
    return ControlState(
        flag=flag,
        active=bool(value.get("active")),
        changed_at=changed_at,
        actor=_text(value.get("actor")),
        source=_text(value.get("source")),
        reason=_text(value.get("reason")),
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
