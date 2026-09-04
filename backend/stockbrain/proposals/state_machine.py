"""The trade-proposal state machine.

Both the web GUI and the Telegram bot mutate proposals exclusively through this
machine, so the two clients cannot diverge.  Illegal transitions raise rather
than being coerced.

Notable rules:

* ``EXECUTING`` never returns to ``APPROVED``.  Once the execution transaction
  has started, the only exits are ``EXECUTED``, ``FAILED`` or
  ``EXECUTION_AMBIGUOUS``.
* ``EXECUTION_AMBIGUOUS`` is not terminal, but it is only left by
  *reconciliation* discovering the truth at the broker -- never by resending.
* ``EXPIRED`` is reachable from every pre-execution state, and from nowhere
  after execution has begun.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from stockbrain.enums import ProposalStatus
from stockbrain.errors import InvalidProposalTransition

__all__ = [
    "ALLOWED_TRANSITIONS",
    "PRE_EXECUTION_STATUSES",
    "TERMINAL_STATUSES",
    "assert_transition",
    "can_transition",
    "is_terminal",
]

S = ProposalStatus

_TRANSITIONS: Final[dict[ProposalStatus, frozenset[ProposalStatus]]] = {
    S.DRAFT: frozenset({S.READY, S.CANCELLED, S.FAILED}),
    S.READY: frozenset({S.NOTIFIED, S.APPROVAL_PENDING, S.REJECTED, S.EXPIRED, S.CANCELLED}),
    S.NOTIFIED: frozenset({S.APPROVAL_PENDING, S.REJECTED, S.EXPIRED, S.CANCELLED}),
    # Returning to NOTIFIED covers the user backing out of the confirmation
    # dialog without rejecting the proposal outright.
    S.APPROVAL_PENDING: frozenset(
        {S.APPROVED, S.NOTIFIED, S.REJECTED, S.EXPIRED, S.CANCELLED, S.FAILED}
    ),
    S.APPROVED: frozenset({S.EXECUTING, S.EXPIRED, S.CANCELLED, S.FAILED}),
    S.EXECUTING: frozenset({S.EXECUTED, S.FAILED, S.EXECUTION_AMBIGUOUS}),
    S.EXECUTION_AMBIGUOUS: frozenset({S.EXECUTED, S.FAILED, S.CANCELLED}),
    S.EXECUTED: frozenset(),
    S.REJECTED: frozenset(),
    S.EXPIRED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}

ALLOWED_TRANSITIONS: Final[MappingProxyType[ProposalStatus, frozenset[ProposalStatus]]] = (
    MappingProxyType(_TRANSITIONS)
)

TERMINAL_STATUSES: Final[frozenset[ProposalStatus]] = frozenset(
    status for status, targets in _TRANSITIONS.items() if not targets
)

#: States in which no broker mutation has been attempted, so expiry is safe.
PRE_EXECUTION_STATUSES: Final[frozenset[ProposalStatus]] = frozenset(
    {S.DRAFT, S.READY, S.NOTIFIED, S.APPROVAL_PENDING, S.APPROVED}
)


def is_terminal(status: ProposalStatus) -> bool:
    return status in TERMINAL_STATUSES


def can_transition(current: ProposalStatus, target: ProposalStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def assert_transition(current: ProposalStatus, target: ProposalStatus) -> None:
    """Raise :class:`InvalidProposalTransition` unless the transition is legal."""
    if not can_transition(current, target):
        raise InvalidProposalTransition(current.value, target.value)
