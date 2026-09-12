"""Which categories of event reach the chat, and which stay in the database.

Every notification StockBrain has ever sent was a proposal or execution
transition, because those are the only ones where doing nothing has a cost.
Widening that to the whole pipeline -- discovery, classification, promotion,
research -- makes the channel far more useful and, done carelessly, makes it
unreadable: a deployment ingesting a few hundred articles a day would produce a
few hundred messages a day, and an alert stream nobody reads is worse than no
alerts because it looks like coverage.

So three rules shape this module.

**Off by default where volume is high.**  ``EVENT_DISCOVERED`` and
``EVENT_RELEVANT`` are one message per article and per classified article; both
ship disabled.  ``EVENT_CANDIDATE`` -- the article the classifier thought was
worth paying to research -- ships enabled, because that is the first point in
the pipeline where the system is about to spend money.

**Stages are distinct, not synonyms.**  "Discovered", "considered relevant" and
"promoted to candidate" are three different facts about the same article and are
three different categories.  Collapsing them would mean the only way to hear
about a promotion is to hear about every scraped search result.

**One category cannot be switched off.**  ``EXECUTION_CRITICAL`` carries the
ambiguous-order message: an order that may or may not exist at the broker, where
the obvious reaction is the one action that turns an unknown into a duplicated
position.  A preference that could silence it would be a preference that could
lose money, so :meth:`NotificationPreferences.update` refuses to disable it.

Preferences live in ``app_settings``, so they survive a restart and are the same
values every worker reads.  They are cached for a few seconds because the
ingestion path consults them once per event, and a database round trip per
ingested article to answer "should we say anything" would cost more than the
message.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.dialects.postgresql import insert as pg_insert

from stockbrain.db.base import utcnow
from stockbrain.db.models.system import AppSetting
from stockbrain.db.session import Database
from stockbrain.enums import NotificationEvent
from stockbrain.logging import get_logger

__all__ = [
    "DEFAULT_PREFERENCES",
    "LOCKED_CATEGORIES",
    "NOTIFICATION_CATEGORY_DETAIL",
    "PREFERENCES_KEY",
    "NotificationCategory",
    "NotificationPreferences",
    "PipelineEvent",
    "category_for_event",
    "category_for_pipeline_event",
]

log = get_logger(__name__)

PREFERENCES_KEY = "telegram.notification_preferences"

#: How long a read is reused before the row is consulted again.  Short enough
#: that a preference change takes effect within one page refresh; long enough
#: that a burst of ingestion does not become a burst of queries.
_CACHE_TTL_SECONDS = 15.0


class NotificationCategory(StrEnum):
    """One independently switchable class of message."""

    EVENT_DISCOVERED = "EVENT_DISCOVERED"
    """A new canonical event was ingested from any discovery source.  Nothing has
    judged it yet: this is the raw firehose, and it is off by default."""

    EVENT_RELEVANT = "EVENT_RELEVANT"
    """The classifier read the event and considered it relevant to public
    equities, without promoting it to research."""

    EVENT_CANDIDATE = "EVENT_CANDIDATE"
    """The event cleared the importance, confidence and materiality thresholds
    and became a research candidate.  The first point at which the pipeline is
    about to spend money on this story."""

    RESEARCH_STARTED = "RESEARCH_STARTED"
    """A research run began.  Off by default: the completion carries the answer,
    and a run that finishes in two minutes does not need two messages."""

    RESEARCH_COMPLETED = "RESEARCH_COMPLETED"
    """A research run finished, succeeded or failed, with its action and
    confidence where it produced a thesis."""

    PROPOSAL_BLOCKED = "PROPOSAL_BLOCKED"
    """Research recommended a trade and the deterministic risk engine refused it.
    The message names the rules that refused it, so "the system said buy and
    then went quiet" is answerable from the channel."""

    PROPOSAL = "PROPOSAL"
    """A proposal was created and awaits authorization, or was authorized
    automatically."""

    PROPOSAL_OUTCOME = "PROPOSAL_OUTCOME"
    """A proposal the operator was told about was rejected, invalidated, expired,
    or had an authorization refused by a fresh risk revalidation."""

    EXECUTION = "EXECUTION"
    """An order reached the broker, was refused by it, was never transmitted, was
    reconciled, or filled."""

    EXECUTION_CRITICAL = "EXECUTION_CRITICAL"
    """An order's state is unknown.  Cannot be disabled."""

    OPERATIONAL = "OPERATIONAL"
    """Provider outages, budget exhaustion, a stalled queue, a halted system --
    the operational alert scan's output."""


