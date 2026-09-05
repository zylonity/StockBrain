"""The commands, driven through the real handlers.

Handlers are adapters, so what is asserted here is adapter behaviour: an
unauthorised caller learns nothing, an authorised one gets the same facts the
API would give, and the privileged commands write the same durable rows the web
routes write.  The domain answers themselves are tested where they are computed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

import pytest
import sqlalchemy as sa
from telegram.ext import ContextTypes

from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.models.system import AuditLog
from stockbrain.db.session import Database
from stockbrain.enums import ApprovalStage, ControlFlag, ProposalStatus
from stockbrain.observability.health import ProviderHealthRegistry
from stockbrain.telegram.approvals import ApprovalCoordinator
from stockbrain.telegram.auth import TelegramAuthorizer
from stockbrain.telegram.handlers import TelegramHandlers
from stockbrain.telegram.service import TelegramService
from stockbrain.telegram.tokens import TokenService, parse_callback_data
from telegram import Update
from tests import proposal_helpers as helpers
from tests.telegram_helpers import FakeUpdate, make_update

pytestmark = pytest.mark.integration

OWNER = 4242
STRANGER = 9999


@dataclass
class FakeContext:
    args: list[str] | None = None


def _context(*args: str) -> ContextTypes.DEFAULT_TYPE:
    return cast(ContextTypes.DEFAULT_TYPE, FakeContext(args=list(args)))


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "telegram_enabled": True,
        "telegram_bot_token": "123:test-token",
        "telegram_allowed_user_ids": str(OWNER),
        # A zero interval keeps the rate-limit out of the way of the tests that
        # are not about the rate limit.
        "telegram_research_interval_seconds": 0,
    }
    base.update(overrides)
    return helpers.settings(**base)


def _handlers(database: Database, settings: Settings) -> TelegramHandlers:
    control = ControlStateService(database)
    service = TelegramService(database, settings, health=ProviderHealthRegistry(), control=control)
    proposals = helpers.service_with(database, settings, control=control)
    coordinator = ApprovalCoordinator(
        database,
        settings,
        tokens=TokenService(),
        proposals=proposals,
        service=service,
        control=control,
    )
    return TelegramHandlers(
        settings,
        authorizer=TelegramAuthorizer(
            allowed_user_ids=settings.telegram_allowed_user_ids,
            allowed_chat_ids=settings.telegram_allowed_chat_ids,
            allow_group_chats=settings.telegram_allow_group_chats,
        ),
        service=service,
        coordinator=coordinator,
        control=control,
    )


def _update(**kwargs: Any) -> tuple[FakeUpdate, Update]:
    """An update from the owner's private chat unless a test says otherwise."""
    kwargs.setdefault("user_id", OWNER)
    kwargs.setdefault("chat_id", kwargs["user_id"])
    fake = make_update(**kwargs)
    return fake, cast(Update, fake)


def _replies(fake: FakeUpdate) -> list[str]:
    assert fake.effective_message is not None
    return [text for text, _ in fake.effective_message.replies]


def _markups(fake: FakeUpdate) -> list[Any]:
    assert fake.effective_message is not None
    return [markup for _, markup in fake.effective_message.replies if markup is not None]


async def _seeded_proposal(database: Database, settings: Settings) -> uuid.UUID:
    await helpers.seed(database)
    await helpers.fund(database, positions={"MSFT_US_EQ": (Decimal("5"), Decimal("2"))})
    proposals = helpers.service_with(
        database,
        settings,
        market_data=helpers.StubMarketData(),
        control=ControlStateService(database),
    )
    result = await proposals.generate(helpers.THESIS_ID)
    assert result.proposal_id is not None, result.reason
    return result.proposal_id


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
async def test_an_unauthorised_user_learns_nothing_from_start(
    clean_tables: Database,
) -> None:
    handlers = _handlers(clean_tables, _settings())
    fake, update = _update(user_id=STRANGER, chat_id=STRANGER)
    await handlers.start(update, _context())
    assert _replies(fake) == ["Not authorised."]


async def test_every_privileged_command_refuses_an_unauthorised_user(
    clean_tables: Database,
) -> None:
    handlers = _handlers(clean_tables, _settings())
    commands = (
        handlers.status,
        handlers.portfolio,
        handlers.positions,
        handlers.proposals,
        handlers.events,
        handlers.help,
        handlers.pause,
        handlers.resume,
        handlers.kill,
    )
    for command in commands:
        fake, update = _update(user_id=STRANGER, chat_id=STRANGER)
        await command(update, _context())
        assert _replies(fake) == ["Not authorised."]

    # ...and nothing was written by any of them.
    snapshot = await ControlStateService(clean_tables).snapshot()
    assert not snapshot.trading_halted


async def test_a_group_chat_is_refused_even_for_the_owner(clean_tables: Database) -> None:
    handlers = _handlers(clean_tables, _settings())
    fake, update = _update(user_id=OWNER, chat_id=-100999, chat_type="supergroup")
    await handlers.status(update, _context())
    assert _replies(fake) == ["Not authorised."]


