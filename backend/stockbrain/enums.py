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
    "EXECUTION_GRADE_PRICE_SOURCES",
    "ActorType",
    "AliasType",
    "ApprovalChannel",
    "ApprovalStage",
    "BarTimeframe",
    "Broker",
    "CapabilityState",
    "EventSourceRelationship",
    "EventStatus",
    "ExecutionOutcome",
    "ImpactDirection",
    "InstrumentSupport",
    "JobStatus",
    "JobType",
    "MarketSession",
    "NotificationClass",
    "NotificationStatus",
    "OrderSide",
    "OrderType",
    "PriceSource",
    "ProposalStatus",
    "ProviderStatus",
    "ReactionStatus",
    "ResearchStatus",
    "ResolutionMethod",
    "ResolutionStatus",
    "SourceCategory",
    "SourceProvider",
    "ThesisAction",
    "TimeHorizon",
]


class SourceProvider(StrEnum):
    ALPACA = "ALPACA"
    FIRECRAWL = "FIRECRAWL"
    SEC = "SEC"
    MANUAL = "MANUAL"


class SourceCategory(StrEnum):
    """Transparent trust category for a source.

    Kept as data on the row rather than as editorial scoring inside an LLM
    prompt, so the ranking is inspectable and adjustable (spec section 37).
    ``UNKNOWN`` sources must not independently trigger a high-confidence
    proposal without corroboration.
    """

    REGULATOR = "REGULATOR"
    ISSUER = "ISSUER"
    GOVERNMENT = "GOVERNMENT"
    NEWSWIRE = "NEWSWIRE"
    PRESS = "PRESS"
    UNKNOWN = "UNKNOWN"


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


class ResolutionStatus(StrEnum):
    """Outcome of resolving a classifier company hint to a broker instrument.

    Only ``RESOLVED`` may ever be used to reach an order request.  Every other
    value blocks progression, and ``AMBIGUOUS`` blocks it *loudly*: two
    plausible listings is a correctness problem to be settled by a human or a
    curated alias, never by picking one.
    """

    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"
    UNSUPPORTED = "UNSUPPORTED"


class ResolutionMethod(StrEnum):
    """How a resolution was reached, strongest evidence first.

    The order of the members is the order the resolver tries them, and the
    order a reviewer should trust them in.
    """

    ISIN_EXACT = "ISIN_EXACT"
    MANUAL_ALIAS = "MANUAL_ALIAS"
    TICKER_EXCHANGE = "TICKER_EXCHANGE"
    NAME_EXCHANGE_CURRENCY = "NAME_EXCHANGE_CURRENCY"
    HEURISTIC_CANDIDATE = "HEURISTIC_CANDIDATE"
    """Generates candidates for review.  Never resolves an executable instrument."""

    NONE = "NONE"


class AliasType(StrEnum):
    """What kind of name a curated alias records.

    ``LISTING`` aliases are scoped to an exchange/currency, which is what makes
    "Alphabet A shares" a different mapping from "Alphabet C shares" without
    creating a cross-listing ambiguity for the bare name "Alphabet".
    """

    LEGAL = "LEGAL"
    COMMON = "COMMON"
    HISTORICAL = "HISTORICAL"
    TICKER = "TICKER"
    LISTING = "LISTING"


class InstrumentSupport(StrEnum):
    """Whether StockBrain will ever price and size this instrument type."""

    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"


class MarketSession(StrEnum):
    """Trading session an instant falls in, for an instrument's own exchange."""

    PRE_MARKET = "PRE_MARKET"
    REGULAR = "REGULAR"
    AFTER_HOURS = "AFTER_HOURS"
    OVERNIGHT = "OVERNIGHT"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


class ReactionStatus(StrEnum):
    """Whether an event's price reaction could be computed, and why not."""

    OK = "OK"
    NO_PRICE_AT_EVENT = "NO_PRICE_AT_EVENT"
    NO_CURRENT_PRICE = "NO_CURRENT_PRICE"
    NO_DATA = "NO_DATA"
    UNSUPPORTED = "UNSUPPORTED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


class CapabilityState(StrEnum):
    """Result of actively probing what a configured provider can reach.

    Deliberately finer-grained than :class:`ProviderStatus`, which is the
    persisted health vocabulary: "the credentials work but the plan does not
    cover this feed" and "the credentials are wrong" both degrade a subsystem,
    but only one of them is fixed by paying for a subscription.
    """

    HEALTHY = "HEALTHY"
    AUTH_FAILED = "AUTH_FAILED"
    ENTITLEMENT_MISSING = "ENTITLEMENT_MISSING"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    DISABLED = "DISABLED"
    UNKNOWN = "UNKNOWN"

    def to_provider_status(self) -> ProviderStatus:
        """Map onto the persisted health vocabulary.

        A missing entitlement degrades rather than downs the subsystem: IEX may
        still answer when SIP does not, and discovery, classification and
        resolution are all unaffected either way.
        """
        return _CAPABILITY_TO_PROVIDER_STATUS[self]


class BarTimeframe(StrEnum):
    """Bar aggregation windows StockBrain requests.

    Values are Alpaca's documented ``timeframe`` strings.  A provider that
    spells them differently translates at its own boundary.
    """

    MIN_1 = "1Min"
    MIN_5 = "5Min"
    MIN_15 = "15Min"
    HOUR_1 = "1Hour"
    DAY_1 = "1Day"


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


#: Price sources that may be used to size a real order.  Trading 212's own API
#: data is *not* here: its API Terms do not guarantee it is real-time, so it is
#: display and reconciliation only (spec section 12).  yfinance is research
#: data, not execution data.
EXECUTION_GRADE_PRICE_SOURCES: frozenset[PriceSource] = frozenset(
    {PriceSource.ALPACA_SIP, PriceSource.ALPACA_IEX}
)

_CAPABILITY_TO_PROVIDER_STATUS: dict[CapabilityState, ProviderStatus] = {
    CapabilityState.HEALTHY: ProviderStatus.HEALTHY,
    CapabilityState.AUTH_FAILED: ProviderStatus.DOWN,
    CapabilityState.ENTITLEMENT_MISSING: ProviderStatus.DEGRADED,
    CapabilityState.DEGRADED: ProviderStatus.DEGRADED,
    CapabilityState.DOWN: ProviderStatus.DOWN,
    CapabilityState.DISABLED: ProviderStatus.DISABLED,
    CapabilityState.UNKNOWN: ProviderStatus.UNKNOWN,
}
