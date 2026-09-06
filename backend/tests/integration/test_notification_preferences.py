"""Granular notification preferences, and the one that cannot be switched off.

The whole point of widening notifications beyond proposals is that the channel
stays readable.  So these tests are about restraint: quiet defaults, a category
map with no gaps, and an ambiguous-order message no preference can silence.
"""

from __future__ import annotations

import pytest

from stockbrain.db.session import Database
from stockbrain.enums import NotificationEvent
from stockbrain.telegram.preferences import (
    DEFAULT_PREFERENCES,
    LOCKED_CATEGORIES,
    NOTIFICATION_CATEGORY_DETAIL,
    NotificationCategory,
    NotificationPreferences,
    PipelineEvent,
    category_for_event,
    category_for_pipeline_event,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# The category map has no holes
# ---------------------------------------------------------------------------


def test_every_proposal_transition_maps_to_a_category() -> None:
    """An unmapped transition would raise at the moment a trade needed announcing."""
    for event in NotificationEvent:
        assert isinstance(category_for_event(event), NotificationCategory)


def test_every_pipeline_stage_maps_to_a_category() -> None:
    for event in PipelineEvent:
        assert isinstance(category_for_pipeline_event(event), NotificationCategory)


def test_every_category_has_a_default_and_a_description() -> None:
    for category in NotificationCategory:
        assert category in DEFAULT_PREFERENCES
        assert category in NOTIFICATION_CATEGORY_DETAIL
        assert NOTIFICATION_CATEGORY_DETAIL[category].volume in {"rare", "low", "medium", "high"}


# ---------------------------------------------------------------------------
# Quiet by default
# ---------------------------------------------------------------------------


def test_the_high_volume_stages_ship_switched_off() -> None:
    """One message per scraped article would train the operator to ignore the channel."""
    assert DEFAULT_PREFERENCES[NotificationCategory.EVENT_DISCOVERED] is False
    assert DEFAULT_PREFERENCES[NotificationCategory.EVENT_RELEVANT] is False
    assert DEFAULT_PREFERENCES[NotificationCategory.RESEARCH_STARTED] is False


def test_the_stages_that_cost_money_or_need_a_decision_ship_switched_on() -> None:
    for category in (
        NotificationCategory.EVENT_CANDIDATE,
        NotificationCategory.RESEARCH_COMPLETED,
        NotificationCategory.PROPOSAL,
        NotificationCategory.PROPOSAL_OUTCOME,
        NotificationCategory.EXECUTION,
        NotificationCategory.EXECUTION_CRITICAL,
        NotificationCategory.OPERATIONAL,
    ):
        assert DEFAULT_PREFERENCES[category] is True


async def test_an_unconfigured_deployment_reads_the_defaults(clean_tables: Database) -> None:
    snapshot = await NotificationPreferences(clean_tables).snapshot()
    assert snapshot.enabled(NotificationCategory.EVENT_DISCOVERED) is False
    assert snapshot.enabled(NotificationCategory.PROPOSAL) is True
    assert snapshot.updated_at is None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def test_a_partial_update_leaves_other_categories_alone(clean_tables: Database) -> None:
    preferences = NotificationPreferences(clean_tables, cache_ttl_seconds=0.0)
    await preferences.update({NotificationCategory.EVENT_DISCOVERED: True}, actor="web:owner")
    snapshot = await preferences.snapshot(refresh=True)
    assert snapshot.enabled(NotificationCategory.EVENT_DISCOVERED) is True
    # Untouched, and specifically not reset to a default.
    assert snapshot.enabled(NotificationCategory.EVENT_RELEVANT) is False
    assert snapshot.enabled(NotificationCategory.PROPOSAL) is True
    assert snapshot.updated_by == "web:owner"


async def test_the_critical_category_cannot_be_switched_off(clean_tables: Database) -> None:
    """The ambiguous-order message is the one where doing nothing is correct.

    A preference that could silence it would be a preference that could lose
    money, so the write is honoured for everything else and forced back on for
    this one -- rather than failing the whole save, which would leave the
    operator's other changes unsaved for no benefit.
    """
    preferences = NotificationPreferences(clean_tables, cache_ttl_seconds=0.0)
    result = await preferences.update(
        {
            NotificationCategory.EXECUTION_CRITICAL: False,
            NotificationCategory.EXECUTION: False,
        },
        actor="web:owner",
    )
    assert result.enabled(NotificationCategory.EXECUTION_CRITICAL) is True
    assert result.enabled(NotificationCategory.EXECUTION) is False
    assert NotificationCategory.EXECUTION_CRITICAL in LOCKED_CATEGORIES


async def test_a_corrupt_stored_row_falls_back_to_the_quiet_defaults(
    clean_tables: Database,
) -> None:
    """Failing to the defaults is safe here: the defaults are quiet, and the one
    category that must never go quiet is forced on regardless."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from stockbrain.db.models.system import AppSetting
    from stockbrain.telegram.preferences import PREFERENCES_KEY

    async with clean_tables.transaction() as session:
        await session.execute(
            pg_insert(AppSetting).values(
                key=PREFERENCES_KEY, value={"categories": "not-a-dict"}, updated_by="test"
            )
        )
    snapshot = await NotificationPreferences(clean_tables).snapshot()
    assert snapshot.enabled(NotificationCategory.EVENT_DISCOVERED) is False
    assert snapshot.enabled(NotificationCategory.EXECUTION_CRITICAL) is True


async def test_an_unknown_stored_category_is_ignored_rather_than_fatal(
    clean_tables: Database,
) -> None:
    """A row written by a newer build must not break an older one, or vice versa."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from stockbrain.db.models.system import AppSetting
    from stockbrain.telegram.preferences import PREFERENCES_KEY

    async with clean_tables.transaction() as session:
        await session.execute(
            pg_insert(AppSetting).values(
                key=PREFERENCES_KEY,
                value={"categories": {"NOT_A_CATEGORY": True, "PROPOSAL": False}},
                updated_by="test",
            )
        )
    snapshot = await NotificationPreferences(clean_tables).snapshot()
    assert snapshot.enabled(NotificationCategory.PROPOSAL) is False


async def test_the_cache_is_invalidated_by_a_write(clean_tables: Database) -> None:
    """Otherwise a saved preference would not take effect until the TTL expired --
    which is exactly the window an operator would test in."""
    preferences = NotificationPreferences(clean_tables, cache_ttl_seconds=3600.0)
    assert await preferences.enabled(NotificationCategory.EVENT_DISCOVERED) is False
    await preferences.update({NotificationCategory.EVENT_DISCOVERED: True}, actor="web:owner")
    assert await preferences.enabled(NotificationCategory.EVENT_DISCOVERED) is True
