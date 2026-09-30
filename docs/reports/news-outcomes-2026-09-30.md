# News-and-candle outcomes: 24-month multi-stock report (as of 2026-09-30)

Raw output: [`news-outcomes-2026-09-30.json`](news-outcomes-2026-09-30.json).
Method and live behaviour: [`../news-candle-outcomes.md`](../news-candle-outcomes.md).

## How it was produced

- Window: 2024-09-30 00:00 UTC to 2026-09-30 00:00 UTC.
  - **Exploration:** before 2026-03-30 (18 months).
  - **Untouched validation:** the last 6 months. Every threshold, bucket, delay
    and cost below was fixed before the validation slice was read.
- Universe, fixed before any outcome was inspected: AAPL, NVDA, JPM, XOM, JNJ,
  TSLA, INTC, PYPL, IONQ, WBA. SPY is the benchmark.
- Data:
  - Alpaca news (Benzinga).
  - Alpaca **SIP** one-minute bars, from a local run with
    `ALPACA_STOCK_FEED=sip`; the server's configured IEX feed is too sparse for
    minute-level windows.
  - Loaded into a separate local database with
    `news_outcomes_cli universe --as-of 2026-09-30T00:00:00Z`: 2,175 seven-day
    windows, **0 partial or failed**.
- Report: `python -m stockbrain.intelligence.news_outcome_report` (read only).
- Measurement rules:
  - Regular session only (09:30–16:00 ET).
  - Entry is simulated at publication + 1/5/15/60 min, at the next minute bar.
    History has no actual live receipt; 5 min is the primary delay.
  - 10 bps round-trip spread proxy.
  - Market-relative return = stock return − SPY return.
- Deduplication:
  - One observation per grouped story.
  - A story tagged with 2–3 universe tickers is pooled once.
  - Stories tagged with more than 3 tickers are market round-ups and are
    excluded; the per-symbol counts are below.

## Coverage (nothing dropped)

| Symbol | Stories measured | Round-ups excluded | Minute bars (RTH) | Bars cover |
|---|---:|---:|---:|---|
| AAPL | 3,012 | 3,142 | 195,019 | full window |
| NVDA | 4,602 | 5,566 | 195,375 | full window |
| JPM | 1,009 | 759 | 194,622 | full window |
| XOM | 575 | 358 | 194,625 | full window |
| JNJ | 632 | 268 | 194,511 | full window |
| TSLA | 4,701 | 3,690 | 195,382 | full window |
| INTC | 1,028 | 1,444 | 195,220 | full window |
| PYPL | 496 | 281 | 194,676 | full window |
| IONQ | 403 | 333 | 195,083 | full window |
| **WBA** | 113 | 71 | 88,309 | **ends 2025-08-27 (taken private)** |

WBA stays in the denominator. It contributes 112 exploration events, 1
validation story and no validation outcome. This is survivorship in reverse:
the holdout contains only listings that survived.

## Overall: exploration vs validation (5-minute simulated arrival, net of costs)

**How to read these tables.** "Stories" counts news items. "Sessions" counts
distinct entry sessions, which is the independent sample. Many stories on one
day share one market path, so read the 95% intervals in the JSON (computed per
story) as too narrow.

| Period | Horizon | Usable stories | Sessions | Positive / negative | P(positive) | Median net | p10 / p90 |
|---|---|---:|---:|---|---:|---:|---|
| Exploration | 1h | 11,492 | 375 | 4,886 / 6,606 | 0.425 | −0.13% | −1.64% / +1.18% |
| Exploration | 1d | 11,928 | 372 | 5,650 / 6,278 | 0.474 | −0.13% | −3.73% / +3.36% |
| Exploration | 5d | 11,550 | 372 | 5,612 / 5,938 | 0.486 | −0.16% | −6.44% / +6.87% |
| Validation | 1h | 3,771 | 127 | 1,594 / 2,177 | 0.423 | −0.17% | −1.86% / +1.15% |
| Validation | 1d | 3,850 | 125 | 1,779 / 2,071 | 0.462 | −0.18% | −3.73% / +2.98% |
| Validation | 5d | 3,662 | 121 | 1,962 / 1,700 | 0.536 | +0.38% | −6.93% / +7.21% |

- There were no flat outcomes.
- Unavailable outcomes are counted separately in the JSON, never as zero:
  exploration 533 / 97 / 475 (1h / 1d / 5d), validation 151 / 72 / 260.
- About 95% of 1-day windows contain later news on the same company. They are
  measured and flagged, not dropped.

**Reading:**

- Buying "a news story" on these names is roughly a coin flip against SPY
  after costs, with wide tails.
- The 5-day validation median turned positive while exploration's was
  negative. That looks like a regime difference, not a stable edge.

## Are we joining too late?

**Time to the first market-relative up-move, measured from the last
pre-publication close:**

