"""Opaque, single-use callback tokens.

Telegram hands back whatever bytes a button was built with, from whoever
pressed it, whenever they press it.  Those bytes are therefore treated as an
*identifier and nothing else*: they name a row in ``approval_actions`` and carry
no meaning of their own.

Concretely, the callback payload is ``sb:<43 url-safe characters>`` -- 46 of the
64 bytes Telegram allows for ``callback_data``.  It contains no proposal id, no
ticker, no side, no quantity, no price, no account, and not even the action to
perform: the *stage* is a column on the server-side row, so which button was
pressed is decided by the database rather than by the payload.  A test asserts
that property against the rendered keyboards.

Only the SHA-256 of the raw token is stored.  The raw value exists in exactly
two places -- inside one Telegram message, and in memory for the microseconds it
takes to hash an incoming callback -- and is never persisted, never logged and
never included in an error message.

Redeeming a token checks five bindings before consuming it, in this order:
proposal, stage, numeric user, numeric chat, expiry.  The row is taken
``FOR UPDATE``, so two taps of the same button serialise and the second finds
``consumed_at`` already set.  A token presented by the wrong user or from the
wrong chat is refused *without* being consumed: a stranger must not be able to
burn the owner's button by pressing it first.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.db.base import utcnow
from stockbrain.db.models.proposals import ApprovalAction
from stockbrain.enums import ApprovalChannel, ApprovalStage
from stockbrain.errors import (
    ApprovalActionConsumed,
    ApprovalActionExpired,
    ApprovalActionForeign,
    ApprovalActionInvalid,
)
from stockbrain.logging import get_logger

__all__ = [
    "CALLBACK_PREFIX",
    "MAX_CALLBACK_DATA_BYTES",
    "IssuedToken",
    "ResolvedAction",
    "TokenService",
    "callback_data",
    "parse_callback_data",
    "token_hash",
]

log = get_logger(__name__)

#: Telegram documents ``callback_data`` as 1-64 bytes.  Asserted in tests
#: against every keyboard the bot builds, because exceeding it fails at send
#: time -- that is, in production, on the message that matters most.
MAX_CALLBACK_DATA_BYTES = 64

CALLBACK_PREFIX = "sb:"

#: 32 random bytes rendered url-safe: 43 characters, 256 bits of entropy.  With
#: the prefix that is 46 of the 64 permitted bytes, leaving headroom without
#: making the token guessable.
_TOKEN_BYTES = 32


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A freshly minted token.  ``raw`` is returned once and never stored."""

    raw: str
    action_id: uuid.UUID
    stage: ApprovalStage
    expires_at: dt.datetime

    @property
    def callback_data(self) -> str:
        return callback_data(self.raw)


@dataclass(frozen=True, slots=True)
class ResolvedAction:
    """A consumed token, resolved to the action it names."""

    action_id: uuid.UUID
    proposal_id: uuid.UUID
    stage: ApprovalStage
    user_id: int
    chat_id: int | None
    parent_action_id: uuid.UUID | None
    message_id: int | None


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def callback_data(raw: str) -> str:
    return f"{CALLBACK_PREFIX}{raw}"


def parse_callback_data(data: str | None) -> str | None:
    """Extract the raw token from a callback payload, or ``None``.

    Strict by construction: anything that is not the exact expected shape is
    rejected here rather than reaching a database query.
    """
    if not data or not data.startswith(CALLBACK_PREFIX):
        return None
    raw = data[len(CALLBACK_PREFIX) :]
    if len(raw) != 43 or not all(ch.isalnum() or ch in "-_" for ch in raw):
        return None
    return raw


