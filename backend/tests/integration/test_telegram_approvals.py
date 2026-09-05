"""The two-stage confirmation, and every way it can lose a race.

Two properties are being defended here.

**Exactly one transition wins.**  A web approval, a Telegram confirmation, an
automatic authorization and a rejection are four clients of one function, and
the PostgreSQL row lock plus the optimistic ``version`` column decide between
them.  Nothing in the Telegram path re-implements a check, so there is nothing
for it to get wrong differently.

**A button is a name, not a permission.**  Every refusal below happens because
the *server* re-read the proposal, not because the callback said something
different -- which is why an expired proposal, a consumed token and a stranger's
tap all fail even though the bytes were perfectly valid Telegram data.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.control.state import ControlStateService
from stockbrain.db.base import utcnow
from stockbrain.db.models.proposals import ApprovalAction, TradeProposal
from stockbrain.db.session import Database
from stockbrain.enums import (
    ApprovalChannel,
    ApprovalStage,
    AuthorizationSource,
    ExecutionPolicy,
    ProposalStatus,
)
from stockbrain.observability.health import ProviderHealthRegistry
from stockbrain.proposals.service import ProposalService
from stockbrain.telegram.approvals import ApprovalCoordinator, telegram_actor
from stockbrain.telegram.service import TelegramService
from stockbrain.telegram.tokens import TokenService, parse_callback_data
from tests import proposal_helpers as helpers
from tests.telegram_helpers import callback_datas

pytestmark = pytest.mark.integration

OWNER = 4242
STRANGER = 9999
OTHER_CHAT = -100777


def _telegram_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "telegram_enabled": True,
        "telegram_bot_token": "123:test-token",
        "telegram_allowed_user_ids": str(OWNER),
    }
    base.update(overrides)
    return helpers.settings(**base)


def _read_model(database: Database, settings: Settings) -> TelegramService:
    return TelegramService(
        database,
        settings,
        health=ProviderHealthRegistry(),
        control=ControlStateService(database),
    )


def _coordinator(
    database: Database, proposals: ProposalService, settings: Settings
) -> ApprovalCoordinator:
    control = ControlStateService(database)
    service = _read_model(database, settings)
    return ApprovalCoordinator(
        database,
        settings,
        tokens=TokenService(),
        proposals=proposals,
        service=service,
        control=control,
    )


async def _world(
    database: Database, **overrides: object
) -> tuple[ProposalService, ApprovalCoordinator, uuid.UUID]:
    settings = _telegram_settings(**overrides)
    proposals = helpers.service_with(
        database,
        settings,
        market_data=helpers.StubMarketData(),
        control=ControlStateService(database),
    )
    await helpers.seed(database)
    await helpers.fund(database)
    result = await proposals.generate(helpers.THESIS_ID)
    assert result.proposal_id is not None, result.reason
    return proposals, _coordinator(database, proposals, settings), result.proposal_id


async def _keyboard_tokens(
    database: Database,
    coordinator: ApprovalCoordinator,
    proposal_id: uuid.UUID,
    *,
    user_id: int = OWNER,
) -> dict[ApprovalStage, str]:
    """Mint the real keyboard and return the raw token behind each button."""
    view = await _read_model(database, _telegram_settings()).proposal(proposal_id)
    assert view is not None
    keyboard, action_ids = await coordinator.issue_proposal_keyboard(
        view, user_id=user_id, chat_id=user_id
    )
    raws = [parse_callback_data(data) for data in callback_datas(keyboard)]
    assert all(raw is not None for raw in raws)
    stages = [ApprovalStage.APPROVE, ApprovalStage.REJECT, ApprovalStage.DETAILS]
    assert len(action_ids) == len(stages)
    return {stage: str(raw) for stage, raw in zip(stages, raws, strict=True)}


async def _status(database: Database, proposal_id: uuid.UUID) -> ProposalStatus:
    async with database.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        return proposal.status


# ---------------------------------------------------------------------------
# The happy path, in two stages
# ---------------------------------------------------------------------------
async def test_stage_one_opens_a_confirmation_and_authorizes_nothing(
    clean_tables: Database,
) -> None:
    """A mis-tap must open a dialog, never a trade."""
    _, coordinator, proposal_id = await _world(clean_tables)
    tokens = await _keyboard_tokens(clean_tables, coordinator, proposal_id)

    result = await coordinator.handle(tokens[ApprovalStage.APPROVE], user_id=OWNER, chat_id=OWNER)
    assert result.text is not None
    assert "Confirm BUY" in result.text
    assert result.keyboard and result.keyboard[0][0].label.startswith("CONFIRM BUY")
    assert await _status(clean_tables, proposal_id) is not ProposalStatus.APPROVED


async def test_stage_two_authorizes_through_the_shared_service(
    clean_tables: Database,
) -> None:
    """The provenance is what proves the shared path was used.

    ``HUMAN_TELEGRAM`` with a numeric actor, ``TELEGRAM`` on the legacy channel
    column, a fresh authorization-stage risk evaluation, and no broker order.
    """
    _, coordinator, proposal_id = await _world(clean_tables)
    tokens = await _keyboard_tokens(clean_tables, coordinator, proposal_id)
    stage_one = await coordinator.handle(
        tokens[ApprovalStage.APPROVE], user_id=OWNER, chat_id=OWNER
    )
    confirm = parse_callback_data(stage_one.keyboard[0][0].callback_data)
    assert confirm is not None

    result = await coordinator.handle(confirm, user_id=OWNER, chat_id=OWNER)
    assert "Authorized" in result.alert
    assert "Phase 8" in result.alert or "Phase 8" in (result.text or "")

    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        assert proposal.status is ProposalStatus.APPROVED
        assert proposal.authorization_source is AuthorizationSource.HUMAN_TELEGRAM
        assert proposal.approved_channel is ApprovalChannel.TELEGRAM
        assert proposal.approved_by == telegram_actor(OWNER)
        assert str(OWNER) in proposal.approved_by
        assert proposal.authorization_policy_snapshot["broker_order_transmitted"] is False


async def test_the_actor_is_the_numeric_id_and_never_a_username(
    clean_tables: Database,
) -> None:
    assert telegram_actor(OWNER) == f"telegram:{OWNER}"
    _, coordinator, proposal_id = await _world(clean_tables)
    tokens = await _keyboard_tokens(clean_tables, coordinator, proposal_id)
    stage_one = await coordinator.handle(
        tokens[ApprovalStage.APPROVE], user_id=OWNER, chat_id=OWNER
    )
    confirm = parse_callback_data(stage_one.keyboard[0][0].callback_data)
    await coordinator.handle(str(confirm), user_id=OWNER, chat_id=OWNER)
    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None
    assert "not-an-identity" not in str(proposal.approved_by)


async def test_the_confirmation_action_descends_from_the_approval(
    clean_tables: Database,
) -> None:
    """Two stages, linked in the database.

    Without the parent link a CONFIRM token would be a single-tap authorization
    wearing a two-stage name.
    """
    _, coordinator, proposal_id = await _world(clean_tables)
    tokens = await _keyboard_tokens(clean_tables, coordinator, proposal_id)
    await coordinator.handle(tokens[ApprovalStage.APPROVE], user_id=OWNER, chat_id=OWNER)

    async with clean_tables.session() as session:
        confirm = (
            await session.execute(
                sa.select(ApprovalAction).where(ApprovalAction.stage == ApprovalStage.CONFIRM)
            )
        ).scalar_one()
        assert confirm.parent_action_id is not None
        parent = await session.get(ApprovalAction, confirm.parent_action_id)
    assert parent is not None
    assert parent.stage is ApprovalStage.APPROVE
    assert parent.proposal_id == proposal_id
    assert parent.user_identifier == str(OWNER)


async def test_an_orphan_confirmation_is_refused(clean_tables: Database) -> None:
    """A CONFIRM with no APPROVE behind it collapses the two stages into one."""
    _, coordinator, proposal_id = await _world(clean_tables)
    tokens = TokenService()
    async with clean_tables.transaction() as session:
        issued = await tokens.mint(
            session,
            proposal_id=proposal_id,
            stage=ApprovalStage.CONFIRM,
            user_id=OWNER,
            chat_id=OWNER,
            expires_at=utcnow() + dt.timedelta(minutes=2),
        )
    result = await coordinator.handle(issued.raw, user_id=OWNER, chat_id=OWNER)
    assert "no longer linked to an approval" in result.alert
    assert await _status(clean_tables, proposal_id) is not ProposalStatus.APPROVED


# ---------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------
async def test_reject_is_durable_and_retires_the_other_buttons(
    clean_tables: Database,
) -> None:
    _, coordinator, proposal_id = await _world(clean_tables)
    tokens = await _keyboard_tokens(clean_tables, coordinator, proposal_id)

    result = await coordinator.handle(tokens[ApprovalStage.REJECT], user_id=OWNER, chat_id=OWNER)
    assert result.alert == "Rejected."
    assert result.clear_original_keyboard

    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        assert proposal.status is ProposalStatus.REJECTED
        assert proposal.rejected_by == telegram_actor(OWNER)
        open_actions = int(
            (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(ApprovalAction)
                    .where(ApprovalAction.consumed_at.is_(None))
                )
            ).scalar_one()
        )
    assert open_actions == 0

    # The approve button from the same keyboard is now inert.
    stale = await coordinator.handle(tokens[ApprovalStage.APPROVE], user_id=OWNER, chat_id=OWNER)
    assert "already been used" in stale.alert


async def test_rejecting_twice_is_refused_rather_than_recorded_twice(
    clean_tables: Database,
) -> None:
    _, coordinator, proposal_id = await _world(clean_tables)
    first = await _keyboard_tokens(clean_tables, coordinator, proposal_id)
    await coordinator.handle(first[ApprovalStage.REJECT], user_id=OWNER, chat_id=OWNER)

    # A second, independently minted reject token: the proposal itself refuses.
    tokens = TokenService()
    async with clean_tables.transaction() as session:
        issued = await tokens.mint(
            session,
            proposal_id=proposal_id,
            stage=ApprovalStage.REJECT,
            user_id=OWNER,
            chat_id=OWNER,
            expires_at=utcnow() + dt.timedelta(minutes=5),
        )
    result = await coordinator.handle(issued.raw, user_id=OWNER, chat_id=OWNER)
    assert "already decided" in result.alert
    assert await _status(clean_tables, proposal_id) is ProposalStatus.REJECTED


# ---------------------------------------------------------------------------
# Bad tokens
# ---------------------------------------------------------------------------
async def test_a_double_tap_authorizes_at_most_once(clean_tables: Database) -> None:
    _, coordinator, proposal_id = await _world(clean_tables)
    tokens = await _keyboard_tokens(clean_tables, coordinator, proposal_id)
    stage_one = await coordinator.handle(
        tokens[ApprovalStage.APPROVE], user_id=OWNER, chat_id=OWNER
    )
    confirm = str(parse_callback_data(stage_one.keyboard[0][0].callback_data))

    first, second = await asyncio.gather(
        coordinator.handle(confirm, user_id=OWNER, chat_id=OWNER),
        coordinator.handle(confirm, user_id=OWNER, chat_id=OWNER),
        return_exceptions=False,
    )
    alerts = sorted([first.alert, second.alert])
    assert any("Authorized" in alert for alert in alerts)
    assert any("already been used" in alert or "already decided" in alert for alert in alerts)

    async with clean_tables.session() as session:
        approved = int(
            (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(TradeProposal)
                    .where(TradeProposal.status == ProposalStatus.APPROVED)
                )
            ).scalar_one()
        )
    assert approved == 1


async def test_a_consumed_token_and_an_expired_token_report_different_reasons(
    clean_tables: Database,
) -> None:
    _, coordinator, proposal_id = await _world(clean_tables)
    tokens = await _keyboard_tokens(clean_tables, coordinator, proposal_id)

    await coordinator.handle(tokens[ApprovalStage.DETAILS], user_id=OWNER, chat_id=OWNER)
    consumed = await coordinator.handle(tokens[ApprovalStage.DETAILS], user_id=OWNER, chat_id=OWNER)
    assert "already been used" in consumed.alert

    expired = await coordinator.handle(
        tokens[ApprovalStage.APPROVE],
        user_id=OWNER,
        chat_id=OWNER,
        now=utcnow() + dt.timedelta(days=2),
    )
    assert "expired" in expired.alert


async def test_a_stranger_and_a_foreign_chat_are_refused_without_burning_the_token(
    clean_tables: Database,
) -> None:
    """The forwarded-message case, end to end."""
    _, coordinator, proposal_id = await _world(clean_tables)
    tokens = await _keyboard_tokens(clean_tables, coordinator, proposal_id)

    for user_id, chat_id in ((STRANGER, STRANGER), (OWNER, OTHER_CHAT)):
        result = await coordinator.handle(
            tokens[ApprovalStage.APPROVE], user_id=user_id, chat_id=chat_id
        )
        assert result.alert == "This button was not issued to you."
        assert result.text is None

    owner = await coordinator.handle(tokens[ApprovalStage.APPROVE], user_id=OWNER, chat_id=OWNER)
    assert owner.text is not None and "Confirm BUY" in owner.text


async def test_an_unknown_token_is_refused(clean_tables: Database) -> None:
    _, coordinator, _ = await _world(clean_tables)
    result = await coordinator.handle("z" * 43, user_id=OWNER, chat_id=OWNER)
    assert "no longer valid" in result.alert


# ---------------------------------------------------------------------------
# Races
# ---------------------------------------------------------------------------
async def test_a_web_approval_beats_a_later_telegram_confirmation(
    clean_tables: Database,
) -> None:
    """The web authorization also retires the outstanding Telegram token.

    So the later tap is refused *before* it reaches the proposal at all --
    which is the button-lifecycle guarantee working, not a weaker version of
    the race check. The race itself, where both arrive together, is the next
    test.
    """
    proposals, coordinator, proposal_id = await _world(clean_tables)
    tokens = await _keyboard_tokens(clean_tables, coordinator, proposal_id)
    stage_one = await coordinator.handle(
        tokens[ApprovalStage.APPROVE], user_id=OWNER, chat_id=OWNER
    )
    confirm = str(parse_callback_data(stage_one.keyboard[0][0].callback_data))

    await proposals.authorize(
        proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:local-operator"
    )
    result = await coordinator.handle(confirm, user_id=OWNER, chat_id=OWNER)
    assert result.alert in {
        "This button has already been used.",
        "Another action already decided this proposal.",
    }

    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
    assert proposal is not None
    assert proposal.authorization_source is AuthorizationSource.HUMAN_WEB


async def test_a_simultaneous_web_and_telegram_authorization_produces_one_winner(
    clean_tables: Database,
) -> None:
    """The specification's section 33 race, driven through both clients at once."""
    proposals, coordinator, proposal_id = await _world(clean_tables)
    tokens = await _keyboard_tokens(clean_tables, coordinator, proposal_id)
    stage_one = await coordinator.handle(
        tokens[ApprovalStage.APPROVE], user_id=OWNER, chat_id=OWNER
    )
    confirm = str(parse_callback_data(stage_one.keyboard[0][0].callback_data))

    async def web() -> str:
        try:
            await proposals.authorize(
                proposal_id, source=AuthorizationSource.HUMAN_WEB, actor="web:local-operator"
            )
        except Exception as exc:
            return type(exc).__name__
        return "authorized"

    async def telegram() -> str:
        result = await coordinator.handle(confirm, user_id=OWNER, chat_id=OWNER)
        return "authorized" if "Authorized" in result.alert else result.alert

    outcomes = await asyncio.gather(web(), telegram())
    assert outcomes.count("authorized") == 1

    async with clean_tables.session() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        approvals = int(
            (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(TradeProposal)
                    .where(TradeProposal.status == ProposalStatus.APPROVED)
                )
            ).scalar_one()
        )
    assert proposal is not None and proposal.status is ProposalStatus.APPROVED
    assert approvals == 1
    assert proposal.version >= 2


