"""Pipeline-stage notifications: distinct stages, quiet defaults, one message each.

The risk in widening notifications past proposals is not that a message fails to
arrive -- it is that too many of them do, and the operator stops reading the
channel that also carries "your order may or may not exist".  So these tests are
mostly about silence: a stage switched off produces no message, three stages of
one article read as three different things, and a stage announced twice is still
announced once.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.base import utcnow
from stockbrain.db.models.sources import Event
from stockbrain.db.models.system import Notification
from stockbrain.db.session import Database
from stockbrain.enums import EventStatus, NotificationStatus
from stockbrain.observability.health import ProviderHealthRegistry
from stockbrain.telegram.approvals import ApprovalCoordinator
from stockbrain.telegram.notifier import ProposalNotifier, pipeline_dedupe_key
from stockbrain.telegram.preferences import (
    NotificationCategory,
    NotificationPreferences,
    PipelineEvent,
)
from stockbrain.telegram.service import TelegramService
from stockbrain.telegram.tokens import TokenService
from tests import proposal_helpers as helpers
from tests.telegram_helpers import RecordingSender

pytestmark = pytest.mark.integration

OWNER = 4242


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "telegram_enabled": True,
        "telegram_bot_token": "123:test-token",
        "telegram_allowed_user_ids": str(OWNER),
    }
    base.update(overrides)
    return helpers.settings(**base)


def _notifier(
    database: Database,
    settings: Settings,
    sender: RecordingSender | None,
    preferences: NotificationPreferences,
) -> ProposalNotifier:
    control = ControlStateService(database)
    service = TelegramService(database, settings, health=ProviderHealthRegistry(), control=control)
    coordinator = ApprovalCoordinator(
        database,
        settings,
        tokens=TokenService(),
        proposals=None,
        service=service,
        control=control,
    )
    return ProposalNotifier(
        database,
        settings,
        service=service,
        coordinator=coordinator,
        sender=sender,
        preferences=preferences,
    )


async def _event(database: Database, *, status: EventStatus, title: str) -> uuid.UUID:
    event_id = uuid.uuid4()
    async with database.transaction() as session:
        session.add(
            Event(
                id=event_id,
                title=title,
                title_hash=uuid.uuid4().hex,
                status=status,
                event_type="EARNINGS",
                first_seen_at=utcnow(),
                importance_score=0.81,
                candidate_score=0.77,
            )
        )
    return event_id


async def _rows(database: Database) -> list[Notification]:
    async with database.session() as session:
        return list(
            (
                await session.execute(sa.select(Notification).order_by(Notification.created_at))
            ).scalars()
        )


# ---------------------------------------------------------------------------
# Stages read as different things
# ---------------------------------------------------------------------------


async def test_the_three_article_stages_are_worded_distinctly(
    clean_tables: Database,
) -> None:
    """ "Discovered", "considered relevant" and "promoted" are three facts.

    If they rendered the same, an operator watching for the one that costs money
    would have to subscribe to the one that fires on every scraped result.
    """
    settings = _settings()
    preferences = NotificationPreferences(clean_tables, cache_ttl_seconds=0.0)
    await preferences.update(
        {
            NotificationCategory.EVENT_DISCOVERED: True,
            NotificationCategory.EVENT_RELEVANT: True,
            NotificationCategory.EVENT_CANDIDATE: True,
        },
        actor="test",
    )
    sender = RecordingSender()
    notifier = _notifier(clean_tables, settings, sender, preferences)

    for stage in (
        PipelineEvent.EVENT_DISCOVERED,
        PipelineEvent.EVENT_RELEVANT,
        PipelineEvent.EVENT_CANDIDATE,
    ):
        event_id = await _event(clean_tables, status=EventStatus.CANDIDATE, title=f"{stage} story")
        result = await notifier.deliver_pipeline(event_id, stage)
        assert result.status is NotificationStatus.SENT, result.reason

    texts = [message.text for message in sender.sent]
    assert "Article discovered" in texts[0]
    assert "Considered relevant" in texts[1]
    assert "Promoted to research candidate" in texts[2]
    # Only the promotion is about to cost money, so only it carries the scores
    # the promotion was made on.
    assert "importance" not in texts[0]
    assert "importance" in texts[2]


async def test_a_stage_notification_never_carries_an_approval_button(
    clean_tables: Database,
) -> None:
    """A pipeline stage is information. The only actions in this system are the
    ones a proposal's own two-stage buttons carry."""
    preferences = NotificationPreferences(clean_tables, cache_ttl_seconds=0.0)
    await preferences.update({NotificationCategory.EVENT_CANDIDATE: True}, actor="test")
    sender = RecordingSender()
    notifier = _notifier(clean_tables, _settings(), sender, preferences)

    event_id = await _event(clean_tables, status=EventStatus.CANDIDATE, title="Anything")
    await notifier.deliver_pipeline(event_id, PipelineEvent.EVENT_CANDIDATE)
    assert all(message.keyboard is None for message in sender.sent)


