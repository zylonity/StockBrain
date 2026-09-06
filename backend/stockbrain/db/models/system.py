"""System-level tables: audit, jobs, settings, discovery topics, health, users.

The job queue is PostgreSQL-backed (``FOR UPDATE SKIP LOCKED``); there is no
Redis in this system.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stockbrain.db.base import Base, JSONDict, TimestampMixin, UUIDPrimaryKeyMixin
from stockbrain.db.models._types import pg_enum
from stockbrain.enums import (
    ActorType,
    JobStatus,
    NotificationClass,
    NotificationStatus,
    ProviderCallKind,
    ProviderCallOutcome,
    ProviderStatus,
    WebDiscoveryKind,
)

__all__ = [
    "AppSetting",
    "AuditLog",
    "DiscoveryQuery",
    "DiscoveryTopic",
    "Job",
    "Notification",
    "ProviderCall",
    "ProviderHealthRecord",
    "User",
]


class AuditLog(UUIDPrimaryKeyMixin, Base):
    """Append-oriented record of every significant decision.

    Never contains credentials.  Rows are written in the same transaction as the
    state change they describe wherever that is possible.
    """

    __tablename__ = "audit_log"

    occurred_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    actor_type: Mapped[ActorType] = mapped_column(pg_enum(ActorType, "actor_type"), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(sa.Text)
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(sa.Text)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(as_uuid=True))
    details: Mapped[JSONDict] = mapped_column(nullable=False, server_default=sa.text("'{}'::jsonb"))

    __table_args__ = (
        sa.Index("ix_audit_log_occurred_at", "occurred_at"),
        sa.Index("ix_audit_log_entity", "entity_type", "entity_id"),
        sa.Index("ix_audit_log_action", "action"),
    )


class Job(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable background work item.

    Claimed with ``SELECT ... FOR UPDATE SKIP LOCKED``; ``locked_at`` allows a
    crashed worker's jobs to be reclaimed after a timeout.
    """

    __tablename__ = "jobs"

    job_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[JSONDict] = mapped_column(nullable=False, server_default=sa.text("'{}'::jsonb"))
    status: Mapped[JobStatus] = mapped_column(
        pg_enum(JobStatus, "job_status"),
        nullable=False,
        default=JobStatus.PENDING,
        server_default=JobStatus.PENDING.value,
    )
    priority: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="100")
    run_after: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="3")
    locked_by: Mapped[str | None] = mapped_column(sa.Text)
    locked_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    dedupe_key: Mapped[str | None] = mapped_column(sa.Text)
    """Optional key preventing duplicate scheduling of the same logical work."""

    __table_args__ = (
        sa.Index(
            "ix_jobs_claim",
            "priority",
            "created_at",
            postgresql_where=sa.text("status = 'PENDING'"),
        ),
        sa.Index("ix_jobs_status_run_after", "status", "run_after"),
        sa.Index(
            "uq_jobs_dedupe_key_active",
            "dedupe_key",
            unique=True,
            postgresql_where=sa.text("dedupe_key IS NOT NULL AND status IN ('PENDING', 'RUNNING')"),
        ),
    )


