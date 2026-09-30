# News and candle outcomes

This first version gives research a dated, descriptive cohort of earlier news for
the same resolved company and event type. If no exact type match exists, it can
show a company-only cohort labelled `same_company_other_event_type_loose`.
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
`candles` and read-only `report` modes. It requires a database URL; backfill
requires timezone-aware start/end timestamps and one to five symbols. Each
invocation uses the fixed caps above. Apply the migration before using it.

The cache's general article query uses publication/observation time to answer
what was public by a past date. The research provider applies the stricter
boundary `ingested_at <= packet.as_of` to candles and uses source
`received_at <= packet.as_of` for news, so a packet cannot gain data imported
after its analysis time. Existing classified, resolved canonical events supply
the event type. Backfilled articles that have not been classified and linked to
a resolved company do not yet enter the matched cohort.
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
