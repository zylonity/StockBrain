# Short-term strategy tests: pre-registration

Written 2026-10-01, before any strategy was run on this data.

## Data and universe

- 104 symbols: S&P 100 constituents as recalled for 2025–26, plus PLTR, UBER,
  ANET and SPY.
- Daily bars from Alpaca SIP, adjusted for splits and dividends, 2019-01-02 to
  2026-09-29.
- Known bias: these are today's large caps, so the list is survivorship-biased
  in favour of the stocks that did well. Dip-buying and buy-and-hold look
  better than they really are.

## Periods

Each period is simulated separately, starting from the same capital.

- **Exploration:** 2019-01 to 2023-12. Covers the 2020 crash and the 2022 bear
  market.
- **Validation:** 2024-01 to 2026-09. No parameter is changed after the
  exploration results are seen.

## Execution model (long only, as in a Trading 212 share account)

- Signals use only data known before the entry. Every entry is at a day's
  **open**.
- Rules decided from the prior close enter at the next open. Gap rules enter at
  the gap day's open, which is optimistic because real market orders fill a
  little after the open.
- **Costs:**
  - Base case: 0.20% per side (spread plus Trading 212's 0.15% GBP↔USD fee),
    0.40% per round trip.
  - Stress case: 0.30% per side.
- Cash earns 0%. That's conservative, because Trading 212 pays interest on
  cash.
- **Portfolio:**
  - At most 10 positions, each funded with a tenth of current equity.
  - If more signals arrive than there are free slots, the strongest signal
    wins.
  - No leverage.

## Strategies

Parameters are textbook values, not tuned.

| ID | Rule | Exit |
|---|---|---|
| S0 | SPY buy and hold | — |
| S1 | SPY when its close is above its 200-day average (checked monthly), else cash | monthly check |
| S2 | Gap-down rebound: open ≤ −2% vs prior close, relative to SPY's gap. Buy at the open; bigger gap first | close 5 sessions later |
| S3 | Gap-up continuation: open ≥ +2% relative. Buy at the open | close 5 sessions later |
| S4 | RSI(2) pullback: close above the 200-day average and 2-day RSI < 10. Buy next open; lowest RSI first | next open after a close above the 5-day average, or after 10 sessions |
| S5 | Weekly reversal: first session of each week, buy the 10 worst 5-day performers relative to SPY | next week's first open |
| S6 | Weekly momentum: the 10 best 5-day performers | next week's first open |
| S7 | Monthly 12-1 momentum: first session of each month, buy the top 10 by return over the prior 12 months excluding the latest month | next month's first open |
| S8 | Big up-day with volume: close-to-close return vs SPY ≥ +4% and volume ≥ 2× the 20-day average. Buy next open | close 10 sessions later |
| S9 | Big down-day with volume: return vs SPY ≤ −4% and volume ≥ 2× the 20-day average. Buy next open | close 5 sessions later |

## What counts as a result

- **Primary measure:** validation-period annual return (CAGR) and Sharpe ratio
  after base costs, compared with S0 (SPY) over the same period.
- **Also reported:** maximum drawdown, trades per year, average net trade, win
  rate, time invested, and the stress-cost case.
- **Multiple testing:** ten strategies are tried, so some will look good by
  chance in exploration. A strategy "works" only if it beats SPY on
  risk-adjusted return (Sharpe) in **both** periods after base costs **and**
  stays positive under stress costs.

## Addendum (2026-10-01): news filter for gaps

Written after the price-only results (S2 and S3 fail after costs) and before any
news was fetched for this universe.

- **B, no-news gap-down rebound:**
  - Signal: open ≤ −2% vs the prior close, relative to SPY's gap.
  - "No news": no Alpaca/Benzinga story tagged with ≤ 3 tickers that mentions the
    symbol, published between 16:00 ET on the prior session and 09:30 ET on the
    gap day.
  - Trade: buy at the open, exit at the close 5 sessions later.
- **Controls:** gap-downs with news, gap-ups with news, gap-ups without news.
- **Reporting:** event-level net returns (average, median, share of winners,
  distinct days) plus the 10-slot portfolio simulation, for both periods, at
  0.20% and 0.30% per side.
- **Known risk:** news coverage may be thinner in earlier years, so "no news"
  could partly mean "not covered". Story counts per year are reported.

## Addendum 2 (2026-10-01 02:00 UTC): volatile-stock universe

Written before any data was fetched or run. The same strategies S0–S9, with the
same parameters, costs and periods, are run on a separate universe of
high-volatility US stocks. Nothing is retuned.

Universe, chosen from memory and including failures:

- TSLA, NVDA, AMD, PLTR, COIN, MSTR, RIOT, MARA, SOFI, HOOD, RIVN, LCID, NIO
- PLUG, FCEL, GME, AMC, UPST, AFRM, SNAP, ROKU, SHOP, NET, CRWD, DKNG
- IONQ, RGTI, QBTS, SMCI, CVNA, AI, BBAI, SOUN, OPEN, CHPT, RUN, ENPH, BYND
- PTON, SPCE, MRNA, NVAX, XPEV, LI, U, PATH, TLRY, CGC, BB, CLOV
- Delisted or bankrupt, included where Alpaca has data: NKLA, WKHS, GOEV,
  RIDE, BBBY, WE, FFIE, MULN

Missing symbols are reported, never replaced. SPY stays the benchmark.

## Addendum 3 (2026-10-01 ~03:00 UTC): post-earnings drift from headline surprises

Written before any earnings data was fetched.

- **Data:**
  - Alpaca/Benzinga news headlines for the 104 large-cap symbols, 2019-01 to
    2026-09.
  - Only headlines of the form "… EPS $A Beats|Misses|In-Line $B Estimate"
    (optionally with "Sales $C Beat|Miss $D Estimate") are kept.
  - The first such headline per symbol per day is the event.
- **Surprise:** EPS (A − B) / |B|. Sales beat or miss as stated.
- **Entry:** the first regular-session open after publication. A pre-market
  headline enters that day's open; an after-close headline enters the next
  day's open.
- **Rules, long only:**
  - **E1:** EPS beat **and** sales beat ("double beat"); buy, hold 20 sessions,
    exit at the close.
  - **E2:** EPS surprise ≥ +5%; buy, hold 20 sessions.
  - **Control E0:** EPS miss **and** sales miss; reported as a comparison, not
    traded.
- **Reporting:**
  - Event level: net average, median, share of winners and SPY-relative
    return, for both periods, at 0.20% per side.
  - The 10-slot portfolio simulation with the same engine.
- **Pass criterion:** the same as before. Beat SPY on Sharpe in both periods,
  and stay positive under 0.30% per side.