| Period | Threshold | Crossed after feasible entry | Before simulated receipt | Before entry | Not reached (full 5-day window) | Unknown | Median trading minutes to cross |
|---|---|---:|---:|---:|---:|---:|---:|
| Exploration | +0.5% | 10,060 | 209 | 46 | 1,571 | 139 | 38 |
| Exploration | +1% | 9,269 | 47 | 15 | 2,468 | 226 | 137 |
| Exploration | +2% | 7,455 | 12 | 3 | 4,213 | 342 | 391 |
| Validation | +0.5% | 3,288 | 73 | 9 | 503 | 49 | 33 |
| Validation | +1% | 3,012 | 28 | 6 | 798 | 78 | 136 |
| Validation | +2% | 2,472 | 7 | 1 | 1,289 | 153 | 392 |

- Median clock time to +1% is about 19 hours, versus about 2.3 trading hours,
  because overnight and weekends count in clock time.
- Only about 0.5% of events had already moved +1% before a 5-minute arrival
  could act.
- Most crossings happen well after entry, and a +1% relative move within 5
  sessions is common background volatility for these names. "Crossing" is
  therefore not evidence the news caused the move.

**The comparison that matters: after arriving, given how far the stock had
already moved, what happened next?** All events at a 5-minute arrival, net of
costs:

| Move already made at entry | Exploration 1d: P(+), median (sessions) | Validation 1d | Exploration 5d | Validation 5d |
|---|---|---|---|---|
| ≤ −1% | 0.499, −0.00% (197) | 0.326, −0.57% (79) | 0.505, +0.30% (190) | 0.493, −0.44% (75) |
| −1% to +0.5% | 0.481, −0.08% (372) | 0.479, −0.13% (125) | 0.496, −0.05% (372) | 0.543, +0.45% (121) |
| +0.5% to +1% | 0.475, −0.16% (220) | 0.466, −0.02% (77) | 0.504, +0.12% (213) | 0.502, +0.29% (71) |
| +1% to +2% | 0.461, −0.60% (169) | 0.469, −0.18% (64) | 0.439, −1.09% (164) | 0.432, −0.53% (61) |
| > +2% | 0.374, −1.22% (111) | 0.493, −0.38% (39) | 0.388, −2.35% (109) | **0.710, +1.78% (38)** |

**What held up in validation:**

- An entry after the stock had already risen 1–2% relative to SPY did worse
  than an early entry, at 1d and 5d, in both periods.
- In exploration, chasing a move above 2% was the worst bucket, with a 5-day
  median of −2.35%.

**What did not hold up:** the > +2% bucket **reversed** in validation, with a
5-day median of +1.78% over 38 sessions. That is too few independent sessions
and too unstable to encode as a rule.

The 1/15/60-minute delays in the JSON tell the same story. Arrival delay
matters much less than the move already made at entry.

## By headline type (keyword rule, report only)

| Type | Exploration 1d P(+), median | Validation 1d | Exploration 5d | Validation 5d |
|---|---|---|---|---|
| EARNINGS | 0.435, −0.40% | 0.355, −0.99% | 0.429, −1.04% | 0.450, −0.91% |
| ANALYST | 0.444, −0.28% | 0.374, −0.86% | 0.476, −0.30% | 0.483, −0.20% |
| M_AND_A | 0.550, +0.08% | 0.671, +1.05% | 0.555, +0.30% | 0.686, +2.09% |
| LEGAL_REGULATORY | 0.490, −0.03% | 0.309, −0.69% | 0.504, 0.00% | 0.656, +1.11% |
| GUIDANCE | 0.475, −0.28% | 0.413, −0.33% | 0.409, −1.64% | 0.556, +0.31% |
| OTHER | 0.480, −0.09% | 0.484, −0.10% | 0.493, −0.08% | 0.546, +0.48% |

- M&A headlines were the only type positive in both periods. There are only
  about 50 validation sessions, and the direction of a deal (acquirer vs
  target) is not distinguished.
- Earnings and analyst headlines lost after costs in both periods.

## Catalyst deadline vs holding (derived on exploration, tested on validation)

| Type | Exploration sample (responders / non) | Deadline (sessions, incl. +1 buffer) | Validation pairs | Deadline exits | Rule better / hold better / tie |
|---|---|---:|---:|---:|---|
| ANALYST | 1,076 (847 / 229) | 5 | 323 | 0 | 0 / 0 / 323 |
| EARNINGS | 1,008 (771 / 237) | 6 | 347 | 0 | 0 / 0 / 347 |
| LEGAL_REGULATORY | 222 (170 / 52) | 6 | 64 | 0 | 0 / 0 / 64 |
| M_AND_A | 164 (139 / 25) | 3 | 70 | 29 | **11 / 18** / 41 |
| OTHER | 8,848 (7,056 / 1,792) | 5 | 2,795 | 0 | 0 / 0 / 2,795 |
| GUIDANCE | 148 (109 / 39) | none — response rate below 75% | — | — | — |

- For most types, the evidence-based deadline (75th percentile of response time
  across all complete events, including non-responses, plus one session) falls
  at or beyond the 5-session horizon. The rule therefore never exits early.
