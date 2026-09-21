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
    "TRANSIENT_RULE_IDS",
    "ActorType",
    "AliasType",
    "ApprovalChannel",
    "ApprovalStage",
    "AuthorizationSource",
    "BarTimeframe",
    "Broker",
    "CapabilityState",
    "ControlFlag",
    "EventSourceRelationship",
    "EventStatus",
    "ExecutionFailure",
    "ExecutionOutcome",
    "ExecutionPolicy",
    "ExtractionMethod",
    "ImpactDirection",
    "InstrumentSupport",
    "JobStatus",
    "JobType",
    "MarketSession",
    "NotificationClass",
    "NotificationEvent",
    "NotificationStatus",
    "OrderSide",
    "OrderType",
    "OutcomeCheckpoint",
    "OutcomeStatus",
    "PriceSource",
    "ProposalStatus",
    "ProviderCallKind",
    "ProviderCallOutcome",
    "ProviderStatus",
    "ReactionStatus",
    "ReconciliationResult",
    "ResearchStatus",
    "ResolutionMethod",
    "ResolutionStatus",
    "RiskOutcome",
    "RuleOutcome",
    "SourceCategory",
    "SourceProvider",
    "SpreadStatus",
    "ThesisAction",
    "TimeHorizon",
    "WebDiscoveryKind",
    "WebDiscoveryProviderName",
]


class SourceProvider(StrEnum):
    """Which discovery provider produced a source row.

    ``FIRECRAWL`` is retained because rows discovered by the Phase 2-9 Firecrawl
    search still exist and are still valid evidence.  Firecrawl is no longer a
    discovery provider -- it is a fallback *extractor* -- but rewriting those
    rows to claim Brave or Exa found them would be falsifying provenance.
    """

    ALPACA = "ALPACA"
    BRAVE = "BRAVE"
    EXA = "EXA"
    FIRECRAWL = "FIRECRAWL"
    SEC = "SEC"
    MANUAL = "MANUAL"
    # Keyless non-US disclosure wires.  Each is its own provenance value; there
    # is deliberately no generic "DISCLOSURE" member.
    INVESTEGATE = "INVESTEGATE"
    EQS = "EQS"
    CNMV = "CNMV"
    GLOBENEWSWIRE = "GLOBENEWSWIRE"
    ACTUSNEWS = "ACTUSNEWS"


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


class OutcomeStatus(StrEnum):
    """Lifecycle of a graded thesis outcome (spec §4.1)."""

    PENDING = "PENDING"
    CLOSED = "CLOSED"
    ABANDONED = "ABANDONED"


class OutcomeCheckpoint(StrEnum):
    """When an outcome is graded: N trading days after entry, or on close."""

    D1 = "D1"
    D5 = "D5"
    D20 = "D20"
    D60 = "D60"
    CLOSE = "CLOSE"

    @property
    def trading_days(self) -> int | None:
        """The day count a checkpoint waits for; ``None`` for CLOSE."""
        return None if self is OutcomeCheckpoint.CLOSE else int(self.value[1:])


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
    INVALIDATED = "INVALIDATED"
    """A precondition the proposal was built on stopped being true.

    Distinct from ``EXPIRED`` (the clock ran out), ``REJECTED`` (a human said
    no) and ``CANCELLED`` (someone withdrew it): the proposal was still within
    its TTL and nobody acted on it, but the listing, the position, the account
    or the market moved out from under the numbers it carries."""


class ExecutionPolicy(StrEnum):
    """Who is expected to authorize a proposal.

    Recorded on the proposal row at creation, so flipping the deployment's
    policy can never retroactively authorize work that was generated under the
    other one.  This is orthogonal to :class:`~stockbrain.config.ExecutionMode`,
    which gates *transmission* to the broker and is unchanged by this phase.
    """

    MANUAL = "MANUAL"
    AUTOMATIC = "AUTOMATIC"


