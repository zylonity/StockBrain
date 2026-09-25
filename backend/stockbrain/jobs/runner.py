"""Job worker pool.

A small number of asyncio tasks poll the queue. Each claims a job in its own
short transaction, runs the handler *outside* that transaction, then records the
outcome in a second transaction.

Splitting the transactions matters: a handler may take minutes (an LLM call, a
slow provider), and holding a row lock and a pooled connection for that long
would starve everything else.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import structlog

from stockbrain.db.session import Database
from stockbrain.jobs.queue import JobQueue
from stockbrain.jobs.registry import HandlerContext, JobRegistry
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = ["JobRunner"]

from stockbrain.errors import ProviderRateLimited

log = get_logger(__name__)


class JobRunner:
    def __init__(
        self,
        database: Database,
        registry: JobRegistry,
        *,
        services: Any,
        concurrency: int = 4,
        poll_interval_seconds: float = 1.0,
        claim_timeout_seconds: int = 900,
        instance_id: str = "worker",
        queue: JobQueue | None = None,
    ) -> None:
        self._database = database
        self._registry = registry
        self._services = services
        self._concurrency = max(1, concurrency)
        self._poll_interval = poll_interval_seconds
        self._claim_timeout = claim_timeout_seconds
        self._instance_id = instance_id
        self._queue = queue or JobQueue()
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        for index in range(self._concurrency):
            worker_id = f"{self._instance_id}#{index}"
            self._tasks.append(
                asyncio.create_task(self._worker(worker_id), name=f"job-{worker_id}")
            )
        self._tasks.append(asyncio.create_task(self._reaper(), name="job-reaper"))
        log.info(
            "job_runner_started",
            concurrency=self._concurrency,
            handlers=self._registry.known_types(),
        )

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        log.info("job_runner_stopped")

    # ------------------------------------------------------------------
    async def _worker(self, worker_id: str) -> None:
        while not self._stopping.is_set():
            try:
                processed = await self._run_one(worker_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("job_worker_loop_error", worker=worker_id, error=str(exc))
                processed = False
            if not processed:
                await asyncio.sleep(self._poll_interval)

    async def _run_one(self, worker_id: str) -> bool:
        async with self._database.transaction() as session:
            job = await self._queue.claim(session, worker_id)
            if job is None:
                return False
            context = HandlerContext(
                job_id=job.id,
                job_type=job.job_type,
                payload=dict(job.payload),
                attempt=job.attempts,
                max_attempts=job.max_attempts,
                database=self._database,
                services=self._services,
            )

        structlog.contextvars.bind_contextvars(
            job_id=str(context.job_id), job_type=context.job_type
        )
        handler = self._registry.get(context.job_type)
        try:
            if handler is None:
                # An unknown type is a deployment error, not a transient fault.
                raise LookupError(f"no handler registered for job type {context.job_type!r}")
            await handler(context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            async with self._database.transaction() as session:
                retrying = await self._queue.fail(
                    session, context.job_id, message, **_retry_backoff(exc)
                )
            METRICS.inc(
                "stockbrain_jobs_processed_total",
                labels={"job_type": context.job_type, "outcome": "retry" if retrying else "failed"},
            )
            log.warning(
                "job_failed",
                error_type=type(exc).__name__,
                attempt=context.attempt,
                will_retry=retrying,
            )
        else:
            async with self._database.transaction() as session:
                await self._queue.complete(session, context.job_id)
            METRICS.inc(
                "stockbrain_jobs_processed_total",
                labels={"job_type": context.job_type, "outcome": "succeeded"},
            )
            log.debug("job_succeeded", attempt=context.attempt)
        finally:
            structlog.contextvars.unbind_contextvars("job_id", "job_type")
        return True

    async def _reaper(self) -> None:
        """Periodically return abandoned jobs and publish queue depth."""
        while not self._stopping.is_set():
            try:
                async with self._database.transaction() as session:
                    await self._queue.reclaim_abandoned(
                        session, timeout_seconds=self._claim_timeout
                    )
                    pending = await self._queue.pending_count(session)
                METRICS.set("stockbrain_jobs_pending", float(pending))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("job_reaper_error", error=str(exc))
            await asyncio.sleep(30.0)


def _retry_backoff(exc: BaseException) -> dict[str, float]:
    """A rate limit needs minutes to clear, not the default seconds.

    Retrying a 429 five seconds later mostly earns another 429; honouring the
    provider's hint (or starting at a minute) lets the window actually reset.
    """
    if isinstance(exc, ProviderRateLimited):
        hint = exc.retry_after_seconds or 0.0
        return {"retry_base_seconds": max(60.0, hint), "retry_max_seconds": 900.0}
    return {}
