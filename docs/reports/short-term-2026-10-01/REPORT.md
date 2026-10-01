# How should StockBrain make money with £250? Overnight findings (2026-10-01)

Everything below was run offline or read-only. Nothing was deployed. No server
setting, live database row or order was changed, and no secrets were read.

## Bottom line

1. **The bot is currently losing money, and its BUY decisions are the reason.**
   - Its 25 closed trades since going live on 21 September lost about 3% before
     fees. It bought about 2.4× the account in 9 days, so fees pile up.
   - Across all 599 BUY verdicts its research produced, the stocks it chose went
     on to trail SPY by about 1.5% within three days (see §2).
   - Its SELL/REDUCE verdicts do point the right way. It's better at spotting
     bad news than good news.
2. **No short-term trading rule we tested beat simply holding SPY.** That
   includes post-earnings drift and news-filtered gaps. We tested
   10 common rules on 104 large US stocks and 58 volatile ones, 2019–2026, with
   every rule fixed before running. Judged by return for the risk taken, and
   checked on later data each rule hadn't seen, none beat holding SPY.
   - The main reason is cost. A GBP account pays roughly 0.4% per round trip
     in spread and the 0.15% currency fee each way. Strategies that trade every
     week give back 20%+ a year.
   - Rules that look great on volatile stocks owe it to hindsight in choosing
     the stocks: with the obvious winners removed, the edge disappears (see §4).
3. **The one consistent, usable signal is to avoid chasing.**
   - Buying after a stock has already jumped (an unexplained gap-up, a +1–2%
     move, or a "why is X stock soaring?" article) lost money in both periods,
     in every dataset we tried.
   - Those rules are implemented as three off-by-default guards on branch
     `feature/buy-quality-guards`.

**Most efficient option for £250:** a low-cost index fund. It beat every
active rule tested. One priced in GBP (an S&P 500 or all-world ETF listed in
London) avoids the 0.15% currency fee in and out. If the bot keeps trading,
deploy it with the guards switched on and smaller turnover, as in §6.

## 1. Live track record (Trading 212 live, 21–29 September 2026)

From the production database, read-only, on commit `9c053e5`:

- **67 orders:** 42 buys and 25 sells. 17 positions are still open.
- **Bought about $814 of stock with about $335 of capital,** turning the
  account over about 2.4 times in 9 days.
- **Realised result on the 25 closing sells:** −$15.0 on $484 of cost (−3.1%)
  before fees; 7 of the 25 made money. Currency fees add roughly another
  −$1.5, and the open positions were also slightly negative.
- **Every exit was a research SELL/REDUCE verdict within 1–4 days of buying.**
- **The company matching is correct.** Trading 212 keeps legacy tickers, so FB
  is Meta, DMYI is IonQ, IPOE is SoFi and UTX is RTX.
- **Many buys were triggered by stories about moves that had already happened,
  or by stale content:**
  - "What's Going On With SpaceX Stock Today?"
  - "Why Is Meta Platforms Stock Gaining Monday?"
  - "AMD Stock Hits Fresh Record High, Up 183%"
  - "Full Transcript: … Earnings Call"
- **The same story was bought more than once.** The same TSMC, IonQ, Microsoft
  and IREN articles each triggered two buys.
- **`filled_value` is never stored.** Fill prices above are the quoted price
  just before submission, so treat the $ figures as approximate.

## 2. Grading every live research verdict (`live_verdict_grades.txt`)

These are all 1,266 verdicts from 21 September to 1 October. Each entry is the
next regular-session open after the verdict, measured against SPY over the same
period. There are no costs, so this compares stock-picking only.

| Verdict | Typical 3-day result vs SPY | Share beating SPY | Number |
|---|---|---|---|
| BUY | **−1.55%** | 37% | 298 |
| HOLD / NO_ACTION (control) | −0.82% | 41% | 232 |
| SELL / REDUCE | −1.91% (correctly negative) | 34% | 164 |
| High-confidence SELL / REDUCE (≥0.75) | −8.1% | 27% | 15 |
| High-confidence BUY (≥0.75) | −1.04% | 46% | 13 |

