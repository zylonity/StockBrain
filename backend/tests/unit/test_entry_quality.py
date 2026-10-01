"""Headline classification for BUY triggers."""

from __future__ import annotations

import pytest

from stockbrain.proposals.entry_quality import headline_entry_problem


@pytest.mark.parametrize(
    "headline",
    [
        "What's Going On With SpaceX Stock Today?",
        "Why Is Meta Platforms Stock Gaining Monday?",
        "QUICK SPARK: AMD Stock Hits Fresh Record High, Up 183% This Year",
        "Nasdaq 100 Flirts with Records, Oil Sinks for Fifth Day: Stock Market Today",
        "Accenture shares are trading higher after the company announced a partnership",
        "Strategy Stock Rises 7% as Bitcoin Climbs to $85,000",
        "Full Transcript: SentinelOne Q2 2027 Earnings Call",
        "Meta vs. Amazon: The Battle to Own E-Commerce's Invisible Layer",
        "Canada Just Triggered a Mining Capex Rush: 2 Stocks Analysts Love Right Now",
    ],
)
def test_reports_of_a_move_and_recaps_are_refused(headline: str) -> None:
    assert headline_entry_problem(headline) is not None


@pytest.mark.parametrize(
    "headline",
    [
        "Costco Wholesale Q4 EPS $6.75 Beats $6.52 Estimate, Sales $95.723B Beat",
        "L3Harris Obtains THAAD Propulsion Contract Valued At $6B",
        "AstraZeneca Has Entered Into An Agreement To Invest $2B In Summit Therapeutics",
        "Micron Likely To Report Higher Q4 Earnings Ahead Of Earnings Call",
        "Lifecore Biomedical To Be Acquired By Webster Equity Partners",
        None,
        "",
    ],
)
def test_fresh_catalysts_are_allowed(headline: str | None) -> None:
    assert headline_entry_problem(headline) is None