class AuthorizationSource(StrEnum):
    """What authorized a proposal.

    Human approval is one source among several rather than the definition of
    authorization, so an automatic deployment and a manual one converge on the
    same ``APPROVED`` representation and differ only in provenance.
    """

    HUMAN_WEB = "HUMAN_WEB"
    HUMAN_TELEGRAM = "HUMAN_TELEGRAM"
    SYSTEM_AUTOMATIC = "SYSTEM_AUTOMATIC"

    @property
    def is_human(self) -> bool:
        return self in {AuthorizationSource.HUMAN_WEB, AuthorizationSource.HUMAN_TELEGRAM}

    @property
    def channel(self) -> ApprovalChannel | None:
        """The legacy human channel, or ``None`` for a system authorization."""
        if self is AuthorizationSource.HUMAN_WEB:
            return ApprovalChannel.WEB
        if self is AuthorizationSource.HUMAN_TELEGRAM:
            return ApprovalChannel.TELEGRAM
        return None


class RiskOutcome(StrEnum):
    """The deterministic risk engine's verdict for a whole evaluation."""

    ALLOW = "ALLOW"
    REDUCE_SIZE = "REDUCE_SIZE"
    BLOCK = "BLOCK"


class RuleOutcome(StrEnum):
    """One rule's verdict.

    ``WARN`` is visible and recorded but changes nothing; ``REDUCE`` lowers the
    permitted size; ``BLOCK`` is absolute and no other signal can lift it.
    """

    PASS = "PASS"  # noqa: S105 - a rule verdict, not a credential
    WARN = "WARN"
    REDUCE = "REDUCE"
    BLOCK = "BLOCK"


class SpreadStatus(StrEnum):
    """Why a bid/ask pair is or is not usable as an execution reference.

    Every abnormal book shape gets its own name rather than collapsing into
    "no spread", because they have different causes and an operator reading a
    blocked proposal needs to know which one happened.
    """

    OK = "OK"
    MISSING = "MISSING"
    """One or both sides absent from the quote entirely."""

    NON_POSITIVE = "NON_POSITIVE"
    """A side was present but zero or negative.  Alpaca documents ``0`` as
    "no active bid/ask" rather than a price of zero."""

    ONE_SIDED = "ONE_SIDED"
    """Exactly one side is live.  There is no mid, so there is no reference."""

    CROSSED = "CROSSED"
    """``ask < bid``.  A book in this state is mid-update or mid-halt."""

    LOCKED = "LOCKED"
    """``ask == bid``.  A zero-width two-sided quote in an equity is an
    anomaly, not free liquidity, so it is refused rather than celebrated."""

    EXCESSIVE = "EXCESSIVE"
    """A well-formed book whose relative spread exceeds the configured ceiling.
    A live overnight IEX quote showed a $33 spread on a $321 AAPL mid -- a 10%
    round trip -- which the age check happened to catch.  A book that wide
    *during* regular hours would pass an age check and still be ruinous."""


class ApprovalChannel(StrEnum):
    WEB = "WEB"
    TELEGRAM = "TELEGRAM"


class ApprovalStage(StrEnum):
    """What a single-use approval token permits its holder to ask for.

    The stage is stored on the server-side ``approval_actions`` row, never in
    the Telegram callback payload, so the *action* a button performs is decided
    by the database rather than by the bytes Telegram hands back.
    """

    APPROVE = "APPROVE"
    """Stage one of the two-stage confirmation: opens a confirmation, authorizes
    nothing."""

    CONFIRM = "CONFIRM"
    """Stage two.  Must descend from an ``APPROVE`` action on the same proposal,
    and is the only stage that reaches ``ProposalService.authorize``."""

    REJECT = "REJECT"
    """A durable refusal.  One stage, because refusing is the safe direction."""

    DETAILS = "DETAILS"
    """A read-only expansion of a proposal the holder may already list.  Still
    single-use, still bound to a user and chat: a read token that outlived its
    message would be one more thing to reason about for no benefit."""


