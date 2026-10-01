# Audit: short-term strategy backtests (`backtest.py`, `daily.csv` / `volatile.csv`)

## Verdict

The stated conclusion survives audit: on both universes, after 0.20% per side, no
strategy beats SPY buy-and-hold on Sharpe in **both** periods, and the
high-turnover rules are destroyed by costs. I found no look-ahead in any signal and
no accounting error that materially moves the numbers. There is one genuine latent
bug (negative-index wrap in the SPY trend curve) whose measured impact on this data
is zero, plus several low-severity methodological/reporting issues. Details below.

Reproduced baseline (0.20%/side): SPY Sharpe 0.81 (exploration) / 1.29
(validation). Best non-SPY Sharpe per period: exploration S8 0.59 / S7 0.71
(volatile), validation S7 1.18 / 0.96 (volatile) — all below SPY in at least one
period. Cost sensitivity confirms the cost claim (e.g. S4 validation Sharpe
1.29 gross → 0.13 base → −0.44 stress; S5 validation 1.42 → 0.48 → 0.01).

## Findings

### F1 — `spy_curve` reads the *last* bar of the file for the first bar of the exploration period
- **Severity:** Medium defect, **zero measured impact here**.
- **Where:** `backtest.py:215` (`want = C.iat[i - 1, col] > sma200.iat[i - 1, col] ...`).
- **Scenario:** For exploration `lo_i = 0` (data starts 2019-01-02) and `i == lo_i` is
  an explicit check, so `i - 1 == -1` indexes the **last** row (2026-09-29) instead of
  being absent. The 2019-01-02 S1 state is therefore decided with 2026 SPY data.
- **Impact:** none on this dataset by coincidence. `C.iat[-1] = 764.20`,
  `sma200.iat[-1] = 715.70`, so `want = True`, identical to the intended
  `NaN → True` fallback. Confirmation: S1's 2019 return equals S0's (33.1%).
  If the final bar were below its 200-day SMA, S1 would incorrectly open the
  exploration in cash and miss January 2019 (~+8%), cutting exploration CAGR by
  ~1–2pp. Validation is unaffected (`lo_i = 1258`, so `i-1` is 2023-12-29).
- **Fix:** `if i == lo_i and trend: want = True` (or guard `i > 0` before indexing).

### F2 — Trades whose exit falls past the period end are dropped, not marked to market
- **Severity:** Low (small, direction-dependent bias).
- **Where:** `backtest.py:154` (`t.exit_i <= hi_i`).
- **Scenario:** A Dec-2023 monthly S7 entry (exit Jan-2024 open) or the last days'
  fixed-hold entries are removed entirely, so that capital sits in 0%-cash for the
  tail instead of being invested. For monthly holds this drops a whole month.
- **Impact (recomputed, include-instead-of-drop variant):**
  - `daily.csv` exploration: S7 Sharpe 0.48 → 0.51, CAGR 9.5% → 10.4%; S8 CAGR
    7.9% → 8.0%; all other strategies ≤0.1pp.
  - `volatile.csv` exploration: S7 Sharpe 0.71 → 0.74, CAGR 26.6% → 29.0%; S6 0.62 →
    0.60; others ≤0.3pp. Validation ~unchanged.
  - No conclusion changes (SPY exploration Sharpe 0.81).
- **Secondary effect:** open trades are excluded from `closed`, so `trades_per_year`,
  `win_rate` and `avg_trade_net_pct` slightly understate the tail.
- **Fix:** include any trade with `lo_i <= entry_i <= hi_i` and mark it to market at
  `hi_i` (or extend the simulation to the last exit).

### F3 — `grade_theses.py` computes a duplicate flag but never uses it
- **Severity:** Low.
- **Where:** `grade_theses.py:27` (`dup = (sym, action, r["headline"]) in seen`).
- **Scenario:** Repeated identical verdict rows (same symbol/action/headline) are all
  counted, overweighting repeated headlines and inflating effective `n`.
- **Impact:** cannot be recomputed (no `theses.csv` present); reported
  means/medians/beat-rates are biased toward duplicated rows.
- **Fix:** `if dup: continue` after `seen.add(...)`.

### F4 — News-event benchmark comparison mixes net strategy returns with gross SPY
- **Severity:** Low.
- **Where:** `gap_news_analysis.py:44` (`rel = raw - (bt.C.iat[x, spy]/bt.O.iat[i, spy] - 1)`).
- **Scenario:** `raw` already includes the 0.40% round trip; the SPY leg is gross. The
  reported `vs_spy_mean_pct` / `vs_spy_median_pct` are therefore pessimistic for the
  news strategies by ~0.4pp. Also `gap_news_analysis.py:29-31` silently drops
  `focused is None` records, so a no-news bucket can partly mean "not covered"
  (the prereg flags this risk, but `coverage` is computed only over known records).
