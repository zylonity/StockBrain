"""Outbound proposal notifications, derived from database state.

Telegram is a delivery channel, never the system of record.  Every message this
module sends corresponds to a ``notifications`` row created *first*, keyed by
``telegram:proposal:<id>:<event>`` and protected by
``uq_notifications_dedupe_key``.  That unique index is what makes "one
notification per proposal transition" true across a redelivered job, two
workers, and a restart -- rather than true only while a process happens to
remember what it already sent.

A delivery failure is recorded as a ``FAILED`` row and **not** retried on a
later tick.  The alternative -- an automatic resend -- cannot tell "the message
never arrived" from "the message arrived and the status write did not", so it
turns one transient network error into a duplicate trade alert.  A failed
notification is visible in the table, in the log and in the bot's health; the
proposal itself is unaffected, because a notification is derived from state
rather than being it.

Anti-spam is a rule, not a rate limiter: terminal announcements (rejected,
invalidated, expired, refused) are sent **only for proposals the operator was
actually told about**.  A proposal that was born blocked, or that expired before
anyone saw it, produces no chatter.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from stockbrain.config import Settings
from stockbrain.db.base import utcnow
from stockbrain.db.models.proposals import TradeProposal
from stockbrain.db.models.system import Notification
from stockbrain.db.session import Database
from stockbrain.enums import (
    NotificationClass,
    NotificationEvent,
    NotificationStatus,
    ProposalStatus,
)
from stockbrain.errors import TelegramSendError
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS
from stockbrain.proposals.state_machine import assert_transition, can_transition
from stockbrain.telegram import messages
from stockbrain.telegram.approvals import ApprovalCoordinator
from stockbrain.telegram.formatting import bold, chunk, code, esc, trim
from stockbrain.telegram.service import ProposalView, TelegramService
from stockbrain.telegram.transport import MessageSender

__all__ = ["CHANNEL", "NotificationResult", "ProposalNotifier", "dedupe_key"]

log = get_logger(__name__)

CHANNEL = "telegram"

#: Transitions that are only worth announcing if the operator saw the proposal.
_FOLLOW_UP_EVENTS: frozenset[NotificationEvent] = frozenset(
    {
        NotificationEvent.PROPOSAL_REJECTED,
        NotificationEvent.PROPOSAL_INVALIDATED,
        NotificationEvent.PROPOSAL_EXPIRED,
        NotificationEvent.AUTHORIZATION_REFUSED,
    }
)

_OPENING_EVENTS: frozenset[NotificationEvent] = frozenset(
    {NotificationEvent.PROPOSAL_MANUAL, NotificationEvent.PROPOSAL_AUTO_AUTHORIZED}
)

#: Execution transitions are announced unconditionally.  They are deliberately
#: *not* in ``_FOLLOW_UP_EVENTS``: an order that may exist at a broker has to be
#: reported whether or not the proposal that produced it was ever announced,
#: because the operator's exposure does not depend on having read a message.
_EXECUTION_EVENTS: frozenset[NotificationEvent] = frozenset(
    {
        NotificationEvent.EXECUTION_SUBMITTED,
        NotificationEvent.EXECUTION_REJECTED,
        NotificationEvent.EXECUTION_FAILED,
        NotificationEvent.EXECUTION_AMBIGUOUS,
        NotificationEvent.EXECUTION_RECONCILED,
        NotificationEvent.EXECUTION_CONFIRMED,
    }
)

#: Execution outcomes that must be impossible to skim past. An ambiguous order
#: is the one message in this system where doing nothing is the correct action
#: and doing the obvious thing -- sending it again -- is the worst one.
_CRITICAL_EVENTS: frozenset[NotificationEvent] = frozenset({NotificationEvent.EXECUTION_AMBIGUOUS})

_CLASSES: dict[NotificationEvent, NotificationClass] = {
    NotificationEvent.PROPOSAL_MANUAL: NotificationClass.PROPOSAL,
    NotificationEvent.PROPOSAL_AUTO_AUTHORIZED: NotificationClass.PROPOSAL,
    NotificationEvent.PROPOSAL_REJECTED: NotificationClass.PROPOSAL,
    NotificationEvent.PROPOSAL_INVALIDATED: NotificationClass.SYSTEM_WARNING,
    NotificationEvent.PROPOSAL_EXPIRED: NotificationClass.SYSTEM_WARNING,
    NotificationEvent.AUTHORIZATION_REFUSED: NotificationClass.SYSTEM_WARNING,
    NotificationEvent.EXECUTION_SUBMITTED: NotificationClass.PROPOSAL,
    NotificationEvent.EXECUTION_REJECTED: NotificationClass.SYSTEM_WARNING,
    NotificationEvent.EXECUTION_FAILED: NotificationClass.SYSTEM_WARNING,
    NotificationEvent.EXECUTION_AMBIGUOUS: NotificationClass.CRITICAL,
    NotificationEvent.EXECUTION_RECONCILED: NotificationClass.PROPOSAL,
    NotificationEvent.EXECUTION_CONFIRMED: NotificationClass.PROPOSAL,
}

_TITLES: dict[NotificationEvent, str] = {
    NotificationEvent.PROPOSAL_MANUAL: "Trade proposal awaiting authorization",
    NotificationEvent.PROPOSAL_AUTO_AUTHORIZED: "Trade proposal authorized automatically",
    NotificationEvent.PROPOSAL_REJECTED: "Trade proposal rejected",
    NotificationEvent.PROPOSAL_INVALIDATED: "Trade proposal invalidated",
    NotificationEvent.PROPOSAL_EXPIRED: "Trade proposal expired",
    NotificationEvent.AUTHORIZATION_REFUSED: "Authorization refused by deterministic risk",
    NotificationEvent.EXECUTION_SUBMITTED: "Order accepted by the broker",
    NotificationEvent.EXECUTION_REJECTED: "Broker refused the order",
    NotificationEvent.EXECUTION_FAILED: "Order not transmitted",
    NotificationEvent.EXECUTION_AMBIGUOUS: "ORDER STATE UNKNOWN — do not resend",
    NotificationEvent.EXECUTION_RECONCILED: "Reconciliation resolved an order",
    NotificationEvent.EXECUTION_CONFIRMED: "Order filled",
}


def dedupe_key(proposal_id: uuid.UUID, event: NotificationEvent) -> str:
    return f"{CHANNEL}:proposal:{proposal_id}:{event.value}"


@dataclass(frozen=True, slots=True)
class NotificationResult:
    status: NotificationStatus
    delivered: int = 0
    reason: str | None = None


class ProposalNotifier:
    """Turns a proposal transition into at most one Telegram message per chat."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        service: TelegramService,
        coordinator: ApprovalCoordinator,
        sender: MessageSender | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._service = service
        self._coordinator = coordinator
        self._sender = sender

    def bind(self, sender: MessageSender | None) -> None:
        """Attach (or detach) the live bot.  Detached, notifications suppress."""
        self._sender = sender

    @property
    def targets(self) -> list[int]:
        return self._settings.telegram_notification_targets

    # ------------------------------------------------------------------
    async def deliver(
        self,
        proposal_id: uuid.UUID,
        event: NotificationEvent,
        *,
        detail: str | None = None,
        now: dt.datetime | None = None,
    ) -> NotificationResult:
        """Claim, render and send one notification.  Idempotent."""
        moment = now or utcnow()
        proposal = await self._service.proposal(proposal_id)
        if proposal is None:
            return NotificationResult(NotificationStatus.SUPPRESSED, reason="proposal not found")

        claimed = await self._claim(proposal_id, event, proposal, detail, moment)
        if claimed is None:
            # Another worker, an earlier run, or a restart already handled this
            # transition. Not an error: it is the guarantee working.
            METRICS.inc("stockbrain_telegram_notifications_total", labels={"result": "duplicate"})
            return NotificationResult(NotificationStatus.SUPPRESSED, reason="already recorded")

        reason = await self._suppression_reason(proposal, event)
        if reason is not None:
            await self._finish(claimed, NotificationStatus.SUPPRESSED, error=reason, now=moment)
            METRICS.inc("stockbrain_telegram_notifications_total", labels={"result": "suppressed"})
            return NotificationResult(NotificationStatus.SUPPRESSED, reason=reason)

        assert self._sender is not None  # implied by _suppression_reason
        text = self._render(proposal, event, detail, moment)
        delivered = 0
        failures: list[str] = []
        first_message: str | None = None
        for chat_id in self.targets:
            try:
                message_id = await self._send(proposal, event, chat_id, text, moment)
            except TelegramSendError as exc:
                failures.append(f"{chat_id}: {exc.category}")
                continue
            delivered += 1
            if first_message is None:
                first_message = str(message_id)

        if delivered == 0:
            await self._finish(
                claimed,
                NotificationStatus.FAILED,
                error="; ".join(failures)[:500] or "no configured target chats",
                now=moment,
            )
            METRICS.inc("stockbrain_telegram_notifications_total", labels={"result": "failed"})
            log.warning(
                "telegram_notification_failed",
                proposal_id=str(proposal_id),
                notification_event=event.value,
                failures=len(failures),
            )
            return NotificationResult(NotificationStatus.FAILED, reason="; ".join(failures))

        await self._finish(
            claimed,
            NotificationStatus.SENT,
            reference=first_message,
            error="; ".join(failures)[:500] or None,
            now=moment,
        )
        if event is NotificationEvent.PROPOSAL_MANUAL:
            await self._mark_notified(proposal_id, moment)
        METRICS.inc("stockbrain_telegram_notifications_total", labels={"result": "sent"})
        log.info(
            "telegram_notification_sent",
            proposal_id=str(proposal_id),
            notification_event=event.value,
            chats=delivered,
        )
        return NotificationResult(NotificationStatus.SENT, delivered=delivered)

    # ------------------------------------------------------------------
    async def _claim(
        self,
        proposal_id: uuid.UUID,
        event: NotificationEvent,
        proposal: ProposalView,
        detail: str | None,
        now: dt.datetime,
    ) -> uuid.UUID | None:
        """Insert the notification row, or return ``None`` if it already exists.

        ``ON CONFLICT DO NOTHING`` against the unique dedupe index rather than a
        select-then-insert: check-then-act is a race, a unique index is not.
        """
        title = _TITLES[event]
        body = f"{proposal.broker_ticker} {proposal.side} {proposal.quantity}"
        if detail:
            body = f"{body} — {detail[:400]}"
        statement = (
            pg_insert(Notification)
            .values(
                notification_class=_CLASSES[event],
                channel=CHANNEL,
                title=title,
                body=body,
                status=NotificationStatus.PENDING,
                entity_type="trade_proposal",
                entity_id=proposal_id,
                dedupe_key=dedupe_key(proposal_id, event),
                created_at=now,
            )
            # `uq_notifications_dedupe_key` is a *partial* unique index
            # (`WHERE dedupe_key IS NOT NULL`), and PostgreSQL will only infer a
            # partial index when the predicate is restated here. Without it the
            # insert fails with "no unique or exclusion constraint matching the
            # ON CONFLICT specification" -- at the moment a proposal needs to be
            # announced, which is the worst possible time to discover it.
            .on_conflict_do_nothing(
                index_elements=[Notification.dedupe_key],
                index_where=sa.text("dedupe_key IS NOT NULL"),
            )
            .returning(Notification.id)
        )
        async with self._database.transaction() as session:
            result = await session.execute(statement)
            return result.scalars().first()

    async def _finish(
        self,
        notification_id: uuid.UUID,
        status: NotificationStatus,
        *,
        reference: str | None = None,
        error: str | None = None,
        now: dt.datetime,
    ) -> None:
        async with self._database.transaction() as session:
            await session.execute(
                sa.update(Notification)
                .where(Notification.id == notification_id)
                .values(
                    status=status,
                    sent_at=now if status is NotificationStatus.SENT else None,
                    delivery_reference=reference,
                    error=error,
                )
            )

    async def _suppression_reason(
        self, proposal: ProposalView, event: NotificationEvent
    ) -> str | None:
        if not self._settings.telegram_notifications_enabled:
            return "TELEGRAM_NOTIFICATIONS_ENABLED is false"
        if self._sender is None:
            return "the Telegram bot is not running"
        if not self.targets:
            return "no Telegram chat is configured to notify"
        if event in _FOLLOW_UP_EVENTS and not await self._was_announced(proposal.id):
            # Never announce the death of something nobody was told was born.
            return "the proposal was never announced, so its outcome is not announced either"
        return None

    async def _was_announced(self, proposal_id: uuid.UUID) -> bool:
        keys = [dedupe_key(proposal_id, event) for event in _OPENING_EVENTS]
        async with self._database.session() as session:
            found = await session.scalar(
                sa.select(sa.func.count())
                .select_from(Notification)
                .where(
                    Notification.dedupe_key.in_(keys),
                    Notification.status == NotificationStatus.SENT,
                )
            )
        return bool(found)

    # ------------------------------------------------------------------
    def _render(
        self,
        proposal: ProposalView,
        event: NotificationEvent,
        detail: str | None,
        now: dt.datetime,
    ) -> str:
        if event in _OPENING_EVENTS:
            body = messages.render_proposal_notification(proposal, now=now)
            if event is NotificationEvent.PROPOSAL_MANUAL and not all(
                self._is_private_target(chat) for chat in self.targets
            ):
                body += "\n" + esc("Send /proposals in this chat to authorize or reject.")
            return body
        headline = bold(_TITLES[event])
        lines = [
            headline,
            f"{esc(proposal.broker_ticker)} {esc(proposal.side)} "
            f"{esc(proposal.quantity)} — now {esc(proposal.status.value)}",
        ]
        if event in _EXECUTION_EVENTS:
            lines.append(
                f"Broker: {esc(proposal.broker)} · environment {esc(proposal.broker_environment)}"
            )
            if proposal.broker_order_id:
                lines.append(f"Broker order: {code(proposal.broker_order_id)}")
        if detail:
            lines.append(trim(detail, 400))
        elif proposal.status_reason:
            lines.append(trim(proposal.status_reason, 400))
        if event in _CRITICAL_EVENTS:
            lines.append("")
            lines.append(bold("DO NOT RESEND THIS ORDER."))
            lines.append(
                esc(
                    "StockBrain transmitted a request and did not receive a definitive "
                    "response. The order may or may not exist. Reconciliation is reading "
                    "Trading 212; it will not retry. Do not place the trade manually until "
                    "the outcome is known."
                )
            )
        elif event not in _EXECUTION_EVENTS:
            lines.append(esc(messages.PHASE_NOTICE))
        return "\n".join(lines)

    async def _send(
        self,
        proposal: ProposalView,
        event: NotificationEvent,
        chat_id: int,
        text: str,
        now: dt.datetime,
    ) -> int:
        assert self._sender is not None
        keyboard = None
        action_ids: list[uuid.UUID] = []
        if event is NotificationEvent.PROPOSAL_MANUAL and self._is_private_target(chat_id):
            # Every token binds one proposal, one numeric user and one numeric
            # chat, so a button issued here can only ever be redeemed by that
            # user from that chat. `issue_proposal_keyboard` returns nothing at
            # all for an AUTOMATIC proposal, which is why an automatically
            # authorized proposal cannot grow an approve button.
            keyboard, action_ids = await self._coordinator.issue_proposal_keyboard(
                proposal, user_id=chat_id, chat_id=chat_id, now=now
            )
        parts = chunk(text)
        message_id = await self._sender.send_message(
            chat_id, parts[0], keyboard=keyboard if len(parts) == 1 else None
        )
        for extra in parts[1:-1]:
            await self._sender.send_message(chat_id, extra)
        if len(parts) > 1:
            message_id = await self._sender.send_message(chat_id, parts[-1], keyboard=keyboard)
        if action_ids:
            await self._coordinator.attach_message(
                action_ids, message_id=message_id, chat_id=chat_id
            )
        return message_id

    def _is_private_target(self, chat_id: int) -> bool:
        """Whether this destination is one allowlisted person's own chat.

        A private chat's id equals its user's id, so a keyboard sent there can
        be bound to exactly one identity.  A *group* target has no single
        holder, and minting a button for "whoever taps first" would make the
        user binding meaningless -- so a group notification carries no approval
        buttons at all, and the operator uses ``/proposals`` in that chat, where
        the tokens are minted for the numeric user who actually asked.
        """
        return chat_id in set(self._settings.telegram_allowed_user_ids)

    async def _mark_notified(self, proposal_id: uuid.UUID, now: dt.datetime) -> None:
        """Record that a human was actually told, when the state machine allows.

        ``NOTIFIED`` means "the operator has seen this", which is a different
        fact from ``READY``. The transition is attempted under a lock and
        skipped if the proposal has already moved on -- a notification arriving
        a moment after a web approval must not drag the row backwards.
        """
        async with self._database.transaction() as session:
            proposal = (
                await session.execute(
                    sa.select(TradeProposal)
                    .where(TradeProposal.id == proposal_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if proposal is None or not can_transition(proposal.status, ProposalStatus.NOTIFIED):
                return
            assert_transition(proposal.status, ProposalStatus.NOTIFIED)
            proposal.status = ProposalStatus.NOTIFIED
            proposal.notified_at = now
            proposal.updated_at = now

    # ------------------------------------------------------------------
    async def blank_keyboards(self, proposal_id: uuid.UUID) -> None:
        """Remove the inline keyboards on every message about this proposal.

        Cosmetic tidy-up only.  The tokens behind those buttons were already
        consumed by the transition that ended the proposal's authorizable life,
        so a message that cannot be edited is not a security problem -- it is a
        stale picture of a decision that has already been made.
        """
        if self._sender is None:
            return
        for chat_id, message_id in await self._coordinator.open_message_targets(proposal_id):
            try:
                await self._sender.edit_reply_markup(chat_id, message_id, keyboard=None)
            except TelegramSendError as exc:
                log.debug(
                    "telegram_keyboard_blank_failed",
                    proposal_id=str(proposal_id),
                    category=exc.category,
                )
