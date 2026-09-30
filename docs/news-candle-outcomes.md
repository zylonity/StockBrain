# News and candle outcomes

## Registered 24-month validation plan (set before reading outcomes)

The fixed as-of date for the example report is 2026-09-30 00:00 UTC. Include
publication times from 2024-09-30 00:00 UTC up to that instant. Use the earlier
18 months (before 2026-03-30 00:00 UTC) for exploration and reserve the last
six months for chronological validation. Do not tune thresholds, delay bins,
cost assumptions, or the universe after inspecting the validation slice.

The preselected symbol universe is **AAPL, NVDA, JPM, XOM, JNJ, TSLA, INTC,
PYPL, IONQ, and WBA**, with SPY as benchmark. It spans technology, banking,
energy, healthcare, autos, payments and a smaller speculative listing; the
intent is to include both strong and weak price paths, subject to verified
coverage. WBA is retained even if delisted data cannot be retrieved. Missing
symbols, delisted periods, sparse news, and failed candle requests remain in
the denominator of the coverage report. A stock with no usable events cannot
be silently dropped and replaced after results are inspected.

Deduplicate articles by canonical event ID before aggregating. Report exact
company-and-event-type matches separately from company-only fallback matches.
For each event show the published timestamp, actual live receipt when known,
historical cache-import timestamp, simulated entry, entry-to-publication delay,
and net 1h/1d/5d results with explicit unknown statuses. Backfilled receipt
must never be called an observed live receipt. Threshold response times at
+0.5%, +1%, and +2% are measured from completed, aligned stock and SPY bars.
Show both wall-clock and observed trading-session time, with missing/overnight
cases labelled. Report response phase relative to actual live receipt and
feasible entry; if actual live receipt was not stored, its phase is unknown.

The relevant trading comparison conditions on both actual receipt delay and
the already observed market-relative move at entry. Include positive, flat,
negative and unavailable net results in every cohort. Suppress probability
and median below ten usable observations. A deadline requires at least ten
complete comparable events and a 75% observed +1% post-entry response rate;
the 75th percentile is taken across *all* complete events, so nonresponses
can prevent a deadline. Add one configured trading session, not 24 clock
hours. A missing or sparse cohort has no quantitative deadline.

The offline validation functions live in
`stockbrain.intelligence.news_outcome_validation`. They consume cached,
deduplicated event outcomes and aligned bars only; they do not query a vendor,
make a trading decision, or write to a live store. The held-out six months
must be reported separately, including a zero-coverage result if the local
test cache is empty. The current checkout has no verified 24-month cached
dataset, so numerical performance claims remain unavailable pending a
bounded local backfill.

This first version gives research a dated, descriptive cohort of earlier news for
the same resolved company and event type. If no exact type match exists, it can
show a company-only cohort labelled `same_company_event_type_unverified_loose`.
Backfilled articles carry no classifier event type, so a cohort built only from
backfill is always the loose company-only match; the packet then marks
`evidence_weight: sparse_or_loose_give_little_or_no_weight` and no
quantitative catalyst deadline is derived.
Articles linked to one canonical event count once. The research packet contains
at most eight examples and summary counts from at most 24 earlier events; it
never contains raw candle series. No result changes a category preference,
position size, or execution rule.
Packet enrichment is off by default (`NEWS_OUTCOME_PACKET_ENABLED=false`) until
the operator has validated cache coverage and examples.

## Measurements

- The publication reference is the last completed pre-publication bar. The
  earliest possible entry is the first completed bar open at least five minutes
  after StockBrain received a linked article. Publications outside a market
  session wait for the next observed session. The publication-to-entry result
  reports the market-relative move that had already occurred.
- The calculator measures market-relative returns after entry at one trading
  hour, one trading day and five trading days. Daily horizons use observed
  session dates; a target session must have fully elapsed by `as_of`. Missing
  bars, in-session gaps, detected split discontinuities and overlapping news
  cause explicit unavailable results.
- The research provider uses a fixed 10 bps round-trip spread **proxy**. It is
  not a historical quote or measured slippage. When a listing currency differs
  from the USD benchmark, an FX-adjusted net outcome requires aligned historical
  FX bars. The research provider does not fetch them, so that number is
  unavailable. The pure calculator accepts FX bars and an explicit FX cost for
  offline studies.
- All examples are descriptive. Means and medians are withheld when a horizon
  has fewer than ten usable examples. The calculator's own aggregate output is
  always marked `research_only`, including when a larger sample exists.

## Cache and usage