async def test_an_untrusted_headline_is_escaped_and_never_becomes_a_link(
    clean_tables: Database,
) -> None:
    preferences = NotificationPreferences(clean_tables, cache_ttl_seconds=0.0)
    await preferences.update({NotificationCategory.EVENT_CANDIDATE: True}, actor="test")
    sender = RecordingSender()
    notifier = _notifier(clean_tables, _settings(), sender, preferences)

    event_id = await _event(
        clean_tables,
        status=EventStatus.CANDIDATE,
        title='<b>PUMP</b> now at http://evil.example/"onclick="',
    )
    await notifier.deliver_pipeline(event_id, PipelineEvent.EVENT_CANDIDATE)
    text = sender.sent[0].text
    assert "&lt;b&gt;PUMP&lt;/b&gt;" in text
    assert "<a href" not in text


# ---------------------------------------------------------------------------
# Silence
# ---------------------------------------------------------------------------


async def test_a_disabled_category_records_the_row_and_sends_nothing(
    clean_tables: Database,
) -> None:
    """The row is still written, so the Notifications table stays a complete
    record of what the system decided -- and why it stayed quiet."""
    sender = RecordingSender()
    preferences = NotificationPreferences(clean_tables, cache_ttl_seconds=0.0)
    notifier = _notifier(clean_tables, _settings(), sender, preferences)

    event_id = await _event(clean_tables, status=EventStatus.NEW, title="Routine article")
    result = await notifier.deliver_pipeline(event_id, PipelineEvent.EVENT_DISCOVERED)

    assert result.status is NotificationStatus.SUPPRESSED
    assert "EVENT_DISCOVERED" in (result.reason or "")
    assert sender.sent == []
    rows = await _rows(clean_tables)
    assert [row.status for row in rows] == [NotificationStatus.SUPPRESSED]


async def test_a_deployment_with_no_bot_suppresses_rather_than_fails(
    clean_tables: Database,
) -> None:
    preferences = NotificationPreferences(clean_tables, cache_ttl_seconds=0.0)
    await preferences.update({NotificationCategory.EVENT_CANDIDATE: True}, actor="test")
    notifier = _notifier(clean_tables, _settings(), None, preferences)

    event_id = await _event(clean_tables, status=EventStatus.CANDIDATE, title="Story")
    result = await notifier.deliver_pipeline(event_id, PipelineEvent.EVENT_CANDIDATE)
    assert result.status is NotificationStatus.SUPPRESSED
    assert "not running" in (result.reason or "")


async def test_a_missing_entity_is_not_an_error(clean_tables: Database) -> None:
    """A job enqueued four minutes ago describes a world that may have moved."""
    preferences = NotificationPreferences(clean_tables, cache_ttl_seconds=0.0)
    await preferences.update({NotificationCategory.EVENT_CANDIDATE: True}, actor="test")
    notifier = _notifier(clean_tables, _settings(), RecordingSender(), preferences)

    result = await notifier.deliver_pipeline(uuid.uuid4(), PipelineEvent.EVENT_CANDIDATE)
    assert result.status is NotificationStatus.SUPPRESSED
    assert await _rows(clean_tables) == []