class ExecutionOutcome(StrEnum):
    """Result of a single broker submission attempt.

    ``FAILED_BEFORE_SEND`` is the only outcome from which a *new* attempt may be
    created, because it proves no bytes reached the broker.  ``AMBIGUOUS`` must
    be resolved by reconciliation or by the operator, never by resending.

    These four questions are all different and are kept apart on purpose:
    *did we try* (``PENDING``), *did the bytes leave* (``FAILED_BEFORE_SEND``),
    *did the broker answer* (``SUBMITTED`` / ``REJECTED_BY_BROKER``), and *do we
    know* (``AMBIGUOUS``).  Collapsing any two of them is how a system decides
    to resend an order that already exists.
    """

    PENDING = "PENDING"
    """An attempt exists and its result is not yet recorded.  Found in this
    state after a crash, it is treated as ``AMBIGUOUS``, never as "not sent"."""

    FAILED_BEFORE_SEND = "FAILED_BEFORE_SEND"
    """Proof that nothing reached the broker: a refused preflight, a local rate
    limiter denial, a DNS or connect failure.  The only outcome that permits
    another attempt."""

    SUBMITTED = "SUBMITTED"
    REJECTED_BY_BROKER = "REJECTED_BY_BROKER"
    AMBIGUOUS = "AMBIGUOUS"
    RECONCILED_FILLED = "RECONCILED_FILLED"
    RECONCILED_NOT_PLACED = "RECONCILED_NOT_PLACED"

    @property
    def is_terminal(self) -> bool:
        """Whether the attempt still needs work from a sweep or an operator."""
        return self in {
            ExecutionOutcome.FAILED_BEFORE_SEND,
            ExecutionOutcome.SUBMITTED,
            ExecutionOutcome.REJECTED_BY_BROKER,
            ExecutionOutcome.RECONCILED_FILLED,
            ExecutionOutcome.RECONCILED_NOT_PLACED,
        }


class ExecutionFailure(StrEnum):
    """Why a transmission attempt did not end in a confirmed broker order.

    Recorded as a *category* rather than as provider text, because a
    python-telegram-bot- or httpx-shaped message can carry a URL, and a Trading
    212 URL carries nothing secret but a provider body can echo a request.  The
    category is what an operator triages on and what a metric aggregates.
    """

    PREFLIGHT_REFUSED = "PREFLIGHT_REFUSED"
    """A pre-send check said no.  Nothing was transmitted."""

    RATE_LIMITED_LOCALLY = "RATE_LIMITED_LOCALLY"
    """StockBrain's own token bucket declined before any socket was opened."""

    CONNECT_FAILED = "CONNECT_FAILED"
    """DNS, TLS or TCP failed before a request line was written."""

    BROKER_REJECTED = "BROKER_REJECTED"
    """HTTP 400: the broker validated the request and refused it."""

    BROKER_AUTH_REJECTED = "BROKER_AUTH_REJECTED"
    """HTTP 401/403.  403 specifically means the API key lacks the documented
    ``orders:execute`` scope, which is a configuration fault, not a market one."""

    BROKER_RATE_LIMITED = "BROKER_RATE_LIMITED"
    """HTTP 429.  Treated as ambiguous: Trading 212 does not document whether
    the limiter runs before or after order acceptance."""

    BROKER_TIMEOUT = "BROKER_TIMEOUT"
    """HTTP 408, which the broker documents for this endpoint.  The server timed
    out; whether it timed out before or after creating the order is unknown."""

    TRANSPORT_AMBIGUOUS = "TRANSPORT_AMBIGUOUS"
    """The request was written but no complete response arrived."""

    UNREADABLE_SUCCESS = "UNREADABLE_SUCCESS"
    """The broker answered 2xx with a body StockBrain could not parse.  An order
    almost certainly exists and its id is unknown -- the most dangerous shape of
    all, and the reason a malformed success is ambiguous rather than failed."""

    UNEXPECTED_STATUS = "UNEXPECTED_STATUS"
    """A status the documentation does not list.  Ambiguous by default."""

    CRASH_RECOVERY = "CRASH_RECOVERY"
    """The process died between recording the send and recording the result."""

    PENDING_ORDER_LIMIT = "PENDING_ORDER_LIMIT"
    """The broker's per-ticker pending-order queue is full, or could not be
    read.  Nothing was transmitted.

    Its own category rather than a generic ``PREFLIGHT_REFUSED`` because the
    remedy is completely different: nothing about the trade is wrong and the
    authorization is still good -- the queue has to drain, or the operator has
    to cancel something in the app.  Aggregating it with a risk refusal would
    hide a condition that resolves on its own."""


class ReconciliationResult(StrEnum):
    """What a reconciliation pass concluded.

    ``INCONCLUSIVE`` is a first-class answer.  The alternative -- guessing --
    means either resending an order that exists or releasing the reservation for
    one that does.
    """

    ORDER_FOUND = "ORDER_FOUND"
    ORDER_NOT_PLACED = "ORDER_NOT_PLACED"
    INCONCLUSIVE = "INCONCLUSIVE"
    MULTIPLE_CANDIDATES = "MULTIPLE_CANDIDATES"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"


