"""Pure rendering: a read-model view in, an HTML string out.

No database, no network, no clock of its own beyond an injected ``now``.  That
makes every message assertable in a unit test, which is the only practical way
to keep a promise like "untrusted text can never inject a link".

The bot's whole vocabulary of untrusted values -- company names, instrument
names, article titles, thesis text, refusal reasons -- passes through
:func:`~stockbrain.telegram.formatting.trim`, which bounds and then escapes.
Nothing in this module concatenates a raw value into markup.
"""

from __future__ import annotations

import datetime as dt

from stockbrain.control.state import ControlSnapshot
from stockbrain.enums import ProposalStatus, ProviderStatus
from stockbrain.telegram.formatting import age, bold, code, esc, money, quantity, stamp, trim
from stockbrain.telegram.preferences import PipelineEvent
from stockbrain.telegram.service import (
    EventView,
    PortfolioView,
    PositionView,
    ProposalView,
    ResearchRunView,
    ResearchView,
    StatusView,
)

__all__ = [
    "AMBIGUOUS_NOTICE",
    "AUTHORIZATION_NOTICE",
    "control_line",
    "render_control",
    "render_event_stage",
    "render_events",
    "render_help",
    "render_portfolio",
    "render_positions",
    "render_proposal_confirmation",
    "render_proposal_detail",
    "render_proposal_notification",
    "render_proposals",
    "render_research",
    "render_research_stage",
    "render_start",
    "render_status",
    "render_terminal_status",
]

#: Repeated on every message that could be mistaken for an order.  Authorizing
#: and transmitting are separately gated, and a control interface that leaves
#: that ambiguous is worse than one that does not exist.
AUTHORIZATION_NOTICE = (
    "Authorizing records that deterministic risk allowed the trade and who signed "
    "it off. Transmission is a separate, separately gated step, and every order is "
    "sent at most once."
)

#: Shown on any proposal whose order state is unknown. The wording matters more
#: than most: the obvious reaction to "we do not know if the order arrived" is to
#: send it again, and that is the one action that turns an unknown into a real,
#: duplicated position.
AMBIGUOUS_NOTICE = (
    "ORDER STATE UNKNOWN. StockBrain sent a request and did not receive a definitive "
    "response, so the order may or may not exist. It will NOT be retried. "
    "Reconciliation is reading Trading 212 - do not resend and do not place the trade "
    "manually until the outcome is known."
)

_NAME_LIMIT = 64
_TEXT_LIMIT = 320
_REASON_LIMIT = 200
_TITLE_LIMIT = 140


def render_start(authorised: bool) -> str:
    if not authorised:
        # Deliberately says nothing about what the system is, holds or does.
        return "Not authorised."
    return (
        f"{bold('StockBrain')}\n"
        "Connected. This chat is authorised for status, portfolio and proposal "
        "authorization.\n\n"
        f"{esc(AUTHORIZATION_NOTICE)}\n\n"
        "Send /help for the command list."
    )


def render_help() -> str:
    rows = [
        ("/status", "application and provider health, policy, control state"),
        ("/portfolio", "account summary from the latest broker snapshot"),
        ("/positions", "open positions"),
        ("/proposals", "active and recent trade proposals"),
        ("/events", "recent significant events"),
        ("/research &lt;id|symbol&gt;", "normalised thesis for a proposal, event or symbol"),
        ("/pause", "stop new proposals and all authorization"),
        ("/resume", "lift a pause"),
        ("/kill", "emergency stop: no authorization, no future execution"),
    ]
    body = "\n".join(f"{code(command)} — {esc(description)}" for command, description in rows)
    return (
        f"{bold('StockBrain commands')}\n{body}\n\n"
        f"{esc(AUTHORIZATION_NOTICE)}\n"
        f"{esc('/kill never liquidates a position and never cancels a broker order.')}"
    )


def control_line(control: ControlSnapshot) -> str:
    if not control.trading_halted:
        return "Trading control: <b>running</b>"
    return (
        "Trading control: "
        + bold("HALTED")
        + "\n"
        + "\n".join(f"• {esc(blocker)}" for blocker in control.blockers)
    )


