"""PostgreSQL-backed job queue and worker pool.

There is no Redis here by design, so these tests cover what a broker would
otherwise provide: exactly-once claiming, retry with backoff, a dead-letter
state, and recovery of jobs whose worker died.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
import sqlalchemy as sa

from stockbrain.db.base import utcnow
from stockbrain.db.models.system import Job
from stockbrain.db.session import Database
from stockbrain.enums import JobStatus, JobType
from stockbrain.jobs.queue import JobQueue
from stockbrain.jobs.registry import HandlerContext, JobRegistry
from stockbrain.jobs.runner import JobRunner

pytestmark = pytest.mark.integration


async def test_enqueue_and_claim_roundtrip(clean_tables: Database) -> None:
    queue = JobQueue()
    async with clean_tables.transaction() as session:
        job_id = await queue.enqueue(session, JobType.SEC_REFRESH, payload={"cik": "0000320193"})
    assert job_id is not None

    async with clean_tables.transaction() as session:
        job = await queue.claim(session, "worker-1")
        assert job is not None
        assert job.id == job_id
        assert job.status is JobStatus.RUNNING
        assert job.locked_by == "worker-1"
        assert job.attempts == 1
        assert job.payload == {"cik": "0000320193"}

    async with clean_tables.transaction() as session:
        assert await queue.claim(session, "worker-2") is None


async def test_dedupe_key_suppresses_a_second_pending_job(clean_tables: Database) -> None:
    queue = JobQueue()
    async with clean_tables.transaction() as session:
        first = await queue.enqueue(session, JobType.SEC_REFRESH, dedupe_key="sec:watchlist")
        second = await queue.enqueue(session, JobType.SEC_REFRESH, dedupe_key="sec:watchlist")
    assert first is not None
    assert second is None

    async with clean_tables.transaction() as session:
        count = (await session.execute(sa.select(sa.func.count()).select_from(Job))).scalar_one()
    assert count == 1


async def test_dedupe_key_frees_up_once_the_job_finishes(clean_tables: Database) -> None:
    queue = JobQueue()
    async with clean_tables.transaction() as session:
        job_id = await queue.enqueue(session, JobType.SEC_REFRESH, dedupe_key="sec:watchlist")
        assert job_id is not None
    async with clean_tables.transaction() as session:
        await queue.complete(session, job_id)
    async with clean_tables.transaction() as session:
        assert await queue.enqueue(session, JobType.SEC_REFRESH, dedupe_key="sec:watchlist")


async def test_run_after_defers_a_job(clean_tables: Database) -> None:
    queue = JobQueue()
    async with clean_tables.transaction() as session:
        await queue.enqueue(
            session, JobType.SEC_REFRESH, run_after=utcnow() + dt.timedelta(hours=1)
        )
    async with clean_tables.transaction() as session:
        assert await queue.claim(session, "worker-1") is None


async def test_priority_ordering(clean_tables: Database) -> None:
    queue = JobQueue()
    async with clean_tables.transaction() as session:
        await queue.enqueue(session, "LOW", priority=200)
        await queue.enqueue(session, "HIGH", priority=1)
    async with clean_tables.transaction() as session:
        job = await queue.claim(session, "w")
        assert job is not None
        assert job.job_type == "HIGH"


async def test_failure_reschedules_until_the_budget_is_spent(clean_tables: Database) -> None:
    queue = JobQueue()
    async with clean_tables.transaction() as session:
        job_id = await queue.enqueue(session, "FLAKY", max_attempts=2)
    assert job_id is not None

    async with clean_tables.transaction() as session:
        await queue.claim(session, "w")
        assert await queue.fail(session, job_id, "boom") is True

    async with clean_tables.session() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.PENDING
        assert job.last_error == "boom"
        # Backoff pushed it into the future rather than hot-looping.
        assert job.run_after > utcnow()

    async with clean_tables.transaction() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.run_after = utcnow()
    async with clean_tables.transaction() as session:
        await queue.claim(session, "w")
        assert await queue.fail(session, job_id, "boom again") is False

    async with clean_tables.session() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.FAILED


async def test_long_error_text_is_truncated(clean_tables: Database) -> None:
    queue = JobQueue()
    async with clean_tables.transaction() as session:
        job_id = await queue.enqueue(session, "NOISY")
        assert job_id is not None
        await queue.claim(session, "w")
        await queue.fail(session, job_id, "x" * 9000)
    async with clean_tables.session() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.last_error is not None
        assert len(job.last_error) == 2000


async def test_abandoned_jobs_are_reclaimed(clean_tables: Database) -> None:
    """A worker that dies mid-job must not strand that job as RUNNING forever."""
    queue = JobQueue()
    async with clean_tables.transaction() as session:
        job_id = await queue.enqueue(session, "STUCK", max_attempts=3)
        await queue.claim(session, "dead-worker")
    async with clean_tables.transaction() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.locked_at = utcnow() - dt.timedelta(hours=1)

    async with clean_tables.transaction() as session:
        reclaimed = await queue.reclaim_abandoned(session, timeout_seconds=60)
    assert reclaimed == 1

    async with clean_tables.session() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.PENDING


async def test_an_abandoned_job_with_no_attempts_left_is_failed_not_retried(
    clean_tables: Database,
) -> None:
    queue = JobQueue()
    async with clean_tables.transaction() as session:
        job_id = await queue.enqueue(session, "POISON", max_attempts=1)
        await queue.claim(session, "dead-worker")
    async with clean_tables.transaction() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.locked_at = utcnow() - dt.timedelta(hours=1)

    async with clean_tables.transaction() as session:
        assert await queue.reclaim_abandoned(session, timeout_seconds=60) == 0

    async with clean_tables.session() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.FAILED


async def test_concurrent_workers_never_claim_the_same_job(clean_tables: Database) -> None:
    queue = JobQueue()
    async with clean_tables.transaction() as session:
        for index in range(12):
            await queue.enqueue(session, "PARALLEL", payload={"n": index})

    async def worker(name: str) -> list[str]:
        claimed: list[str] = []
        while True:
            async with clean_tables.transaction() as session:
                job = await queue.claim(session, name)
                if job is None:
                    return claimed
                claimed.append(str(job.id))
                await queue.complete(session, job.id)

    batches = await asyncio.gather(*(worker(f"w{i}") for i in range(4)))
    all_claimed = [job_id for batch in batches for job_id in batch]
    assert len(all_claimed) == 12
    assert len(set(all_claimed)) == 12, "a job was handed to two workers"


async def test_runner_executes_a_registered_handler(clean_tables: Database) -> None:
    seen: list[dict[str, object]] = []

    async def handler(context: HandlerContext) -> None:
        seen.append(dict(context.payload))

    registry = JobRegistry()
    registry.register("ECHO", handler)
    runner = JobRunner(
        clean_tables, registry, services=None, concurrency=2, poll_interval_seconds=0.05
    )

    queue = JobQueue()
    async with clean_tables.transaction() as session:
        await queue.enqueue(session, "ECHO", payload={"hello": "world"})

    await runner.start()
    try:
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.05)
    finally:
        await runner.stop()

    assert seen == [{"hello": "world"}]
    async with clean_tables.session() as session:
        job = (await session.execute(sa.select(Job))).scalar_one()
    assert job.status is JobStatus.SUCCEEDED


async def test_runner_marks_an_unknown_job_type_failed(clean_tables: Database) -> None:
    """An unregistered type is a deployment error and must be visible, not silent."""
    registry = JobRegistry()
    runner = JobRunner(
        clean_tables, registry, services=None, concurrency=1, poll_interval_seconds=0.05
    )
    queue = JobQueue()
    async with clean_tables.transaction() as session:
        await queue.enqueue(session, "NO_SUCH_TYPE", max_attempts=1)

    await runner.start()
    try:
        for _ in range(100):
            async with clean_tables.session() as session:
                job = (await session.execute(sa.select(Job))).scalar_one()
                if job.status is JobStatus.FAILED:
                    break
            await asyncio.sleep(0.05)
    finally:
        await runner.stop()

    async with clean_tables.session() as session:
        job = (await session.execute(sa.select(Job))).scalar_one()
    assert job.status is JobStatus.FAILED
    assert job.last_error is not None
    assert "no handler registered" in job.last_error


def test_registry_rejects_a_duplicate_registration() -> None:
    registry = JobRegistry()

    async def handler(context: HandlerContext) -> None:  # pragma: no cover
        return None

    registry.register("X", handler)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("X", handler)