- Where it could exit (M&A, 3 sessions), holding beat the deadline exit 18 to
  11, with a median advantage of 0.
- **Conclusion: there is no support for automatic deadline exits.** Keep
  `NEWS_CATALYST_EXIT_PROPOSALS_ENABLED=false`. Deadline reviews then stay
  advisory reassessments.

## Example live research packet

Source: [`news-outcomes-example-packet.json`](news-outcomes-example-packet.json),
built from the report database.

- Current story (NVDA, 2026-09-29 21:16 UTC): *"NVIDIA's Jensen Huang Says
  Declaration Agreed At White House Has A Lot Of Teeth"*.
- `as_of` is after the cache import.
- The `news_outcomes` datum the researcher receives says:

  - `match: same_company_event_type_unverified_loose`: backfilled stories carry
    no classifier type.
  - `evidence_weight: sparse_or_loose_give_little_or_no_weight`.
  - 24 candidate stories: only those old enough for the 5-day window to close,
    most recent first.
  - 5-day result: 24 of 24 positive. But this is **6 distinct entry sessions**,
    and all 24 windows contain later NVDA news, so no median or mean is shown.
  - The examples label `actual_live_received_at: null`, show a separate
    `cache_imported_at`, and use `entry_timing: simulated_publication_plus_5m`.
    One example is an unrelated biotech IPO story tagged NVDA, which shows the
    noise in loose matching.
  - `catalyst_deadline_evidence.reason: sparse_comparable_history`, so no
    quantitative deadline is set.

**Decision effect, by design:** this history should carry little or no weight.
The researcher's BUY/SELL/HOLD/NO_ACTION rests on the current article and the
current price reaction, and any resulting proposal still passes every risk,
authorization and execution check. No LLM research call was made for this
example.

## Example failed-reaction reassessment

This comes from the database-backed integration scenario
`test_a_failed_catalyst_triggers_one_review_that_favours_an_exit`, with
fixture prices. A filled news-driven BUY recorded at entry:

```
entry_at 2026-09-08 14:00 UTC (order_submitted_at_proxy — no broker fill time)
expected +1.00% market-relative, deadline 2 sessions after entry (1 + 1 buffer),
supporting sample 12 (10 responders, 2 non-responses)
```

At the close of the second session the monitor measured +0.20% and requested
exactly one `CATALYST_DEADLINE_POSITION_REASSESSMENT`. That review's packet
contains:

```json
"catalyst_review": {
  "expected_market_relative_move": "0.01000000",
  "deadline_sessions_after_entry": 2,
  "observed_market_relative_move_since_entry": "0.00200000",
  "measurement": "bar_proxy",
  "outcome": "expected_reaction_failed",
  "guidance": "The expected post-entry reaction did not occur by the deadline fixed at entry. Unless current evidence newly sustains the thesis, favour SELL or REDUCE over repeating the original BUY."
}
```

The verdict is advisory while exit proposals are disabled:

- it creates no proposal;
- the exit sweep and rebalance ignore it;
- a second monitor tick does not request another review.

## Limitations (unknown is not zero)

- **Feed:**
  - SIP minute bars were used here. Production uses IEX unless reconfigured,
    and IEX minute data is too sparse for these windows.
  - Extended hours are excluded, so pre-market news enters at the 09:30 open.
- **Session:**
  - Session days are inferred from bar dates, not an exchange calendar.
  - Early closes are handled as past days.
- **Spread and slippage:** a flat 10 bps round-trip proxy. There are no
  historical quotes and no fees.
- **FX:** not applied. Every name is USD, but a GBP account pays FX on entry
  and exit.
- **Corporate actions:** raw bars. Split-like jumps are excluded as suspected
  splits, which could also drop a genuine gap.
- **Confounding news:** most daily windows contain later company news. It is
  flagged and counted, not removed. Round-ups tagged with more than 3 tickers
  are excluded.
- **Independence:**
  - Stories cluster by day. Statistics are gated on distinct sessions, but the
    JSON's Wilson intervals are per story and too narrow.
  - The universe is 10 names over 2 years (~500 sessions).
- **Selection:**
  - This is a feed-selected set of stories for a small, pre-chosen universe.
  - Headline types come from a keyword rule, not the live classifier.
  - Backfilled stories cannot form exact-type cohorts in live research, so
    history is labelled loose and low-weight until classified live events
    accumulate.
- **Missing data:** unavailable windows are reported per horizon (e.g.
  validation 5d: 260).
- **Timing:** history has no live receipt, so the "before receipt/entry"
  phases use a simulated arrival.

## Verification

- Backend suite: **2,546 passed**, 30 live tests deselected, run against an
  isolated local test database.
- `ruff check`, `ruff format --check` and `mypy stockbrain tests` are clean.
- Independent review by a DeepSeek `dsh` agent: one HIGH, one MEDIUM and
  several LOW findings, all fixed. The HIGH was that the proposal backlog
  could turn an advisory review into an exit proposal. Its final verdict: all
  six guarantees hold.
