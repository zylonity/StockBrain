"""Periodic scheduling.

The scheduler only ever *enqueues* jobs; it never does work itself. That keeps
the recurring cadence and the execution concurrency independent, and it means a
slow provider delays one job rather than the whole schedule.

Every schedule carries jitter so that a restart does not line every poller up on
the same second, and every enqueue carries a dedupe key so a backlog cannot
accumulate duplicate work while a provider is down.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from stockbrain.db.base import utcnow
from stockbrain.db.session import Database
from stockbrain.logging import get_logger

__all__ = ["ScheduledTask", "Scheduler"]

log = get_logger(__name__)


@dataclass(slots=True)
class ScheduledTask:
    name: str
    interval_seconds: float
    run: Callable[[], Awaitable[None]]
    jitter_ratio: float = 0.15
    """Fraction of the interval to randomise, avoiding synchronised bursts."""

    enabled: bool = True
    initial_delay_seconds: float = 0.0
    last_run_at: dt.datetime | None = field(default=None, init=False)
    last_error: str | None = field(default=None, init=False)

    def next_delay(self) -> float:
        spread = self.interval_seconds * self.jitter_ratio
        return max(1.0, self.interval_seconds + random.uniform(-spread, spread))  # noqa: S311


class Scheduler:
    """Runs a set of periodic tasks as independent asyncio tasks."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._tasks: dict[str, ScheduledTask] = {}
        self._running: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()

    def add(self, task: ScheduledTask) -> None:
        if task.name in self._tasks:
            raise ValueError(f"scheduled task {task.name!r} already registered")
        self._tasks[task.name] = task

    def tasks(self) -> list[ScheduledTask]:
        return list(self._tasks.values())

    async def start(self) -> None:
        self._stopping.clear()
        for task in self._tasks.values():
            if not task.enabled:
                log.info("scheduled_task_disabled", task=task.name)
                continue
            self._running.append(
                asyncio.create_task(self._loop(task), name=f"schedule-{task.name}")
            )
        log.info("scheduler_started", tasks=[t.name for t in self._tasks.values() if t.enabled])

    async def stop(self) -> None:
        self._stopping.set()
        for running in self._running:
            running.cancel()
        for running in self._running:
            with contextlib.suppress(asyncio.CancelledError):
                await running
        self._running.clear()
        log.info("scheduler_stopped")

    async def _loop(self, task: ScheduledTask) -> None:
        if task.initial_delay_seconds:
            await asyncio.sleep(random.uniform(0, task.initial_delay_seconds))  # noqa: S311
        while not self._stopping.is_set():
            try:
                await task.run()
                task.last_run_at = utcnow()
                task.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                task.last_error = f"{type(exc).__name__}: {exc}"
                # A scheduled task failing must never stop the schedule: the next
                # tick may well succeed, and stopping silently would be worse.
                log.warning("scheduled_task_error", task=task.name, error_type=type(exc).__name__)
            await asyncio.sleep(task.next_delay())
