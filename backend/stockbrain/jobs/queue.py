"""PostgreSQL-backed job queue.

Claiming uses ``SELECT ... FOR UPDATE SKIP LOCKED``, which gives at-least-once
delivery with no broker to operate, back up or restore. For a workload measured
in events per minute this is sufficient, and it has a property Redis does not:
the queue *is* the audit trail.

There is no Redis, Celery or Kafka in this system, by design.
"""

from __future__ import annotations

import datetime as dt
import random
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.base import utcnow
from stockbrain.db.models.system import Job
from stockbrain.enums import JobStatus, JobType
from stockbrain.logging import get_logger

__all__ = ["JobQueue"]

log = get_logger(__name__)


class JobQueue:
    """Enqueue, claim and complete durable work items.

    **Scheduling uses the database clock throughout.** ``run_after`` defaults to
    ``now()`` evaluated by PostgreSQL, and claiming compares against ``now()`` in
    the claiming transaction. Mixing the application clock with the database
    clock would let a job enqueued a few milliseconds "in the future" -- by
    container clock skew, or simply because a transaction timestamp precedes a
    later Python call -- become unclaimable until the next poll.
    """

    async def enqueue(
        self,
        session: AsyncSession,
        job_type: JobType | str,
        *,
        payload: dict[str, Any] | None = None,
        priority: int = 100,
        run_after: dt.datetime | None = None,
        max_attempts: int = 3,
        dedupe_key: str | None = None,
    ) -> uuid.UUID | None:
        """Insert a job.  Returns ``None`` when a dedupe key suppressed it.

        Suppression is not a failure: it is the point. ``uq_jobs_dedupe_key_active``
        makes "one pending refresh per company" a database guarantee rather than
        a hopeful check.
        """
        name = job_type.value if isinstance(job_type, JobType) else job_type
        job = Job(
            job_type=name,
            payload=payload or {},
            status=JobStatus.PENDING,
            priority=priority,
            # Server-side clock, so the row is immediately claimable by any
            # worker, including one in the very same transaction.
            run_after=run_after if run_after is not None else sa.func.now(),
            max_attempts=max_attempts,
            dedupe_key=dedupe_key,
        )
        savepoint = await session.begin_nested()
        try:
            session.add(job)
            await session.flush()
        except IntegrityError:
            await savepoint.rollback()
            log.debug("job_deduped", job_type=name, dedupe_key=dedupe_key)
            return None
        await savepoint.commit()
        return job.id

    async def claim(self, session: AsyncSession, worker_id: str) -> Job | None:
        """Claim one runnable job, or return ``None``.

        The ``FOR UPDATE SKIP LOCKED`` select is what lets several workers poll
        the same table concurrently without ever handing out the same row twice.
        """
        claim_stmt = (
            sa.select(Job.id)
            .where(Job.status == JobStatus.PENDING, Job.run_after <= sa.func.now())
            .order_by(Job.priority.asc(), Job.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job_id = (await session.execute(claim_stmt)).scalar_one_or_none()
        if job_id is None:
            return None

        job = await session.get(Job, job_id)
        if job is None:  # pragma: no cover - the row was locked a statement ago
            return None

        job.status = JobStatus.RUNNING
        job.locked_by = worker_id
        job.locked_at = utcnow()
        job.attempts += 1
        await session.flush()
        return job

    async def complete(self, session: AsyncSession, job_id: uuid.UUID) -> None:
        await session.execute(
            sa.update(Job)
            .where(Job.id == job_id)
            .values(
                status=JobStatus.SUCCEEDED,
                locked_by=None,
                locked_at=None,
                last_error=None,
                updated_at=utcnow(),
            )
        )

    async def fail(
        self,
        session: AsyncSession,
        job_id: uuid.UUID,
        error: str,
        *,
        retry_base_seconds: float = 5.0,
        retry_max_seconds: float = 900.0,
    ) -> bool:
        """Record a failure. Returns True when the job was rescheduled.

        Backoff is exponential with jitter so a provider outage does not produce
        a synchronised retry storm the moment it recovers.
        """
        job = await session.get(Job, job_id)
        if job is None:  # pragma: no cover
            return False

        # Errors can carry provider text; truncate so one bad response cannot
        # bloat the table.
        job.last_error = error[:2000]
        job.locked_by = None
        job.locked_at = None
        job.updated_at = utcnow()

        if job.attempts >= job.max_attempts:
            job.status = JobStatus.FAILED
            await session.flush()
            return False

        delay = min(retry_max_seconds, retry_base_seconds * (2 ** (job.attempts - 1)))
        jittered = random.uniform(delay / 2, delay)  # noqa: S311 - jitter, not crypto
        job.status = JobStatus.PENDING
        # Database clock plus an interval, for the same reason as `enqueue`.
        job.run_after = sa.func.now() + dt.timedelta(seconds=jittered)
        await session.flush()
        return True

    async def reclaim_abandoned(self, session: AsyncSession, *, timeout_seconds: int) -> int:
        """Return jobs whose worker died back to PENDING.

        A worker that crashes mid-job leaves the row RUNNING forever. Anything
        locked longer than the timeout is assumed abandoned and retried, subject
        to the same ``max_attempts`` budget so a job that reliably kills its
        worker cannot loop indefinitely.
        """
        cutoff = utcnow() - dt.timedelta(seconds=timeout_seconds)
        result = await session.execute(
            sa.update(Job)
            .where(
                Job.status == JobStatus.RUNNING,
                Job.locked_at.is_not(None),
                Job.locked_at < cutoff,
                Job.attempts < Job.max_attempts,
            )
            .values(
                status=JobStatus.PENDING,
                locked_by=None,
                locked_at=None,
                last_error="reclaimed after worker timeout",
                run_after=sa.func.now(),
                updated_at=utcnow(),
            )
            .returning(Job.id)
        )
        reclaimed = list(result.scalars())

        # A job that exhausted its attempts while locked is dead, not runnable.
        await session.execute(
            sa.update(Job)
            .where(
                Job.status == JobStatus.RUNNING,
                Job.locked_at.is_not(None),
                Job.locked_at < cutoff,
                Job.attempts >= Job.max_attempts,
            )
            .values(
                status=JobStatus.FAILED,
                locked_by=None,
                locked_at=None,
                last_error="worker timed out with no attempts remaining",
                updated_at=utcnow(),
            )
        )

        if reclaimed:
            log.warning("jobs_reclaimed", count=len(reclaimed), timeout_seconds=timeout_seconds)
        return len(reclaimed)

    async def pending_count(self, session: AsyncSession) -> int:
        result = await session.execute(
            sa.select(sa.func.count()).select_from(Job).where(Job.status == JobStatus.PENDING)
        )
        return int(result.scalar_one())