class ActorType(StrEnum):
    SYSTEM = "SYSTEM"
    USER = "USER"
    LLM = "LLM"
    BROKER = "BROKER"


class ProviderStatus(StrEnum):
    """How a single external dependency is currently behaving.

    ``BUDGET_EXHAUSTED`` is separate from ``DEGRADED`` on purpose.  Both mean
    "this provider is not doing work right now", but the operator's next action
    differs completely: a degraded provider is a fault to investigate, an
    exhausted one is a spending limit doing exactly its job.  Phase 9 reported
    the second as the first, and no panel could tell them apart.
    """

    HEALTHY = "HEALTHY"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
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
    EXECUTE_PROPOSAL = "EXECUTE_PROPOSAL"
    RECONCILE_EXECUTION = "RECONCILE_EXECUTION"
    RESOLVE_CANDIDATES = "RESOLVE_CANDIDATES"
    RUN_RESEARCH = "RUN_RESEARCH"
    GENERATE_PROPOSAL = "GENERATE_PROPOSAL"
    REASSESS_POSITION = "REASSESS_POSITION"
    SEND_NOTIFICATION = "SEND_NOTIFICATION"
    BROKER_RECONCILE = "BROKER_RECONCILE"
    WEB_DISCOVERY_SEARCH = "WEB_DISCOVERY_SEARCH"
    """One stored discovery query, run against whichever provider it names.

    Replaces ``FIRECRAWL_TOPIC_SEARCH``.  The provider is payload and
    configuration, not job identity, so adding a fourth search backend never
    needs a fourth job type -- and the scheduler protections that stop a paid
    call being made twice are written once.
    """

    CONTENT_EXTRACT = "CONTENT_EXTRACT"
    """Fetch and extract the body of one already-triaged source.

    Replaces ``FIRECRAWL_ENRICH``.  Local extraction first; the paid Firecrawl
    scrape is a fallback inside this handler, not a job type of its own, because
    "which extractor ran" is an outcome rather than a plan.
    """

    SEC_REFRESH = "SEC_REFRESH"
    DISCLOSURE_FEED_POLL = "DISCLOSURE_FEED_POLL"
    """One disclosure feed's walk-until-known poll; the feed name is payload."""
    INSTRUMENT_REFRESH = "INSTRUMENT_REFRESH"
    EXPIRE_PROPOSALS = "EXPIRE_PROPOSALS"
    PROVIDER_HEALTH_CHECK = "PROVIDER_HEALTH_CHECK"


class ProviderCallKind(StrEnum):
    """Which paid operation a ledger row accounts for.

    Two kinds, because they bill differently and are capped separately: a
    *search* returns metadata for many results, a *scrape* fetches one page.
    Every metered provider maps onto one of the two -- Brave and Exa only ever
    SEARCH, Firecrawl now only ever SCRAPE.
    """

    SEARCH = "SEARCH"
    SCRAPE = "SCRAPE"


class ProviderCallOutcome(StrEnum):
    """How a paid-call ledger row ended.

    ``RESERVED`` is written and committed *before* the HTTP request, exactly as
    ``execution_attempts.sent_to_broker`` is, so a process that dies mid-call
    leaves an over-estimate rather than an unaccounted spend. The budget counts
    a ``RESERVED`` row at its reserved estimate; it never assumes a call that
    vanished was free.
    """

    RESERVED = "RESERVED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class WebDiscoveryKind(StrEnum):
    """What a stored discovery query is *for*, which decides who runs it.

    ``ROUTINE`` is a conventional recent-news/thematic search: keyword-shaped,
    frequent, cheap, and answered well by a classical index.  ``SEMANTIC`` is
    second-order discovery -- "who benefits from a transformer shortage" -- which
    a keyword index answers badly and an embedding index answers well, at
    roughly ten times the price per call.

    Keeping them as distinct kinds rather than one query list with a provider
    column is what stops the expensive one being run on the cheap one's cadence.
    """

    ROUTINE = "ROUTINE"
    SEMANTIC = "SEMANTIC"


class WebDiscoveryProviderName(StrEnum):
    """Search backends StockBrain knows how to talk to.

    ``NONE`` is a real, safe setting: the kind it is configured for simply does
    not run, and every other kind carries on.
    """

    NONE = "none"
    BRAVE = "brave"
    EXA = "exa"