class AppSetting(TimestampMixin, Base):
    """Runtime configuration and feature flags that must survive restarts.

    Secrets never live here; they come from the environment.  This table holds
    things like the kill switch, discovery pause state and editable risk limits.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    value: Mapped[JSONDict] = mapped_column(nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    updated_by: Mapped[str | None] = mapped_column(sa.Text)


class DiscoveryTopic(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A user-editable thematic discovery topic (spec section 35)."""

    __tablename__ = "discovery_topics"

    slug: Mapped[str] = mapped_column(sa.Text, nullable=False)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    interval_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="30")
    result_limit: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="10")
    freshness_days: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="7")
    """How far back results may be published, in days.

    A number rather than a provider token.  ``qdr:d`` was Firecrawl's spelling,
    ``pd`` is Brave's and an ISO instant is Exa's; storing one of the three in
    the database made the column unusable by the other two.  The adapter
    translates.
    """

    include_domains: Mapped[JSONDict] = mapped_column(
        nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    exclude_domains: Mapped[JSONDict] = mapped_column(
        nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    last_run_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    queries: Mapped[list[DiscoveryQuery]] = relationship(
        back_populates="topic", cascade="all, delete-orphan"
    )

    __table_args__ = (sa.UniqueConstraint("slug", name="uq_discovery_topics_slug"),)


class DiscoveryQuery(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "discovery_queries"

    topic_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("discovery_topics.id", ondelete="CASCADE"), nullable=False
    )
    query: Mapped[str] = mapped_column(sa.Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())

    search_kind: Mapped[WebDiscoveryKind] = mapped_column(
        pg_enum(WebDiscoveryKind, "web_discovery_kind"),
        nullable=False,
        default=WebDiscoveryKind.ROUTINE,
        server_default=WebDiscoveryKind.ROUTINE.value,
    )
    """Whether this is a conventional recent-news search or a second-order one.

    The kind decides which provider answers it and on which schedule, so it is a
    property of the query rather than of the deployment.  Every row that existed
    before the provider split is ``ROUTINE``: they were keyword searches, and
    calling them semantic afterwards would put them on Exa's price."""

    provider: Mapped[str | None] = mapped_column(sa.Text)
    """Pin this query to one backend, or ``NULL`` to use the configured default
    for its kind.  A pin whose provider is not configured does not fall through
    to another one -- the query simply does not run.  Silent fan-out to a second
    paid provider is the failure mode this column exists to make impossible."""

    interval_minutes: Mapped[int | None] = mapped_column(sa.Integer)
    """Per-query cadence override, or ``NULL`` to inherit the topic's.  Always
    subject to the configured floor, which is applied where the interval is
    *used* so an old row cannot go faster than the operator allowed."""

    result_limit: Mapped[int | None] = mapped_column(sa.Integer)
    """Per-query result ceiling, or ``NULL`` to inherit the topic's.  Honoured
    only as a reduction against the configured maximum."""

    priority: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="100")
    """Lower runs first when a day's budget covers only part of the backlog."""

    last_run_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_success_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    consecutive_failures: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    results_seen: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    credits_used: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    next_eligible_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    """The earliest instant this query may be searched again, written durably.

    Derived from ``last_run_at`` plus the effective interval on success, and
    from the failure cooldown on failure. Stored rather than recomputed so a
    process restart cannot reset a cooldown: the Phase 2 incident was
    partly a scheduler that ran every task once on start-up, and a cooldown
    that lives only in a Python object is a cooldown a restart pays for."""

    searches_performed: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, server_default="0"
    )
    """Lifetime count of paid searches issued for this query."""

    topic: Mapped[DiscoveryTopic] = relationship(back_populates="queries")

    __table_args__ = (
        sa.UniqueConstraint("topic_id", "query", name="uq_discovery_queries_topic_id_query"),
    )


