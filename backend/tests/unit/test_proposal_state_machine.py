"""Trade-proposal state machine tests."""

from __future__ import annotations

import itertools

import pytest

from stockbrain.enums import ProposalStatus as S
from stockbrain.errors import InvalidProposalTransition
from stockbrain.proposals.state_machine import (
    ACTIVE_STATUSES,
    ALLOWED_TRANSITIONS,
    AUTHORIZABLE_STATUSES,
    EXECUTABLE_STATUSES,
    EXPOSURE_RESERVING_STATUSES,
    IN_FLIGHT_STATUSES,
    PRESEND_RECOVERY_TARGET,
    TERMINAL_STATUSES,
    assert_transition,
    can_transition,
    is_terminal,
)


def test_every_status_has_a_transition_entry() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(S)


def test_terminal_statuses() -> None:
    assert (
        frozenset({S.EXECUTED, S.REJECTED, S.EXPIRED, S.FAILED, S.CANCELLED, S.INVALIDATED})
        == TERMINAL_STATUSES
    )
    for status in TERMINAL_STATUSES:
        assert is_terminal(status)
        assert ALLOWED_TRANSITIONS[status] == frozenset()


def test_happy_path_is_walkable() -> None:
    path = [
        S.DRAFT,
        S.READY,
        S.NOTIFIED,
        S.APPROVAL_PENDING,
        S.APPROVED,
        S.EXECUTING,
        S.EXECUTED,
    ]
    for current, target in itertools.pairwise(path):
        assert_transition(current, target)


def test_executing_returns_to_approved_only_for_a_proven_pre_send_failure() -> None:
    """Phase 8 replaced "never" with "from exactly one place", deliberately.

    Phase 6 wrote that ``EXECUTING`` never returns to ``APPROVED``. That
    contradicted :class:`~stockbrain.enums.ExecutionOutcome`, which documents
    ``FAILED_BEFORE_SEND`` as "the only outcome from which a *new* attempt may
    be created": with no way back, a DNS failure permanently killed an
    authorized proposal the broker had never heard of.

    The transition is now legal, and its narrowness is enforced above the state
    machine: it is taken only after ``sent_to_broker`` has been retracted on the
    strength of an httpx exception that cannot occur once a request line has been
    written. ``test_only_one_call_site_takes_the_presend_recovery_transition``
    in the execution suite pins that down.
    """
    assert can_transition(S.EXECUTING, S.APPROVED)
    assert PRESEND_RECOVERY_TARGET is S.APPROVED


def test_an_ambiguous_proposal_can_never_return_to_approved() -> None:
    """The distinction the pre-send transition must not blur.

    "The bytes provably never left" is recoverable. "We do not know" is not:
    re-approving an ambiguous proposal would put it back in the queue for a
    second transmission, which is the one thing this system exists to prevent.
    """
    assert not can_transition(S.EXECUTION_AMBIGUOUS, S.APPROVED)
    with pytest.raises(InvalidProposalTransition):
        assert_transition(S.EXECUTION_AMBIGUOUS, S.APPROVED)


def test_executing_cannot_expire_or_be_cancelled() -> None:
    """A broker mutation may be in flight; expiry would lie about the outcome."""
    assert not can_transition(S.EXECUTING, S.EXPIRED)
    assert not can_transition(S.EXECUTING, S.CANCELLED)


def test_executing_outcomes_are_exactly_the_four_possible_ones() -> None:
    """Three broker outcomes, plus the proven-pre-send route back."""
    assert ALLOWED_TRANSITIONS[S.EXECUTING] == frozenset(
        {S.EXECUTED, S.FAILED, S.EXECUTION_AMBIGUOUS, S.APPROVED}
    )


def test_only_approved_proposals_are_executable() -> None:
    """An authorization is the only licence to transmit.

    ``EXECUTING`` is deliberately absent, so a second worker cannot read the
    status as permission to start a second attempt.
    """
    assert frozenset({S.APPROVED}) == EXECUTABLE_STATUSES
    for status in S:
        if status is not S.APPROVED:
            assert status not in EXECUTABLE_STATUSES


def test_in_flight_statuses_are_the_two_where_an_order_may_exist() -> None:
    """These are the states a sweep must never expire or cancel out from under."""
    assert frozenset({S.EXECUTING, S.EXECUTION_AMBIGUOUS}) == IN_FLIGHT_STATUSES
    assert not (IN_FLIGHT_STATUSES & TERMINAL_STATUSES)


