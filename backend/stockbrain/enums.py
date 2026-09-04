"""Shared domain vocabulary.

These enums are the single source of truth for the values persisted in
PostgreSQL native enum types and exposed over the REST API.  They live at the
package root so that the risk engine, broker adapters, proposal service and
database layer all agree without importing each other.

Renaming a member is a migration-affecting change: the PostgreSQL type must be
altered in lockstep.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ActorType",
    "ApprovalChannel",
    "ApprovalStage",
    "Broker",
    "EventSourceRelationship",
    "EventStatus",
    "ExecutionOutcome",
    "ImpactDirection",
    "JobStatus",
    "JobType",
    "NotificationClass",
    "NotificationStatus",
    "OrderSide",
    "OrderType",
    "PriceSource",
    "ProposalStatus",
    "ProviderStatus",
    "ResearchStatus",
    "SourceProvider",
    "ThesisAction",
    "TimeHorizon",
]


class SourceProvider(StrEnum):
    ALPACA = "ALPACA"
    FIRECRAWL = "FIRECRAWL"
    SEC = "SEC"
    MANUAL = "MANUAL"


class EventStatus(StrEnum):
    """Lifecycle of a canonical event, from raw ingestion to research."""

    NEW = "NEW"
    CLASSIFYING = "CLASSIFYING"
    CLASSIFIED = "CLASSIFIED"
    CLASSIFICATION_FAILED = "CLASSIFICATION_FAILED"
    IRRELEVANT = "IRRELEVANT"
    CANDIDATE = "CANDIDATE"
    RESEARCHING = "RESEARCHING"
    RESEARCHED = "RESEARCHED"
    ARCHIVED = "ARCHIVED"


class EventSourceRelationship(StrEnum):
    PRIMARY = "PRIMARY"
    CORROBORATING = "CORROBORATING"
    UPDATE = "UPDATE"
    CONTRADICTING = "CONTRADICTING"


class ImpactDirection(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class ResearchStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class ThesisAction(StrEnum):
    BUY = "BUY"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    SELL = "SELL"
    NO_ACTION = "NO_ACTION"


class TimeHorizon(StrEnum):
    INTRADAY = "intraday"
    DAYS = "days"
    WEEKS = "weeks"
    MONTHS = "months"


class Broker(StrEnum):
    TRADING212 = "TRADING212"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class ProposalStatus(StrEnum):
    """See :mod:`stockbrain.proposals.state_machine` for the legal transitions."""

    DRAFT = "DRAFT"
    READY = "READY"
    NOTIFIED = "NOTIFIED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    EXECUTION_AMBIGUOUS = "EXECUTION_AMBIGUOUS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ApprovalChannel(StrEnum):
    WEB = "WEB"
    TELEGRAM = "TELEGRAM"


class ApprovalStage(StrEnum):
    APPROVE = "APPROVE"
    CONFIRM = "CONFIRM"


class ExecutionOutcome(StrEnum):
    """Result of a single broker submission attempt.

    ``FAILED_BEFORE_SEND`` is the only outcome from which a *new* attempt may be
    created, because it proves no bytes reached the broker.  ``AMBIGUOUS`` must
    be resolved by reconciliation or by the operator, never by resending.
    """

    PENDING = "PENDING"
    FAILED_BEFORE_SEND = "FAILED_BEFORE_SEND"
    SUBMITTED = "SUBMITTED"
    REJECTED_BY_BROKER = "REJECTED_BY_BROKER"
    AMBIGUOUS = "AMBIGUOUS"
    RECONCILED_FILLED = "RECONCILED_FILLED"
    RECONCILED_NOT_PLACED = "RECONCILED_NOT_PLACED"


class ActorType(StrEnum):
    SYSTEM = "SYSTEM"
    USER = "USER"
    LLM = "LLM"
    BROKER = "BROKER"


class ProviderStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    DISABLED = "DISABLED"
    UNKNOWN = "UNKNOWN"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobType(StrEnum):
    """Job types are stored as free text in the database (``jobs.job_type``) so a
    new handler can be deployed without a migration; this enum documents the
    known set and is used by the dispatcher."""

    CLASSIFY_EVENT = "CLASSIFY_EVENT"
    RESOLVE_CANDIDATES = "RESOLVE_CANDIDATES"
    RUN_RESEARCH = "RUN_RESEARCH"
    GENERATE_PROPOSAL = "GENERATE_PROPOSAL"
    REASSESS_POSITION = "REASSESS_POSITION"
    SEND_NOTIFICATION = "SEND_NOTIFICATION"
    BROKER_RECONCILE = "BROKER_RECONCILE"
    FIRECRAWL_TOPIC_SEARCH = "FIRECRAWL_TOPIC_SEARCH"
    SEC_REFRESH = "SEC_REFRESH"
    INSTRUMENT_REFRESH = "INSTRUMENT_REFRESH"
    EXPIRE_PROPOSALS = "EXPIRE_PROPOSALS"
    PROVIDER_HEALTH_CHECK = "PROVIDER_HEALTH_CHECK"


class NotificationClass(StrEnum):
    CRITICAL = "CRITICAL"
    PROPOSAL = "PROPOSAL"
    PORTFOLIO_EVENT = "PORTFOLIO_EVENT"
    SYSTEM_WARNING = "SYSTEM_WARNING"
    DAILY_SUMMARY = "DAILY_SUMMARY"


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SUPPRESSED = "SUPPRESSED"


class PriceSource(StrEnum):
    """Provenance of a reference price.

    Trading 212 API market data is documented as *not* real-time, so
    ``BROKER_T212`` must never be used as the sole pre-trade price source.
    """

    ALPACA_IEX = "ALPACA_IEX"
    ALPACA_SIP = "ALPACA_SIP"
    ALPACA_DELAYED_SIP = "ALPACA_DELAYED_SIP"
    YFINANCE = "YFINANCE"
    BROKER_T212 = "BROKER_T212"
    MANUAL = "MANUAL"