@dataclass(frozen=True, slots=True)
class CategoryDetail:
    label: str
    description: str
    volume: str
    """A plain-language estimate, so enabling one is an informed choice."""


NOTIFICATION_CATEGORY_DETAIL: dict[NotificationCategory, CategoryDetail] = {
    NotificationCategory.EVENT_DISCOVERED: CategoryDetail(
        label="Article discovered",
        description=(
            "Every new canonical event, straight from ingestion, before anything has "
            "judged it. Useful for a day of watching the pipeline; not for living with."
        ),
        volume="high",
    ),
    NotificationCategory.EVENT_RELEVANT: CategoryDetail(
        label="Considered relevant",
        description=(
            "The classifier read the article and judged it relevant to public equities "
            "without promoting it to research."
        ),
        volume="medium",
    ),
    NotificationCategory.EVENT_CANDIDATE: CategoryDetail(
        label="Promoted to candidate",
        description=(
            "The article cleared every classification threshold and is queued for paid "
            "research. The first point at which this story is about to cost money."
        ),
        volume="low",
    ),
    NotificationCategory.RESEARCH_STARTED: CategoryDetail(
        label="Research started",
        description="A research run began on a promoted candidate.",
        volume="low",
    ),
    NotificationCategory.RESEARCH_COMPLETED: CategoryDetail(
        label="Research completed",
        description=(
            "A research run finished, with its action, confidence and thesis summary, or "
            "with the reason it failed."
        ),
        volume="low",
    ),
    NotificationCategory.PROPOSAL_BLOCKED: CategoryDetail(
        label="Trade blocked by risk",
        description=(
            "Research recommended a trade and the risk engine refused it. Lists the blocking rules."
        ),
        volume="low",
    ),
    NotificationCategory.PROPOSAL: CategoryDetail(
        label="Proposal created",
        description=(
            "A trade proposal awaits authorization, or was authorized automatically. "
            "Manual proposals carry the approve and reject buttons."
        ),
        volume="low",
    ),
    NotificationCategory.PROPOSAL_OUTCOME: CategoryDetail(
        label="Proposal outcome",
        description=(
            "Rejected, invalidated, expired, or refused by a fresh risk revalidation. "
            "Only ever sent for a proposal you were actually told about."
        ),
        volume="low",
    ),
    NotificationCategory.EXECUTION: CategoryDetail(
        label="Execution",
        description=(
            "An order was accepted by the broker, refused by it, never transmitted, "
            "reconciled or filled."
        ),
        volume="low",
    ),
    NotificationCategory.EXECUTION_CRITICAL: CategoryDetail(
        label="Order state unknown",
        description=(
            "An order may or may not exist at the broker. This cannot be switched off: "
            "the correct response is to do nothing, and the obvious one is to resend."
        ),
        volume="rare",
    ),
    NotificationCategory.OPERATIONAL: CategoryDetail(
        label="Health and budget alerts",
        description=(
            "Provider outages, exhausted budgets, a stalled job queue, a halted system, "
            "and the matching all-clear when each one resolves."
        ),
        volume="low",
    ),
}

