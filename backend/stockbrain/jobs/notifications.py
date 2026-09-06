"""Enqueueing a pipeline-stage notification, gated by the operator's preferences.

One function, in one place, because the interesting decision is *not* to enqueue.
A deployment ingesting a few hundred articles a day with ``EVENT_DISCOVERED``
switched off should create zero jobs, not a few hundred jobs that each load a
preference row and suppress themselves.  Checking the preference here -- against
the short-lived cache in
:class:`~stockbrain.telegram.preferences.NotificationPreferences` -- keeps the
queue empty and the database quiet.

The check is a fast path, not the guarantee.  The delivery side re-reads the
preference before it sends, so a category switched off while a job sat in the
queue still produces no message.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.enums import JobType
from stockbrain.jobs.queue import JobQueue
from stockbrain.telegram.preferences import (
    NotificationPreferences,
    PipelineEvent,
    category_for_pipeline_event,
)

__all__ = ["enqueue_pipeline_notification"]


async def enqueue_pipeline_notification(
    session: AsyncSession,
    queue: JobQueue,
    preferences: NotificationPreferences | None,
    *,
    entity_id: uuid.UUID,
    event: PipelineEvent,
) -> bool:
    """Queue one pipeline notification if its category is switched on.

    Returns whether a job was created.  Enqueued inside the caller's
    transaction, so a stage that is announced is a stage that actually happened:
    if the surrounding write rolls back, so does the announcement.
    """
    if preferences is None:
        return False
    if not await preferences.enabled(category_for_pipeline_event(event)):
        return False
    job_id = await queue.enqueue(
        session,
        JobType.SEND_NOTIFICATION,
        payload={"entity_id": str(entity_id), "pipeline_event": event.value},
        # The same key shape the delivery row uses, so a redelivered job and a
        # second enqueue collapse onto one message rather than two.
        dedupe_key=f"pipeline-notify:{entity_id}:{event.value}",
        priority=40,
        # One attempt, like every other notification in this system: a resend
        # cannot distinguish "never arrived" from "arrived, status write
        # failed", and the second reading is a duplicate alert.
        max_attempts=1,
    )
    return job_id is not None
