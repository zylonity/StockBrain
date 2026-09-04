"""Job handler registry.

Handlers are registered by name so a new job type needs no migration: the
``jobs.job_type`` column is free text and this map is the dispatcher.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from stockbrain.db.session import Database

__all__ = ["HandlerContext", "JobHandler", "JobRegistry"]


@dataclass(slots=True)
class HandlerContext:
    """Everything a handler is allowed to reach.

    Deliberately narrow. A handler receives the database and the shared service
    container; it does not receive broker credentials, and no handler in this
    phase performs a broker mutation.
    """

    job_id: uuid.UUID
    job_type: str
    payload: dict[str, Any]
    attempt: int
    max_attempts: int
    """So a handler can distinguish "will be retried" from "this is the last try",
    and record a permanent failure on the final attempt rather than leaving the
    entity in a transient state forever."""

    database: Database
    services: Any

    @property
    def is_final_attempt(self) -> bool:
        return self.attempt >= self.max_attempts


JobHandler = Callable[[HandlerContext], Awaitable[None]]


class JobRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler] = {}

    def register(self, job_type: str, handler: JobHandler) -> None:
        if job_type in self._handlers:
            raise ValueError(f"handler already registered for job type {job_type!r}")
        self._handlers[job_type] = handler

    def get(self, job_type: str) -> JobHandler | None:
        return self._handlers.get(job_type)

    def known_types(self) -> list[str]:
        return sorted(self._handlers)