class ProviderCall(UUIDPrimaryKeyMixin, Base):
    """One paid call to one metered provider, accounted durably.

    This table is every provider budget.  It exists because the Phase 2
    implementation counted Firecrawl credits in a Python integer and a
    Prometheus counter, neither of which survives a restart and neither of which
    can refuse a call -- so nothing in the system could have stopped 21 searches
    an hour from emptying the allowance.

    The row is inserted and **committed before the HTTP request**, in the same
    spirit as ``execution_attempts.sent_to_broker``: the honest question is not
    "what did we spend" but "what may we already have spent".  A call whose
    process died is therefore charged at its pre-call estimate rather than
    forgotten.  ``units_charged`` is the single column the budgets sum, so the
    reconciliation from estimate to whatever the provider itself reported
    happens in one place.

    Formerly ``firecrawl_calls``.  Renamed rather than replaced, and the rows
    from the Phase 2 incident are still here under ``provider = 'firecrawl'``:
    they are the only record of what it cost.
    """

    __tablename__ = "provider_calls"

    provider: Mapped[str] = mapped_column(sa.Text, nullable=False)
    """Which metered provider this row accounts for: ``brave``, ``exa``,
    ``firecrawl``.  Free text rather than an enum because adding a provider is a
    configuration change, not a migration -- and because the historical rows
    must keep naming a provider that is no longer a search backend."""

    kind: Mapped[ProviderCallKind] = mapped_column(
        pg_enum(ProviderCallKind, "provider_call_kind"), nullable=False
    )
    outcome: Mapped[ProviderCallOutcome] = mapped_column(
        pg_enum(ProviderCallOutcome, "provider_call_outcome"),
        nullable=False,
        default=ProviderCallOutcome.RESERVED,
        server_default=ProviderCallOutcome.RESERVED.value,
    )
    reserved_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    """Database clock, always. The daily and monthly windows are computed from
    the same clock that writes this, so a container with a skewed system time
    cannot widen its own budget window."""

    completed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    query_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("discovery_queries.id", ondelete="SET NULL")
    )
    topic_slug: Mapped[str | None] = mapped_column(sa.Text)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("sources.id", ondelete="SET NULL")
    )
    target_url: Mapped[str | None] = mapped_column(sa.Text)

    requested_limit: Mapped[int | None] = mapped_column(sa.Integer)
    requested_sources: Mapped[list[str]] = mapped_column(
        # A JSONB *array*, so the annotation says so. Several older columns in
        # this file are annotated ``JSONDict`` while holding a list, which
        # type-checks only because nothing reads them as one; a new column
        # should not repeat that.
        pg.JSONB,
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    )
    """Whatever the provider calls its result slices: Firecrawl's ``sources``,
    Brave's ``result_filter``, Exa's ``category``."""

    scrape_requested: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )

    units_reserved: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    """The worst case this call could cost in the provider's own billing unit,
    computed from its published model before the request left.  A unit is a
    Firecrawl credit, a Brave request or an Exa request -- never comparable
    across providers, which is why each has its own caps."""

    units_reported: Mapped[int | None] = mapped_column(sa.Integer)
    """What the provider said it charged, when it says anything."""

    units_charged: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    """What the budget counts: the provider's number when there is one and it is
    higher, the reservation otherwise.  Never lower than what was actually
    billed as far as StockBrain can tell."""

    cost_usd_reported: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 6))
    """The provider's own price for this call.  Exa returns ``costDollars``;
    Brave and Firecrawl report nothing per call and leave this NULL."""

    cost_usd_charged: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 6))
    """Charged cost in USD where the provider's price is knowable at all.  NULL
    for Firecrawl, which bills credits against a monthly allowance rather than
    dollars per call -- reporting an invented dollar figure there would be
    worse than reporting none."""

    results_returned: Mapped[int | None] = mapped_column(sa.Integer)
    pages_scraped: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    http_status: Mapped[int | None] = mapped_column(sa.Integer)
    error_category: Mapped[str | None] = mapped_column(sa.Text)
    """The exception class name, never a provider body: a provider error body
    can echo the request, and the request carries an Authorization header."""

    __table_args__ = (
        sa.Index("ix_provider_calls_reserved_at", "reserved_at"),
        sa.Index("ix_provider_calls_provider_reserved_at", "provider", "reserved_at"),
        sa.Index("ix_provider_calls_kind_reserved_at", "kind", "reserved_at"),
        sa.Index("ix_provider_calls_query_id", "query_id"),
        sa.CheckConstraint("units_reserved >= 0", name="units_reserved_non_negative"),
        sa.CheckConstraint("units_charged >= 0", name="units_charged_non_negative"),
        sa.CheckConstraint("pages_scraped >= 0", name="pages_scraped_non_negative"),
    )


class ProviderHealthRecord(TimestampMixin, Base):
    """Latest known health of each external dependency.

    A DOWN provider degrades its own subsystem only; unrelated functionality
    keeps running (spec section 24).
    """

    __tablename__ = "provider_health"

    provider: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    status: Mapped[ProviderStatus] = mapped_column(
        pg_enum(ProviderStatus, "provider_status"),
        nullable=False,
        default=ProviderStatus.UNKNOWN,
        server_default=ProviderStatus.UNKNOWN.value,
    )
    detail: Mapped[str | None] = mapped_column(sa.Text)
    last_ok_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_checked_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    metrics: Mapped[JSONDict] = mapped_column(nullable=False, server_default=sa.text("'{}'::jsonb"))


class Notification(UUIDPrimaryKeyMixin, Base):
    """Every outbound notification is a database row first.

    Telegram is a delivery channel, never the system of record.
    """

    __tablename__ = "notifications"

    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    notification_class: Mapped[NotificationClass] = mapped_column(
        pg_enum(NotificationClass, "notification_class"), nullable=False
    )
    channel: Mapped[str] = mapped_column(sa.Text, nullable=False)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        pg_enum(NotificationStatus, "notification_status"),
        nullable=False,
        default=NotificationStatus.PENDING,
        server_default=NotificationStatus.PENDING.value,
    )
    entity_type: Mapped[str | None] = mapped_column(sa.Text)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(as_uuid=True))
    sent_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    delivery_reference: Mapped[str | None] = mapped_column(sa.Text)
    error: Mapped[str | None] = mapped_column(sa.Text)
    dedupe_key: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.Index("ix_notifications_created_at", "created_at"),
        sa.Index("ix_notifications_status", "status"),
        sa.Index(
            "uq_notifications_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=sa.text("dedupe_key IS NOT NULL"),
        ),
    )


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Local web account.

    Authorization decisions are always made server-side from this row; the
    frontend never asserts its own privileges.
    """

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(sa.Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    is_owner: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    """Only an owner may approve trades or change execution settings."""

    telegram_user_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.Index(
            "uq_users_telegram_user_id",
            "telegram_user_id",
            unique=True,
            postgresql_where=sa.text("telegram_user_id IS NOT NULL"),
        ),
    )