async def test_approve_racing_reject_produces_exactly_one_terminal_state(
    clean_tables: Database,
) -> None:
    _, coordinator, proposal_id = await _world(clean_tables)
    tokens = await _keyboard_tokens(clean_tables, coordinator, proposal_id)
    stage_one = await coordinator.handle(
        tokens[ApprovalStage.APPROVE], user_id=OWNER, chat_id=OWNER
    )
    confirm = str(parse_callback_data(stage_one.keyboard[0][0].callback_data))

    confirm_result, reject_result = await asyncio.gather(
        coordinator.handle(confirm, user_id=OWNER, chat_id=OWNER),
        coordinator.handle(tokens[ApprovalStage.REJECT], user_id=OWNER, chat_id=OWNER),
    )
    alerts = [confirm_result.alert, reject_result.alert]
    assert (
        sum("Authorized" in alert for alert in alerts)
        + sum(alert == "Rejected." for alert in alerts)
        >= 1
    )
    status = await _status(clean_tables, proposal_id)
    assert status in {ProposalStatus.APPROVED, ProposalStatus.REJECTED}


async def test_an_automatically_authorized_proposal_offers_no_approve_button(
    clean_tables: Database,
) -> None:
    """Telegram cannot turn a system authorization into a human one.

    The keyboard for an AUTOMATIC proposal has no approve or reject button at
    all, so there is nothing to press that could rewrite the provenance.
    """
    settings = _telegram_settings(execution_policy=ExecutionPolicy.AUTOMATIC)
    assert settings.automatic_authorization_permitted, settings.automation_blockers
    proposals = helpers.service_with(
        clean_tables,
        settings,
        market_data=helpers.StubMarketData(),
        control=ControlStateService(clean_tables),
    )
    await helpers.seed(clean_tables)
    await helpers.fund(clean_tables)
    result = await proposals.generate(helpers.THESIS_ID)
    assert result.authorized
    assert result.proposal_id is not None

    coordinator = _coordinator(clean_tables, proposals, settings)
    view = await _read_model(clean_tables, settings).proposal(result.proposal_id)
    assert view is not None
    assert view.authorization_source == AuthorizationSource.SYSTEM_AUTOMATIC.value
    keyboard, action_ids = await coordinator.issue_proposal_keyboard(
        view, user_id=OWNER, chat_id=OWNER
    )
    assert keyboard == []
    assert action_ids == []