async def test_a_username_change_is_irrelevant(clean_tables: Database) -> None:
    """The double carries a username precisely so this can be asserted."""
    handlers = _handlers(clean_tables, _settings())
    fake, update = _update(user_id=OWNER, chat_id=OWNER)
    assert fake.effective_user is not None
    fake.effective_user.username = "someone_else_entirely"
    await handlers.help(update, _context())
    assert "StockBrain commands" in _replies(fake)[0]


# ---------------------------------------------------------------------------
# Read-only commands
# ---------------------------------------------------------------------------
async def test_start_and_help_answer_an_authorised_user(clean_tables: Database) -> None:
    handlers = _handlers(clean_tables, _settings())
    fake, update = _update()
    await handlers.start(update, _context())
    await handlers.help(update, _context())
    joined = "\n".join(_replies(fake))
    assert "Connected" in joined
    assert "/proposals" in joined
    # Phase 8 replaced "no order is ever sent" with the separation that is
    # actually true: authorizing and transmitting are different permissions.
    assert "separately gated" in joined


async def test_status_reports_policy_control_and_proposal_counts(
    clean_tables: Database,
) -> None:
    settings = _settings()
    await _seeded_proposal(clean_tables, settings)
    handlers = _handlers(clean_tables, settings)
    fake, update = _update()
    await handlers.status(update, _context())
    rendered = _replies(fake)[0]
    assert "MANUAL" in rendered
    assert "Trading control: <b>running</b>" in rendered
    assert "Proposals awaiting authorization: 1" in rendered


async def test_portfolio_and_positions_read_the_stored_snapshot(
    clean_tables: Database,
) -> None:
    settings = _settings()
    await _seeded_proposal(clean_tables, settings)
    handlers = _handlers(clean_tables, settings)

    fake, update = _update()
    await handlers.portfolio(update, _context())
    assert "Total value" in _replies(fake)[0]

    fake, update = _update()
    await handlers.positions(update, _context())
    rendered = _replies(fake)[0]
    assert "MSFT_US_EQ" in rendered
    assert "qty 5 (tradable 2)" in rendered


async def test_portfolio_is_honest_when_no_snapshot_exists(clean_tables: Database) -> None:
    handlers = _handlers(clean_tables, _settings())
    fake, update = _update()
    await handlers.portfolio(update, _context())
    assert "Unavailable" in _replies(fake)[0]


async def test_events_lists_classified_work_only(clean_tables: Database) -> None:
    settings = _settings()
    await _seeded_proposal(clean_tables, settings)
    handlers = _handlers(clean_tables, settings)
    fake, update = _update()
    await handlers.events(update, _context())
    rendered = _replies(fake)[0]
    assert "Apple announces something material" in rendered
    assert "CANDIDATE" in rendered


async def test_proposals_lists_and_offers_controls_for_the_actionable_one(
    clean_tables: Database,
) -> None:
    settings = _settings()
    proposal_id = await _seeded_proposal(clean_tables, settings)
    handlers = _handlers(clean_tables, settings)
    fake, update = _update()
    await handlers.proposals(update, _context())

    rendered = "\n".join(_replies(fake))
    assert "AAPL" in rendered
    assert "broker order sent: no" in rendered
    assert _markups(fake), "an actionable proposal should carry controls"

    from stockbrain.db.models.proposals import ApprovalAction

    async with clean_tables.session() as session:
        stages = sorted(
            action.stage.value
            for action in (
                await session.execute(
                    sa.select(ApprovalAction).where(ApprovalAction.proposal_id == proposal_id)
                )
            ).scalars()
        )
    assert stages == ["APPROVE", "DETAILS", "REJECT"]


async def test_research_requires_an_argument_and_is_rate_limited(
    clean_tables: Database,
) -> None:
    settings = _settings(telegram_research_interval_seconds=60)
    await _seeded_proposal(clean_tables, settings)
    handlers = _handlers(clean_tables, settings)

    fake, update = _update()
    await handlers.research(update, _context())
    assert "Usage:" in _replies(fake)[0]

    fake, update = _update()
    await handlers.research(update, _context("AAPL_US_EQ"))
    assert "Research thesis" in _replies(fake)[0]

    fake, update = _update()
    await handlers.research(update, _context("AAPL_US_EQ"))
    assert "Rate limited" in _replies(fake)[0]


async def test_research_reports_a_miss_rather_than_guessing(clean_tables: Database) -> None:
    handlers = _handlers(clean_tables, _settings())
    fake, update = _update()
    await handlers.research(update, _context("NOSUCHTICKER"))
    assert "No thesis found" in _replies(fake)[0]


