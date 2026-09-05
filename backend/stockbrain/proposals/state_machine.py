"""The trade-proposal state machine.

Every client -- the web GUI now, Telegram in Phase 7, execution in Phase 8 --
mutates proposals exclusively through this table, so they cannot diverge.
Illegal transitions raise rather than being coerced.

Notable rules:

* ``READY`` may reach ``APPROVED`` directly.  Authorization is one act with one
  full revalidation, whoever performs it; ``APPROVAL_PENDING`` remains for the
  two-stage confirmation that guards *execution* in Phase 8, which is a
  different question from "is this proposal authorized".
* ``INVALIDATED`` is reachable from every pre-execution state and from nowhere
  after execution has begun.  It is distinct from ``EXPIRED`` (the clock ran
  out), ``REJECTED`` (a human said no) and ``CANCELLED`` (someone withdrew it):
  the proposal was still live and unacted-on, but the listing, the position, the
  account or the market moved out from under the numbers it carries.
* ``EXECUTING`` never returns to ``APPROVED``.  Once the execution transaction
  has started, the only exits are ``EXECUTED``, ``FAILED`` or
  ``EXECUTION_AMBIGUOUS``.
* ``EXECUTION_AMBIGUOUS`` is not terminal, but it is only left by
  *reconciliation* discovering the truth at the broker -- never by resending.

**Authorization is not execution.**  Reaching ``APPROVED`` in Phase 6 means the
deterministic risk engine allowed the trade and a recorded authority signed off.
No broker order has been sent, and no code in this phase can send one.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from stockbrain.enums import ProposalStatus
from stockbrain.errors import InvalidProposalTransition

__all__ = [
    "ACTIVE_STATUSES",
    "ALLOWED_TRANSITIONS",
    "AUTHORIZABLE_STATUSES",
    "EXPOSURE_RESERVING_STATUSES",
    "PRE_EXECUTION_STATUSES",
    "TERMINAL_STATUSES",
    "assert_transition",
    "can_transition",
    "is_terminal",
]

S = ProposalStatus

_INVALIDATION_TARGETS = frozenset({S.INVALIDATED})

_TRANSITIONS: Final[dict[ProposalStatus, frozenset[ProposalStatus]]] = {
    S.DRAFT: frozenset({S.READY, S.CANCELLED, S.FAILED}) | _INVALIDATION_TARGETS,
    S.READY: frozenset(
        {S.NOTIFIED, S.APPROVAL_PENDING, S.APPROVED, S.REJECTED, S.EXPIRED, S.CANCELLED}
    )
    | _INVALIDATION_TARGETS,
    S.NOTIFIED: frozenset({S.APPROVAL_PENDING, S.APPROVED, S.REJECTED, S.EXPIRED, S.CANCELLED})
    | _INVALIDATION_TARGETS,
    # Returning to NOTIFIED covers the user backing out of the confirmation
    # dialog without rejecting the proposal outright.
    S.APPROVAL_PENDING: frozenset(
        {S.APPROVED, S.NOTIFIED, S.REJECTED, S.EXPIRED, S.CANCELLED, S.FAILED}
    )
    | _INVALIDATION_TARGETS,
    S.APPROVED: frozenset({S.EXECUTING, S.EXPIRED, S.CANCELLED, S.FAILED}) | _INVALIDATION_TARGETS,
    S.EXECUTING: frozenset({S.EXECUTED, S.FAILED, S.EXECUTION_AMBIGUOUS}),
    S.EXECUTION_AMBIGUOUS: frozenset({S.EXECUTED, S.FAILED, S.CANCELLED}),
    S.EXECUTED: frozenset(),
    S.REJECTED: frozenset(),
    S.EXPIRED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
    S.INVALIDATED: frozenset(),
}

ALLOWED_TRANSITIONS: Final[MappingProxyType[ProposalStatus, frozenset[ProposalStatus]]] = (
    MappingProxyType(_TRANSITIONS)
)

TERMINAL_STATUSES: Final[frozenset[ProposalStatus]] = frozenset(
    status for status, targets in _TRANSITIONS.items() if not targets
)

#: States in which no broker mutation has been attempted, so expiry and
#: invalidation are safe.
PRE_EXECUTION_STATUSES: Final[frozenset[ProposalStatus]] = frozenset(
    {S.DRAFT, S.READY, S.NOTIFIED, S.APPROVAL_PENDING, S.APPROVED}
)

#: States from which authorization is legal.  ``APPROVED`` is deliberately
#: absent: authorizing an already-authorized proposal is the double-click case,
#: and it must be refused rather than recorded twice.
AUTHORIZABLE_STATUSES: Final[frozenset[ProposalStatus]] = frozenset(
    {S.READY, S.NOTIFIED, S.APPROVAL_PENDING}
)

#: States in which a proposal still occupies its instrument.  Mirrors the
#: partial unique index ``uq_trade_proposals_active_instrument``.
ACTIVE_STATUSES: Final[tuple[ProposalStatus, ...]] = (
    S.DRAFT,
    S.READY,
    S.NOTIFIED,
    S.APPROVAL_PENDING,
    S.APPROVED,
    S.EXECUTING,
    S.EXECUTION_AMBIGUOUS,
)

#: States in which a proposal's notional is treated as already committed.
#:
#: Every non-terminal state reserves.  The alternative -- reserving only from
#: ``APPROVED`` -- would let a queue of unapproved proposals each be sized
#: against the same cash, so that approving them in sequence breaches every cap
#: that each individually respected.  Only exposure-*increasing* sides reserve;
#: a pending sell frees cash rather than committing it.
EXPOSURE_RESERVING_STATUSES: Final[tuple[ProposalStatus, ...]] = ACTIVE_STATUSES


def is_terminal(status: ProposalStatus) -> bool:
    return status in TERMINAL_STATUSES


def can_transition(current: ProposalStatus, target: ProposalStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def assert_transition(current: ProposalStatus, target: ProposalStatus) -> None:
    """Raise :class:`InvalidProposalTransition` unless the transition is legal."""
    if not can_transition(current, target):
        raise InvalidProposalTransition(current.value, target.value)