def render_status(view: StatusView, *, now: dt.datetime | None = None) -> str:
    lines = [
        f"{bold('StockBrain status')} — {esc(view.overall.value)}",
        f"Policy: {esc(view.execution_policy.value)} · broker env {esc(view.broker_environment)}",
        f"Live execution permitted: {esc(_yes_no(view.live_execution_permitted))}",
        f"Automatic authorization permitted: "
        f"{esc(_yes_no(view.automatic_authorization_permitted))}",
        control_line(view.control),
        f"Proposals awaiting authorization: {view.awaiting_authorization}",
    ]
    if view.proposal_counts:
        summary = ", ".join(
            f"{esc(status)} {count}" for status, count in sorted(view.proposal_counts.items())
        )
        lines.append(f"Proposals by status: {summary}")
    if not view.schema_current:
        lines.append(bold("Database schema is not at the expected migration revision."))
    if view.degraded_providers:
        lines.append(bold("Degraded providers"))
        for name, status, detail in view.degraded_providers[:6]:
            suffix = f" — {trim(detail, _REASON_LIMIT)}" if detail else ""
            lines.append(f"• {esc(name)}: {esc(status.value)}{suffix}")
    else:
        lines.append("All configured providers healthy or disabled.")
    lines.append(_telegram_line(view.telegram))
    lines.append(esc(AUTHORIZATION_NOTICE))
    return "\n".join(lines)


def _telegram_line(status: dict[str, object]) -> str:
    state = str(status.get("status", ProviderStatus.UNKNOWN.value))
    parts = [f"Telegram: {esc(state)}"]
    if (last := status.get("last_contact_at")) is not None:
        parts.append(f"last contact {esc(last)}")
    if (targets := status.get("notification_targets")) is not None:
        parts.append(f"{esc(targets)} notification target(s)")
    if error := status.get("last_error_category"):
        parts.append(f"last error {esc(error)}")
    return " · ".join(parts)