async def test_an_automatic_proposal_still_in_flight_refuses_a_manual_approval(
    clean_tables: Database,
) -> None:
    """Belt and braces for the same property.

    Even if a button somehow existed, stage one refuses an AUTOMATIC proposal,
    and stage two would still hit the check constraint that only permits
    ``SYSTEM_AUTOMATIC`` on an AUTOMATIC proposal.
    """
    settings = _telegram_settings(execution_policy=ExecutionPolicy.AUTOMATIC)
    proposals = helpers.service_with(
        clean_tables,
        settings,
        market_data=helpers.StubMarketData(),
        control=ControlStateService(clean_tables),
    )
    await helpers.seed(clean_tables)
    await helpers.fund(clean_tables)
    generated = await proposals.generate(helpers.THESIS_ID)
    assert generated.proposal_id is not None
    coordinator = _coordinator(clean_tables, proposals, settings)

    tokens = TokenService()
    async with clean_tables.transaction() as session:
        issued = await tokens.mint(
            session,
            proposal_id=generated.proposal_id,
            stage=ApprovalStage.APPROVE,
            user_id=OWNER,
            chat_id=OWNER,
            expires_at=utcnow() + dt.timedelta(minutes=5),
        )
    result = await coordinator.handle(issued.raw, user_id=OWNER, chat_id=OWNER)
    assert "AUTOMATIC execution policy" in result.alert


