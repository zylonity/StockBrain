"""The server side of every Telegram button.

This module is deliberately free of ``python-telegram-bot``: it takes a raw
callback token plus the numeric user and chat that presented it, and returns a
:class:`CallbackResult` describing what to say and which buttons to draw.  That
is what lets the whole approval flow -- including every race in specification
section 33 -- be tested without a bot, a network, or a Telegram account.

The flow it implements:

1. **Approve** consumes an ``APPROVE`` token, checks the proposal is still
   authorizable and that trading is not halted, and mints a short-lived
   ``CONFIRM`` token whose parent is the approve action.  It authorizes nothing.
2. **Confirm** consumes the ``CONFIRM`` token, verifies it descends from an
   ``APPROVE`` action on the same proposal issued to the same user in the same
   chat, and only then calls
   :meth:`~stockbrain.proposals.service.ProposalService.authorize` with
   ``HUMAN_TELEGRAM`` and the numeric user id.

Nothing here re-implements a risk check, and nothing here can pass a quantity,
a price, a side or an instrument to the proposal service: :meth:`authorize`
takes a proposal id, a source and an actor, and re-reads everything else from
the row under lock.  The two-stage split exists so that a mis-tap opens a
dialog rather than authorizing a trade, and the second stage names the side and
the quantity so the operator confirms what they actually read.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.base import utcnow
from stockbrain.db.models.proposals import ApprovalAction
from stockbrain.db.session import Database
from stockbrain.enums import ApprovalStage, AuthorizationSource
from stockbrain.errors import (
    ApprovalActionForeign,
    ApprovalActionInvalid,
    AuthorizationNotPermitted,
    ProposalAlreadyConsumed,
    ProposalExpired,
    ProposalInvalidated,
    RiskBlocked,
)
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS
from stockbrain.proposals.service import ProposalService
from stockbrain.proposals.state_machine import AUTHORIZABLE_STATUSES
from stockbrain.telegram import messages
from stockbrain.telegram.formatting import bold, esc
from stockbrain.telegram.service import ProposalView, TelegramService
from stockbrain.telegram.tokens import IssuedToken, TokenService

__all__ = ["ApprovalCoordinator", "Button", "CallbackResult", "telegram_actor"]

log = get_logger(__name__)

#: Telegram truncates an ``answerCallbackQuery`` notification at 200 characters.
MAX_ALERT_CHARS = 200


def telegram_actor(user_id: int) -> str:
    """The actor string persisted for a Telegram action.

    The numeric id verbatim, with a channel prefix so an audit reader can tell
    a Telegram authorization from a web one without joining another column.
    A username never appears: it is not an identity, it is a display name its
    owner can give away.
    """
    return f"telegram:{user_id}"


@dataclass(frozen=True, slots=True)
class Button:
    label: str
    callback_data: str


@dataclass(frozen=True, slots=True)
class CallbackResult:
    """What the adapter should do with Telegram after a button was pressed."""

    alert: str
    """Short notification for ``answerCallbackQuery`` (Telegram caps it at 200)."""

    text: str | None = None
    """A new message to send, already rendered as escaped HTML."""

    keyboard: list[list[Button]] = field(default_factory=list)
    clear_original_keyboard: bool = False
    """Whether the message the button lived on should lose its keyboard.

    Cosmetic only.  The token is already consumed and the proposal's own state
    is authoritative, so a failed edit cannot re-enable anything.
    """

    original_suffix: str | None = None
    """Final status to append to the original message, when it can be edited."""

    proposal_id: uuid.UUID | None = None
    minted_action_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def short_alert(self) -> str:
        return self.alert[:MAX_ALERT_CHARS]


class ApprovalCoordinator:
    """Resolves callback tokens and drives the two-stage authorization."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        tokens: TokenService,
        proposals: ProposalService | None,
        service: TelegramService,
        control: ControlStateService,
    ) -> None:
        self._database = database
        self._settings = settings
        self._tokens = tokens
        self._proposals = proposals
        self._service = service
        self._control = control

    # ------------------------------------------------------------------
    # Keyboards
    # ------------------------------------------------------------------
    async def issue_proposal_keyboard(
        self,
        proposal: ProposalView,
        *,
        user_id: int,
        chat_id: int,
        message_id: int | None = None,
        now: dt.datetime | None = None,
    ) -> tuple[list[list[Button]], list[uuid.UUID]]:
        """Mint the buttons for one proposal, for one user, in one chat.

        An automatic proposal gets no Approve button.  Offering one would invite
        a human to "approve" something ``SYSTEM_AUTOMATIC`` already authorized,
        producing either a confusing refusal or, worse, the impression that the
        human decision mattered.
        """
        moment = now or utcnow()
        if not proposal.awaiting_authorization:
            return [], []
        expiry = min(
            moment + dt.timedelta(seconds=self._settings.telegram_action_ttl_seconds),
            proposal.expires_at,
        )
        if expiry <= moment:
            return [], []

        stages: list[tuple[ApprovalStage, str]] = []
        if proposal.is_manual:
            stages.append((ApprovalStage.APPROVE, "✅ Approve"))
            stages.append((ApprovalStage.REJECT, "✖ Reject"))
        stages.append((ApprovalStage.DETAILS, "📄 Details"))

        issued: list[IssuedToken] = []
        async with self._database.transaction() as session:
            for stage, _ in stages:
                issued.append(
                    await self._tokens.mint(
                        session,
                        proposal_id=proposal.id,
                        stage=stage,
                        user_id=user_id,
                        chat_id=chat_id,
                        expires_at=expiry,
                        message_id=message_id,
                    )
                )
        buttons = [
            Button(label=label, callback_data=token.callback_data)
            for (_, label), token in zip(stages, issued, strict=True)
        ]
        rows = [buttons[:2], buttons[2:]] if len(buttons) == 3 else [buttons]
        return [row for row in rows if row], [token.action_id for token in issued]

    async def open_message_targets(self, proposal_id: uuid.UUID) -> list[tuple[int, int]]:
        """``(chat_id, message_id)`` pairs whose keyboards should be blanked."""
        async with self._database.session() as session:
            return await self._tokens.open_messages(session, proposal_id)

    async def attach_message(
        self, action_ids: list[uuid.UUID], *, message_id: int, chat_id: int
    ) -> None:
        async with self._database.transaction() as session:
            await self._tokens.attach_message(
                session, action_ids, message_id=message_id, chat_id=chat_id
            )

    # ------------------------------------------------------------------
    # Callback dispatch
    # ------------------------------------------------------------------
    async def handle(
        self,
        raw_token: str,
        *,
        user_id: int,
        chat_id: int,
        message_id: int | None = None,
        now: dt.datetime | None = None,
    ) -> CallbackResult:
        """Redeem a token and perform the action *the database* says it permits.

        The stage is never read from the callback payload.  A token is a name;
        the row it names carries the permission.
        """
        moment = now or utcnow()
        try:
            async with self._database.transaction() as session:
                action = await self._tokens.resolve_and_consume(
                    session, raw_token, user_id=user_id, chat_id=chat_id, now=moment
                )
                parent_ok = await self._parent_is_valid(session, action.action_id, action.stage)
        except ApprovalActionForeign as exc:
            METRICS.inc("stockbrain_telegram_callbacks_total", labels={"result": "foreign"})
            log.warning(
                "telegram_callback_rejected",
                reason="token bound to a different user or chat",
                telegram_user_id=user_id,
                telegram_chat_id=chat_id,
                error=str(exc),
            )
            return CallbackResult(alert=exc.user_message)
        except ApprovalActionInvalid as exc:
            METRICS.inc(
                "stockbrain_telegram_callbacks_total",
                labels={"result": type(exc).__name__},
            )
            log.info(
                "telegram_callback_rejected",
                reason=type(exc).__name__,
                telegram_user_id=user_id,
                telegram_chat_id=chat_id,
            )
            return CallbackResult(alert=exc.user_message)

        if not parent_ok:
            log.warning(
                "telegram_confirmation_without_parent",
                telegram_user_id=user_id,
                proposal_id=str(action.proposal_id),
            )
            return CallbackResult(
                alert="This confirmation is no longer linked to an approval. Start again.",
                clear_original_keyboard=True,
            )

        if action.stage is ApprovalStage.DETAILS:
            return await self._details(action.proposal_id, user_id, chat_id, moment)
        if action.stage is ApprovalStage.APPROVE:
            return await self._begin_confirmation(
                action.proposal_id, action.action_id, user_id, chat_id, message_id, moment
            )
        if action.stage is ApprovalStage.REJECT:
            return await self._reject(action.proposal_id, user_id, moment)
        return await self._authorize(action.proposal_id, user_id, moment)

    # ------------------------------------------------------------------
    async def _parent_is_valid(
        self, session: AsyncSession, action_id: uuid.UUID, stage: ApprovalStage
    ) -> bool:
        """A CONFIRM must descend from an APPROVE on the same proposal and holder.

        Guards against a confirmation token that somehow existed without the
        first stage having happened -- which would collapse the two-stage
        confirmation into one tap.
        """
        if stage is not ApprovalStage.CONFIRM:
            return True
        child = await session.get(ApprovalAction, action_id)
        if child is None or child.parent_action_id is None:
            return False
        parent = await session.get(ApprovalAction, child.parent_action_id)
        return bool(
            parent is not None
            and parent.stage is ApprovalStage.APPROVE
            and parent.proposal_id == child.proposal_id
            and parent.user_identifier == child.user_identifier
            and parent.chat_identifier == child.chat_identifier
        )

    # ------------------------------------------------------------------
    async def _details(
        self, proposal_id: uuid.UUID, user_id: int, chat_id: int, now: dt.datetime
    ) -> CallbackResult:
        proposal = await self._service.proposal(proposal_id)
        if proposal is None:  # pragma: no cover - the row was just referenced
            return CallbackResult(alert="That proposal no longer exists.")
        # Backing out of a confirmation must actually retract it, rather than
        # leaving a live CONFIRM token for the remainder of its short TTL.
        async with self._database.transaction() as session:
            await self._retire_confirmations(session, proposal_id, user_id, now)
        keyboard, action_ids = await self.issue_proposal_keyboard(
            proposal, user_id=user_id, chat_id=chat_id, now=now
        )
        return CallbackResult(
            alert="Proposal details",
            text=messages.render_proposal_detail(proposal, now=now),
            keyboard=keyboard,
            proposal_id=proposal_id,
            minted_action_ids=action_ids,
        )

    async def _begin_confirmation(
        self,
        proposal_id: uuid.UUID,
        parent_action_id: uuid.UUID,
        user_id: int,
        chat_id: int,
        message_id: int | None,
        now: dt.datetime,
    ) -> CallbackResult:
        proposal = await self._service.proposal(proposal_id)
        if proposal is None:  # pragma: no cover - the row was just referenced
            return CallbackResult(alert="That proposal no longer exists.")

        refusal = await self._preflight(proposal, now)
        if refusal is not None:
            return CallbackResult(
                alert=refusal,
                text=f"{bold('Cannot approve')}\n{esc(refusal)}",
                clear_original_keyboard=True,
                proposal_id=proposal_id,
            )

        expiry = min(
            now + dt.timedelta(seconds=self._settings.telegram_confirm_ttl_seconds),
            proposal.expires_at,
        )
        async with self._database.transaction() as session:
            confirm = await self._tokens.mint(
                session,
                proposal_id=proposal_id,
                stage=ApprovalStage.CONFIRM,
                user_id=user_id,
                chat_id=chat_id,
                expires_at=expiry,
                parent_action_id=parent_action_id,
                message_id=message_id,
            )
            back = await self._tokens.mint(
                session,
                proposal_id=proposal_id,
                stage=ApprovalStage.DETAILS,
                user_id=user_id,
                chat_id=chat_id,
                expires_at=expiry,
                message_id=message_id,
            )
        label = f"CONFIRM {proposal.research_action or proposal.side}"
        return CallbackResult(
            alert="Confirm to authorize",
            text=messages.render_proposal_confirmation(proposal, now=now),
            keyboard=[
                [
                    Button(label=label, callback_data=confirm.callback_data),
                    Button(label="◀ Back", callback_data=back.callback_data),
                ]
            ],
            proposal_id=proposal_id,
            minted_action_ids=[confirm.action_id, back.action_id],
        )

    async def _authorize(
        self, proposal_id: uuid.UUID, user_id: int, now: dt.datetime
    ) -> CallbackResult:
        if self._proposals is None:
            return CallbackResult(alert="The proposal service is not configured.")
        actor = telegram_actor(user_id)
        try:
            await self._proposals.authorize(
                proposal_id,
                source=AuthorizationSource.HUMAN_TELEGRAM,
                actor=actor,
            )
        except ProposalExpired as exc:
            return await self._refusal(proposal_id, "Expired before confirmation.", exc, now)
        except ProposalInvalidated as exc:
            return await self._refusal(
                proposal_id, "The proposal was invalidated and needs new analysis.", exc, now
            )
        except ProposalAlreadyConsumed as exc:
            return await self._refusal(
                proposal_id, "Another action already decided this proposal.", exc, now
            )
        except AuthorizationNotPermitted as exc:
            return await self._refusal(proposal_id, str(exc), exc, now)
        except RiskBlocked as exc:
            return await self._refusal(proposal_id, f"Deterministic risk refused: {exc}", exc, now)

        async with self._database.transaction() as session:
            await self._tokens.retire_open_actions(
                session, proposal_id, reason="proposal authorized", now=now
            )
        METRICS.inc("stockbrain_telegram_callbacks_total", labels={"result": "authorized"})
        log.info(
            "telegram_proposal_authorized",
            proposal_id=str(proposal_id),
            telegram_user_id=user_id,
            authorization_source=AuthorizationSource.HUMAN_TELEGRAM.value,
            broker_order_transmitted=False,
        )
        proposal = await self._service.proposal(proposal_id)
        suffix = messages.render_terminal_status(proposal) if proposal else None
        return CallbackResult(
            # Authorizing is not transmitting. Whether an order follows depends
            # on the execution gates, which this handler cannot read and must
            # not guess at: the old wording ("no broker order sent") became a
            # lie the moment the transmission path existed.
            alert="Authorized. Transmission is separately gated.",
            text=(f"{bold('AUTHORIZED')}\n{esc(messages.AUTHORIZATION_NOTICE)}"),
            clear_original_keyboard=True,
            original_suffix=suffix,
            proposal_id=proposal_id,
        )

    async def _reject(
        self, proposal_id: uuid.UUID, user_id: int, now: dt.datetime
    ) -> CallbackResult:
        if self._proposals is None:
            return CallbackResult(alert="The proposal service is not configured.")
        try:
            await self._proposals.reject(
                proposal_id,
                actor=telegram_actor(user_id),
                reason="rejected from Telegram",
            )
        except ProposalAlreadyConsumed as exc:
            # Racing a web approval, an automatic authorization or a second tap.
            # The other actor won; the refusal says so rather than pretending.
            return await self._refusal(
                proposal_id, "Another action already decided this proposal.", exc, now
            )
        async with self._database.transaction() as session:
            await self._tokens.retire_open_actions(
                session, proposal_id, reason="proposal rejected", now=now
            )
        METRICS.inc("stockbrain_telegram_callbacks_total", labels={"result": "rejected"})
        log.info(
            "telegram_proposal_rejected",
            proposal_id=str(proposal_id),
            telegram_user_id=user_id,
        )
        proposal = await self._service.proposal(proposal_id)
        return CallbackResult(
            alert="Rejected.",
            text=bold("REJECTED"),
            clear_original_keyboard=True,
            original_suffix=messages.render_terminal_status(proposal) if proposal else None,
            proposal_id=proposal_id,
        )

    # ------------------------------------------------------------------
    async def _preflight(self, proposal: ProposalView, now: dt.datetime) -> str | None:
        """Cheap reasons not to even open a confirmation.

        Advisory, never authoritative: the same conditions are re-checked inside
        ``ProposalService.authorize`` under a row lock, which is what actually
        decides.  This exists so a halted system says so immediately instead of
        after a second tap.
        """
        control = await self._control.snapshot()
        if control.trading_halted:
            return "Authorization is halted: " + "; ".join(control.blockers)
        if not proposal.is_manual:
            return (
                "This proposal was generated under the AUTOMATIC execution policy and is "
                "authorized by the system, not by a person."
            )
        if proposal.status not in AUTHORIZABLE_STATUSES:
            return f"The proposal is {proposal.status.value} and can no longer be authorized."
        if proposal.expires_at <= now:
            return "The proposal expired."
        return None

    async def _refusal(
        self,
        proposal_id: uuid.UUID,
        headline: str,
        exc: Exception,
        now: dt.datetime,
    ) -> CallbackResult:
        """Report an authoritative refusal, and retire every button for it.

        The proposal's own state is already whatever the service decided --
        including ``INVALIDATED`` when a fresh risk check refused -- so the
        message reports that state rather than a client-side guess.
        """
        async with self._database.transaction() as session:
            await self._tokens.retire_open_actions(
                session, proposal_id, reason=f"refused: {type(exc).__name__}", now=now
            )
        METRICS.inc("stockbrain_telegram_callbacks_total", labels={"result": "refused"})
        log.info(
            "telegram_action_refused",
            proposal_id=str(proposal_id),
            error_type=type(exc).__name__,
            reason=str(exc)[:300],
        )
        proposal = await self._service.proposal(proposal_id)
        body = f"{bold('Refused')}\n{esc(headline)}"
        if proposal is not None:
            body += f"\nProposal is now {esc(proposal.status.value)}."
        return CallbackResult(
            alert=headline,
            text=body,
            clear_original_keyboard=True,
            original_suffix=messages.render_terminal_status(proposal) if proposal else None,
            proposal_id=proposal_id,
        )

    async def _retire_confirmations(
        self,
        session: AsyncSession,
        proposal_id: uuid.UUID,
        user_id: int,
        now: dt.datetime,
    ) -> None:
        await session.execute(
            sa.update(ApprovalAction)
            .where(
                ApprovalAction.proposal_id == proposal_id,
                ApprovalAction.user_identifier == str(user_id),
                ApprovalAction.stage == ApprovalStage.CONFIRM,
                ApprovalAction.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )
