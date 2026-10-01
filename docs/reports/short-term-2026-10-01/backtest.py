"""Backtest of the pre-registered short-term strategies (see PREREGISTRATION.md)."""

import json
import math
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

DATA = sys.argv[1]
SIDE_COST = float(sys.argv[2]) if len(sys.argv) > 2 else 0.002
SLOTS = 10
PERIODS = {
    "exploration": ("2019-01-01", "2023-12-31"),
    "validation": ("2024-01-01", "2026-09-30"),
}

raw = pd.read_csv(DATA, parse_dates=["date"])
O = raw.pivot(index="date", columns="symbol", values="open").sort_index()
C = raw.pivot(index="date", columns="symbol", values="close").sort_index()
V = raw.pivot(index="date", columns="symbol", values="volume").sort_index()
dates = C.index
stocks = [s for s in C.columns if s != "SPY"]
pos = {d: i for i, d in enumerate(dates)}


@dataclass
class Trade:
    symbol: str
    entry_i: int  # entry at open of this day index
    exit_i: int
    exit_at: str  # "open" or "close"
    score: float  # higher = preferred when slots are scarce


def sma(frame, n):
    return frame.rolling(n, min_periods=n).mean()


def rsi2(close):
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    au = up.ewm(alpha=1 / 2, adjust=False, min_periods=2).mean()
    ad = down.ewm(alpha=1 / 2, adjust=False, min_periods=2).mean()
    rs = au / ad.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


ret1 = C / C.shift(1) - 1
rel1 = ret1.sub(ret1["SPY"], axis=0)
gap = O / C.shift(1) - 1
relgap = gap.sub(gap["SPY"], axis=0)
vol20 = V.rolling(20, min_periods=20).mean().shift(1)
sma200, sma5 = sma(C, 200), sma(C, 5)
R2 = rsi2(C)
n = len(dates)


def fixed_hold(signal, entry_offset, hold, exit_at, score):
    trades = []
    idx = np.argwhere(signal.values)
    for i, j in idx:
        e = i + entry_offset
        x = e + hold
        if x >= n:
            continue
        sym = signal.columns[j]
        if np.isnan(O.iat[e, O.columns.get_loc(sym)]):
            continue
        trades.append(Trade(sym, e, x, exit_at, float(score.iat[i, j])))
    return trades


def week_starts():
    iso = [(d.isocalendar().year, d.isocalendar().week) for d in dates]
    return [i for i in range(1, n) if iso[i] != iso[i - 1]]


def month_starts():
    return [i for i in range(1, n) if dates[i].month != dates[i - 1].month]


def rotation(starts, lookback_fn, best):
    trades = []
    for k, i in enumerate(starts[:-1]):
        nxt = starts[k + 1]
        scores = lookback_fn(i)
        if scores is None:
            continue
        scores = scores.dropna()
        picks = scores.sort_values(ascending=not best).head(SLOTS)
        for sym, val in picks.items():
            if np.isnan(O.iat[i, O.columns.get_loc(sym)]):
                continue
            trades.append(Trade(sym, i, nxt, "open", float(val if best else -val)))
    return trades


def strategies():
    S = {}
    universe = C.columns != "SPY"
    only = lambda frame: frame.loc[:, universe]  # noqa: E731
    # S2 / S3: gap rules enter at the gap day's open (offset 0), exit at close 5 sessions later.
    S["S2_gap_down_rebound"] = fixed_hold(only(relgap <= -0.02), 0, 5, "close", only(-relgap))
    S["S3_gap_up_continuation"] = fixed_hold(only(relgap >= 0.02), 0, 5, "close", only(relgap))
    # S4: RSI(2) pullback in an uptrend, path-dependent exit.
    sig = only((C > sma200) & (R2 < 10))
    s4 = []
    for i, j in np.argwhere(sig.values):
        e = i + 1
        if e >= n:
            continue
        sym = sig.columns[j]
        col = C.columns.get_loc(sym)
        x = None
        for k in range(e, min(e + 10, n - 1)):
            if C.iat[k, col] > sma5.iat[k, col]:
                x = k + 1
                break
        if x is None:
            x = min(e + 10, n - 1)
        s4.append(Trade(sym, e, x, "open", float(-R2.iat[i, col])))
    S["S4_rsi2_pullback"] = s4

    def past5(i):
        if i < 6:
            return None
        r = C.iloc[i - 1] / C.iloc[i - 6] - 1
        return (r - r["SPY"]).drop("SPY")

    S["S5_weekly_reversal"] = rotation(week_starts(), past5, best=False)
    S["S6_weekly_momentum"] = rotation(week_starts(), past5, best=True)

    def mom12_1(i):
        if i < 253:
            return None
        return (C.iloc[i - 22] / C.iloc[i - 253] - 1).drop("SPY")

    S["S7_monthly_momentum"] = rotation(month_starts(), mom12_1, best=True)
    volume_spike = V > 2 * vol20
    S["S8_big_up_volume"] = fixed_hold(only((rel1 >= 0.04) & volume_spike), 1, 10, "close", only(rel1))
    S["S9_big_down_volume"] = fixed_hold(only((rel1 <= -0.04) & volume_spike), 1, 5, "close", only(-rel1))
    return S


