"""Outbound notifications: exactly one per transition, ever.

"Ever" is the hard part.  The job queue is at-least-once, a worker can die
between sending and recording, and the process restarts.  So the guarantee is
not held by a flag in memory: each message corresponds to a ``notifications``
row inserted first under ``uq_notifications_dedupe_key``, and the second attempt
loses the insert rather than sending a second trade alert.

The other property here is anti-spam by *rule*: the death of a proposal is only
announced if its birth was.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.models.proposals import ApprovalAction, TradeProposal
from stockbrain.db.models.system import Notification
from stockbrain.db.session import Database
from stockbrain.enums import (
    ExecutionPolicy,
    NotificationEvent,
    NotificationStatus,
    ProposalStatus,
)
from stockbrain.observability.health import ProviderHealthRegistry
from stockbrain.proposals.service import ProposalService
from stockbrain.telegram.approvals import ApprovalCoordinator
from stockbrain.telegram.notifier import ProposalNotifier, dedupe_key
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


def _proposals(database: Database, settings: Settings) -> ProposalService:
    return helpers.service_with(
        database,
        settings,
        market_data=helpers.StubMarketData(),
        control=ControlStateService(database),
    )


def _notifier(
    database: Database, settings: Settings, sender: RecordingSender | None = None
) -> ProposalNotifier:
    """A notifier built the way a restarted process would build it.

    Nothing is carried over between instances: whatever stops a duplicate has
    to be in PostgreSQL.
    """
    control = ControlStateService(database)
    service = TelegramService(database, settings, health=ProviderHealthRegistry(), control=control)
    coordinator = ApprovalCoordinator(
        database,
        settings,
        tokens=TokenService(),
        proposals=_proposals(database, settings),
        service=service,
        control=control,
    )
    return ProposalNotifier(
        database, settings, service=service, coordinator=coordinator, sender=sender
    )


async def _proposal(database: Database, settings: Settings) -> uuid.UUID:
    await helpers.seed(database)
    await helpers.fund(database)
    result = await _proposals(database, settings).generate(helpers.THESIS_ID)
    assert result.proposal_id is not None, result.reason
    return result.proposal_id


async def _rows(database: Database) -> list[Notification]:
    async with database.session() as session:
        return list(
            (
                await session.execute(sa.select(Notification).order_by(Notification.created_at))
            ).scalars()
        )


# ---------------------------------------------------------------------------
# One per transition
# ---------------------------------------------------------------------------
async def test_a_manual_proposal_produces_one_message_with_controls(
    clean_tables: Database,
) -> None:
    settings = _settings()
    proposal_id = await _proposal(clean_tables, settings)
    sender = RecordingSender()

    result = await _notifier(clean_tables, settings, sender).deliver(
        proposal_id, NotificationEvent.PROPOSAL_MANUAL
    )
    assert result.status is NotificationStatus.SENT
    assert len(sender.sent) == 1
    message = sender.sent[0]
    assert message.chat_id == OWNER
    assert "approval required" in message.text
    labels = [button.label for row in (message.keyboard or []) for button in row]
    assert any("Approve" in label for label in labels)
    assert any("Reject" in label for label in labels)


async def test_delivering_the_same_transition_twice_sends_one_message(
    clean_tables: Database,
) -> None:
    """At-least-once job delivery, made once-only by a unique index."""
    settings = _settings()
    proposal_id = await _proposal(clean_tables, settings)
    sender = RecordingSender()
    notifier = _notifier(clean_tables, settings, sender)

    first = await notifier.deliver(proposal_id, NotificationEvent.PROPOSAL_MANUAL)
    second = await notifier.deliver(proposal_id, NotificationEvent.PROPOSAL_MANUAL)
    assert first.status is NotificationStatus.SENT
    assert second.status is NotificationStatus.SUPPRESSED
    assert len(sender.sent) == 1


async def test_a_restart_does_not_resend(clean_tables: Database) -> None:
    """A brand new notifier, a brand new sender, the same database."""
    settings = _settings()
    proposal_id = await _proposal(clean_tables, settings)
    before = RecordingSender()
    await _notifier(clean_tables, settings, before).deliver(
        proposal_id, NotificationEvent.PROPOSAL_MANUAL
    )

    after = RecordingSender()
    result = await _notifier(clean_tables, settings, after).deliver(
        proposal_id, NotificationEvent.PROPOSAL_MANUAL
    )
    assert result.status is NotificationStatus.SUPPRESSED
    assert after.sent == []
    assert len(before.sent) == 1

    rows = await _rows(clean_tables)
    assert len(rows) == 1
    assert rows[0].dedupe_key == dedupe_key(proposal_id, NotificationEvent.PROPOSAL_MANUAL)


async def test_a_delivered_manual_notification_moves_the_proposal_to_notified(
    clean_tables: Database,
) -> None:
    """``NOTIFIED`` means a human was actually told, which ``READY`` does not."""
    settings = _settings()
    proposal_id = await _proposal(clean_tables, settings)
    await _notifier(clean_tables, settings, RecordingSender()).deliver(
        proposal_id, NotificationEvent.PROPOSAL_MANUAL
    )
    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None
    assert proposal.status is ProposalStatus.NOTIFIED
    assert proposal.notified_at is not None


# ---------------------------------------------------------------------------
# Automatic proposals
# ---------------------------------------------------------------------------
async def test_an_automatic_authorization_is_announced_without_an_approve_button(
    clean_tables: Database,
) -> None:
    """Offering a decision the system already made would be a lie in a button."""
    settings = _settings(execution_policy=ExecutionPolicy.AUTOMATIC)
    await helpers.seed(clean_tables)
    await helpers.fund(clean_tables)
    generated = await _proposals(clean_tables, settings).generate(helpers.THESIS_ID)
    assert generated.authorized and generated.proposal_id is not None

    sender = RecordingSender()
    await _notifier(clean_tables, settings, sender).deliver(
        generated.proposal_id, NotificationEvent.PROPOSAL_AUTO_AUTHORIZED
    )
    assert len(sender.sent) == 1
    assert sender.sent[0].keyboard is None
    assert "authorized automatically" in sender.sent[0].text
    assert "SYSTEM_AUTOMATIC" in sender.sent[0].text
    assert "separately gated" in sender.sent[0].text


# ---------------------------------------------------------------------------
# Anti-spam
# ---------------------------------------------------------------------------
async def test_the_outcome_of_a_proposal_nobody_was_told_about_is_not_announced(
    clean_tables: Database,
) -> None:
    settings = _settings()
    proposal_id = await _proposal(clean_tables, settings)
    sender = RecordingSender()

    result = await _notifier(clean_tables, settings, sender).deliver(
        proposal_id, NotificationEvent.PROPOSAL_EXPIRED
    )
    assert result.status is NotificationStatus.SUPPRESSED
    assert result.reason is not None and "never announced" in result.reason
    assert sender.sent == []


async def test_the_outcome_is_announced_when_the_proposal_was(
    clean_tables: Database,
) -> None:
    settings = _settings()
    proposal_id = await _proposal(clean_tables, settings)
    sender = RecordingSender()
    notifier = _notifier(clean_tables, settings, sender)
    await notifier.deliver(proposal_id, NotificationEvent.PROPOSAL_MANUAL)

    await _proposals(clean_tables, settings).reject(proposal_id, actor="telegram:4242")
    result = await notifier.deliver(proposal_id, NotificationEvent.PROPOSAL_REJECTED)
    assert result.status is NotificationStatus.SENT
    assert "rejected" in sender.sent[-1].text.lower()


async def test_a_risk_refusal_is_announced_with_its_reason(clean_tables: Database) -> None:
    settings = _settings()
    proposal_id = await _proposal(clean_tables, settings)
    sender = RecordingSender()
    notifier = _notifier(clean_tables, settings, sender)
    await notifier.deliver(proposal_id, NotificationEvent.PROPOSAL_MANUAL)

    result = await notifier.deliver(
        proposal_id,
        NotificationEvent.AUTHORIZATION_REFUSED,
        detail="spread 1024 bps exceeds the 50 bps ceiling",
    )
    assert result.status is NotificationStatus.SENT
    assert "1024 bps" in sender.sent[-1].text


# ---------------------------------------------------------------------------
# Failure and suppression
# ---------------------------------------------------------------------------
async def test_a_send_failure_is_recorded_and_never_auto_resent(
    clean_tables: Database,
) -> None:
    """A resend cannot tell "never arrived" from "arrived, status write failed".

    So a failure is a visible row rather than a retry loop that turns one
    network blip into a duplicate trade alert.
    """
    settings = _settings()
    proposal_id = await _proposal(clean_tables, settings)
    failing = RecordingSender(fail_send=True)
    notifier = _notifier(clean_tables, settings, failing)

    result = await notifier.deliver(proposal_id, NotificationEvent.PROPOSAL_MANUAL)
    assert result.status is NotificationStatus.FAILED
    rows = await _rows(clean_tables)
    assert len(rows) == 1
    assert rows[0].status is NotificationStatus.FAILED
    assert rows[0].error is not None and "NetworkError" in rows[0].error

    # A later attempt -- a redelivered job, or a restart -- does not resend.
    working = RecordingSender()
    again = await _notifier(clean_tables, settings, working).deliver(
        proposal_id, NotificationEvent.PROPOSAL_MANUAL
    )
    assert again.status is NotificationStatus.SUPPRESSED
    assert working.sent == []


async def test_notifications_are_suppressed_when_the_bot_is_not_running(
    clean_tables: Database,
) -> None:
    settings = _settings()
    proposal_id = await _proposal(clean_tables, settings)
    notifier = _notifier(clean_tables, settings, None)
    result = await notifier.deliver(proposal_id, NotificationEvent.PROPOSAL_MANUAL)
    assert result.status is NotificationStatus.SUPPRESSED
    assert result.reason == "the Telegram bot is not running"


async def test_notifications_can_be_switched_off_without_switching_off_the_bot(
    clean_tables: Database,
) -> None:
    settings = _settings(telegram_notifications_enabled=False)
    proposal_id = await _proposal(clean_tables, settings)
    sender = RecordingSender()
    result = await _notifier(clean_tables, settings, sender).deliver(
        proposal_id, NotificationEvent.PROPOSAL_MANUAL
    )
    assert result.status is NotificationStatus.SUPPRESSED
    assert sender.sent == []


# ---------------------------------------------------------------------------
# Button lifecycle
# ---------------------------------------------------------------------------
async def test_keyboards_are_blanked_after_a_terminal_transition(
    clean_tables: Database,
) -> None:
    settings = _settings()
    proposal_id = await _proposal(clean_tables, settings)
    sender = RecordingSender()
    notifier = _notifier(clean_tables, settings, sender)
    await notifier.deliver(proposal_id, NotificationEvent.PROPOSAL_MANUAL)

    await _proposals(clean_tables, settings).reject(proposal_id, actor="telegram:4242")
    await notifier.blank_keyboards(proposal_id)
    assert sender.edits
    assert all(keyboard is None for _, _, keyboard in sender.edits)


async def test_a_failed_edit_does_not_leave_a_live_button(clean_tables: Database) -> None:
    """Security never depends on the keyboard being removed.

    The rejection already consumed every outstanding token; blanking the
    keyboard is a tidy-up whose failure changes nothing.
    """
    settings = _settings()
    proposal_id = await _proposal(clean_tables, settings)
    sender = RecordingSender(fail_edit=True)
    notifier = _notifier(clean_tables, settings, sender)
    await notifier.deliver(proposal_id, NotificationEvent.PROPOSAL_MANUAL)

    await _proposals(clean_tables, settings).reject(proposal_id, actor="telegram:4242")
    await notifier.blank_keyboards(proposal_id)  # swallows the failure
    assert sender.edits == []

    async with clean_tables.session() as session:
        open_actions = int(
            (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(ApprovalAction)
                    .where(ApprovalAction.consumed_at.is_(None))
                )
            ).scalar_one()
        )
    assert open_actions == 0


async def test_a_group_target_gets_no_approval_buttons(clean_tables: Database) -> None:
    """A group chat has no single holder, so a button cannot be bound to one.

    The message still arrives; the operator authorizes with ``/proposals`` in
    that chat, where the tokens are minted for the numeric user who asked.
    """
    settings = _settings(telegram_allowed_chat_ids="-100999", telegram_allow_group_chats=True)
    proposal_id = await _proposal(clean_tables, settings)
    sender = RecordingSender()
    await _notifier(clean_tables, settings, sender).deliver(
        proposal_id, NotificationEvent.PROPOSAL_MANUAL
    )
    assert len(sender.sent) == 1
    assert sender.sent[0].chat_id == -100999
    assert sender.sent[0].keyboard is None
    assert "/proposals" in sender.sent[0].text


async def test_a_missing_proposal_is_suppressed_rather_than_raised(
    clean_tables: Database,
) -> None:
    settings = _settings()
    result = await _notifier(clean_tables, settings, RecordingSender()).deliver(
        uuid.UUID("00000000-0000-0000-0000-0000000000ff"), NotificationEvent.PROPOSAL_MANUAL
    )
    assert result.status is NotificationStatus.SUPPRESSED
    assert result.reason == "proposal not found"


async def test_every_notification_event_has_a_title_and_a_class() -> None:
    """A transition with no rendering would deliver an empty message."""
    from stockbrain.telegram.notifier import _CLASSES, _TITLES

    for event in NotificationEvent:
        assert event in _TITLES
        assert event in _CLASSES


async def test_hostile_thesis_text_cannot_inject_markup_into_a_notification(
    clean_tables: Database,
) -> None:
    settings = _settings()
    await helpers.seed(clean_tables)
    await helpers.fund(clean_tables)
    async with clean_tables.transaction() as session:
        from stockbrain.db.models.research import Thesis

        thesis = await session.get(Thesis, helpers.THESIS_ID)
        assert thesis is not None
        thesis.summary = '<a href="https://evil.example">CONFIRM BUY</a>'
    generated = await _proposals(clean_tables, settings).generate(helpers.THESIS_ID)
    assert generated.proposal_id is not None

    sender = RecordingSender()
    await _notifier(clean_tables, settings, sender).deliver(
        generated.proposal_id, NotificationEvent.PROPOSAL_MANUAL
    )
    text = sender.sent[0].text
    assert "<a href" not in text
    assert "&lt;a href=" in text
    assert Decimal("0") == Decimal("0")  # keeps Decimal import honest for money assertions
