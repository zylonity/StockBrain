"""Opaque single-use callback tokens.

The property: possessing the bytes Telegram hands back is *not* possessing the
action.  The bytes name a row; the row carries the proposal, the permitted
stage, the numeric user, the numeric chat and an expiry, and all five are
checked before it is consumed.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa

from stockbrain.db.base import utcnow
from stockbrain.db.models.proposals import ApprovalAction
from stockbrain.db.session import Database
from stockbrain.enums import ApprovalChannel, ApprovalStage
from stockbrain.errors import (
    ApprovalActionConsumed,
    ApprovalActionExpired,
    ApprovalActionForeign,
    ApprovalActionInvalid,
)
from stockbrain.telegram.tokens import (
    MAX_CALLBACK_DATA_BYTES,
    TokenService,
    callback_data,
    parse_callback_data,
    token_hash,
)
from tests import proposal_helpers as helpers

pytestmark = pytest.mark.integration

OWNER = 4242
STRANGER = 9999
OTHER_CHAT = -100777


async def _proposal(database: Database) -> uuid.UUID:
    await helpers.seed(database)
    await helpers.fund(database)
    result = await helpers.service(database).generate(helpers.THESIS_ID)
    assert result.proposal_id is not None
    return result.proposal_id


async def _mint(
    database: Database,
    proposal_id: uuid.UUID,
    *,
    stage: ApprovalStage = ApprovalStage.APPROVE,
    user_id: int = OWNER,
    chat_id: int | None = OWNER,
    ttl_seconds: int = 900,
    parent_action_id: uuid.UUID | None = None,
) -> tuple[TokenService, str, uuid.UUID]:
    tokens = TokenService()
    async with database.transaction() as session:
        issued = await tokens.mint(
            session,
            proposal_id=proposal_id,
            stage=stage,
            user_id=user_id,
            chat_id=chat_id,
            expires_at=utcnow() + dt.timedelta(seconds=ttl_seconds),
            parent_action_id=parent_action_id,
        )
    return tokens, issued.raw, issued.action_id


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------
async def test_callback_data_fits_telegrams_limit_and_carries_only_the_token(
    clean_tables: Database,
) -> None:
    """Telegram documents ``callback_data`` as 1-64 bytes.

    Exceeding it fails at *send* time -- in production, on the message that
    matters most -- so the bound is asserted rather than assumed.
    """
    proposal_id = await _proposal(clean_tables)
    _, raw, _ = await _mint(clean_tables, proposal_id)
    data = callback_data(raw)
    assert len(data.encode("utf-8")) <= MAX_CALLBACK_DATA_BYTES
    assert parse_callback_data(data) == raw

    # Nothing identifying the trade appears in the payload.
    for forbidden in (str(proposal_id), "AAPL_US_EQ", "200.05", "APPROVE"):
        assert forbidden not in data

    # Structural, and therefore not a probabilistic claim about a random
    # string: two tokens for the *same* proposal and stage differ, so the
    # payload cannot be a function of the proposal at all, and the length is
    # fixed regardless of what the proposal contains.
    _, second, _ = await _mint(clean_tables, proposal_id)
    assert second != raw
    assert len(callback_data(second)) == len(data)


def test_a_malformed_payload_is_rejected_before_any_query() -> None:
    for payload in (None, "", "approve:1", "sb:", "sb:short", "sb:" + "!" * 43, "xx:" + "a" * 43):
        assert parse_callback_data(payload) is None


async def test_only_the_hash_is_persisted(clean_tables: Database) -> None:
    """A database leak must not yield usable approval tokens."""
    proposal_id = await _proposal(clean_tables)
    _, raw, action_id = await _mint(clean_tables, proposal_id)
    async with clean_tables.session() as session:
        action = await session.get(ApprovalAction, action_id)
        assert action is not None
        assert action.opaque_token_hash == token_hash(raw)
        assert len(action.opaque_token_hash) == 64
        rendered = repr({c.name: getattr(action, c.name) for c in action.__table__.columns})
    assert raw not in rendered


async def test_token_hashes_are_unique_across_many_mints(clean_tables: Database) -> None:
    proposal_id = await _proposal(clean_tables)
    tokens = TokenService()
    raws: set[str] = set()
    async with clean_tables.transaction() as session:
        for _ in range(50):
            issued = await tokens.mint(
                session,
                proposal_id=proposal_id,
                stage=ApprovalStage.DETAILS,
                user_id=OWNER,
                chat_id=OWNER,
                expires_at=utcnow() + dt.timedelta(minutes=5),
            )
            raws.add(issued.raw)
    assert len(raws) == 50
    async with clean_tables.session() as session:
        hashes = list(
            (await session.execute(sa.select(ApprovalAction.opaque_token_hash))).scalars()
        )
    assert len(hashes) == len(set(hashes)) == 50


# ---------------------------------------------------------------------------
# Redemption
# ---------------------------------------------------------------------------
async def test_a_valid_token_resolves_and_is_consumed_exactly_once(
    clean_tables: Database,
) -> None:
    proposal_id = await _proposal(clean_tables)
    tokens, raw, action_id = await _mint(clean_tables, proposal_id)

    async with clean_tables.transaction() as session:
        action = await tokens.resolve_and_consume(session, raw, user_id=OWNER, chat_id=OWNER)
    assert action.proposal_id == proposal_id
    assert action.stage is ApprovalStage.APPROVE
    assert action.action_id == action_id

    with pytest.raises(ApprovalActionConsumed):
        async with clean_tables.transaction() as session:
            await tokens.resolve_and_consume(session, raw, user_id=OWNER, chat_id=OWNER)


async def test_an_unknown_token_is_refused(clean_tables: Database) -> None:
    tokens = TokenService()
    with pytest.raises(ApprovalActionInvalid):
        async with clean_tables.transaction() as session:
            await tokens.resolve_and_consume(session, "a" * 43, user_id=OWNER, chat_id=OWNER)


async def test_an_expired_token_is_refused(clean_tables: Database) -> None:
    proposal_id = await _proposal(clean_tables)
    tokens, raw, _ = await _mint(clean_tables, proposal_id, ttl_seconds=30)
    with pytest.raises(ApprovalActionExpired):
        async with clean_tables.transaction() as session:
            await tokens.resolve_and_consume(
                session,
                raw,
                user_id=OWNER,
                chat_id=OWNER,
                now=utcnow() + dt.timedelta(seconds=31),
            )


async def test_a_different_user_cannot_redeem_or_burn_the_token(clean_tables: Database) -> None:
    """The forwarded-message case.

    Refused *without* consuming: if a stranger's tap spent the token, obtaining
    a copy of the message would be enough to disable the owner's real button.
    """
    proposal_id = await _proposal(clean_tables)
    tokens, raw, action_id = await _mint(clean_tables, proposal_id)

    with pytest.raises(ApprovalActionForeign):
        async with clean_tables.transaction() as session:
            await tokens.resolve_and_consume(session, raw, user_id=STRANGER, chat_id=STRANGER)

    async with clean_tables.session() as session:
        action = await session.get(ApprovalAction, action_id)
        assert action is not None and action.consumed_at is None

    async with clean_tables.transaction() as session:
        resolved = await tokens.resolve_and_consume(session, raw, user_id=OWNER, chat_id=OWNER)
    assert resolved.proposal_id == proposal_id


async def test_a_different_chat_cannot_redeem_the_token(clean_tables: Database) -> None:
    """Copying the button into another chat does not carry the permission."""
    proposal_id = await _proposal(clean_tables)
    tokens, raw, _ = await _mint(clean_tables, proposal_id)
    with pytest.raises(ApprovalActionForeign):
        async with clean_tables.transaction() as session:
            await tokens.resolve_and_consume(session, raw, user_id=OWNER, chat_id=OTHER_CHAT)


async def test_a_token_issued_for_one_stage_is_not_valid_for_another(
    clean_tables: Database,
) -> None:
    proposal_id = await _proposal(clean_tables)
    tokens, raw, _ = await _mint(clean_tables, proposal_id, stage=ApprovalStage.DETAILS)
    with pytest.raises(ApprovalActionInvalid):
        async with clean_tables.transaction() as session:
            await tokens.resolve_and_consume(
                session,
                raw,
                user_id=OWNER,
                chat_id=OWNER,
                expected_stages=(ApprovalStage.CONFIRM,),
            )


# ---------------------------------------------------------------------------
# Retirement
# ---------------------------------------------------------------------------
async def test_retiring_open_actions_makes_every_outstanding_button_inert(
    clean_tables: Database,
) -> None:
    proposal_id = await _proposal(clean_tables)
    tokens, first, _ = await _mint(clean_tables, proposal_id)
    _, second, _ = await _mint(clean_tables, proposal_id, stage=ApprovalStage.REJECT)

    async with clean_tables.transaction() as session:
        retired = await tokens.retire_open_actions(session, proposal_id, reason="terminal")
    assert retired == 2

    for raw in (first, second):
        with pytest.raises(ApprovalActionConsumed):
            async with clean_tables.transaction() as session:
                await tokens.resolve_and_consume(session, raw, user_id=OWNER, chat_id=OWNER)


async def test_retirement_can_spare_the_action_currently_being_processed(
    clean_tables: Database,
) -> None:
    proposal_id = await _proposal(clean_tables)
    tokens, _, keep = await _mint(clean_tables, proposal_id)
    _, other, _ = await _mint(clean_tables, proposal_id, stage=ApprovalStage.REJECT)
    async with clean_tables.transaction() as session:
        retired = await tokens.retire_open_actions(
            session, proposal_id, reason="terminal", exclude=keep
        )
    assert retired == 1
    assert other  # the retired one


async def test_open_messages_reports_only_fully_bound_ui_context(
    clean_tables: Database,
) -> None:
    """Context is audit/UI only, so a half-populated entry is skipped rather
    than guessed at."""
    proposal_id = await _proposal(clean_tables)
    tokens, _, action_id = await _mint(clean_tables, proposal_id)
    _, _, no_context = await _mint(clean_tables, proposal_id, stage=ApprovalStage.DETAILS)

    async with clean_tables.transaction() as session:
        await tokens.attach_message(session, [action_id], message_id=77, chat_id=OWNER)
    async with clean_tables.session() as session:
        pairs = await tokens.open_messages(session, proposal_id)
    assert pairs == [(OWNER, 77)]
    assert no_context


async def test_the_channel_is_recorded_as_telegram(clean_tables: Database) -> None:
    proposal_id = await _proposal(clean_tables)
    _, _, action_id = await _mint(clean_tables, proposal_id)
    async with clean_tables.session() as session:
        action = await session.get(ApprovalAction, action_id)
    assert action is not None
    assert action.channel is ApprovalChannel.TELEGRAM
    assert action.user_identifier == str(OWNER)
    assert action.chat_identifier == str(OWNER)