#: Categories that ship enabled.  Everything absent from this mapping is off.
DEFAULT_PREFERENCES: dict[NotificationCategory, bool] = {
    NotificationCategory.EVENT_DISCOVERED: False,
    NotificationCategory.EVENT_RELEVANT: False,
    NotificationCategory.EVENT_CANDIDATE: True,
    NotificationCategory.RESEARCH_STARTED: False,
    NotificationCategory.RESEARCH_COMPLETED: True,
    NotificationCategory.PROPOSAL_BLOCKED: True,
    NotificationCategory.PROPOSAL: True,
    NotificationCategory.PROPOSAL_OUTCOME: True,
    NotificationCategory.EXECUTION: True,
    NotificationCategory.EXECUTION_CRITICAL: True,
    NotificationCategory.OPERATIONAL: True,
}

#: Categories no preference may disable.
LOCKED_CATEGORIES: frozenset[NotificationCategory] = frozenset(
    {NotificationCategory.EXECUTION_CRITICAL}
)


class PipelineEvent(StrEnum):
    """Stages of the discovery-to-research pipeline that can be announced.

    Deliberately separate from :class:`~stockbrain.enums.NotificationEvent`,
    which enumerates *proposal* transitions and whose values are half of a
    ``notifications.dedupe_key`` guaranteeing one message per proposal
    transition. Adding pipeline stages to it would widen a guarantee that is
    about proposals onto entities that are not proposals.
    """

    EVENT_DISCOVERED = "EVENT_DISCOVERED"
    EVENT_RELEVANT = "EVENT_RELEVANT"
    EVENT_CANDIDATE = "EVENT_CANDIDATE"
    RESEARCH_STARTED = "RESEARCH_STARTED"
    RESEARCH_COMPLETED = "RESEARCH_COMPLETED"
    PROPOSAL_BLOCKED = "PROPOSAL_BLOCKED"


_PIPELINE_CATEGORIES: dict[PipelineEvent, NotificationCategory] = {
    PipelineEvent.EVENT_DISCOVERED: NotificationCategory.EVENT_DISCOVERED,
    PipelineEvent.EVENT_RELEVANT: NotificationCategory.EVENT_RELEVANT,
    PipelineEvent.EVENT_CANDIDATE: NotificationCategory.EVENT_CANDIDATE,
    PipelineEvent.RESEARCH_STARTED: NotificationCategory.RESEARCH_STARTED,
    PipelineEvent.RESEARCH_COMPLETED: NotificationCategory.RESEARCH_COMPLETED,
    PipelineEvent.PROPOSAL_BLOCKED: NotificationCategory.PROPOSAL_BLOCKED,
}

_PROPOSAL_CATEGORIES: dict[NotificationEvent, NotificationCategory] = {
    NotificationEvent.PROPOSAL_MANUAL: NotificationCategory.PROPOSAL,
    NotificationEvent.PROPOSAL_AUTO_AUTHORIZED: NotificationCategory.PROPOSAL,
    NotificationEvent.PROPOSAL_REJECTED: NotificationCategory.PROPOSAL_OUTCOME,
    NotificationEvent.PROPOSAL_INVALIDATED: NotificationCategory.PROPOSAL_OUTCOME,
    NotificationEvent.PROPOSAL_EXPIRED: NotificationCategory.PROPOSAL_OUTCOME,
    NotificationEvent.AUTHORIZATION_REFUSED: NotificationCategory.PROPOSAL_OUTCOME,
    NotificationEvent.EXECUTION_SUBMITTED: NotificationCategory.EXECUTION,
    NotificationEvent.EXECUTION_REJECTED: NotificationCategory.EXECUTION,
    NotificationEvent.EXECUTION_FAILED: NotificationCategory.EXECUTION,
    NotificationEvent.EXECUTION_RECONCILED: NotificationCategory.EXECUTION,
    NotificationEvent.EXECUTION_CONFIRMED: NotificationCategory.EXECUTION,
    NotificationEvent.EXECUTION_AMBIGUOUS: NotificationCategory.EXECUTION_CRITICAL,
}


def category_for_event(event: NotificationEvent) -> NotificationCategory:
    return _PROPOSAL_CATEGORIES[event]


def category_for_pipeline_event(event: PipelineEvent) -> NotificationCategory:
    return _PIPELINE_CATEGORIES[event]