def test_an_in_flight_proposal_still_reserves_its_exposure() -> None:
    """An order that may exist at the broker is committed cash.

    Releasing the reservation while the outcome is unknown would let the next
    proposal be sized against money that is already spent.
    """
    for status in IN_FLIGHT_STATUSES:
        assert status in EXPOSURE_RESERVING_STATUSES


def test_ambiguous_state_is_only_resolved_forward() -> None:
    """Reconciliation resolves ambiguity; resubmission is not a transition."""
    assert can_transition(S.EXECUTION_AMBIGUOUS, S.EXECUTED)
    assert can_transition(S.EXECUTION_AMBIGUOUS, S.FAILED)
    assert not can_transition(S.EXECUTION_AMBIGUOUS, S.EXECUTING)
    assert not can_transition(S.EXECUTION_AMBIGUOUS, S.APPROVED)


def test_expired_proposal_cannot_be_approved_or_executed() -> None:
    assert not can_transition(S.EXPIRED, S.APPROVED)
    assert not can_transition(S.EXPIRED, S.EXECUTING)


def test_rejected_proposal_is_final() -> None:
    for target in S:
        assert not can_transition(S.REJECTED, target)


def test_only_approved_may_begin_execution() -> None:
    starts_execution = {status for status in S if can_transition(status, S.EXECUTING)}
    assert starts_execution == {S.APPROVED}


def test_execution_states_are_unreachable_from_draft_directly() -> None:
    assert not can_transition(S.DRAFT, S.APPROVED)
    assert not can_transition(S.DRAFT, S.EXECUTING)
    assert not can_transition(S.READY, S.EXECUTING)


# ---------------------------------------------------------------------------
# Phase 6: authorization and invalidation
# ---------------------------------------------------------------------------
def test_authorization_is_reachable_without_a_separate_pending_stage() -> None:
    """Manual and automatic authorization converge on one APPROVED state.

    ``APPROVAL_PENDING`` remains for the two-stage confirmation that guards
    *execution*, which is a different question from "is this authorized".
    """
    assert can_transition(S.READY, S.APPROVED)
    assert can_transition(S.NOTIFIED, S.APPROVED)
    assert can_transition(S.APPROVAL_PENDING, S.APPROVED)


def test_an_already_authorized_proposal_cannot_be_authorized_again() -> None:
    """The double-click case: APPROVED is not an authorizable state."""
    assert S.APPROVED not in AUTHORIZABLE_STATUSES
    assert not can_transition(S.APPROVED, S.APPROVED)


def test_invalidated_is_terminal_and_reachable_from_every_pre_execution_state() -> None:
    """A precondition can stop holding at any point before execution begins."""
    for status in (S.DRAFT, S.READY, S.NOTIFIED, S.APPROVAL_PENDING, S.APPROVED):
        assert can_transition(status, S.INVALIDATED)
    assert is_terminal(S.INVALIDATED)
    for target in S:
        assert not can_transition(S.INVALIDATED, target)


def test_invalidation_is_impossible_once_execution_has_begun() -> None:
    """Bytes may have reached the broker; "never mind" is not an outcome."""
    assert not can_transition(S.EXECUTING, S.INVALIDATED)
    assert not can_transition(S.EXECUTION_AMBIGUOUS, S.INVALIDATED)


def test_an_invalidated_proposal_can_never_be_authorized() -> None:
    assert not can_transition(S.INVALIDATED, S.APPROVED)
    with pytest.raises(InvalidProposalTransition):
        assert_transition(S.INVALIDATED, S.APPROVED)


def test_exposure_reserving_statuses_match_the_active_index_predicate() -> None:
    """The reserving set and the one-live-proposal-per-listing index agree.

    If they diverged, a proposal could occupy an instrument without reserving
    its cash, and two of them together would breach a cap each respected.
    """
    assert EXPOSURE_RESERVING_STATUSES == ACTIVE_STATUSES
    for status in ACTIVE_STATUSES:
        assert not is_terminal(status)


def test_the_orm_index_predicate_uses_the_state_machines_active_set() -> None:
    """One definition, not two that must be kept in step by hand."""
    from stockbrain.db.models.proposals import ACTIVE_PROPOSAL_STATUSES

    assert ACTIVE_PROPOSAL_STATUSES == ACTIVE_STATUSES