def render_control(control: ControlSnapshot, headline: str) -> str:
    lines = [bold(headline), control_line(control)]
    for state in (control.kill_switch, control.paused):
        if state.active:
            lines.append(
                f"{esc(state.flag.value)}: since {esc(stamp(state.changed_at))} "
                f"by {trim(state.actor, _NAME_LIMIT)} ({trim(state.source, 32)})"
            )
    lines.append(
        esc(
            "No position was closed and no broker order was cancelled: StockBrain has no "
            "order, cancel or amend path."
        )
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Portfolio and positions
# ---------------------------------------------------------------------------
def render_portfolio(view: PortfolioView, *, now: dt.datetime | None = None) -> str:
    if not view.available:
        return f"{bold('Portfolio')}\nUnavailable — {esc(view.reason or 'no snapshot')}."
    lines = [
        bold("Portfolio"),
        f"Total value: {money(view.total_value, view.currency)}",
        f"Invested: {money(view.invested_value, view.currency)} · "
        f"result {money(view.result_value, view.currency)}",
        f"Cash available: {money(view.cash_available, view.currency)}",
        f"Reserved for orders: {money(view.cash_reserved, view.currency)}",
        f"In pies: {money(view.cash_in_pies, view.currency)}",
        f"Open positions: {view.position_count}",
        f"Snapshot: {esc(age(view.captured_at, now=now))} "
        f"({esc(view.broker_environment or 'unknown')} environment)",
    ]
    return "\n".join(lines)


def render_positions(views: list[PositionView], *, now: dt.datetime | None = None) -> str:
    if not views:
        return f"{bold('Positions')}\nNo positions in the latest broker snapshot."
    lines = [bold("Positions")]
    for position in views:
        label = trim(position.name or position.broker_ticker, _NAME_LIMIT)
        held = quantity(position.quantity)
        tradable = quantity(position.quantity_available)
        lines.append(
            f"{code(position.broker_ticker)} {label}\n"
            f"  qty {esc(held)} (tradable {esc(tradable)}) · "
            f"avg {money(position.average_price, position.currency, places=4)} · "
            f"P/L {money(position.ppl, position.currency)}"
        )
    lines.append(
        esc(
            "Shares held inside a pie are owned but not individually tradable, "
            "which is why the two quantities differ."
        )
    )
    lines.append(esc(f"Synced {age(views[0].last_synced_at, now=now)}."))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------
def _headline(view: ProposalView) -> str:
    name = trim(view.company or view.instrument_name or view.broker_ticker, _NAME_LIMIT)
    action = view.research_action or view.side
    return f"{bold(action)} {code(view.market_symbol or view.broker_ticker)} — {name}"


def _policy_line(view: ProposalView) -> str:
    policy = "MANUAL" if view.is_manual else "AUTOMATIC"
    source = view.authorization_source or "not authorized"
    return f"Policy: {esc(policy)} · authorization: {esc(source)}"


def render_proposals(views: list[ProposalView], *, now: dt.datetime | None = None) -> str:
    if not views:
        return f"{bold('Proposals')}\nNo proposals recorded."
    lines = [bold("Proposals")]
    for view in views:
        lines.append(
            f"{_headline(view)}\n"
            f"  {esc(view.status.value)} · {esc(quantity(view.quantity))} @ "
            f"{money(view.reference_price, view.reference_currency, places=4)} ≈ "
            f"{money(view.notional, view.account_currency)}\n"
            f"  {_policy_line(view)} · broker order sent: "
            f"{esc(_yes_no(view.broker_order_sent))}\n"
            f"  expires {esc(stamp(view.expires_at))}"
        )
    lines.append(esc(AUTHORIZATION_NOTICE))
    return "\n".join(lines)


def render_proposal_detail(view: ProposalView, *, now: dt.datetime | None = None) -> str:
    lines = [
        _headline(view),
        f"Status: {esc(view.status.value)}"
        + (f" — {trim(view.status_reason, _REASON_LIMIT)}" if view.status_reason else ""),
        _policy_line(view),
        f"Quantity: {esc(quantity(view.quantity))} · notional "
        f"{money(view.notional, view.account_currency)}",
        f"Reference price: {money(view.reference_price, view.reference_currency, places=4)} "
        f"({esc(view.price_source)})",
        f"Book: bid {money(view.quote_bid, places=4)} / ask {money(view.quote_ask, places=4)} · "
        f"spread {money(view.quote_spread, places=4)} "
        f"({money(view.quote_spread_bps, places=1)} bps)",
        f"Quote age: {view.quote_age_ms} ms · session {esc(view.market_session or 'unknown')}",
        f"Research confidence: {esc(_confidence(view.research_confidence))}",
        f"Expires: {esc(stamp(view.expires_at))} ({esc(age(view.expires_at, now=now))})",
        f"Broker: {esc(view.broker)} · environment {esc(view.broker_environment)}",
        f"Broker order sent: {esc(_yes_no(view.broker_order_sent))}"
        + (f" · order {code(view.broker_order_id)}" if view.broker_order_id else "")
        + (f" · {esc(view.execution_outcome)}" if view.execution_outcome else ""),
    ]
    if view.reconciliation_result:
        lines.append(f"Reconciliation: {esc(view.reconciliation_result)}")
    if view.execution_ambiguous:
        lines.append("")
        lines.append(bold("DO NOT RESEND"))
        lines.append(esc(AMBIGUOUS_NOTICE))
    if view.thesis_summary:
        lines.append(f"\n{bold('Thesis')}\n{trim(view.thesis_summary, _TEXT_LIMIT)}")
    if view.risks:
        risks = "\n".join(f"• {trim(risk, _REASON_LIMIT)}" for risk in view.risks[:4])
        lines.append(f"\n{bold('Risks')}\n{risks}")
    if view.blocking_reasons:
        blocks = "\n".join(
            f"• {trim(reason, _REASON_LIMIT)}" for reason in view.blocking_reasons[:4]
        )
        lines.append(f"\n{bold('Blocked by')}\n{blocks}")
    lines.append(f"\n{esc(AUTHORIZATION_NOTICE)}")
    return "\n".join(lines)


def render_proposal_notification(view: ProposalView, *, now: dt.datetime | None = None) -> str:
    """The message a new proposal arrives as.

    Manual and automatic proposals get *different* text, not a different button
    set on the same text: an automatic proposal was authorized by the system and
    saying anything that implies a pending human decision would be a lie.
    """
    header = (
        bold("TRADE PROPOSAL — approval required")
        if view.is_manual
        else bold("TRADE PROPOSAL — authorized automatically")
    )
    lines = [
        header,
        _headline(view),
        f"Quantity: {esc(quantity(view.quantity))} · notional "
        f"{money(view.notional, view.account_currency)}",
        f"Reference price: {money(view.reference_price, view.reference_currency, places=4)} "
        f"({esc(view.price_source)})",
        f"Spread: {money(view.quote_spread, places=4)} "
        f"({money(view.quote_spread_bps, places=1)} bps) · quote age {view.quote_age_ms} ms",
        f"Research confidence: {esc(_confidence(view.research_confidence))}",
        f"Expires: {esc(stamp(view.expires_at))}",
    ]
    if not view.is_manual:
        lines.append(
            f"Authorized by {esc(view.authorization_source or 'SYSTEM_AUTOMATIC')} under the "
            "AUTOMATIC execution policy. No human approval was requested."
        )
    if view.thesis_summary:
        lines.append(f"\n{bold('Thesis')}\n{trim(view.thesis_summary, _TEXT_LIMIT)}")
    if view.risks:
        risks = "\n".join(f"• {trim(risk, _REASON_LIMIT)}" for risk in view.risks[:3])
        lines.append(f"\n{bold('Risks')}\n{risks}")
    lines.append(f"\n{esc(AUTHORIZATION_NOTICE)}")
    return "\n".join(lines)


def render_proposal_confirmation(view: ProposalView, *, now: dt.datetime | None = None) -> str:
    """Stage two: what the operator is actually about to authorize."""
    return "\n".join(
        [
            bold(f"Confirm {view.side} {quantity(view.quantity)} {view.broker_ticker}"),
            _headline(view),
            f"Order type: MARKET · reference "
            f"{money(view.reference_price, view.reference_currency, places=4)} · notional "
            f"{money(view.notional, view.account_currency)}",
            f"Broker: Trading 212 · account {esc(view.account_currency)}",
            "",
            esc(
                "Confirming re-runs the full deterministic risk check against a fresh quote "
                "and a fresh account snapshot. If anything moved, the proposal is invalidated "
                "instead of re-priced."
            ),
            esc(AUTHORIZATION_NOTICE),
        ]
    )


def render_terminal_status(view: ProposalView) -> str:
    """Appended to a message whose buttons have just become inert."""
    if view.status is ProposalStatus.APPROVED:
        source = esc(view.authorization_source or "")
        return f"{bold('AUTHORIZED')} — {source} · {esc(AUTHORIZATION_NOTICE)}"
    reason = f" — {trim(view.status_reason, _REASON_LIMIT)}" if view.status_reason else ""
    return f"{bold(view.status.value)}{reason}"


# ---------------------------------------------------------------------------
# Events and research
# ---------------------------------------------------------------------------
def render_events(views: list[EventView], *, now: dt.datetime | None = None) -> str:
    if not views:
        return f"{bold('Events')}\nNo classified events yet."
    lines = [bold("Recent significant events")]
    for event in views:
        companies = ", ".join(trim(name, 40) for name in event.companies) or "-"
        lines.append(
            f"{trim(event.title, _TITLE_LIMIT)}\n"
            f"  {esc(event.status.value)} · {esc(event.event_type or 'unclassified')} · "
            f"{esc(age(event.first_seen_at, now=now))}\n"
            f"  impact: {companies} · importance "
            f"{esc(_confidence(event.importance))}"
        )
    return "\n".join(lines)


def render_research(view: ResearchView | None, query: str) -> str:
    if view is None:
        return (
            f"{bold('Research')}\nNo thesis found for {code(trim(query, 40))}. "
            "Try a proposal id, an event id, or a ticker."
        )
    lines = [
        bold("Research thesis"),
        f"{esc(view.action)} · confidence {esc(_confidence(view.confidence))} · "
        f"horizon {esc(view.horizon)}",
    ]
    if view.company or view.broker_ticker:
        lines.append(f"{trim(view.company, _NAME_LIMIT)} {code(view.broker_ticker or '-')}")
    if view.summary:
        lines.append(f"\n{bold('Summary')}\n{trim(view.summary, _TEXT_LIMIT)}")
    if view.bull_case:
        lines.append(f"\n{bold('Bull case')}\n{trim(view.bull_case, _TEXT_LIMIT)}")
    if view.bear_case:
        lines.append(f"\n{bold('Bear case')}\n{trim(view.bear_case, _TEXT_LIMIT)}")
    for title, items in (
        ("Catalysts", view.catalysts),
        ("Risks", view.risks),
        ("Invalidation", view.invalidation_conditions),
    ):
        if items:
            body = "\n".join(f"• {trim(item, _REASON_LIMIT)}" for item in items[:4])
            lines.append(f"\n{bold(title)}\n{body}")
    lines.append(
        f"\n{esc('Research is advisory. It contains no quantity, allocation or authorization.')}"
    )
    return "\n".join(lines)


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _confidence(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


# ---------------------------------------------------------------------------
# Pipeline stages
#
# Three different facts about one article, worded so the operator can tell them
# apart at a glance in a chat list. "Discovered" is the firehose, "considered
# relevant" is a judgement that cost one classifier call, and "promoted" is the
# moment the pipeline is about to spend real research money -- which is why the
# third carries the scores the promotion was made on and the first does not.
# ---------------------------------------------------------------------------

_STAGE_HEADLINES: dict[PipelineEvent, str] = {
    PipelineEvent.EVENT_DISCOVERED: "Article discovered",
    PipelineEvent.EVENT_RELEVANT: "Considered relevant",
    PipelineEvent.EVENT_CANDIDATE: "Promoted to research candidate",
    PipelineEvent.RESEARCH_STARTED: "Research started",
    PipelineEvent.RESEARCH_COMPLETED: "Research completed",
}


def stage_headline(event: PipelineEvent) -> str:
    return _STAGE_HEADLINES[event]


def render_event_stage(view: EventView, event: PipelineEvent) -> str:
    """One article, at one stage of the pipeline.

    Carries no link.  Untrusted headlines never become clickable destinations
    (see :mod:`stockbrain.telegram.formatting`), and a chat preview of a page
    StockBrain has not vouched for is not something this bot should generate.
    """
    lines = [
        f"{bold(esc(_STAGE_HEADLINES[event]))} — {esc(view.status.value)}",
        trim(view.title, _TITLE_LIMIT),
    ]
    if view.event_type:
        lines.append(f"Type: {esc(view.event_type)}")
    if view.companies:
        lines.append("Companies: " + esc(", ".join(view.companies)))
    scores: list[str] = []
    if view.importance is not None:
        scores.append(f"importance {view.importance:.2f}")
    if view.candidate_score is not None:
        scores.append(f"candidate {view.candidate_score:.2f}")
    if scores and event is not PipelineEvent.EVENT_DISCOVERED:
        lines.append(esc(" · ".join(scores)))
    lines.append(f"First seen {esc(stamp(view.first_seen_at))}")
    if event is PipelineEvent.EVENT_CANDIDATE:
        lines.append(
            esc(
                "Queued for research. Research is advisory and never authorizes a trade on its own."
            )
        )
    return "\n".join(lines)


def render_research_stage(view: ResearchRunView, event: PipelineEvent) -> str:
    """One research run, starting or finished.

    A finished run reports its action and confidence and nothing else from the
    model: hidden reasoning is not stored anywhere in StockBrain and therefore
    cannot be rendered here, and the confidence is a ranking rather than a
    calibrated probability -- which the message says, because a number in a chat
    reads as certainty unless it is told not to.
    """
    subject = view.company or view.broker_ticker or str(view.id)
    lines = [
        f"{bold(esc(_STAGE_HEADLINES[event]))} — {esc(subject)}",
    ]
    if view.broker_ticker and view.company:
        lines.append(f"Listing: {code(view.broker_ticker)}")
    if view.event_title:
        lines.append(f"Trigger: {trim(view.event_title, _TITLE_LIMIT)}")
    if event is PipelineEvent.RESEARCH_STARTED:
        lines.append(esc("Research is advisory. It never authorizes a trade on its own."))
        return "\n".join(lines)

    lines.append(f"Outcome: {esc(view.status.value)}")
    if view.action:
        confidence = f" · {view.confidence:.0%} confidence" if view.confidence is not None else ""
        horizon = f" · horizon {esc(view.horizon)}" if view.horizon else ""
        lines.append(f"Thesis: {bold(esc(view.action))}{esc(confidence)}{horizon}")
        lines.append(esc("Confidence is a model ranking, not a calibrated probability."))
    if view.summary:
        lines.append(trim(view.summary, _TEXT_LIMIT))
    if view.error_class:
        lines.append(f"Error: {esc(view.error_class)}")
    if view.estimated_cost_usd is not None:
        lines.append(f"Estimated cost: {esc(f'${view.estimated_cost_usd}')}")
    lines.append(esc(AUTHORIZATION_NOTICE))
    return "\n".join(lines)