class ExtractionMethod(StrEnum):
    """How a source's body was obtained.

    Recorded on the row because "did this cost money" must be answerable after
    the fact, and because a page that local extraction handled must never be
    offered to a paid extractor a second time.
    """

    PROVIDER = "PROVIDER"
    """The discovery provider delivered the body itself (Alpaca, SEC)."""

    LOCAL = "LOCAL"
    """Fetched over plain HTTP and extracted locally.  Free."""

    FIRECRAWL = "FIRECRAWL"
    """Paid fallback, used only after local extraction failed on a shortlisted
    URL."""

    NONE = "NONE"
    """Extraction was attempted and produced nothing usable.  Recorded so the
    attempt is not repeated."""


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


class NotificationEvent(StrEnum):
    """The proposal transitions StockBrain announces.

    One member per *transition*, not per message: the value is half of the
    ``notifications.dedupe_key``, which is what makes "one notification per
    proposal transition" a unique-index guarantee rather than a hopeful check
    that a redelivered job or a restart could defeat.
    """

    PROPOSAL_MANUAL = "PROPOSAL_MANUAL"
    """A proposal awaiting human authorization became available."""

    PROPOSAL_AUTO_AUTHORIZED = "PROPOSAL_AUTO_AUTHORIZED"
    """``SYSTEM_AUTOMATIC`` authorized a proposal.  Announced without an approve
    button: offering one would imply a human decision that was never asked for."""

    PROPOSAL_REJECTED = "PROPOSAL_REJECTED"
    PROPOSAL_INVALIDATED = "PROPOSAL_INVALIDATED"
    PROPOSAL_EXPIRED = "PROPOSAL_EXPIRED"
    AUTHORIZATION_REFUSED = "AUTHORIZATION_REFUSED"
    """A fresh risk revalidation refused an authorization somebody asked for."""

    EXECUTION_SUBMITTED = "EXECUTION_SUBMITTED"
    """An order reached the broker and the broker acknowledged it."""

    EXECUTION_REJECTED = "EXECUTION_REJECTED"
    """The broker answered and refused.  Definitive: no order exists."""

    EXECUTION_FAILED = "EXECUTION_FAILED"
    """The attempt failed before anything could reach the broker."""

    EXECUTION_AMBIGUOUS = "EXECUTION_AMBIGUOUS"
    """CRITICAL. The order may or may not exist. Reconciliation decides, and
    nothing may be resent until it does."""

    EXECUTION_RECONCILED = "EXECUTION_RECONCILED"
    """Reconciliation resolved an ambiguous attempt one way or the other."""

    EXECUTION_CONFIRMED = "EXECUTION_CONFIRMED"
    """The broker reports the order filled.  The position is now real."""


class ControlFlag(StrEnum):
    """Durable execution-control state, persisted in ``app_settings``.

    Kept out of process memory on purpose: a kill switch that a restart clears
    is not a kill switch.  See :mod:`stockbrain.control.state`.
    """

    TRADING_PAUSED = "control.trading_paused"
    KILL_SWITCH = "control.kill_switch"


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

#: Rules that can refuse a trade over *market state* -- a shut session, a stale
#: or absent quote, an account snapshot not yet taken, a missing FX rate.  These
#: describe the moment the evaluation happened, not the trade, so a refusal they
#: are solely responsible for is transient: it is recorded and retried rather
#: than treated as final.
#:
#: The definition is deliberately by rule id, and deliberately *never* judgment.
#: A rule that judges the trade itself -- its confidence, its currency
#: alignment, a concentration or cash-reserve cap, a duplicate proposal -- does
#: not fix itself by waiting, and putting one of those in this set would turn
#: "retry at the next open" into an unbounded loop.  The unit test beside the
#: risk rule set pins both directions.
#:
#: It lives in this dependency-free vocabulary module rather than in
#: ``stockbrain.risk.rules`` because the Telegram read model has to classify a
#: refusal to split its message, and Telegram is deliberately forbidden from
#: importing the risk engine at all.  ``stockbrain.risk.rules`` re-exports it, so
#: its canonical risk-side name is unchanged.
TRANSIENT_RULE_IDS: frozenset[str] = frozenset(
    {
        "price_source_execution_grade",
        "quote_available",
        "quote_freshness",
        "quote_two_sided",
        "spread_ceiling",
        "market_session",
        "account_state_available",
        "account_state_freshness",
        "fx_available",
        "fx_freshness",
    }
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
