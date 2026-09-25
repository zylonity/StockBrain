"""The Telegram long-polling runtime, inside StockBrain's own process.

Transport is **long polling** (``getUpdates``), never a webhook.  A webhook
would require an inbound port, a public TLS endpoint and a reverse proxy in
front of a home NAS -- the specification is explicit that StockBrain must not be
exposed to the internet just to support Telegram, and long polling removes the
need entirely.  Telegram documents the two as mutually exclusive, so
python-telegram-bot clears any configured webhook while bootstrapping.

Three properties this module exists to guarantee:

* **Telegram never blocks anything else.**  The bot is started inside a
  supervisor task.  If Telegram is unreachable at boot, the task backs off and
  retries while FastAPI, the job workers, the scheduler and the news stream come
  up normally.  A missing token means the runtime is never constructed at all
  and the provider reads ``DISABLED``.
* **Health is measured, not assumed.**  A quiet bot receives no updates, so "we
  have not crashed" proves nothing.  A periodic ``getMe`` proves the process can
  still reach Telegram, and its result is what drives HEALTHY / DEGRADED / DOWN.
* **A bad token is not retried forever.**  python-telegram-bot's own polling
  loop retries network errors indefinitely with a 1.5x backoff capped at 30s,
  but aborts on ``InvalidToken``.  StockBrain mirrors that judgement: a rejected
  credential is recorded DOWN with a clear reason and the supervisor stops,
  exactly as a provider auth failure is never retried elsewhere in this system.

Pending updates are dropped at startup.  An update that queued while StockBrain
was down is stale by definition, and replaying a control command of unknown age
-- ``/resume`` above all -- is not a thing a restart should do on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from typing import TYPE_CHECKING, Any

from telegram.constants import ParseMode
from telegram.error import Forbidden, InvalidToken, NetworkError, RetryAfter, TimedOut
from telegram.error import TelegramError as PtbTelegramError
from telegram.ext import Application, ApplicationBuilder, Defaults

from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.base import utcnow
from stockbrain.db.session import Database
from stockbrain.enums import ApprovalChannel, ProviderStatus
from stockbrain.errors import TelegramSendError
from stockbrain.logging import get_logger
from stockbrain.observability.health import ProviderHealthRegistry, ProviderName
from stockbrain.portfolio_reviews import PositionReviewService
from stockbrain.proposals.rebalance import RebalanceService
from stockbrain.proposals.service import ProposalService
from stockbrain.telegram.approvals import ApprovalCoordinator, Button
from stockbrain.telegram.auth import TelegramAuthorizer
from stockbrain.telegram.handlers import ALLOWED_UPDATES, TelegramHandlers, to_markup
from stockbrain.telegram.notifier import ProposalNotifier
from stockbrain.telegram.preferences import NotificationPreferences
from stockbrain.telegram.service import TelegramService
from stockbrain.telegram.tokens import TokenService
from telegram import InlineKeyboardMarkup, LinkPreviewOptions

if TYPE_CHECKING:
    from stockbrain.proposals.exits import ExitSweepService

__all__ = ["BotSender", "TelegramRuntime", "error_category"]

log = get_logger(__name__)

_INITIAL_BACKOFF_SECONDS = 5.0

#: A run of failures this long stops being "degraded" and starts being "down".
#: The same number ends the watchdog's patience and rebuilds the application.
_DOWN_AFTER_FAILURES = 3


def error_category(exc: BaseException) -> str:
    """A short, secret-free label for a Telegram failure.

    The exception's own text is deliberately discarded: python-telegram-bot
    error messages can carry the request URL, and the request URL carries the
    bot token.  Only the class name crosses into a log or a health record.
    """
    return type(exc).__name__


class BotSender:
    """Adapts python-telegram-bot to StockBrain's four-operation send protocol."""

    def __init__(self, application: Application[Any, Any, Any, Any, Any, Any]) -> None:
        self._application = application

    async def send_message(
        self, chat_id: int, text: str, *, keyboard: list[list[Button]] | None = None
    ) -> int:
        markup: InlineKeyboardMarkup | None = to_markup(keyboard)
        try:
            message = await self._application.bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup
            )
        except PtbTelegramError as exc:
            raise TelegramSendError(error_category(exc)) from exc
        return int(message.message_id)

    async def edit_reply_markup(
        self, chat_id: int, message_id: int, *, keyboard: list[list[Button]] | None = None
    ) -> None:
        try:
            await self._application.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=to_markup(keyboard)
            )
        except PtbTelegramError as exc:
            raise TelegramSendError(error_category(exc)) from exc

    async def append_to_message(self, chat_id: int, message_id: int, suffix: str) -> None:
        try:
            await self._application.bot.send_message(
                chat_id=chat_id,
                text=suffix,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=message_id,
            )
        except PtbTelegramError as exc:
            raise TelegramSendError(error_category(exc)) from exc


