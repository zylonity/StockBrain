"""Doubles for the Telegram surface.

Two seams make the bot testable without a network, a token or an event loop
full of sockets:

* :class:`RecordingSender` implements the four-operation
  :class:`~stockbrain.telegram.transport.MessageSender` protocol and keeps every
  message it was asked to send;
* :class:`FakeUpdate` carries only the attributes a handler reads --
  ``effective_user``, ``effective_chat``, ``effective_message``,
  ``callback_query`` -- which is itself the point: a handler that started
  reading a *username* would not compile against this double.

Handlers are typed against ``telegram.Update``, so the tests cast.  That is
deliberate rather than lazy: the cast is where a reader can see exactly how
little of an update StockBrain is willing to depend on.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from stockbrain.db.base import utcnow
from stockbrain.enums import ExecutionPolicy, ProposalStatus
from stockbrain.errors import TelegramSendError
from stockbrain.telegram.approvals import Button
from stockbrain.telegram.service import ProposalView

__all__ = [
    "FakeCallbackQuery",
    "FakeChat",
    "FakeMessage",
    "FakeUpdate",
    "FakeUser",
    "RecordingSender",
    "SentMessage",
    "callback_datas",
    "make_update",
    "proposal_view",
]


@dataclass(slots=True)
class SentMessage:
    chat_id: int
    text: str
    keyboard: list[list[Button]] | None
    message_id: int


class RecordingSender:
    """A :class:`MessageSender` that records instead of sending."""

    def __init__(self, *, fail_send: bool = False, fail_edit: bool = False) -> None:
        self.sent: list[SentMessage] = []
        self.edits: list[tuple[int, int, list[list[Button]] | None]] = []
        self.appended: list[tuple[int, int, str]] = []
        self._fail_send = fail_send
        self._fail_edit = fail_edit
        self._next_id = 1000

    async def send_message(
        self, chat_id: int, text: str, *, keyboard: list[list[Button]] | None = None
    ) -> int:
        if self._fail_send:
            raise TelegramSendError("NetworkError")
        self._next_id += 1
        self.sent.append(
            SentMessage(chat_id=chat_id, text=text, keyboard=keyboard, message_id=self._next_id)
        )
        return self._next_id

    async def edit_reply_markup(
        self, chat_id: int, message_id: int, *, keyboard: list[list[Button]] | None = None
    ) -> None:
        if self._fail_edit:
            raise TelegramSendError("BadRequest")
        self.edits.append((chat_id, message_id, keyboard))

    async def append_to_message(self, chat_id: int, message_id: int, suffix: str) -> None:
        self.appended.append((chat_id, message_id, suffix))

    # -- convenience -----------------------------------------------------
    @property
    def texts(self) -> list[str]:
        return [message.text for message in self.sent]

    @property
    def keyboards(self) -> list[list[list[Button]]]:
        return [message.keyboard for message in self.sent if message.keyboard]


def callback_datas(keyboard: list[list[Button]] | None) -> list[str]:
    return [button.callback_data for row in (keyboard or []) for button in row]


# ---------------------------------------------------------------------------
# Update doubles
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class FakeUser:
    id: int
    # Present precisely so a test can prove it is never consulted.
    username: str = "not-an-identity"
    first_name: str = "Test"
    is_bot: bool = False


@dataclass(slots=True)
class FakeChat:
    id: int
    type: str = "private"


@dataclass(slots=True)
class FakeMessage:
    message_id: int = 1
    text: str = ""
    replies: list[tuple[str, Any]] = field(default_factory=list)

    async def reply_text(
        self, text: str, parse_mode: str | None = None, reply_markup: Any = None
    ) -> FakeMessage:
        self.replies.append((text, reply_markup))
        return FakeMessage(message_id=self.message_id + len(self.replies))


@dataclass(slots=True)
class FakeCallbackQuery:
    data: str
    message: FakeMessage
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    answers: list[tuple[str, bool]] = field(default_factory=list)
    markup_edits: list[Any] = field(default_factory=list)
    edit_fails: bool = False

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def edit_message_reply_markup(self, reply_markup: Any = None) -> None:
        if self.edit_fails:
            raise RuntimeError("message is too old to edit")
        self.markup_edits.append(reply_markup)


@dataclass(slots=True)
class FakeUpdate:
    effective_user: FakeUser | None
    effective_chat: FakeChat | None
    effective_message: FakeMessage | None
    callback_query: FakeCallbackQuery | None = None


def make_update(
    *,
    user_id: int | None = 111,
    chat_id: int | None = 111,
    chat_type: str = "private",
    callback_data: str | None = None,
    edit_fails: bool = False,
) -> FakeUpdate:
    message = FakeMessage()
    query = (
        FakeCallbackQuery(data=callback_data, message=message, edit_fails=edit_fails)
        if callback_data is not None
        else None
    )
    return FakeUpdate(
        effective_user=FakeUser(id=user_id) if user_id is not None else None,
        effective_chat=FakeChat(id=chat_id, type=chat_type) if chat_id is not None else None,
        effective_message=message,
        callback_query=query,
    )


def proposal_view(**overrides: Any) -> ProposalView:
    """A believable proposal view, with one boring default per field.

    Tests override exactly the field they are about, so a rendering assertion
    reads as "given a hostile company name..." rather than as fifteen lines of
    scaffolding.
    """
    now = utcnow()
    base: dict[str, Any] = {
        "id": uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001"),
        "status": ProposalStatus.READY,
        "status_reason": None,
        "execution_policy": ExecutionPolicy.MANUAL,
        "authorization_source": None,
        "approved_by": None,
        "company": "Apple Inc.",
        "instrument_name": "Apple Inc.",
        "broker": "TRADING212",
        "broker_environment": "demo",
        "broker_ticker": "AAPL_US_EQ",
        "market_symbol": "AAPL",
        "side": "BUY",
        "research_action": "BUY",
        "quantity": Decimal("3"),
        "notional": Decimal("600.15"),
        "account_currency": "USD",
        "reference_price": Decimal("200.05"),
        "reference_currency": "USD",
        "price_source": "ALPACA_IEX",
        "quote_bid": Decimal("199.95"),
        "quote_ask": Decimal("200.05"),
        "quote_spread": Decimal("0.10"),
        "quote_spread_bps": Decimal("5.0"),
        "quote_age_ms": 250,
        "market_session": "REGULAR",
        "research_confidence": 0.9,
        "thesis_summary": "A thesis.",
        "risks": ["A risk."],
        "expires_at": now + dt.timedelta(minutes=30),
        "created_at": now,
        "blocking_reasons": [],
        "broker_order_sent": False,
    }
    base.update(overrides)
    return ProposalView(**base)
