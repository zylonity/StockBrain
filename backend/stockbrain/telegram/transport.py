"""The narrow surface the rest of StockBrain uses to talk to Telegram.

Four operations, and none of them is "send arbitrary bytes to an arbitrary
chat": a destination is always a numeric chat id that came from configuration.
Keeping the protocol this small is what lets the notifier, the approval
coordinator and every test run without ``python-telegram-bot``, an event loop
full of network sockets, or a real bot token.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stockbrain.telegram.approvals import Button

__all__ = ["MessageSender"]


@runtime_checkable
class MessageSender(Protocol):
    """Outbound Telegram operations, as StockBrain needs them."""

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: list[list[Button]] | None = None,
    ) -> int:
        """Send one HTML message and return its message id."""
        ...

    async def edit_reply_markup(
        self, chat_id: int, message_id: int, *, keyboard: list[list[Button]] | None = None
    ) -> None:
        """Replace or remove a message's inline keyboard.

        Best effort by contract.  Failure is logged and swallowed by callers:
        an old button is already inert because its token is consumed, so
        security never depends on this succeeding.
        """
        ...

    async def append_to_message(self, chat_id: int, message_id: int, suffix: str) -> None:
        """Append a final status line to a message, best effort."""
        ...