class TelegramRuntime:
    """Owns the bot's lifecycle and reports its health."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        health: ProviderHealthRegistry,
        proposals: ProposalService | None,
        control: ControlStateService,
        preferences: NotificationPreferences | None = None,
        exits: ExitSweepService | None = None,
        position_reviews: PositionReviewService | None = None,
        rebalance: RebalanceService | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._rebalance = rebalance
        self._health = health
        self._control = control
        self._position_reviews = position_reviews

        self._authorizer = TelegramAuthorizer(
            allowed_user_ids=settings.telegram_allowed_user_ids,
            allowed_chat_ids=settings.telegram_allowed_chat_ids,
            allow_group_chats=settings.telegram_allow_group_chats,
        )
        self._service = TelegramService(
            database, settings, health=health, control=control, exits=exits
        )
        self._tokens = TokenService(channel=ApprovalChannel.TELEGRAM)
        self._coordinator = ApprovalCoordinator(
            database,
            settings,
            tokens=self._tokens,
            proposals=proposals,
            service=self._service,
            control=control,
        )
        # Shared with the ingestion and classification paths through the service
        # container: they consult the same preferences to decide whether to
        # enqueue a notification job at all, and a second instance would be a
        # second cache that could disagree with this one.
        self.preferences = preferences or NotificationPreferences(database)
        self.notifier = ProposalNotifier(
            database,
            settings,
            service=self._service,
            coordinator=self._coordinator,
            preferences=self.preferences,
        )

        self._application: Application[Any, Any, Any, Any, Any, Any] | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._bot_identified = False
        self._started_at: dt.datetime | None = None
        self._last_contact_at: dt.datetime | None = None
        self._last_error_category: str | None = None
        self._consecutive_failures = 0
        self._fatal: str | None = None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._settings.telegram_available

    def status(self) -> dict[str, object]:
        """Health facts, containing no token and no secret.

        Reported the same way every other provider is, so a Telegram outage
        reads like an Alpaca outage rather than needing its own vocabulary.
        """
        blockers = self._settings.telegram_blockers
        if blockers:
            state = ProviderStatus.DISABLED
        elif self._fatal is not None:
            state = ProviderStatus.DOWN
        elif self._consecutive_failures >= _DOWN_AFTER_FAILURES:
            # Checked before "is the application built", because a bot that
            # cannot start reports its *failures*, not an absence of news. The
            # earlier ordering made a run of connection refusals read UNKNOWN.
            state = ProviderStatus.DOWN
        elif self._consecutive_failures:
            state = ProviderStatus.DEGRADED
        elif self._application is None:
            state = ProviderStatus.UNKNOWN
        else:
            state = ProviderStatus.HEALTHY
        return {
            "status": state.value,
            "bot_configured": bool(self._settings.telegram_bot_token.get_secret_value()),
            # Deliberately a boolean rather than the bot's numeric id. A bot id
            # is the part of the token *before* the colon, so reporting it would
            # publish half the credential in a health payload and in every
            # startup log line -- for no operational benefit, since "did the
            # token authenticate" is the only question anyone asks here.
            "bot_identified": self._bot_identified,
            "transport": "long_polling",
            "webhook_configured": False,
            "polling": self._application is not None and self._fatal is None,
            "started_at": _iso(self._started_at),
            "last_contact_at": _iso(self._last_contact_at),
            "last_error_category": self._last_error_category,
            "consecutive_failures": self._consecutive_failures,
            "authorized_users": self._authorizer.user_count,
            "authorized_chats": self._authorizer.chat_count,
            "notification_targets": len(self._settings.telegram_notification_targets),
            "group_chats_allowed": self._settings.telegram_allow_group_chats,
            "blockers": blockers,
            "fatal": self._fatal,
        }

    def _record_health(self) -> None:
        status = self.status()
        state = ProviderStatus(str(status["status"]))
        detail = self._fatal or self._last_error_category
        if state is ProviderStatus.DISABLED:
            self._health.set_disabled(
                ProviderName.TELEGRAM, "; ".join(self._settings.telegram_blockers)
            )
            return
        self._health.record(
            ProviderName.TELEGRAM,
            state,
            detail=detail,
            metrics={key: value for key, value in status.items() if key != "status"},
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Begin supervising the bot.  Returns immediately.

        Deliberately non-blocking: the caller is the application's startup
        sequence, and a Telegram outage must delay neither the HTTP server nor
        the job workers.
        """
        if not self.enabled:
            log.info("telegram_disabled", blockers=self._settings.telegram_blockers)
            self._record_health()
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._supervise(), name="telegram-runtime")

    async def stop(self) -> None:
        """Stop polling and shut the bot down cleanly.

        The PTB shutdown order is the documented one for a manually managed
        application: stop the updater, stop the application, then shut down.
        """
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._teardown()
        self.notifier.bind(None)
        log.info("telegram_stopped")

    async def _supervise(self) -> None:
        backoff = _INITIAL_BACKOFF_SECONDS
        while not self._stopping.is_set():
            try:
                await self._launch()
            except InvalidToken:
                # A rejected credential will not fix itself, and retrying it
                # only produces noise. Same judgement as ProviderAuthError.
                self._fatal = "Telegram rejected the bot token"
                self._last_error_category = "InvalidToken"
                self._record_health()
                log.error("telegram_token_rejected", remediation="check TELEGRAM_BOT_TOKEN")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._consecutive_failures += 1
                self._last_error_category = error_category(exc)
                self._record_health()
                log.warning(
                    "telegram_start_failed",
                    error_category=self._last_error_category,
                    retry_in_seconds=round(backoff, 1),
                )
                await self._teardown()
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(backoff):
                        await self._stopping.wait()
                backoff = min(backoff * 2, self._settings.telegram_max_backoff_seconds)
                continue

            backoff = _INITIAL_BACKOFF_SECONDS
            await self._watch()
            if self._stopping.is_set():
                return
            # The watchdog decided the connection is gone. Rebuild rather than
            # nurse a half-dead application.
            await self._teardown()

    async def _launch(self) -> None:
        token = self._settings.telegram_bot_token.get_secret_value()
        defaults = Defaults(
            parse_mode=ParseMode.HTML,
            # Untrusted text is escaped, so it cannot become a disguised link;
            # disabling previews additionally stops Telegram rendering a card
            # for a bare URL that appeared inside a headline or a thesis.
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        application = (
            ApplicationBuilder()
            .token(token)
            .defaults(defaults)
            # StockBrain schedules its own work in PostgreSQL. PTB's JobQueue
            # would be a second scheduler with none of the durability, and the
            # specification says the app's own scheduler stays authoritative.
            .job_queue(None)
            .build()
        )
        handlers = TelegramHandlers(
            self._settings,
            authorizer=self._authorizer,
            service=self._service,
            coordinator=self._coordinator,
            control=self._control,
            runtime=self,
            position_reviews=self._position_reviews,
            rebalance=self._rebalance,
        )
        handlers.register(application)
        application.add_error_handler(self._on_error)

        await application.initialize()
        # `get_me` is what actually proves the token authenticates; the identity
        # it returns is not retained beyond a boolean.
        await application.bot.get_me()
        await application.updater.start_polling(  # type: ignore[union-attr]
            poll_interval=self._settings.telegram_poll_interval_seconds,
            timeout=self._settings.telegram_poll_timeout_seconds,
            allowed_updates=list(ALLOWED_UPDATES),
            # Anything queued while StockBrain was down is of unknown age, and
            # replaying a control command from an unknown time is not something
            # a restart should decide to do.
            drop_pending_updates=True,
            # Our own supervisor owns the backoff, so a bootstrap failure is
            # surfaced here rather than retried invisibly inside the library.
            bootstrap_retries=0,
            error_callback=self._on_poll_error,
        )
        await application.start()

        self._application = application
        self._bot_identified = True
        self._started_at = utcnow()
        self._last_contact_at = self._started_at
        self._consecutive_failures = 0
        self._last_error_category = None
        self._fatal = None
        self.notifier.bind(BotSender(application))
        self._record_health()
        log.info(
            "telegram_started",
            transport="long_polling",
            authorized_users=self._authorizer.user_count,
            authorized_chats=self._authorizer.chat_count,
            notification_targets=len(self._settings.telegram_notification_targets),
        )

    async def _watch(self) -> None:
        """Prove periodically that the bot can still reach Telegram.

        Returns when the connection is judged lost, or when shutdown is asked
        for.  ``getMe`` is the cheapest call that exercises the whole path:
        credentials, DNS, TLS and the API.
        """
        interval = self._settings.telegram_health_interval_seconds
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(interval):
                    await self._stopping.wait()
            if self._stopping.is_set():
                return
            assert self._application is not None
            try:
                await self._application.bot.get_me()
            except InvalidToken:
                self._fatal = "Telegram rejected the bot token"
                self._last_error_category = "InvalidToken"
                self._record_health()
                log.error("telegram_token_rejected", remediation="check TELEGRAM_BOT_TOKEN")
                self._stopping.set()
                return
            except (NetworkError, TimedOut, RetryAfter, Forbidden, PtbTelegramError) as exc:
                self._consecutive_failures += 1
                self._last_error_category = error_category(exc)
                self._record_health()
                log.warning(
                    "telegram_contact_failed",
                    error_category=self._last_error_category,
                    consecutive_failures=self._consecutive_failures,
                )
                # python-telegram-bot keeps retrying getUpdates on its own, so a
                # single failure degrades rather than restarts. A run of them
                # means the application is not coming back by itself.
                if self._consecutive_failures >= _DOWN_AFTER_FAILURES:
                    return
                continue
            self._last_contact_at = utcnow()
            self._consecutive_failures = 0
            self._last_error_category = None
            self._record_health()

    async def _teardown(self) -> None:
        application = self._application
        self._application = None
        self.notifier.bind(None)
        if application is None:
            return
        with contextlib.suppress(Exception):
            if application.updater is not None and application.updater.running:
                await application.updater.stop()
        with contextlib.suppress(Exception):
            if application.running:
                await application.stop()
        with contextlib.suppress(Exception):
            await application.shutdown()

    # ------------------------------------------------------------------
    def _on_poll_error(self, error: PtbTelegramError) -> None:
        """Called by python-telegram-bot for a polling error it will retry.

        Must not raise: the library documents that an exception here aborts its
        retry loop.
        """
        self._consecutive_failures += 1
        self._last_error_category = error_category(error)
        log.warning(
            "telegram_poll_error",
            error_category=self._last_error_category,
            consecutive_failures=self._consecutive_failures,
        )
        self._record_health()

    async def _on_error(self, _update: object, context: object) -> None:
        """Handler-level errors.  One bad update must not stop the bot."""
        error = getattr(context, "error", None)
        log.warning(
            "telegram_handler_error",
            error_category=error_category(error) if error else "Unknown",
        )


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None