The stocks it chose to buy did worse than the stocks it passed on. This is only
ten days in a single market regime, so treat it as an early warning, not
proof.

## 3. Large US stocks, 2019–2026 (`bt_*.json`, `PREREGISTRATION.md`)

- 104 large US stocks plus SPY.
- 10 slots, long only.
- Costs of 0.20% per side.
- Exploration 2019–23, untouched validation 2024–26.

| Rule | 2019–23 annual return / Sharpe | 2024–26 annual return / Sharpe |
|---|---|---|
| **Hold SPY** | **16.0% / 0.81** | **20.5% / 1.29** |
| SPY with 200-day trend filter | 10.8% / 0.82 | 13.7% / 1.13 |
| Buy gap-downs, hold 5 days | 3.0% / 0.24 | 0.6% / 0.12 |
| Buy gap-ups, hold 5 days | −1.7% / 0.03 | −1.9% / −0.03 |
| RSI(2) pullback | −5.4% / −0.30 | 0.8% / 0.13 |
| Weekly losers (reversal) | −0.8% / 0.13 | 8.2% / 0.48 |
| Weekly winners (momentum) | −6.0% / −0.12 | 0.4% / 0.11 |
| Monthly 12-month momentum | 9.5% / 0.48 | 31.5% / 1.18 |
| Big up-day with heavy volume, hold 10 days | 7.9% / 0.59 | 7.2% / 0.61 |
| Big down-day with heavy volume, hold 5 days | −2.3% / −0.10 | −3.7% / −0.28 |

At 0.05% per side, roughly no currency fee, weekly reversal makes 15.9% /
26.6% a year. **Costs, not the absence of a pattern, are what kill short-term
trading for a GBP account.**

**News filter on gaps.** We checked overnight news for every one of the 7,969
gaps, with no lookup failures:
- **Buying no-news gap-downs and holding 5 days** made money in both periods,
  but trailed SPY: 10.7% and 5.7% a year.
- **Buying no-news gap-ups** lost in both periods: −1.1% and −1.2% per trade,
  with 40–45% winners. That is the clearest "don't buy" signal found.

## 4. Volatile stocks, 2019–2026 (`vol_*.json`)

- 58 volatile names, including bankrupt ones (NKLA, GOEV, RIDE, BBBY, WE, FFIE,
  MULN).
- Same rules, same parameters.

| Rule | 2019–23 | 2024–26 | Worst drawdown |
|---|---|---|---|
| Hold SPY | 16.0% / 0.81 | 20.5% / 1.29 | −34% |
| Hold the whole volatile list, equally weighted | 35.0% / 0.87 | 7.8% / 0.39 | −78% |
| Monthly momentum | 26.6% / 0.71 | 45.5% / 0.96 | −81% |
| Weekly momentum | 22.0% / 0.62 | 58.3% / 0.89 | −91% |
| Buy gap-downs, hold 5 days | −14.6% | −36.3% | −92% |
| Weekly losers (reversal) | −14.2% | −62.5% | −95% |

The momentum results in 2024–26 come from hindsight: I chose the list in 2026
and included stocks I knew had soared. With the obvious winners removed (IONQ,
RGTI, QBTS, PLTR, MSTR, HOOD, SOUN, BBAI, SMCI, COIN, CVNA, SOFI, OPEN, AFRM),
monthly momentum makes **6.3%** and weekly momentum 20.6% at Sharpe 0.59 with a
−71% drawdown. SPY over the same period made 20.5%. With £250 in volatile
names, a 70–90% drawdown is the normal outcome, not the tail.

## 5. Spikes, worked backwards (24-month minute data, 10 stocks)

- **Most intraday spikes had no story in the hour before them.** Of 797 up
  spikes, 69% had none and 15% only had a story afterwards.
- **Headline words predict that a stock will move, not which way.** Earnings
  words ("eps", "beats", "guidance") came before up-spikes 3× more often than a
  typical story, and before down-spikes just as often.