class TokenService:
    """Mint, redeem and retire approval-action tokens."""

    def __init__(self, *, channel: ApprovalChannel = ApprovalChannel.TELEGRAM) -> None:
        self._channel = channel

    # ------------------------------------------------------------------
    async def mint(
        self,
        session: AsyncSession,
        *,
        proposal_id: uuid.UUID,
        stage: ApprovalStage,
        user_id: int,
        chat_id: int | None,
        expires_at: dt.datetime,
        parent_action_id: uuid.UUID | None = None,
        message_id: int | None = None,
    ) -> IssuedToken:
        """Create one single-use token bound to a proposal, user, chat and stage."""
        raw = secrets.token_urlsafe(_TOKEN_BYTES)
        action = ApprovalAction(
            proposal_id=proposal_id,
            channel=self._channel,
            stage=stage,
            opaque_token_hash=token_hash(raw),
            user_identifier=str(user_id),
            chat_identifier=str(chat_id) if chat_id is not None else None,
            parent_action_id=parent_action_id,
            expires_at=expires_at,
            context={"message_id": message_id} if message_id is not None else {},
        )
        session.add(action)
        await session.flush()
        return IssuedToken(raw=raw, action_id=action.id, stage=stage, expires_at=action.expires_at)

    async def attach_message(
        self, session: AsyncSession, action_ids: list[uuid.UUID], *, message_id: int, chat_id: int
    ) -> None:
        """Record which message carries these buttons.

        Non-authoritative UI context, used only to blank the keyboard once the
        proposal reaches a terminal state.  Security never depends on it: an
        un-blanked button still resolves to a consumed row.
        """
        if not action_ids:
            return
        await session.execute(
            sa.update(ApprovalAction)
            .where(ApprovalAction.id.in_(action_ids))
            .values(context={"message_id": message_id, "chat_id": chat_id})
        )

    # ------------------------------------------------------------------
    async def resolve_and_consume(
        self,
        session: AsyncSession,
        raw: str,
        *,
        user_id: int,
        chat_id: int | None,
        expected_stages: tuple[ApprovalStage, ...] | None = None,
        now: dt.datetime | None = None,
    ) -> ResolvedAction:
        """Redeem a token, or raise the specific reason it cannot be redeemed.

        Must be called inside a transaction: the row is locked ``FOR UPDATE`` so
        a double tap, two devices, or a Telegram redelivery serialise into one
        winner and one :class:`~stockbrain.errors.ApprovalActionConsumed`.
        """
        moment = now or utcnow()
        action = (
            await session.execute(
                sa.select(ApprovalAction)
                .where(ApprovalAction.opaque_token_hash == token_hash(raw))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if action is None:
            raise ApprovalActionInvalid("no approval action matches this token")

        # Identity is checked before consumption. A token presented by the wrong
        # user must not be *spent* by them, or a stranger who obtained a
        # forwarded message could disable the owner's real button.
        if action.user_identifier != str(user_id):
            raise ApprovalActionForeign("this action belongs to a different Telegram user")
        if action.chat_identifier is not None and action.chat_identifier != str(chat_id):
            raise ApprovalActionForeign("this action belongs to a different Telegram chat")

        if action.consumed_at is not None:
            raise ApprovalActionConsumed("this approval action was already used")
        if action.expires_at <= moment:
            raise ApprovalActionExpired("this approval action expired")
        if expected_stages is not None and action.stage not in expected_stages:
            # Cannot happen through the bot's own keyboards; it would mean a
            # token was replayed against a handler it was not issued for.
            raise ApprovalActionInvalid("this approval action is not valid here")

        action.consumed_at = moment
        context = dict(action.context or {})
        raw_message_id = context.get("message_id")
        return ResolvedAction(
            action_id=action.id,
            proposal_id=action.proposal_id,
            stage=action.stage,
            user_id=user_id,
            chat_id=chat_id,
            parent_action_id=action.parent_action_id,
            message_id=raw_message_id if isinstance(raw_message_id, int) else None,
        )

    # ------------------------------------------------------------------
    async def retire_open_actions(
        self,
        session: AsyncSession,
        proposal_id: uuid.UUID,
        *,
        reason: str,
        now: dt.datetime | None = None,
        exclude: uuid.UUID | None = None,
    ) -> int:
        """Consume every outstanding token for a proposal.

        Called whenever a proposal reaches a state no button may still act on.
        The buttons themselves are also blanked where the message can be edited,
        but that is cosmetic: this is what actually makes them inert, and it
        works for a message that was forwarded, screenshotted, or edited by a
        client that refused the edit.
        """
        moment = now or utcnow()
        conditions = [
            ApprovalAction.proposal_id == proposal_id,
            ApprovalAction.consumed_at.is_(None),
        ]
        if exclude is not None:
            conditions.append(ApprovalAction.id != exclude)
        result = await session.execute(
            sa.update(ApprovalAction)
            .where(*conditions)
            .values(consumed_at=moment)
            .returning(ApprovalAction.id)
        )
        retired = len(list(result.scalars()))
        if retired:
            log.info(
                "approval_actions_retired",
                proposal_id=str(proposal_id),
                count=retired,
                reason=reason,
            )
        return retired

    async def open_messages(
        self, session: AsyncSession, proposal_id: uuid.UUID
    ) -> list[tuple[int, int]]:
        """``(chat_id, message_id)`` pairs whose keyboards should be blanked."""
        rows = (
            await session.execute(
                sa.select(ApprovalAction.context).where(
                    ApprovalAction.proposal_id == proposal_id,
                    ApprovalAction.channel == self._channel,
                )
            )
        ).scalars()
        seen: dict[tuple[int, int], None] = {}
        for context in rows:
            chat_id = context.get("chat_id")
            message_id = context.get("message_id")
            if isinstance(chat_id, int) and isinstance(message_id, int):
                seen[(chat_id, message_id)] = None
        return list(seen)
