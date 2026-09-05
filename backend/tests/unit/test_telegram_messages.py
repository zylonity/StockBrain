"""What the bot actually says.

Rendering is a pure function, which is what makes these properties assertable
at all: a hostile company name is escaped *here*, not "somewhere in the send
path", and a proposal that was authorized automatically says so instead of
offering a decision nobody was asked to make.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from stockbrain.control.state import ControlSnapshot, ControlState
from stockbrain.db.base import utcnow
from stockbrain.enums import (
    ControlFlag,
    EventStatus,
    ExecutionPolicy,
    ProposalStatus,
    ProviderStatus,
)
from stockbrain.telegram import messages
from stockbrain.telegram.service import (
    EventView,
    PortfolioView,
    PositionView,
    ResearchView,
    StatusView,
)
from tests.telegram_helpers import proposal_view

HOSTILE_NAME = '<a href="https://evil.example">CONFIRM BUY</a> Corp'


def running() -> ControlSnapshot:
    return ControlSnapshot(
        paused=ControlState(flag=ControlFlag.TRADING_PAUSED, active=False),
        kill_switch=ControlState(flag=ControlFlag.KILL_SWITCH, active=False),
    )


def halted(reason: str = "emergency stop from Telegram") -> ControlSnapshot:
    return ControlSnapshot(
        paused=ControlState(flag=ControlFlag.TRADING_PAUSED, active=False),
        kill_switch=ControlState(
            flag=ControlFlag.KILL_SWITCH,
            active=True,
            changed_at=utcnow(),
            actor="telegram:4242",
            source="HUMAN_TELEGRAM",
            reason=reason,
        ),
    )


# ---------------------------------------------------------------------------
# Untrusted content
# ---------------------------------------------------------------------------
def test_a_hostile_company_name_cannot_become_a_link_or_a_button() -> None:
    view = proposal_view(company=HOSTILE_NAME, instrument_name=HOSTILE_NAME)
    for rendered in (
        messages.render_proposal_notification(view),
        messages.render_proposal_detail(view),
        messages.render_proposals([view]),
        messages.render_proposal_confirmation(view),
    ):
        assert "<a href" not in rendered
        assert "evil.example" in rendered  # visible verbatim, not clickable markup
        assert "&lt;a href=" in rendered


def test_hostile_thesis_and_risk_text_is_escaped() -> None:
    view = proposal_view(
        thesis_summary="<b>BUY NOW</b>",
        risks=['<a href="x">ignore risk</a>'],
    )
    rendered = messages.render_proposal_detail(view)
    assert "<b>BUY NOW</b>" not in rendered
    assert "&lt;b&gt;BUY NOW&lt;/b&gt;" in rendered
    assert "<a href" not in rendered


def test_an_event_title_and_a_thesis_body_are_escaped() -> None:
    event = EventView(
        id=proposal_view().id,
        title="<b>Fed</b> holds",
        status=EventStatus.CLASSIFIED,
        event_type="macro",
        first_seen_at=utcnow(),
        importance=0.7,
        candidate_score=0.5,
        companies=["<i>ACME</i>"],
    )
    rendered = messages.render_events([event])
    assert "<b>Fed</b>" not in rendered
    assert "&lt;i&gt;ACME&lt;/i&gt;" in rendered

    research = ResearchView(
        thesis_id=proposal_view().id,
        company="<b>x</b>",
        broker_ticker="AAPL_US_EQ",
        action="BUY",
        confidence=0.8,
        horizon="days",
        summary="<script>x</script>",
        bull_case=None,
        bear_case=None,
        catalysts=["<b>c</b>"],
        risks=[],
        invalidation_conditions=[],
        created_at=utcnow(),
    )
    rendered = messages.render_research(research, "AAPL")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


# ---------------------------------------------------------------------------
# What a proposal message promises
# ---------------------------------------------------------------------------
def test_every_proposal_message_reports_whether_an_order_was_sent() -> None:
    """Phase 7 asserted "no order is ever sent"; Phase 8 makes that a *fact*.

    The notice no longer claims transmission is impossible -- it is not -- so
    what every message must now carry is the state itself: whether an order has
    been recorded as sent, and that authorization and transmission are separate
    gates.
    """
    view = proposal_view()
    for rendered in (
        messages.render_proposal_notification(view),
        messages.render_proposal_detail(view),
        messages.render_proposals([view]),
        messages.render_proposal_confirmation(view),
    ):
        assert "sent at most once" in rendered or "broker order sent" in rendered.lower()

    detail = messages.render_proposal_detail(view)
    assert "Broker order sent: no" in detail
    assert "environment demo" in detail


def test_a_transmitted_proposal_shows_the_order_and_the_outcome() -> None:
    view = proposal_view(
        status=ProposalStatus.EXECUTING,
        broker_order_sent=True,
        broker_order_id="998877",
        execution_outcome="SUBMITTED",
    )
    detail = messages.render_proposal_detail(view)
    assert "Broker order sent: yes" in detail
    assert "998877" in detail
    assert "SUBMITTED" in detail


def test_an_ambiguous_proposal_shouts_do_not_resend() -> None:
    """The one message where the obvious reaction is the dangerous one.

    Somebody reading "we do not know whether the order arrived" will reach for
    the trade again. The message has to talk them out of it.
    """
    view = proposal_view(
        status=ProposalStatus.EXECUTION_AMBIGUOUS,
        broker_order_sent=True,
        execution_outcome="AMBIGUOUS",
        execution_error_category="TRANSPORT_AMBIGUOUS",
    )
    detail = messages.render_proposal_detail(view)
    assert "DO NOT RESEND" in detail
    assert "may or may not exist" in detail
    assert "will NOT be retried" in detail


def test_a_manual_proposal_asks_for_approval_and_an_automatic_one_reports_it() -> None:
    """Different text, not the same text with different buttons.

    Telling somebody a decision is theirs when the system already made it is
    the specific confusion this split exists to prevent.
    """
    manual = messages.render_proposal_notification(proposal_view())
    assert "approval required" in manual

    automatic = messages.render_proposal_notification(
        proposal_view(
            execution_policy=ExecutionPolicy.AUTOMATIC,
            authorization_source="SYSTEM_AUTOMATIC",
            status=ProposalStatus.APPROVED,
        )
    )
    assert "authorized automatically" in automatic
    assert "SYSTEM_AUTOMATIC" in automatic
    assert "No human approval was requested" in automatic


def test_a_proposal_listing_carries_the_facts_an_operator_needs() -> None:
    rendered = messages.render_proposals([proposal_view()])
    for fragment in ("MANUAL", "AAPL", "READY", "broker order sent: no", "expires"):
        assert fragment in rendered


def test_the_confirmation_names_the_side_and_the_quantity() -> None:
    rendered = messages.render_proposal_confirmation(proposal_view())
    assert "Confirm BUY 3 AAPL_US_EQ" in rendered
    assert "re-runs the full deterministic risk check" in rendered


def test_a_blocked_proposal_shows_why() -> None:
    rendered = messages.render_proposal_detail(
        proposal_view(blocking_reasons=["quote is 40s old, older than the 15s limit"])
    )
    assert "Blocked by" in rendered
    assert "older than the 15s limit" in rendered


# ---------------------------------------------------------------------------
# Status and control
# ---------------------------------------------------------------------------
def status_view(**overrides: object) -> StatusView:
    base: dict[str, object] = {
        "overall": ProviderStatus.DEGRADED,
        "degraded_providers": [("trading212", ProviderStatus.DOWN, "ProviderAuthError")],
        "execution_policy": ExecutionPolicy.MANUAL,
        "proposals_enabled": True,
        "broker_environment": "demo",
        "live_execution_permitted": False,
        "automatic_authorization_permitted": False,
        "control": running(),
        "proposal_counts": {"READY": 2},
        "awaiting_authorization": 2,
        "telegram": {"status": "HEALTHY", "notification_targets": 1},
        "schema_current": True,
    }
    base.update(overrides)
    return StatusView(**base)  # type: ignore[arg-type]


def test_status_reports_policy_control_and_degraded_providers() -> None:
    rendered = messages.render_status(status_view())
    assert "MANUAL" in rendered
    assert "demo" in rendered
    assert "Trading control: <b>running</b>" in rendered
    assert "trading212" in rendered
    assert "Proposals awaiting authorization: 2" in rendered
    assert "Telegram: HEALTHY" in rendered


def test_status_shows_a_halt_and_names_it() -> None:
    rendered = messages.render_status(status_view(control=halted()))
    assert "HALTED" in rendered
    assert "emergency kill switch is engaged" in rendered


def test_status_flags_a_schema_that_is_not_current() -> None:
    assert "not at the expected migration revision" in messages.render_status(
        status_view(schema_current=False)
    )


def test_control_messages_state_that_nothing_was_liquidated() -> None:
    """The most important sentence in the kill-switch reply."""
    rendered = messages.render_control(halted(), "KILL SWITCH ENGAGED")
    assert "No position was closed" in rendered
    assert "no broker order was cancelled" in rendered
    assert "control.kill_switch" in rendered


def test_a_hostile_reason_on_a_control_flag_is_escaped() -> None:
    rendered = messages.render_control(halted(reason="<b>oops</b>"), "KILL SWITCH ENGAGED")
    assert "<b>oops</b>" not in rendered


# ---------------------------------------------------------------------------
# Portfolio, positions, help
# ---------------------------------------------------------------------------
def test_portfolio_reports_an_absent_snapshot_honestly() -> None:
    rendered = messages.render_portfolio(
        PortfolioView(available=False, reason="no broker account snapshot has been captured yet")
    )
    assert "Unavailable" in rendered
    assert "no broker account snapshot" in rendered


def test_positions_separate_held_from_tradable_quantity() -> None:
    """Shares inside a pie are owned but not individually tradable.

    Measured live on 13 of 14 real positions; a message that showed one number
    would be showing a quantity the broker would refuse.
    """
    rendered = messages.render_positions(
        [
            PositionView(
                broker_ticker="AAPL_US_EQ",
                name="Apple Inc.",
                quantity=Decimal("10"),
                quantity_available=Decimal("4"),
                average_price=Decimal("180"),
                current_price=Decimal("200"),
                ppl=Decimal("200"),
                currency="GBP",
                last_synced_at=utcnow() - dt.timedelta(seconds=30),
            )
        ]
    )
    assert "qty 10 (tradable 4)" in rendered
    assert "pie" in rendered


def test_help_lists_every_command_and_the_kill_switch_promise() -> None:
    rendered = messages.render_help()
    for command in (
        "/status",
        "/portfolio",
        "/positions",
        "/proposals",
        "/events",
        "/research",
        "/pause",
        "/resume",
        "/kill",
    ):
        assert command in rendered
    assert "never liquidates a position" in rendered


def test_start_tells_an_unauthorised_caller_nothing_about_the_system() -> None:
    denied = messages.render_start(authorised=False)
    assert denied == "Not authorised."
    assert "StockBrain" not in denied