@dataclass(frozen=True, slots=True)
class PreferenceSnapshot:
    categories: dict[NotificationCategory, bool]
    updated_at: dt.datetime | None
    updated_by: str | None

    def enabled(self, category: NotificationCategory) -> bool:
        if category in LOCKED_CATEGORIES:
            return True
        return self.categories.get(category, DEFAULT_PREFERENCES[category])


class NotificationPreferences:
    """Read and write the per-category switches, with a short read cache."""

    def __init__(
        self, database: Database, *, cache_ttl_seconds: float = _CACHE_TTL_SECONDS
    ) -> None:
        self._database = database
        self._ttl = cache_ttl_seconds
        self._cached: PreferenceSnapshot | None = None
        self._cached_at: float = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    async def snapshot(self, *, refresh: bool = False) -> PreferenceSnapshot:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if not refresh and self._cached is not None and now - self._cached_at < self._ttl:
            return self._cached
        async with self._lock:
            # Re-check inside the lock: several ingestion tasks can arrive at a
            # cold cache together, and one query is enough for all of them.
            now = loop.time()
            if not refresh and self._cached is not None and now - self._cached_at < self._ttl:
                return self._cached
            async with self._database.session() as session:
                row = await session.get(AppSetting, PREFERENCES_KEY)
            snapshot = _from_row(row.value if row else None, row.updated_by if row else None)
            self._cached = snapshot
            self._cached_at = loop.time()
            return snapshot

    async def enabled(self, category: NotificationCategory) -> bool:
        return (await self.snapshot()).enabled(category)

    # ------------------------------------------------------------------
    async def update(
        self, changes: dict[NotificationCategory, bool], *, actor: str
    ) -> PreferenceSnapshot:
        """Apply a partial update.

        A locked category is silently forced back on rather than rejected with an
        error: the caller's intent -- "save my preferences" -- is honoured, and
        the response says plainly that the category is still enabled.
        """
        current = await self.snapshot(refresh=True)
        merged: dict[str, bool] = {
            category.value: current.enabled(category) for category in NotificationCategory
        }
        for category, value in changes.items():
            merged[category.value] = True if category in LOCKED_CATEGORIES else bool(value)

        now = utcnow()
        payload: dict[str, object] = {"categories": merged, "updated_at": now.isoformat()}
        async with self._database.transaction() as session:
            statement = pg_insert(AppSetting).values(
                key=PREFERENCES_KEY,
                value=payload,
                description="Which categories of Telegram notification are delivered.",
                updated_by=actor,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[AppSetting.key],
                    set_={
                        "value": statement.excluded.value,
                        "description": statement.excluded.description,
                        "updated_by": statement.excluded.updated_by,
                        "updated_at": now,
                    },
                )
            )
        log.info(
            "telegram_notification_preferences_changed",
            actor=actor,
            enabled=sorted(key for key, value in merged.items() if value),
        )
        self._cached = None
        self._cached_at = 0.0
        return await self.snapshot(refresh=True)


def _from_row(value: object, updated_by: str | None) -> PreferenceSnapshot:
    """Build a snapshot, treating anything unreadable as the defaults.

    Falling back to the defaults is the safe direction here: the defaults are
    quiet, and the one category that must never go quiet is forced on by
    :meth:`PreferenceSnapshot.enabled` regardless of what the row says.
    """
    categories: dict[NotificationCategory, bool] = dict(DEFAULT_PREFERENCES)
    updated_at: dt.datetime | None = None
    if isinstance(value, dict):
        stored = value.get("categories")
        if isinstance(stored, dict):
            for key, flag in stored.items():
                try:
                    category = NotificationCategory(str(key))
                except ValueError:
                    continue
                categories[category] = bool(flag)
        raw_time = value.get("updated_at")
        if isinstance(raw_time, str):
            try:
                updated_at = dt.datetime.fromisoformat(raw_time)
            except ValueError:  # pragma: no cover - defensive
                updated_at = None
    for locked in LOCKED_CATEGORIES:
        categories[locked] = True
    return PreferenceSnapshot(categories=categories, updated_at=updated_at, updated_by=updated_by)
