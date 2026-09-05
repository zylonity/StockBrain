"""Live, opt-in verification of the Telegram bot.

Run explicitly::

    pytest -m live -s tests/integration/test_phase7_live.py

**One API call by default**: ``getMe``, which proves the token authenticates and
that long polling would work, and costs nothing.  It writes nothing to Telegram
and nothing to the database.

Sending a message is a *second* opt-in, because a message appears on somebody's
phone:

* ``TELEGRAM_LIVE_SEND=yes`` records that a human intended it, and
* ``TELEGRAM_ALLOWED_USER_IDS`` (or ``TELEGRAM_ALLOWED_CHAT_IDS``) must already
  name a destination -- the test never discovers one.

What is sent is a harmless status line.  **No proposal is created, authorized or
rejected here, no button is minted, and no broker endpoint is contacted** -- the
process has no order, cancel or amend path to contact one with.

Nothing secret is printed: not the token, not a chat id, not a user id.  What
reaches stdout is the *contract* -- whether the bot authenticated, whether it is
a bot account, whether a webhook is configured (it must not be, since Telegram
documents ``getUpdates`` and ``setWebhook`` as mutually exclusive), and how many
destinations are allowlisted.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = [pytest.mark.live, pytest.mark.integration]

_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


def _env(name: str) -> str:
    """Read one setting from the process environment or the root ``.env``.

    Returned, never logged.  Only *presence* is ever reported, and only as a
    skip reason.
    """
    from_process = os.environ.get(name)
    if from_process:
        return from_process
    if not _ENV_FILE.is_file():
        return ""
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() != name:
            continue
        return value.split(" #", 1)[0].strip().strip("'\"")
    return ""


def _token() -> str:
    token = _env("TELEGRAM_BOT_TOKEN")
    if not token:
        pytest.skip(
            "TELEGRAM_BOT_TOKEN is not configured "
            f"(checked the process environment and {_ENV_FILE})"
        )
    return token


def _targets() -> list[int]:
    raw = _env("TELEGRAM_ALLOWED_CHAT_IDS") or _env("TELEGRAM_ALLOWED_USER_IDS")
    return [int(part) for part in raw.replace(" ", "").split(",") if part]


async def test_the_bot_authenticates_and_no_webhook_is_configured() -> None:
    """One ``getMe`` and one ``getWebhookInfo``.

    The webhook check matters because Telegram documents ``getUpdates`` and
    ``setWebhook`` as mutually exclusive: a webhook left configured on the bot
    would silently stop long polling from ever receiving an update.
    """
    from telegram import Bot

    bot = Bot(_token())
    async with bot:
        me = await bot.get_me()
        webhook = await bot.get_webhook_info()

    print("\n--- Telegram getMe ---")
    # No bot id and no username: a bot id is the half of the token before the
    # colon, and a username is not an identity this system uses for anything.
    print("authenticated        : True")
    print(f"is_bot               : {me.is_bot}")
    print(f"can_join_groups      : {me.can_join_groups}")
    print(f"can_read_all_group_messages: {me.can_read_all_group_messages}")
    print(f"supports_inline      : {me.supports_inline_queries}")
    print("--- Telegram getWebhookInfo ---")
    print(f"webhook configured   : {bool(webhook.url)}")
    print(f"pending update count : {webhook.pending_update_count}")
    print(f"allowlisted targets  : {len(_targets())}")

    assert me.is_bot
    assert not webhook.url, (
        "a configured webhook makes getUpdates return nothing; long polling is the "
        "designed transport (spec section 4.8)"
    )


async def test_a_harmless_status_message_can_be_delivered() -> None:
    """Second opt-in: this puts a message on somebody's phone.

    Deliberately not a proposal and deliberately without buttons. Nothing here
    creates, authorizes or rejects anything, and no broker endpoint exists to
    be contacted.
    """
    if os.environ.get("TELEGRAM_LIVE_SEND", "").strip().lower() != "yes":
        pytest.skip("TELEGRAM_LIVE_SEND=yes is required before this test sends a message")
    targets = _targets()
    if not targets:
        pytest.skip(
            "TELEGRAM_ALLOWED_USER_IDS / TELEGRAM_ALLOWED_CHAT_IDS are empty; "
            "this test never discovers a destination"
        )

    from telegram.constants import ParseMode

    from stockbrain.telegram.formatting import bold, esc
    from telegram import Bot

    text = f"{bold('StockBrain live verification')}\n" + esc(
        "Phase 7 connectivity check. No proposal, no order, no broker call."
    )
    bot = Bot(_token())
    async with bot:
        message = await bot.send_message(chat_id=targets[0], text=text, parse_mode=ParseMode.HTML)

    print("\n--- Telegram sendMessage ---")
    print(f"delivered            : {message.message_id > 0}")
    print("parse mode           : HTML")
    print(f"targets configured   : {len(targets)}")
    assert message.message_id > 0