## 6. Recommendations

1. **Before trading live again,** decide whether to keep the £250 in the bot
   or move it to a GBP-priced index fund. On tonight's evidence, the fund is
   the efficient choice.
2. **If the bot keeps trading,** deploy `feature/buy-quality-guards` with:
   - `RISK_BLOCK_REACTION_HEADLINES=true`;
   - `RISK_SAME_STORY_REBUY_DAYS=7`;
   - `RISK_MAX_BUY_DAY_MOVE_PCT=0.02`;
   - `RISK_REENTRY_COOLDOWN_HOURS=72`;
   - `RISK_MAX_OPEN_POSITIONS` of around 5, fewer and larger positions so fees
     are a smaller share of each.

   Merging also brings `a61ed45` (the stale-signal and re-buy cooldown guards),
   which production doesn't have yet.
3. **Use the bot's research for exits.** SELL/REDUCE verdicts were directionally
   right, so they should keep driving exits and avoid-lists.
4. **Store actual fill values.** `filled_value` is null for every order, so
   real profit and loss can't be measured exactly. Read Trading 212's order
   history after each fill.

## 7. Post-earnings drift (`earn_*.json`, pre-registered as Addendum 3)

Benzinga headlines such as "EPS $A Beats $B Estimate, Sales … Beat …" were
parsed for the 104 large caps, 2019–2026. That gave 2,857 results; 95
headlines didn't parse, mostly "may not compare to estimate". Each trade buys
at the first open after the headline, holds 20 sessions, and pays 0.20% per
side.

| Rule | Per-trade net, 2019–23 / 2024–26 | Typical vs SPY | Portfolio annual return / Sharpe, 2019–23 / 2024–26 |
|---|---|---|---|
| E1: EPS and sales both beat | +1.04% / +1.85% | +0.15% / −0.30% | 7.3% / 0.50 · 6.7% / 0.56 |
| E2: EPS surprise ≥ +5% | +1.09% / +2.34% | −0.08% / −0.02% | 4.8% / 0.37 · 11.9% / 0.98 |
| E0 control: EPS and sales both miss | +0.81% / +2.64% | −0.30% / +2.07% | (not traded) |
| SPY | | | 16.0% / 0.81 · 20.5% / 1.29 |

There is no tradable earnings drift in these large caps. Beats did no better
than the market, and the control group of misses did as well or better. This
fits the published finding that the effect has faded and survives mainly in
small, thinly covered stocks, which Trading 212's fees and spreads make costly
to trade at £250.

## Independent audit

A DeepSeek agent audited the backtest code; the full report is
`audit-deepseek.md`.

- **Verdict:** the conclusion holds. It found no look-ahead in any signal,
  confirmed costs are applied exactly once per side, and reproduced the SPY
  numbers independently.
- **F1** (an indexing slip at the very first bar) had zero effect on this
  data, and is fixed.
- **F3** (repeated verdict rows in the grading) is fixed. The numbers in §2 are
  unchanged.
- **F2:** trades still open at the end of a period are dropped rather than
  valued. Valuing them instead lifts exploration monthly momentum to 10.4% /
  0.51 (large caps) and 29.0% / 0.74 (volatile). Neither changes a conclusion.
- **F4:** the news-gap comparisons against SPY are about 0.4 percentage points
  too pessimistic, because costs were charged only on the stock leg. That
  doesn't change the B verdict, which used annual return and Sharpe.

## Limitations

- The live period is 10 days in a single market regime.
- The large-cap universe is today's constituents, so it is survivorship-biased
  upwards.
- The volatile list was chosen with hindsight (tested above).
- Costs are proxies: 0.15% currency fee plus about 0.05% spread per side.
  Opening fills are assumed at the open price.
- Cash earns 0% in the backtests, though Trading 212 pays interest on cash.
- Daily data: intraday exits and stops are not modelled.
- Story coverage before 2020 is denser in Benzinga for large caps.