# ---------------------------------------------------------------------------
# Control commands
# ---------------------------------------------------------------------------
async def test_pause_and_resume_write_the_durable_flag(clean_tables: Database) -> None:
    handlers = _handlers(clean_tables, _settings())

    fake, update = _update()
    await handlers.pause(update, _context())
    assert "Trading paused" in _replies(fake)[0]
    snapshot = await ControlStateService(clean_tables).snapshot()
    assert snapshot.paused.active
    assert snapshot.paused.actor == f"telegram:{OWNER}"
    assert snapshot.paused.source == "HUMAN_TELEGRAM"

    fake, update = _update()
    await handlers.resume(update, _context())
    assert "Trading resumed" in _replies(fake)[0]
    assert not (await ControlStateService(clean_tables).snapshot()).trading_halted


async def test_kill_engages_the_switch_and_says_nothing_was_liquidated(
    clean_tables: Database,
) -> None:
    handlers = _handlers(clean_tables, _settings())
    fake, update = _update()
    await handlers.kill(update, _context())
    rendered = _replies(fake)[0]
    assert "KILL SWITCH ENGAGED" in rendered
    assert "No position was closed" in rendered

    snapshot = await ControlStateService(clean_tables).snapshot()
    assert snapshot.kill_switch.active
    assert snapshot.kill_switch.flag is ControlFlag.KILL_SWITCH

    async with clean_tables.session() as session:
        actions = list(
            (
                await session.execute(
                    sa.select(AuditLog.action).where(AuditLog.actor_id.isnot(None))
                )
            ).scalars()
        )
    assert "control.kill_switch.engaged" in actions


async def test_resume_refuses_to_lift_a_kill_switch_and_says_so(
    clean_tables: Database,
) -> None:
    handlers = _handlers(clean_tables, _settings())
    fake, update = _update()
    await handlers.kill(update, _context())

    fake, update = _update()
    await handlers.pause(update, _context())
    fake, update = _update()
    await handlers.resume(update, _context())
    rendered = _replies(fake)[0]
    assert "kill switch still engaged" in rendered
    assert "deliberate second act" in rendered
    assert (await ControlStateService(clean_tables).snapshot()).kill_switch.active


# ---------------------------------------------------------------------------
# Callbacks through the handler
# ---------------------------------------------------------------------------
async def test_a_callback_is_answered_and_the_keyboard_blanked_on_a_terminal_action(
    clean_tables: Database,
) -> None:
    settings = _settings()
    proposal_id = await _seeded_proposal(clean_tables, settings)
    handlers = _handlers(clean_tables, settings)

    fake, update = _update()
    await handlers.proposals(update, _context())
    markup = _markups(fake)[0]
    reject = next(
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if "Reject" in button.text
    )

    fake, update = _update(callback_data=reject)
    await handlers.callback(update, _context())
    assert fake.callback_query is not None
    assert fake.callback_query.answers[0][0] == "Rejected."
    assert fake.callback_query.markup_edits == [None]

    from stockbrain.db.models.proposals import TradeProposal

    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None
    assert proposal.status is ProposalStatus.REJECTED


async def test_a_failed_keyboard_edit_does_not_break_the_flow(
    clean_tables: Database,
) -> None:
    """The message may be too old to edit; the decision still stands."""
    settings = _settings()
    proposal_id = await _seeded_proposal(clean_tables, settings)
    handlers = _handlers(clean_tables, settings)

    tokens = TokenService()
    from stockbrain.db.base import utcnow

    async with clean_tables.transaction() as session:
        issued = await tokens.mint(
            session,
            proposal_id=proposal_id,
            stage=ApprovalStage.REJECT,
            user_id=OWNER,
            chat_id=OWNER,
            expires_at=utcnow().replace(year=utcnow().year + 1),
        )
    fake, update = _update(callback_data=f"sb:{issued.raw}", edit_fails=True)
    await handlers.callback(update, _context())

    assert fake.callback_query is not None
    assert fake.callback_query.answers[0][0] == "Rejected."

    from stockbrain.db.models.proposals import TradeProposal

    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None
    assert proposal.status is ProposalStatus.REJECTED


async def test_an_unauthorised_callback_is_answered_and_does_nothing(
    clean_tables: Database,
) -> None:
    settings = _settings()
    proposal_id = await _seeded_proposal(clean_tables, settings)
    handlers = _handlers(clean_tables, settings)

    tokens = TokenService()
    from stockbrain.db.base import utcnow

    async with clean_tables.transaction() as session:
        issued = await tokens.mint(
            session,
            proposal_id=proposal_id,
            stage=ApprovalStage.REJECT,
            user_id=OWNER,
            chat_id=OWNER,
            expires_at=utcnow().replace(year=utcnow().year + 1),
        )

    fake, update = _update(user_id=STRANGER, chat_id=STRANGER, callback_data=f"sb:{issued.raw}")
    await handlers.callback(update, _context())
    assert fake.callback_query is not None
    assert fake.callback_query.answers == [("Not authorised.", True)]

    from stockbrain.db.models.proposals import TradeProposal

    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None
    assert proposal.status is not ProposalStatus.REJECTED
    assert parse_callback_data(f"sb:{issued.raw}") == issued.raw