def simulate(trades, lo, hi):
    """Slot-limited portfolio; entries at open, exits at open or close; costs per side."""
    lo_i = dates.searchsorted(pd.Timestamp(lo))
    hi_i = dates.searchsorted(pd.Timestamp(hi), side="right") - 1
    by_entry = {}
    for t in trades:
        if lo_i <= t.entry_i and t.exit_i <= hi_i:
            by_entry.setdefault(t.entry_i, []).append(t)
    cash, held, curve, closed = 1.0, [], [], []
    for i in range(lo_i, hi_i + 1):
        def value_at(prices_row):
            total = cash
            for h in held:
                p = prices_row[h["col"]]
                total += h["shares"] * (p if not np.isnan(p) else h["last"])
            return total

        # exits at the open
        for h in [h for h in held if h["t"].exit_i == i and h["t"].exit_at == "open"]:
            px = O.iat[i, h["col"]]
            px = h["last"] if np.isnan(px) else px
            proceeds = h["shares"] * px * (1 - SIDE_COST)
            cash += proceeds
            closed.append(proceeds / h["cost"] - 1)
            held.remove(h)
        # entries at the open
        open_row = O.iloc[i].values
        equity = value_at(open_row)
        held_syms = {h["t"].symbol for h in held}
        for t in sorted(by_entry.get(i, []), key=lambda t: -t.score):
            if len(held) >= SLOTS or t.symbol in held_syms:
                continue
            col = O.columns.get_loc(t.symbol)
            px = O.iat[i, col]
            stake = min(equity / SLOTS, cash)
            if np.isnan(px) or stake <= 1e-9:
                continue
            shares = stake * (1 - SIDE_COST) / px
            cash -= stake
            held.append({"t": t, "col": col, "shares": shares, "cost": stake, "last": px})
            held_syms.add(t.symbol)
        # exits at the close
        for h in [h for h in held if h["t"].exit_i == i and h["t"].exit_at == "close"]:
            px = C.iat[i, h["col"]]
            px = h["last"] if np.isnan(px) else px
            proceeds = h["shares"] * px * (1 - SIDE_COST)
            cash += proceeds
            closed.append(proceeds / h["cost"] - 1)
            held.remove(h)
        close_row = C.iloc[i].values
        for h in held:
            if not np.isnan(close_row[h["col"]]):
                h["last"] = close_row[h["col"]]
        curve.append((dates[i], value_at(close_row), len(held)))
    return curve, closed


def spy_curve(lo, hi, trend=False):
    lo_i = dates.searchsorted(pd.Timestamp(lo))
    hi_i = dates.searchsorted(pd.Timestamp(hi), side="right") - 1
    col = C.columns.get_loc("SPY")
    starts = set(month_starts())
    cash, shares, curve, trades = 1.0, 0.0, [], []
    for i in range(lo_i, hi_i + 1):
        want = True
        if trend:
            if i == lo_i and i == 0:
                want = True  # no prior bar to read (audit F1)
            elif i in starts or i == lo_i:
                want = C.iat[i - 1, col] > sma200.iat[i - 1, col] if not np.isnan(sma200.iat[i - 1, col]) else True
            else:
                want = shares > 0
        if want and shares == 0:
            shares = cash * (1 - SIDE_COST) / O.iat[i, col]
            entry = cash
            cash = 0.0
        elif not want and shares > 0:
            cash = shares * O.iat[i, col] * (1 - SIDE_COST)
            trades.append(cash / entry - 1)
            shares = 0.0
        curve.append((dates[i], cash + shares * C.iat[i, col], int(shares > 0) * SLOTS))
    return curve, trades


def metrics(curve, closed):
    eq = pd.Series([v for _d, v, _k in curve], index=[d for d, _v, _k in curve])
    exposure = np.mean([k for _d, _v, k in curve]) / SLOTS
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    daily = eq.pct_change().dropna()
    cagr = eq.iloc[-1] ** (1 / years) - 1
    sharpe = daily.mean() / daily.std() * math.sqrt(252) if daily.std() > 0 else float("nan")
    dd = (eq / eq.cummax() - 1).min()
    by_year = eq.resample("YE").last()
    first = eq.iloc[0]
    yearly = {}
    prev = 1.0
    for d, v in by_year.items():
        yearly[str(d.year)] = round((v / prev - 1) * 100, 1)
        prev = v
    return {
        "cagr_pct": round(cagr * 100, 1),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(dd * 100, 1),
        "total_return_pct": round((eq.iloc[-1] / 1.0 - 1) * 100, 1),
        "trades_per_year": round(len(closed) / years, 1),
        "avg_trade_net_pct": round(float(np.mean(closed)) * 100, 2) if closed else None,
        "win_rate": round(float(np.mean([c > 0 for c in closed])), 3) if closed else None,
        "exposure": round(float(exposure), 2),
        "yearly_pct": yearly,
        "_first": first,
    }


def main():
    results = {}
    strats = strategies()
    for period, (lo, hi) in PERIODS.items():
        results[period] = {}
        for name, trend in (("S0_spy_buy_hold", False), ("S1_spy_trend_200d", True)):
            curve, closed = spy_curve(lo, hi, trend)
            m = metrics(curve, closed)
            m.pop("_first")
            results[period][name] = m
        for name, trades in strats.items():
            curve, closed = simulate(trades, lo, hi)
            m = metrics(curve, closed)
            m.pop("_first")
            m["signals"] = sum(1 for t in trades if dates[t.entry_i] >= pd.Timestamp(lo) and dates[t.exit_i] <= pd.Timestamp(hi))
            results[period][name] = m
    print(json.dumps({"side_cost": SIDE_COST, "results": results}, indent=1))


if __name__ == "__main__":
    main()