`NewsHistoryCache` in `backend/stockbrain/intelligence/news_history.py` persists
news, minute bars, coverage and backfill runs. Backfill calls are explicit;
research reads the database only. `BackfillLimits` defaults to seven days,
500 articles, 20 bar pages and 20 requests per run. A bound that truncates a
requested window is recorded as partial coverage with a stop reason. Call
`backfill_news(..., client=AlpacaNewsClient(...), limits=...)` and
`backfill_candles(..., provider=AlpacaMarketDataClient(...), limits=...)` for
selected symbols and windows; do not put these calls in the research job path.
`python -m stockbrain.intelligence.news_outcomes_cli` provides explicit `news`,
`candles`, `universe` (resumable 24-month backfill of the registered universe
in seven-day windows; SUCCEEDED windows are skipped, PARTIAL/FAILED ones stay
audited and are retried) and read-only `report`/`validation` modes. It requires a database URL; backfill
requires timezone-aware start/end timestamps and one to five symbols. Each
invocation uses the fixed caps above. Apply the migration before using it.

The cache's general article query uses publication/observation time to answer
what was public by a past date. The research provider applies the stricter
boundary `ingested_at <= packet.as_of` to candles and cached articles (and
source `received_at <= packet.as_of` for live-linked news), so a packet cannot
gain data imported after its analysis time. Backfill links an article to a
company only when its single ticker matches one `Company.primary_symbol`; it
never creates an `EventCompanyImpact`, `Source` or `EventSourceLink`, and its
grouping events are `ARCHIVED`, so nothing reaches the classification sweep.
Cached examples report `actual_live_received_at: null`, the separate
`cache_imported_at`, and `entry_timing: simulated_publication_plus_5m`.
The packet exposes each candle symbol's recorded coverage and partial flag when
that metadata existed by `as_of`. Coverage ranges are audit hints, not proof
that every trading minute or historical event was captured.

## Live trade diagnostic

`run_news_trade_diagnostics(database, as_of=...)` reads live BUY broker orders
and prefers an explicit fill timestamp in the broker payload. The stored
Trading 212 order payload currently lacks one, so submission time is shown
separately as a proxy; actual publication-to-fill delay remains unknown for
those rows. It reports counts and
outcome summaries by publication-to-fill delay and by pre-fill move. It keeps
closed thesis grades separate from interim observed windows, and
uses only completed CLOSE grades in the timing and move association buckets. It
counts missing source, fill, bar and outcome data. It is read only. A broker
order submission or proposal execution timestamp is never labelled as a fill.
The output is an association among selected trades, not evidence that reducing
delay would cause better results. Thesis grades use daily market closes and a
reference entry price, so they are not actual fill-to-sale P&L.

## Data limits to validate

- Historical minute-bar coverage and feed entitlements vary by symbol. One
  provider/feed is selected per series. The backfill adapter currently requests
  raw Alpaca bars; the calculator excludes split-like ratios but cannot
  prove a corporate action occurred. A genuine large news gap, such as a 50%
  rise, may therefore be excluded as a suspected split. Validate corporate
  actions and vendor adjustment policy before relying on a long-window return.
- Observed candle dates are a session proxy, not a complete exchange holiday
  calendar. Sparse trading and early closes can leave windows unavailable.
- SPY is a broad US benchmark, not a sector or currency-matched control. A
  company-only fallback mixes event types. The classified and resolved news set
  is selected and incomplete, especially before feed coverage began.
- Historical bid/ask, actual FX conversion charges, fees and slippage are not
  stored for most prior events. The spread proxy and explicit FX-unavailable
  status are the limits of the net-return claim. Any live fill analysis also
  depends on broker payload timestamps and coverage of closed outcomes.

## Live research with cached outcomes

With `NEWS_OUTCOME_PACKET_ENABLED=true` each research run reads the cache
(database only, bounded to 24 candidates / 20,000 bars per series) and adds one
`news_outcomes` datum to the packet: the matched cohort, positive/flat/negative
counts per horizon, medians only at n >= 10, `evidence_weight`, coverage, and
`catalyst_deadline_evidence`. It is contextual evidence for the
BUY/SELL/HOLD/NO_ACTION judgement. It never places an order, sizes a position
or bypasses a risk, authorization or execution check.

**Cache miss.** When the cohort is empty, or every candidate lacks candles, the
finished run enqueues one `NEWS_OUTCOME_BACKFILL` job keyed by
company and a fixed UTC window (at most 14 days). Later runs for the same
company/window attach their run IDs to the pending job instead of adding
another. Research is never blocked, and the execution policy while the cache is
missing is unchanged. Only first-pass (`previous_thesis_id is None`) runs can
enqueue, so a reassessment cannot start another backfill.

