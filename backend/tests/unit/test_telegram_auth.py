"""Who the bot will talk to.

The property under test is narrow and absolute: authorization is a function of
two numbers and a chat type.  A username, a display name, a message body and a
forwarded message header contribute nothing, so a user who renames themselves
gains nothing and loses nothing, and a stranger who obtains a copy of a
privileged message gains nothing at all.
"""

from __future__ import annotations

from stockbrain.config import Settings
from stockbrain.telegram.auth import (
    AuthOutcome,
    TelegramAuthorizer,
    TelegramIdentity,
    identity_from,
)

OWNER = 4242
GROUP = -100999


def authorizer(**kwargs: object) -> TelegramAuthorizer:
    base: dict[str, object] = {
        "allowed_user_ids": [OWNER],
        "allowed_chat_ids": [],
        "allow_group_chats": False,
    }
    base.update(kwargs)
    return TelegramAuthorizer(**base)  # type: ignore[arg-type]


def private(user_id: int, chat_id: int | None = None) -> TelegramIdentity:
    return TelegramIdentity(user_id=user_id, chat_id=chat_id or user_id, chat_type="private")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def test_an_allowlisted_user_in_their_own_private_chat_is_allowed() -> None:
    assert authorizer().check(private(OWNER)).allowed


def test_an_unknown_user_is_refused() -> None:
    decision = authorizer().check(private(9999))
    assert not decision.allowed
    assert decision.outcome is AuthOutcome.UNKNOWN_USER


def test_an_empty_allowlist_authorises_nobody() -> None:
    """Never read as "everyone".

    This is the failure mode that turns a private bot into a public one, and it
    is the one an operator is most likely to reach by leaving a variable blank.
    """
    decision = authorizer(allowed_user_ids=[]).check(private(OWNER))
    assert not decision.allowed
    assert decision.outcome is AuthOutcome.NO_ALLOWLIST
    assert not authorizer(allowed_user_ids=[]).configured


def test_the_username_is_not_an_input_at_all() -> None:
    """A username can be released and re-registered by someone else.

    Asserted structurally: the identity type has no field for one, so there is
    nothing for a future change to start trusting.
    """
    assert set(TelegramIdentity.__slots__) == {"user_id", "chat_id", "chat_type"}
    assert "username" not in TelegramIdentity.__annotations__


# ---------------------------------------------------------------------------
# Chats
# ---------------------------------------------------------------------------
def test_a_group_chat_is_refused_by_default() -> None:
    identity = TelegramIdentity(user_id=OWNER, chat_id=GROUP, chat_type="supergroup")
    decision = authorizer().check(identity)
    assert not decision.allowed
    assert decision.outcome is AuthOutcome.GROUP_CHATS_DISABLED


def test_a_group_chat_needs_both_the_switch_and_the_allowlist() -> None:
    """Two independent decisions, because they answer different questions.

    Everyone who can read a group can read a trade proposal posted into it, so
    enabling group chats at all and naming *which* group are both deliberate.
    """
    identity = TelegramIdentity(user_id=OWNER, chat_id=GROUP, chat_type="supergroup")
    assert not authorizer(allowed_chat_ids=[GROUP]).check(identity).allowed
    assert not authorizer(allow_group_chats=True).check(identity).allowed
    assert authorizer(allow_group_chats=True, allowed_chat_ids=[GROUP]).check(identity).allowed


def test_a_private_chat_whose_id_is_not_the_user_is_refused() -> None:
    """A private chat's id *is* its user's id.

    Anything claiming otherwise is not a chat this user can be reached in, so it
    is treated as an unknown chat rather than trusted for being labelled
    "private".
    """
    identity = TelegramIdentity(user_id=OWNER, chat_id=777, chat_type="private")
    decision = authorizer().check(identity)
    assert not decision.allowed
    assert decision.outcome is AuthOutcome.UNKNOWN_CHAT


def test_an_explicitly_allowlisted_chat_id_is_accepted_for_a_private_chat() -> None:
    identity = TelegramIdentity(user_id=OWNER, chat_id=777, chat_type="private")
    assert authorizer(allowed_chat_ids=[777]).check(identity).allowed


# ---------------------------------------------------------------------------
# Incomplete updates
# ---------------------------------------------------------------------------
def test_an_update_without_a_user_or_chat_yields_no_identity() -> None:
    """Channel posts and similar updates carry no numeric user.

    Nothing is inferred from them; they simply produce no identity, and the
    handler answers nothing rather than guessing.
    """
    assert identity_from(None, 5, "private") is None
    assert identity_from(5, None, "private") is None
    assert identity_from(5, 5, "") is None
    decision = authorizer().check(None)
    assert decision.outcome is AuthOutcome.INCOMPLETE_UPDATE
    assert decision.identity is None


def test_every_outcome_has_a_sentence() -> None:
    """A refusal an operator cannot explain is a refusal they will work around."""
    for outcome in AuthOutcome:
        from stockbrain.telegram.auth import AuthDecision

        assert AuthDecision(outcome).reason


# ---------------------------------------------------------------------------
# Configuration wiring
# ---------------------------------------------------------------------------
def test_settings_report_the_exact_reasons_the_bot_will_not_run() -> None:
    """One list drives the startup log, the health record and ``/status``.

    Every field is passed explicitly. ``Settings`` reads the developer's own
    ``.env``, so a test that relied on a variable being *absent* there passed
    only until somebody configured it -- which is exactly what happened between
    Phase 7 and Phase 8.
    """
    blank = Settings(app_env="test", telegram_enabled=False, telegram_allowed_user_ids="")
    assert not blank.telegram_available
    assert "TELEGRAM_ENABLED is false" in blank.telegram_blockers

    no_allowlist = Settings(
        app_env="test",
        telegram_enabled=True,
        telegram_bot_token="123:abc",
        telegram_allowed_user_ids="",
    )
    assert not no_allowlist.telegram_available
    assert any("authorises nobody" in blocker for blocker in no_allowlist.telegram_blockers)

    ready = Settings(
        app_env="test",
        telegram_enabled=True,
        telegram_bot_token="123:abc",
        telegram_allowed_user_ids="4242",
    )
    assert ready.telegram_available
    assert ready.telegram_blockers == []


def test_notification_targets_prefer_the_chat_allowlist_and_never_guess() -> None:
    """A chat that was never configured is never written to."""
    users_only = Settings(
        app_env="test",
        telegram_enabled=True,
        telegram_bot_token="123:abc",
        telegram_allowed_user_ids="1,2,2",
        telegram_allowed_chat_ids="",
    )
    assert users_only.telegram_notification_targets == [1, 2]

    with_chats = Settings(
        app_env="test",
        telegram_enabled=True,
        telegram_bot_token="123:abc",
        telegram_allowed_user_ids="1,2",
        telegram_allowed_chat_ids="-500",
    )
    assert with_chats.telegram_notification_targets == [-500]