# ---------------------------------------------------------------------------
# The proposal moving under the operator's finger
# ---------------------------------------------------------------------------
async def test_a_proposal_that_expires_mid_confirmation_is_refused(
    clean_tables: Database,
) -> None:
    _, coordinator, proposal_id = await _world(clean_tables)
    tokens = await _keyboard_tokens(clean_tables, coordinator, proposal_id)
    stage_one = await coordinator.handle(
        tokens[ApprovalStage.APPROVE], user_id=OWNER, chat_id=OWNER
    )
    confirm = str(parse_callback_data(stage_one.keyboard[0][0].callback_data))

    async with clean_tables.transaction() as session:
        proposal = await session.get(TradeProposal, proposal_id)
        assert proposal is not None
        proposal.expires_at = utcnow() - dt.timedelta(seconds=1)

    result = await coordinator.handle(confirm, user_id=OWNER, chat_id=OWNER)
    assert "Expired" in result.alert
    assert await _status(clean_tables, proposal_id) is not ProposalStatus.APPROVED


async def test_a_fresh_risk_refusal_invalidates_the_proposal_and_kills_the_buttons(
    clean_tables: Database,
) -> None:
    """The authoritative outcome is the proposal's own state.

    The confirmation is refused, the proposal is durably ``INVALIDATED`` by the
    shared authorization path, and every remaining button for it is consumed --
    so a second tap cannot re-ask the question.
    """
    settings = _telegram_settings()
    market = helpers.StubMarketData()
    proposals = helpers.service_with(
        clean_tables,
        settings,
        market_data=market,
        control=ControlStateService(clean_tables),
    )
    await helpers.seed(clean_tables)
    await helpers.fund(clean_tables)
    generated = await proposals.generate(helpers.THESIS_ID)
    assert generated.proposal_id is not None
    coordinator = _coordinator(clean_tables, proposals, settings)
    tokens = await _keyboard_tokens(clean_tables, coordinator, generated.proposal_id)
    stage_one = await coordinator.handle(
        tokens[ApprovalStage.APPROVE], user_id=OWNER, chat_id=OWNER
    )
    confirm = str(parse_callback_data(stage_one.keyboard[0][0].callback_data))

    # The book widens far past the ceiling between the two taps.
    market.bid = Decimal("150.00")
    market.ask = Decimal("250.00")

    result = await coordinator.handle(confirm, user_id=OWNER, chat_id=OWNER)
    assert "risk refused" in result.alert.lower()
    assert await _status(clean_tables, generated.proposal_id) is ProposalStatus.INVALIDATED

    async with clean_tables.session() as session:
        open_actions = int(
            (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(ApprovalAction)
                    .where(ApprovalAction.consumed_at.is_(None))
                )
            ).scalar_one()
        )
    assert open_actions == 0


async def test_details_re_renders_the_proposal_and_retracts_a_pending_confirmation(
    clean_tables: Database,
) -> None:
    """Backing out of a confirmation actually retracts it.

    Leaving a live CONFIRM token behind a button labelled "Back" would mean the
    operator's retraction was cosmetic.
    """
    _, coordinator, proposal_id = await _world(clean_tables)
    tokens = await _keyboard_tokens(clean_tables, coordinator, proposal_id)
    stage_one = await coordinator.handle(
        tokens[ApprovalStage.APPROVE], user_id=OWNER, chat_id=OWNER
    )
    confirm = str(parse_callback_data(stage_one.keyboard[0][0].callback_data))
    back = str(parse_callback_data(stage_one.keyboard[0][1].callback_data))

    details = await coordinator.handle(back, user_id=OWNER, chat_id=OWNER)
    assert details.text is not None and "AAPL" in details.text
    assert details.keyboard

    stale = await coordinator.handle(confirm, user_id=OWNER, chat_id=OWNER)
    assert "already been used" in stale.alert
    assert await _status(clean_tables, proposal_id) is not ProposalStatus.APPROVED
