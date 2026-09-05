"""Who may talk to the bot, and from where.

Two rules, and neither has an exception:

1. **Numeric ids only.** ``update.effective_user.id`` and
   ``update.effective_chat.id`` are the only identity inputs. A Telegram
   username is chosen by its owner, can be released, and can then be registered
   by somebody else -- so authorizing a financial action on one would mean
   authorizing whoever holds that name today (spec section 4.8).
2. **An empty allowlist authorises nobody.** Not "everyone", not "the first
   person to say /start". A bot with no configured users refuses every update,
   and the runtime does not start at all.

Chat policy is stated rather than inferred. A private chat with an allowlisted
user is accepted because its chat id *is* that user's id, so the binding is
trivially unforgeable. Any other chat -- group, supergroup, channel -- is
refused unless ``TELEGRAM_ALLOW_GROUP_CHATS`` is true *and* the chat id is in
``TELEGRAM_ALLOWED_CHAT_IDS``: everyone who can read a group can read a trade
proposal posted into it, so that has to be a decision somebody made on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["AuthDecision", "AuthOutcome", "TelegramAuthorizer", "TelegramIdentity"]

PRIVATE_CHAT_TYPE = "private"


class AuthOutcome(StrEnum):
    """Why an update was accepted or refused.

    Named per cause rather than collapsed into a boolean so the audit log can
    distinguish "a stranger found the bot" from "the owner used it from an
    unapproved group", which need different responses.
    """

    ALLOWED = "ALLOWED"
    NO_ALLOWLIST = "NO_ALLOWLIST"
    UNKNOWN_USER = "UNKNOWN_USER"
    UNKNOWN_CHAT = "UNKNOWN_CHAT"
    GROUP_CHATS_DISABLED = "GROUP_CHATS_DISABLED"
    INCOMPLETE_UPDATE = "INCOMPLETE_UPDATE"

    @property
    def allowed(self) -> bool:
        return self is AuthOutcome.ALLOWED


@dataclass(frozen=True, slots=True)
class TelegramIdentity:
    """The numeric facts an update carries.  Never a username or display name."""

    user_id: int
    chat_id: int
    chat_type: str

    @property
    def is_private(self) -> bool:
        return self.chat_type == PRIVATE_CHAT_TYPE


@dataclass(frozen=True, slots=True)
class AuthDecision:
    outcome: AuthOutcome
    identity: TelegramIdentity | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome.allowed

    @property
    def reason(self) -> str:
        return _REASONS[self.outcome]


_REASONS: dict[AuthOutcome, str] = {
    AuthOutcome.ALLOWED: "authorised",
    AuthOutcome.NO_ALLOWLIST: "no Telegram user is allowlisted, so nobody is authorised",
    AuthOutcome.UNKNOWN_USER: "this Telegram user id is not allowlisted",
    AuthOutcome.UNKNOWN_CHAT: "this chat is not allowlisted",
    AuthOutcome.GROUP_CHATS_DISABLED: "group chats are disabled for this deployment",
    AuthOutcome.INCOMPLETE_UPDATE: "the update carried no numeric user and chat",
}


class TelegramAuthorizer:
    """Pure, synchronous allowlist check.  No database, no network, no clock."""

    def __init__(
        self,
        *,
        allowed_user_ids: list[int],
        allowed_chat_ids: list[int],
        allow_group_chats: bool = False,
    ) -> None:
        self._users = frozenset(allowed_user_ids)
        self._chats = frozenset(allowed_chat_ids)
        self._allow_group_chats = allow_group_chats

    @property
    def user_count(self) -> int:
        return len(self._users)

    @property
    def chat_count(self) -> int:
        return len(self._chats)

    @property
    def configured(self) -> bool:
        return bool(self._users)

    def check(self, identity: TelegramIdentity | None) -> AuthDecision:
        if identity is None:
            return AuthDecision(AuthOutcome.INCOMPLETE_UPDATE)
        if not self._users:
            return AuthDecision(AuthOutcome.NO_ALLOWLIST, identity)
        if identity.user_id not in self._users:
            return AuthDecision(AuthOutcome.UNKNOWN_USER, identity)
        if identity.is_private:
            # A private chat's id is the user's own id. Anything else claiming
            # to be private is not a chat this user can be reached in, so it is
            # treated as an ordinary unknown chat rather than trusted.
            if identity.chat_id == identity.user_id or identity.chat_id in self._chats:
                return AuthDecision(AuthOutcome.ALLOWED, identity)
            return AuthDecision(AuthOutcome.UNKNOWN_CHAT, identity)
        if not self._allow_group_chats:
            return AuthDecision(AuthOutcome.GROUP_CHATS_DISABLED, identity)
        if identity.chat_id not in self._chats:
            return AuthDecision(AuthOutcome.UNKNOWN_CHAT, identity)
        return AuthDecision(AuthOutcome.ALLOWED, identity)


def identity_from(
    user_id: int | None, chat_id: int | None, chat_type: str | None
) -> TelegramIdentity | None:
    """Build an identity from the three numeric-ish fields an update may carry.

    Returns ``None`` when any of them is missing -- a channel post, an edited
    business message, anything without both a user and a chat. Nothing further
    is inferred from such an update.
    """
    if user_id is None or chat_id is None or not chat_type:
        return None
    return TelegramIdentity(user_id=user_id, chat_id=chat_id, chat_type=chat_type)