# ---------------------------------------------------------------------------
# Once, ever
# ---------------------------------------------------------------------------


async def test_a_redelivered_stage_produces_no_second_message(
    clean_tables: Database,
) -> None:
    """The guarantee is a unique index, not a flag in a process."""
    preferences = NotificationPreferences(clean_tables, cache_ttl_seconds=0.0)
    await preferences.update({NotificationCategory.EVENT_CANDIDATE: True}, actor="test")
    sender = RecordingSender()
    settings = _settings()
    event_id = await _event(clean_tables, status=EventStatus.CANDIDATE, title="Story")

    first = _notifier(clean_tables, settings, sender, preferences)
    assert (
        await first.deliver_pipeline(event_id, PipelineEvent.EVENT_CANDIDATE)
    ).status is NotificationStatus.SENT

    # A restarted process, sharing nothing but the database.
    second = _notifier(clean_tables, settings, sender, preferences)
    again = await second.deliver_pipeline(event_id, PipelineEvent.EVENT_CANDIDATE)
    assert again.status is NotificationStatus.SUPPRESSED
    assert len(sender.sent) == 1

    rows = await _rows(clean_tables)
    assert [row.dedupe_key for row in rows] == [
        pipeline_dedupe_key(event_id, PipelineEvent.EVENT_CANDIDATE)
    ]


async def test_two_stages_of_one_article_are_two_separate_messages(
    clean_tables: Database,
) -> None:
    preferences = NotificationPreferences(clean_tables, cache_ttl_seconds=0.0)
    await preferences.update(
        {
            NotificationCategory.EVENT_RELEVANT: True,
            NotificationCategory.EVENT_CANDIDATE: True,
        },
        actor="test",
    )
    sender = RecordingSender()
    notifier = _notifier(clean_tables, _settings(), sender, preferences)
    event_id = await _event(clean_tables, status=EventStatus.CANDIDATE, title="Story")

    await notifier.deliver_pipeline(event_id, PipelineEvent.EVENT_RELEVANT)
    await notifier.deliver_pipeline(event_id, PipelineEvent.EVENT_CANDIDATE)
    assert len(sender.sent) == 2


async def test_the_pipeline_namespace_cannot_collide_with_a_proposal_key() -> None:
    identifier = uuid.uuid4()
    assert pipeline_dedupe_key(identifier, PipelineEvent.EVENT_CANDIDATE).startswith(
        "telegram:pipeline:"
    )


# ---------------------------------------------------------------------------
# Proposal notifications now respect their category too
# ---------------------------------------------------------------------------


async def test_switching_the_proposal_category_off_silences_proposal_messages(
    clean_tables: Database,
) -> None:
    from stockbrain.enums import NotificationEvent

    settings = _settings()
    preferences = NotificationPreferences(clean_tables, cache_ttl_seconds=0.0)
    await preferences.update({NotificationCategory.PROPOSAL: False}, actor="test")

    await helpers.seed(clean_tables)
    await helpers.fund(clean_tables)
    proposals = helpers.service_with(
        clean_tables,
        settings,
        market_data=helpers.StubMarketData(),
        control=ControlStateService(clean_tables),
    )
    generated = await proposals.generate(helpers.THESIS_ID)
    assert generated.proposal_id is not None, generated.reason

    sender = RecordingSender()
    notifier = _notifier(clean_tables, settings, sender, preferences)
    result = await notifier.deliver(generated.proposal_id, NotificationEvent.PROPOSAL_MANUAL)

    assert result.status is NotificationStatus.SUPPRESSED
    assert "PROPOSAL" in (result.reason or "")
    assert sender.sent == []
