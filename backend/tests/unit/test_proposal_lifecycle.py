"""Tests for durable side effects shared by proposal workflows."""

from __future__ import annotations

import uuid
from typing import cast
from unittest.mock import AsyncMock

from stockbrain.enums import JobType, NotificationEvent
from stockbrain.proposals.lifecycle import ProposalLifecycle
from stockbrain.proposals.service import ProposalService


async def test_notification_uses_the_existing_once_only_queue_key() -> None:
    queue = AsyncMock()
    lifecycle = ProposalLifecycle(cast(ProposalService, object()), queue)
    proposal_id = uuid.uuid4()
    session = AsyncMock()

    await lifecycle.notify(
        session,
        proposal_id,
        NotificationEvent.PROPOSAL_INVALIDATED,
        detail="x" * 501,
    )

    queue.enqueue.assert_awaited_once_with(
        session,
        JobType.SEND_NOTIFICATION,
        payload={
            "proposal_id": str(proposal_id),
            "event": NotificationEvent.PROPOSAL_INVALIDATED.value,
            "detail": "x" * 500,
        },
        dedupe_key=f"notify:{proposal_id}:{NotificationEvent.PROPOSAL_INVALIDATED.value}",
        priority=15,
    )
