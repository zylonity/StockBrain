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
    ResearchStatus,
)
from stockbrain.proposals.exits import PositionExitStatus
from stockbrain.risk.exits import ExitFloors
from stockbrain.telegram import messages
from stockbrain.telegram.preferences import PipelineEvent
from stockbrain.telegram.service import (
    DailySummaryView,
    EventView,
    PortfolioView,
    PositionView,
    ResearchRunView,
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


def _position_view(**overrides: object) -> PositionView:
    base: dict[str, object] = {
        "broker_ticker": "AAPL_US_EQ",
        "name": "Apple Inc.",
        "quantity": Decimal("10"),
        "quantity_available": Decimal("4"),
        "average_price": Decimal("180"),
        "current_price": Decimal("200"),
        "ppl": Decimal("200"),
        "currency": "GBP",
        "last_synced_at": utcnow() - dt.timedelta(seconds=30),
    }
    base.update(overrides)
    return PositionView(**base)  # type: ignore[arg-type]


def test_positions_separate_held_from_tradable_quantity() -> None:
    """Shares inside a pie are owned but not individually tradable.

    Measured live on 13 of 14 real positions; a message that showed one number
    would be showing a quantity the broker would refuse.
    """
    rendered = messages.render_positions([_position_view()])
    assert "qty 10 (tradable 4)" in rendered
    assert "pie" in rendered


def test_positions_show_a_managed_positions_exit_floors() -> None:
    """The floors a position would meet, in the words the operator reads.

    Rendered from the same ``PositionExitStatus`` the API and the web page show,
    so a floor cannot read differently on two surfaces.  The horizon reuses the
    file's timestamp helper rather than inventing a second date format.
    """
    rendered = messages.render_positions(
        [
            _position_view(
                exit=PositionExitStatus(
                    managed=True,
                    reason=None,
                    floors=ExitFloors(
                        hard_stop=Decimal("92.00"),
                        volatility_floor=Decimal("119.00"),
                        trailing_floor=None,
                        roi_target_price=Decimal("115.00"),
                        horizon_ends_at=dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
                        nearest_floor=Decimal("119.00"),
                        nearest_rule="volatility_stop",
                    ),
                    peak_price=Decimal("130"),
                    atr=Decimal("3"),
                    horizon="days",
                )
            )
        ]
    )
    assert (
        "  exit: stop 92.00 · vol 119.00 · trail - · target 115.00 · "
        "horizon 2026-10-01 00:00 UTC (nearest: vol 119.00)" in rendered
    )


def test_positions_say_when_a_position_is_not_managed() -> None:
    """A position StockBrain never opened gets a sentence, not invented floors."""
    rendered = messages.render_positions(
        [
            _position_view(
                exit=PositionExitStatus(
                    managed=False,
                    reason="no StockBrain buy behind it",
                    floors=None,
                    peak_price=None,
                    atr=None,
                    horizon=None,
                )
            )
        ]
    )
    assert "  exit: not managed — no StockBrain buy behind it" in rendered


def test_positions_say_when_floors_are_unavailable() -> None:
    """A gap in what we know is said plainly, never rendered as a price."""
    rendered = messages.render_positions(
        [
            _position_view(),
            _position_view(
                broker_ticker="MSFT_US_EQ",
                exit=PositionExitStatus(
                    managed=True,
                    reason=None,
                    floors=None,
                    peak_price=None,
                    atr=None,
                    horizon=None,
                ),
            ),
        ]
    )
    assert rendered.count("  exit: floors unavailable") == 2


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


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------
def _research_view(**overrides: object) -> ResearchRunView:
    base: dict[str, object] = {
        "id": proposal_view().id,
        "status": ResearchStatus.SUCCEEDED,
        "company": "Apple Inc.",
        "broker_ticker": "AAPL_US_EQ",
        "event_id": None,
        "event_title": None,
        "action": "BUY",
        "confidence": 0.9,
        "horizon": "days",
        "summary": None,
        "error_class": None,
        "estimated_cost_usd": None,
        "started_at": None,
        "completed_at": None,
    }
    base.update(overrides)
    return ResearchRunView(**base)  # type: ignore[arg-type]


def test_a_blocked_proposal_lists_the_rules_that_refused_it() -> None:
    view = _research_view(
        action="BUY",
        confidence=0.64,
        block_reasons=(
            "research confidence 0.64 is below the 0.70 floor",
            "instrument USD != account GBP",
        ),
    )
    text = messages.render_research_stage(view, PipelineEvent.PROPOSAL_BLOCKED)
    assert "Trade blocked" in text
    assert "0.70 floor" in text and "USD != account GBP" in text
    assert "never authorizes" not in text  # that line belongs to RESEARCH_STARTED


def test_a_blocked_proposal_leads_with_decisive_reasons_and_collapses_market_state() -> None:
    view = _research_view(
        action="BUY",
        confidence=0.68,
        block_reasons=("research confidence 0.68 is below the 0.70 floor",),
        transient_block_reasons=(
            "market-data provider is DEGRADED",
            "quote is 90680850ms old, older than the 15.0s limit",
            "spread 910.0571 bps exceeds the 50 bps ceiling",
            "session CLOSED is not one of REGULAR",
        ),
    )
    text = messages.render_research_stage(view, PipelineEvent.PROPOSAL_BLOCKED)
    assert text.index("0.70 floor") < text.index("market state")
    assert "4 rules" in text and "clears at the open" in text
    assert "session CLOSED" not in text  # collapsed: only the first transient reason is quoted


# ---------------------------------------------------------------------------
# Daily summary
# ---------------------------------------------------------------------------
def _daily_view(**overrides: object) -> DailySummaryView:
    base: dict[str, object] = {
        "available": True,
        "currency": "GBP",
        "total_value": Decimal("1234.56"),
        "invested_value": Decimal("1000.00"),
        "result_value": Decimal("234.56"),
        "cash_available": Decimal("234.56"),
        "captured_at": utcnow() - dt.timedelta(seconds=30),
        "broker_environment": "demo",
        "positions": [],
        "open_proposals": 1,
        "candidates_24h": 2,
        "research_completed_24h": [
            ("Apple Inc.", "BUY"),
            ("Microsoft", "HOLD"),
            ("Nvidia", "SELL"),
        ],
    }
    base.update(overrides)
    return DailySummaryView(**base)  # type: ignore[arg-type]


def test_a_daily_summary_reports_the_portfolio_positions_and_counts() -> None:
    """One message carrying the account, each holding's nearest floor and the day's counts.

    The floors come from the same ``PositionExitStatus`` the API and the web page
    show, so a floor cannot read differently on two surfaces.
    """
    rendered = messages.render_daily_summary(
        _daily_view(
            positions=[
                _position_view(
                    exit=PositionExitStatus(
                        managed=True,
                        reason=None,
                        floors=ExitFloors(
                            hard_stop=Decimal("92.00"),
                            volatility_floor=Decimal("119.00"),
                            trailing_floor=None,
                            roi_target_price=Decimal("115.00"),
                            horizon_ends_at=dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
                            nearest_floor=Decimal("119.00"),
                            nearest_rule="volatility_stop",
                        ),
                        peak_price=Decimal("130"),
                        atr=Decimal("3"),
                        horizon="days",
                    )
                )
            ]
        )
    )
    assert "Total value: 1,234.56 GBP" in rendered
    assert "Open proposals: 1" in rendered
    assert "Research (24h): 3" in rendered
    assert "AAPL_US_EQ" in rendered
    assert "nearest vol 119.00" in rendered


def test_a_daily_summary_caps_the_position_list() -> None:
    """A chat message that needs scrolling to read is a message nobody reads."""
    positions = [_position_view(broker_ticker=f"TICK{i}_US_EQ") for i in range(16)]
    rendered = messages.render_daily_summary(_daily_view(positions=positions))
    assert "… and 1 more" in rendered
    assert "TICK14_US_EQ" in rendered
    assert "TICK15_US_EQ" not in rendered


def test_a_hostile_position_name_is_escaped_in_the_daily_summary() -> None:
    rendered = messages.render_daily_summary(
        _daily_view(positions=[_position_view(name=HOSTILE_NAME)])
    )
    assert "<a href" not in rendered
    assert "&lt;a href=" in rendered


def test_a_daily_summary_without_a_snapshot_says_so() -> None:
    rendered = messages.render_daily_summary(
        DailySummaryView(available=False, reason="no broker account snapshot has been captured yet")
    )
    assert "Unavailable" in rendered
    assert "no broker account snapshot" in rendered