**After backfill.** A PARTIAL window fails the job (retried once, audited). A
complete window with new articles requests one reassessment per affected
`EVENT` run, with a deterministic `uuid5` rerun ID, so redelivery cannot
duplicate it. It uses a fresh `as_of`:

- no order executed -> trigger `NEWS_CACHE_REASSESSMENT`. The proposal sweep
  invalidates the superseded thesis's pending proposal. The execution claim
  also refuses any approved BUY whose thesis has been superseded. A new
  proposal gets a fresh quote and the full risk checks.
- BUY already executing/filled -> trigger
  `NEWS_CACHE_POSITION_REASSESSMENT`, **advisory only**. No proposal is
  generated, and the exit sweep ignores its verdict, so a completed backfill
  alone never reverses a position.

## Catalyst deadline and failed reaction

`CatalystMonitor` (scheduled every 15 minutes when the packet flag is on)
records, for each filled news-driven BUY:

- the entry-time `catalyst_deadline_evidence` (threshold, sample size,
  responders, nonresponses, buffer, and deadline sessions);
- the entry time, from the broker fill time when present, otherwise the
  submission time, labelled `order_submitted_at_proxy`.

Sparse evidence stores no quantitative deadline (`evidence_insufficient`). The
row is written once per broker order and never recomputed or extended.

After the deadline:

1. The monitor tops up post-entry minute bars (stock and SPY) from the newest
   cached bar.
2. Once the deadline sessions have completed for both series, it measures the
   market-relative move since entry.
3. It requests exactly one `CATALYST_DEADLINE_POSITION_REASSESSMENT`. The
   packet's `catalyst_review` block gives the fixed expectation, the observed
   move, and, if the reaction failed, the instruction to favour SELL or REDUCE
   unless current evidence newly sustains the thesis.

That verdict is advisory while `NEWS_CATALYST_EXIT_PROPOSALS_ENABLED=false`
(the default). Enable it only after the offline deadline-exit-versus-hold
comparison (below) supports it. Even then, a SELL/REDUCE goes through the
ordinary proposal, risk, authorization and execution path.

## Offline 24-month report

```
PYTHONPATH=backend backend/.venv/bin/python -m stockbrain.intelligence.news_outcome_report \
  --database-url <local report db> --as-of 2026-09-30T00:00:00Z > report.json
```

The report is read only. It covers exploration and validation separately:

- entry status;
- net 1h/1d/5d distributions with positive, flat, negative and unavailable
  counts, the Wilson 95% interval for P(positive), and p10/median/p90 at
  n >= 10;
- time to the first +0.5/+1/+2% market-relative move, by phase relative to
  simulated receipt and feasible entry;
- arrival cohorts: every event at a simulated arrival of 1/5/15/60 minutes,
  bucketed by the move already made;
- deadlines derived on exploration only, with deadline-exit versus 5-session
  hold tested on the validation holdout.

`headline_type` is a keyword rule used only by this report. Multi-ticker
round-ups (more than 3 tagged symbols) are counted as excluded.

## Deploying with live research enabled

The following was tested locally only. Nothing has been pushed or deployed.

1. Merge `feature/news-candle-outcomes` and build the image as usual.
2. Apply migrations (`alembic upgrade head`; this adds `e93b7c14a6d2` news
   cache and `b20e45a0d933` `news_catalyst_deadlines`).
3. Seed history before enabling, from a host with Alpaca credentials:
   `python -m stockbrain.intelligence.news_outcomes_cli universe --database-url
   "$DATABASE_URL" --as-of <now, UTC>`.
   - Set `ALPACA_STOCK_FEED=sip` for that one command if the account permits
     it; IEX minute bars are sparse.
   - The command is resumable, so rerun it until it reports no
     partial/failed windows.
   - Held/watch-list symbols outside the registered universe can be seeded
     with the `news`/`candles` modes.
4. Set `NEWS_OUTCOME_PACKET_ENABLED=true`. Keep
   `NEWS_CATALYST_DEADLINE_BUFFER_SESSIONS=1` and
   `NEWS_CATALYST_EXIT_PROPOSALS_ENABLED=false`.
5. Restart. Confirm the `news_catalyst_deadlines` scheduler task is running
   and that new research packets contain a `news_outcomes` datum.
6. Watch `NEWS_OUTCOME_BACKFILL` jobs and `news_history_backfill_runs` for
   PARTIAL/FAILED windows.
