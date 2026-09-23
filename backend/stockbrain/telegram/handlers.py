"""Chat command handlers: thin adapters over the service layer.

Every handler does the same four things and nothing else:

    authenticate a numeric identity -> parse the command -> call one service
    method -> format the answer

There is no SQLAlchemy statement, no risk arithmetic, no broker call and no
authorization decision in this module.  Approvals go through
:class:`~stockbrain.telegram.approvals.ApprovalCoordinator`, which goes through
:meth:`~stockbrain.proposals.service.ProposalService.authorize` -- the same
function the web route calls, with the same lock, the same compare-and-swap and
the same full revalidation.  Nothing about a trade is decided here.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.base import utcnow
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS
from stockbrain.telegram import messages
from stockbrain.telegram.approvals import ApprovalCoordinator, Button, telegram_actor
from stockbrain.telegram.auth import (
    AuthOutcome,
    TelegramAuthorizer,
    TelegramIdentity,
    identity_from,
)
from stockbrain.telegram.formatting import bold, chunk, esc
from stockbrain.telegram.service import TelegramService
from stockbrain.telegram.tokens import parse_callback_data
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

if TYPE_CHECKING:  # pragma: no cover - typing only
    from stockbrain.portfolio_reviews import PositionReviewService
    from stockbrain.telegram.runtime import TelegramRuntime

__all__ = ["ALLOWED_UPDATES", "TelegramHandlers", "to_markup"]

log = get_logger(__name__)

#: The only update types StockBrain asks Telegram for.  Narrowing this is free
#: and means an unexpected update kind never reaches a handler at all.
ALLOWED_UPDATES: tuple[str, ...] = ("message", "callback_query")

_DENIED = "Not authorised."

#: Pattern for the callback payloads this bot issues.  Anything else is not
#: routed to a handler; Telegram is answered and the update is dropped.
_CALLBACK_PATTERN = r"^sb:[A-Za-z0-9_\-]{43}$"


def to_markup(keyboard: list[list[Button]] | None) -> InlineKeyboardMarkup | None:
    if not keyboard:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(text=button.label, callback_data=button.callback_data)
                for button in row
            ]
            for row in keyboard
        ]
    )


class TelegramHandlers:
    """Binds the bot's commands to StockBrain services."""

    def __init__(
        self,
        settings: Settings,
        *,
        authorizer: TelegramAuthorizer,
        service: TelegramService,
        coordinator: ApprovalCoordinator,
        control: ControlStateService,
        runtime: TelegramRuntime | None = None,
        position_reviews: PositionReviewService | None = None,
    ) -> None:
        self._settings = settings
        self._auth = authorizer
        self._service = service
        self._coordinator = coordinator
        self._control = control
        self._runtime = runtime
        self._position_reviews = position_reviews
        self._last_research: dict[int, float] = {}

    def register(self, application: Application) -> None:  # type: ignore[type-arg]
        commands = {
            "start": self.start,
            "help": self.help,
            "status": self.status,
            "portfolio": self.portfolio,
            "positions": self.positions,
            "proposals": self.proposals,
            "events": self.events,
            "research": self.research,
            "review": self.review,
            "thesis": self.thesis,
            "pause": self.pause,
            "resume": self.resume,
            "kill": self.kill,
        }
        for name, callback in commands.items():
            application.add_handler(CommandHandler(name, callback))
        application.add_handler(CallbackQueryHandler(self.callback, pattern=_CALLBACK_PATTERN))
        # A callback whose payload is not one of ours still has to be answered,
        # or the sender's client shows a spinner forever.
        application.add_handler(CallbackQueryHandler(self.unknown_callback))

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    def _identify(self, update: Update) -> TelegramIdentity | None:
        user = update.effective_user
        chat = update.effective_chat
        return identity_from(
            user.id if user else None,
            chat.id if chat else None,
            chat.type if chat else None,
        )

    async def _authorise(self, update: Update, command: str) -> TelegramIdentity | None:
        """Return the identity, or ``None`` after refusing and logging.

        The log records the numeric ids and the command name only.  Message
        text is never logged: an unauthorised message is still somebody's
        private message, and a refusal is not a reason to store it.
        """
        decision = self._auth.check(self._identify(update))
        if decision.allowed:
            assert decision.identity is not None
            METRICS.inc("stockbrain_telegram_commands_total", labels={"command": command})
            return decision.identity
        identity = decision.identity
        log.warning(
            "telegram_unauthorised",
            outcome=decision.outcome.value,
            command=command,
            telegram_user_id=identity.user_id if identity else None,
            telegram_chat_id=identity.chat_id if identity else None,
            chat_type=identity.chat_type if identity else None,
        )
        METRICS.inc(
            "stockbrain_telegram_unauthorised_total", labels={"outcome": decision.outcome.value}
        )
        if decision.outcome is not AuthOutcome.INCOMPLETE_UPDATE:
            await self._reply(update, esc(_DENIED))
        return None

    # ------------------------------------------------------------------
    # Replies
    # ------------------------------------------------------------------
    async def _reply(
        self, update: Update, text: str, keyboard: list[list[Button]] | None = None
    ) -> int | None:
        message = update.effective_message
        if message is None:  # pragma: no cover - commands always carry one
            return None
        parts = chunk(text)
        sent = None
        for index, part in enumerate(parts):
            markup = to_markup(keyboard) if index == len(parts) - 1 else None
            sent = await message.reply_text(part, parse_mode=ParseMode.HTML, reply_markup=markup)
        return sent.message_id if sent else None

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    async def start(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        decision = self._auth.check(self._identify(update))
        if not decision.allowed:
            # Says nothing about what the system is or holds. An unknown caller
            # learns only that they are not authorised.
            log.warning(
                "telegram_unauthorised",
                outcome=decision.outcome.value,
                command="start",
                telegram_user_id=decision.identity.user_id if decision.identity else None,
                telegram_chat_id=decision.identity.chat_id if decision.identity else None,
            )
            await self._reply(update, messages.render_start(authorised=False))
            return
        await self._reply(update, messages.render_start(authorised=True))

    async def help(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._authorise(update, "help") is None:
            return
        await self._reply(update, messages.render_help())

    async def status(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._authorise(update, "status") is None:
            return
        telegram_status = self._runtime.status() if self._runtime is not None else {}
        view = await self._service.status(telegram_status=telegram_status)
        await self._reply(update, messages.render_status(view, now=utcnow()))

    async def portfolio(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._authorise(update, "portfolio") is None:
            return
        await self._reply(
            update, messages.render_portfolio(await self._service.portfolio(), now=utcnow())
        )

    async def positions(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._authorise(update, "positions") is None:
            return
        await self._reply(
            update, messages.render_positions(await self._service.positions(), now=utcnow())
        )

    async def proposals(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        identity = await self._authorise(update, "proposals")
        if identity is None:
            return
        now = utcnow()
        views = await self._service.proposals()
        await self._reply(update, messages.render_proposals(views, now=now))
        # Controls are offered for the one proposal that most needs a decision,
        # rather than a wall of buttons: the tokens are per proposal, per user
        # and per chat, so each keyboard costs three rows in the database.
        actionable = next(
            (view for view in views if view.awaiting_authorization and view.is_manual), None
        )
        if actionable is None:
            return
        keyboard, action_ids = await self._coordinator.issue_proposal_keyboard(
            actionable, user_id=identity.user_id, chat_id=identity.chat_id, now=now
        )
        if not keyboard:
            return
        message_id = await self._reply(
            update, messages.render_proposal_detail(actionable, now=now), keyboard
        )
        if message_id is not None:
            await self._coordinator.attach_message(
                action_ids, message_id=message_id, chat_id=identity.chat_id
            )

    async def events(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._authorise(update, "events") is None:
            return
        await self._reply(
            update, messages.render_events(await self._service.events(), now=utcnow())
        )

    async def research(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        identity = await self._authorise(update, "research")
        if identity is None:
            return
        args = context.args or []
        if not args:
            await self._reply(
                update,
                f"{bold('Research')}\n{esc('Usage: /research <proposal id|event id|symbol>')}",
            )
            return
        if (wait := self._research_cooldown(identity.user_id)) > 0:
            await self._reply(update, esc(f"Rate limited. Try again in {wait:.0f}s."))
            return
        query = " ".join(args)[:100]
        await self._reply(
            update, messages.render_research(await self._service.research(query), query)
        )

    async def review(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._authorise(update, "review") is None:
            return
        args = context.args or []
        if len(args) != 1:
            await self._reply(update, esc("Usage: /review <ticker|all>"))
            return
        if self._position_reviews is None:
            await self._reply(update, esc("Position reviews are unavailable."))
            return
        target = args[0].strip()
        result = await self._position_reviews.request(None if target.lower() == "all" else target)
        await self._reply(update, messages.render_position_review(result))

    async def thesis(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._authorise(update, "thesis") is None:
            return
        args = context.args or []
        if len(args) != 1:
            await self._reply(update, esc("Usage: /thesis <ticker|all>"))
            return
        target = args[0].strip()
        views = await self._service.opening_theses(None if target.lower() == "all" else target)
        await self._reply(update, messages.render_opening_theses(views, target))

    def _research_cooldown(self, user_id: int) -> float:
        """Seconds the caller must still wait, and stamp the call if they may go.

        A monotonic clock, so a system time change cannot open the gate.
        """
        interval = self._settings.telegram_research_interval_seconds
        if interval <= 0:
            return 0.0
        now = time.monotonic()
        previous = self._last_research.get(user_id)
        if previous is not None and (remaining := interval - (now - previous)) > 0:
            return remaining
        self._last_research[user_id] = now
        return 0.0

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------
    async def pause(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        identity = await self._authorise(update, "pause")
        if identity is None:
            return
        snapshot = await self._control.pause(
            actor=telegram_actor(identity.user_id),
            source="HUMAN_TELEGRAM",
            reason="paused from Telegram",
        )
        await self._reply(update, messages.render_control(snapshot, "Trading paused"))

    async def resume(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        identity = await self._authorise(update, "resume")
        if identity is None:
            return
        snapshot = await self._control.resume(
            actor=telegram_actor(identity.user_id),
            source="HUMAN_TELEGRAM",
            reason="resumed from Telegram",
        )
        if snapshot.kill_switch.active:
            # /resume lifts a pause and nothing else. Letting the routine
            # control also clear an emergency stop would put the kill switch one
            # habitual tap away from being undone.
            await self._reply(
                update,
                messages.render_control(snapshot, "Pause lifted — kill switch still engaged")
                + "\n"
                + esc(
                    "The emergency kill switch is separate and is still engaged. Releasing it "
                    "is a deliberate second act: run /kill again to see its state, or clear "
                    "control.kill_switch from the web/system control endpoint."
                ),
            )
            return
        await self._reply(update, messages.render_control(snapshot, "Trading resumed"))

    async def kill(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        identity = await self._authorise(update, "kill")
        if identity is None:
            return
        # Deliberately one step. An emergency stop that needs confirming is a
        # worse emergency stop, and every consequence of engaging it is in the
        # safe direction: nothing is bought, sold, cancelled or closed.
        snapshot = await self._control.engage_kill_switch(
            actor=telegram_actor(identity.user_id),
            source="HUMAN_TELEGRAM",
            reason="emergency stop from Telegram",
        )
        await self._reply(update, messages.render_control(snapshot, "KILL SWITCH ENGAGED"))

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------
    async def callback(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:  # pragma: no cover - the handler pattern guarantees one
            return
        decision = self._auth.check(self._identify(update))
        if not decision.allowed:
            log.warning(
                "telegram_unauthorised_callback",
                outcome=decision.outcome.value,
                telegram_user_id=decision.identity.user_id if decision.identity else None,
                telegram_chat_id=decision.identity.chat_id if decision.identity else None,
            )
            METRICS.inc(
                "stockbrain_telegram_unauthorised_total",
                labels={"outcome": decision.outcome.value},
            )
            await query.answer(_DENIED, show_alert=True)
            return
        identity = decision.identity
        assert identity is not None

        raw = parse_callback_data(query.data)
        if raw is None:  # pragma: no cover - the handler pattern guarantees the shape
            await query.answer("Unrecognised action.")
            return

        message = query.message
        result = await self._coordinator.handle(
            raw,
            user_id=identity.user_id,
            chat_id=identity.chat_id,
            message_id=message.message_id if message else None,
            now=utcnow(),
        )
        # Telegram shows a progress bar until the callback is answered, so this
        # happens before any further API call.
        await query.answer(result.short_alert)

        if result.clear_original_keyboard and message is not None:
            await self._safe_edit(update, result.original_suffix)
        if result.text is None:
            return
        message_id = await self._reply(update, result.text, result.keyboard)
        if message_id is not None and result.minted_action_ids:
            await self._coordinator.attach_message(
                result.minted_action_ids, message_id=message_id, chat_id=identity.chat_id
            )

    async def unknown_callback(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.callback_query is not None:
            await update.callback_query.answer("Unrecognised action.")

    async def _safe_edit(self, update: Update, suffix: str | None) -> None:
        """Blank the original keyboard, best effort.

        Every reason this can fail -- the message is too old to edit, a client
        rejected it, Telegram is briefly unavailable -- leaves a button drawn on
        somebody's screen.  That is cosmetic: the token behind it is already
        consumed and the proposal's state is authoritative, so the failure is
        logged and the flow continues.
        """
        query = update.callback_query
        if query is None or query.message is None:  # pragma: no cover - guarded by caller
            return
        try:
            await query.edit_message_reply_markup(reply_markup=None)
            if suffix:
                await self._reply(update, suffix)
        except Exception as exc:  # see docstring: a failed edit is cosmetic
            log.info(
                "telegram_message_edit_failed",
                error_type=type(exc).__name__,
                note="buttons are already inert; the consumed token is what matters",
            )
