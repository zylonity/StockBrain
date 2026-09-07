"""The bull must get to answer the bear, not just open against it."""

from __future__ import annotations

from stockbrain.intelligence.research import ROLES
from stockbrain.intelligence.tradingagents_adapter import debate_sequence


def test_single_round_matches_the_previous_linear_pass() -> None:
    names = [name for name, _ in debate_sequence(1)]
    assert names == list(ROLES)


def test_extra_round_lets_each_side_rebut_once() -> None:
    pairs = debate_sequence(2)
    names = [name for name, _ in pairs]
    roles = [role for _, role in pairs]

    # Graph node names must stay unique or LangGraph rejects the graph.
    assert len(set(names)) == len(names)
    # Both debaters speak twice; every other role still speaks exactly once.
    assert roles.count("bull") == 2
    assert roles.count("bear") == 2
    for role in ("market", "fundamentals", "sentiment", "manager", "trader"):
        assert roles.count(role) == 1
    # The bull's second turn must land after the bear's first, or it is not a
    # rebuttal -- it is two opening statements.
    assert roles.index("bear") < len(roles) - 1 - roles[::-1].index("bull")
    # The manager still reads last, after the whole debate.
    assert roles.index("manager") > len(roles) - 1 - roles[::-1].index("bear")


def test_every_node_maps_to_a_known_report_role() -> None:
    for rounds in (1, 2, 3):
        for _, role in debate_sequence(rounds):
            assert role in ROLES
