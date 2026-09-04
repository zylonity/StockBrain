"""Trade-proposal state machine tests."""

from __future__ import annotations

import itertools

import pytest

from stockbrain.enums import ProposalStatus as S
from stockbrain.errors import InvalidProposalTransition
from stockbrain.proposals.state_machine import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    assert_transition,
    can_transition,
    is_terminal,
)


def test_every_status_has_a_transition_entry() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(S)


def test_terminal_statuses() -> None:
    assert (
        frozenset({S.EXECUTED, S.REJECTED, S.EXPIRED, S.FAILED, S.CANCELLED}) == TERMINAL_STATUSES
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


def test_executing_cannot_return_to_approved() -> None:
    """Once execution has begun the proposal must never become re-approvable."""
    assert not can_transition(S.EXECUTING, S.APPROVED)
    with pytest.raises(InvalidProposalTransition):
        assert_transition(S.EXECUTING, S.APPROVED)


def test_executing_cannot_expire_or_be_cancelled() -> None:
    """A broker mutation may be in flight; expiry would lie about the outcome."""
    assert not can_transition(S.EXECUTING, S.EXPIRED)
    assert not can_transition(S.EXECUTING, S.CANCELLED)


def test_executing_outcomes_are_exactly_the_three_possible_ones() -> None:
    assert ALLOWED_TRANSITIONS[S.EXECUTING] == frozenset(
        {S.EXECUTED, S.FAILED, S.EXECUTION_AMBIGUOUS}
    )


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
