"""Application exception hierarchy.

Distinguishing *definite* failures from *ambiguous* ones is not cosmetic: it
decides whether an order may be retried.  See
:class:`DefinitePreSendFailure` and :class:`AmbiguousTransportFailure`.
"""

from __future__ import annotations

__all__ = [
    "AccountStateUnavailable",
    "AmbiguousTransportFailure",
    "AuthorizationNotPermitted",
    "BrokerError",
    "ConfigurationError",
    "DefinitePreSendFailure",
    "ExecutionNotPermitted",
    "InstrumentResolutionError",
    "InvalidProposalTransition",
    "ProposalAlreadyConsumed",
    "ProposalExpired",
    "ProposalInvalidated",
    "ProviderAuthError",
    "ProviderEntitlementError",
    "ProviderError",
    "ProviderRateLimited",
    "ProviderResponseError",
    "ProviderUnavailable",
    "RiskBlocked",
    "StockBrainError",
]


class StockBrainError(Exception):
    """Base class for every error raised deliberately by StockBrain."""


class ConfigurationError(StockBrainError):
    """The process is configured in a way that cannot be safely resolved."""


class ProviderError(StockBrainError):
    """An external provider failed in a way the caller should handle."""


class ProviderAuthError(ProviderError):
    """The provider rejected our credentials (HTTP 401/403, WS auth failure).

    Never retried: retrying a bad credential only burns rate limit and, for
    SEC EDGAR, earns an IP block.
    """


class ProviderEntitlementError(ProviderError):
    """Credentials are valid but the account lacks the required subscription.

    Alpaca returns this for a feed outside the account's plan.  The subsystem
    degrades; the application does not crash.
    """


class ProviderRateLimited(ProviderError):
    """The provider rate-limited us.  Carries the retry hint when one is given."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderUnavailable(ProviderError):
    """A transient provider failure (5xx, connect error, timeout).  Retryable."""


class ProviderResponseError(ProviderError):
    """The provider answered, but the payload did not match its documented schema.

    Raised rather than coerced: silently accepting an unexpected shape is how
    wrong data reaches the research pipeline.
    """


class InstrumentResolutionError(StockBrainError):
    """A company could not be resolved to a verified broker instrument.

    This must block proposal generation: an unresolved or low-confidence
    instrument may never reach an order request.
    """


class InvalidProposalTransition(StockBrainError):
    """An illegal proposal state transition was attempted."""

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"Illegal proposal transition {current} -> {target}")
        self.current = current
        self.target = target


class ProposalAlreadyConsumed(StockBrainError):
    """Another actor already took the final action on this proposal.

    Raised when a Telegram confirm and a web confirm race; the loser sees this
    and the API surfaces it as HTTP 409.
    """


class ProposalExpired(StockBrainError):
    """The proposal's TTL elapsed before confirmation."""


class ProposalInvalidated(StockBrainError):
    """A precondition the proposal was built on stopped being true.

    Raised when authorization is attempted against a proposal whose listing,
    account state, position or market has moved since it was generated.  The
    proposal is durably marked ``INVALIDATED`` rather than silently re-priced:
    re-pricing under the user's finger is how a person approves a trade they
    did not read.
    """


class RiskBlocked(StockBrainError):
    """The deterministic risk engine refused.

    Carries the blocking rule ids so the refusal is legible without re-reading
    the whole snapshot.  Nothing -- not research confidence, not an operator
    flag, not an automatic policy -- converts this into permission.
    """

    def __init__(self, message: str, *, rule_ids: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.rule_ids = rule_ids


class AccountStateUnavailable(StockBrainError):
    """Required broker account state is missing or stale.

    Sizing without a current cash and position picture is guessing, so this
    fails closed rather than falling back to the last known snapshot.
    """


class AuthorizationNotPermitted(StockBrainError):
    """Authorization was attempted through a route this deployment forbids.

    The automatic path raises this when the broker/environment does not
    advertise automation capability, which is a policy answer rather than a
    risk answer -- and there is deliberately no flag that bypasses it.
    """


class ExecutionNotPermitted(StockBrainError):
    """Execution is blocked by configuration, the kill switch, or risk."""


class BrokerError(ProviderError):
    """The broker returned a definitive error response."""


class DefinitePreSendFailure(BrokerError):
    """The request provably never reached the broker.

    Examples: DNS failure, TLS handshake failure, connection refused, a local
    validation error raised before the socket was written.  Only this class of
    failure permits creating a *new* execution attempt.
    """


class AmbiguousTransportFailure(BrokerError):
    """Bytes may have reached the broker but no complete response was received.

    Read timeouts, connection resets mid-request and truncated responses all
    land here.  The order may or may not exist.  StockBrain must never retry:
    the proposal enters ``EXECUTION_AMBIGUOUS`` and reconciliation decides.
    """
