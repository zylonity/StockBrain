"""System-level tables: audit, jobs, settings, discovery topics, health, users.

The job queue is PostgreSQL-backed (``FOR UPDATE SKIP LOCKED``); there is no
Redis in this system.
"""

from __future__ import annotations

import datetime as dt
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stockbrain.db.base import Base, JSONDict, TimestampMixin, UUIDPrimaryKeyMixin
from stockbrain.db.models._types import pg_enum
from stockbrain.enums import (
    ActorType,
    JobStatus,
    NotificationClass,
    NotificationStatus,
    ProviderStatus,
)

__all__ = [
    "AppSetting",
    "AuditLog",
    "DiscoveryQuery",
    "DiscoveryTopic",
    "Job",
    "Notification",
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
    freshness: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="qdr:d")
    """Firecrawl ``tbs`` freshness token."""

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
    last_run_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_success_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    consecutive_failures: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    results_seen: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    credits_used: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")

    topic: Mapped[DiscoveryTopic] = relationship(back_populates="queries")

    __table_args__ = (
        sa.UniqueConstraint("topic_id", "query", name="uq_discovery_queries_topic_id_query"),
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