- **Impact:** small constant offset on the `vs_spy_*` columns; bucket counts for
  unknown-news years distorted.
- **Fix:** net the SPY leg by the same cost (or report both); report unknown records
  as their own bucket and include them in `coverage`.

### F5 — Delisted names are sold/marked at their last stale close
- **Severity:** Low, acknowledged in the prereg.
- **Where:** `backtest.py:162,168,192` (`px = h["last"] if np.isnan(px) else px`).
- **Scenario:** For the 7 delisted volatile names (BBBY, FFIE, GOEV, MULN, NKLA,
  RIDE, WE) a position scheduled to exit after the last quote is liquidated at the
  last close (GOEV $0.37, MULN $0.066, NKLA $0.183, WE $0.836, RIDE $2.20, FFIE
  $1.45, BBBY $4.35) with no bankruptcy write-down or exit slippage. Positions still
  open at period end are marked at that same stale price.
- **Impact:** optimistic, but bounded (last prices for the bankrupt names are already
  near zero). It cannot turn a losing strategy into the Sharpe winner.
- **Fix:** none required for the conclusion; optionally write off delisted names at
  their last close after N missing sessions, or tag such trades.

### F6 — S1 defaults to *invested* while the 200-day average is unavailable
- **Severity:** Informational.
- **Where:** `backtest.py:215` (`... else True`).
- **Scenario:** The first valid `sma200` is 2019-10-16; every monthly check before
  then (and the buggy `i==lo_i` case) defaults to long. Exploration S1 therefore
  holds SPY through the strong Jan–Sep 2019 stretch (2019 = +33.1%, same as S0).
- **Impact:** helps S1's exploration Sharpe stated here; the prereg does not specify
  the warm-up stance. Not a correctness bug, but it is an unstated assumption.
- **Fix:** state the warm-up rule in the prereg, or default to cash until `sma200`
  is defined.

## Checked and found correct

- **Look-ahead:** every signal is causal against its entry index. `relgap`/`gap`
  (S2/S3) use only same-day open + prior close (the prereg permits the same-day open
  for gap rules); `rel1` and shifted `vol20` (S8/S9) are read on day `i` with entry
  at `i+1`; `sma200` and `R2` (S4) are read on `i` with entry at `i+1`; `sma5` exit
  tests close `k` and exits at open `k+1`; `past5` uses `C[i-1]/C[i-6]` and
  `mom12_1` uses `C[i-22]/C[i-253]` with entries at the same-day open. No signal
  touches the entry day's close.
- **Entry/exit offsets** match the prereg table (S2/S3 offset 0 hold 5c; S4 next
  open, path exit; S5/S6/S7 first session of week/month to next first open; S8
  next open hold 10c; S9 hold 5c).
- **Execution order:** open exits → open entries → close exits; a slot/mark is freed
  and cash returned before the same open's entries. No leverage.
- **Sizing:** `stake = min(equity/SLOTS, cash)` (`backtest.py:183`), equity marked at
  the open; sum of stakes cannot exceed cash.
- **Costs:** applied exactly once per side — buy `shares = stake*(1-c)/px`, sell
  `proceeds = shares*px*(1-c)`, i.e. round-trip factor `(1-c)^2 = 0.996004` at
  c = 0.2%.
- **SPY buy-and-hold:** raw open→close return 109.7% (exploration) / 67.2%
  (validation); after the 0.2% entry cost 109.3% / 66.8%, exactly the reported
  totals. Independent close-to-close Sharpe 0.809 / 1.286 and max drawdown
  −33.8% / −18.8% match the report.
- **Metrics:** CAGR from date span, daily Sharpe ×√252, `eq/cummax−1` drawdown,
  year-end resampling with `prev` chaining, and `trades_per_year = closed/years`
  all reproduce.
- **Signal counts reproduced independently:** S2 = 2205 (exploration) / 1541
  (validation); S3 = 2400 / 1812 — identical to the program's `signals`.
- **Data plumbing:** `O`, `C`, `V` pivots have identical column order; no duplicate
  (date,symbol); SPY present; 104 (daily) / 59 (volatile) symbols.
- **Conclusion robustness:** cost sweep (0 / 0.20% / 0.30%) reproduces "high-turnover
  killed by costs" on both universes; the Sharpe winner is SPY in both periods.

## Not verifiable in this sandbox

`gap_news.jsonl`, `theses.csv`, `thesis_daily.csv` are absent, so
`gap_news_analysis.py` and `grade_theses.py` were reviewed statically only.
`gap_test.py` and `spikes.py` need a live database and were out of scope.
