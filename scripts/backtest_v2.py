"""
scripts/backtest_v2.py — Phase 1 acceptance test for Pocket Hedge Fund v2.

Backtests the v2 deterministic signal (52-week-high proximity / residual momentum)
on 2023-01 -> 2026-09 with honest costs, no lookahead, and a walk-forward/OOS split.

Design (from research memo 2026-10-06):
  - Universe: the 112-name curated large-cap WATCHLIST (+ SPY/QQQ as benchmarks only).
  - Signal: 52WH proximity (George & Hwang 2004) or residual momentum
    (Blitz-Huij-Martens 2011, market-model residuals). Long-only, top-K.
  - Rebalance monthly (21 trading days); execute at NEXT open (no lookahead).
  - Sizing: equal-weight, or vol-targeted (weight ~ 1/vol, gross scaled to 20% target
    vol, capped at 100%). Conviction plays NO role — it is dead by operator order.
  - Costs: 5 bps one-way on traded notional (research: 2-5 bps for liquid large caps).
  - No per-name stops (rebalance-driven exits, like MTUM). The live -10% DD breaker
    is a safety guard, not part of the signal — not modeled here.

Data: yfinance auto_adjust=True (split AND dividend adjusted — required: NVDA 10:1
2024 etc. would corrupt any unadjusted momentum sort). Disk-cached after first pull.

KNOWN DEVIATIONS (read before trusting a number):
  - Survivorship bias: today's 112-name list applied to history flatters absolute
    returns (dead/delisted names are absent). Mitigations: point-in-time eligibility
    (>=252d history + $10M/day liquidity at each rebalance), and the pass bar demands
    a CLEAR margin over unbiased benchmarks, not a squeaker.
  - No LLM sentiment layer yet — this tests the deterministic signal alone. The
    sentiment feature must prove INCREMENTAL value later (ablation gate).

Pass bar: beats BOTH SPY and QQQ net of costs over the full period with maxDD < 35%,
  AND out-of-sample (2025-01 -> 2026-09) excess > 0 vs both. Tune on 2023-2024 only.

Usage:
  python scripts/backtest_v2.py                      # primary config
  python scripts/backtest_v2.py --signal residual --top-k 30 --vol-target
  python scripts/backtest_v2.py --sweep              # small staged config sweep (IS only)
"""

import argparse
import os
import sys
import time
from datetime import date

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         ".backtest_v2_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

DATA_START = "2022-01-01"   # warmup year for the 252d lookback
DATA_END = "2026-10-01"
IS_END = "2024-12-31"       # in-sample (tuning) ends here
OOS_START = "2025-01-01"    # out-of-sample validation starts here

COST_BPS_ONE_WAY = 5.0
LIQ_MIN_DOLLAR_VOL = 10_000_000   # $10M/day avg over 63d
LOOKBACK = 252
SKIP = 21                          # skip most recent month (reversal contamination)
VOL_WINDOW = 63


def _universe() -> list[str]:
    # Regex-extracted (not imported): importing agents.screener pulls config ->
    # dotenv/alpaca/anthropic, none of which the offline backtest needs.
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "agents", "screener.py")).read()
    m = re.search(r"WATCHLIST = \[(.*?)\]", src, re.S)
    return sorted(set(re.findall(r'"([A-Z][A-Z\.]*)"', m.group(1))))


def download_bars(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Download split+dividend-adjusted daily bars, disk-cached. One batch per call."""
    import yfinance as yf

    cache_path = os.path.join(CACHE_DIR, "bars_v1.parquet")
    if os.path.exists(cache_path):
        print(f"  loading cached bars ({os.path.basename(cache_path)})", flush=True)
        df = pd.read_parquet(cache_path)
        have = set(df.columns.get_level_values(1).unique())
        missing = [s for s in symbols if s not in have]
        if not missing:
            return {s: df.xs(s, axis=1, level=1).copy() for s in symbols}
        print(f"  {len(missing)} symbols missing from cache — fetching those", flush=True)
        symbols = missing
    else:
        df = None

    print(f"  downloading {len(symbols)} symbols {DATA_START} -> {DATA_END} ...", flush=True)
    # Two batches with a pause: yfinance rate-limits bulk pulls.
    frames = []
    for i in range(0, len(symbols), 60):
        batch = symbols[i:i + 60]
        for attempt in range(4):
            try:
                b = yf.download(batch, start=DATA_START, end=DATA_END,
                                auto_adjust=True, progress=False, threads=True)
                if b is not None and len(b):
                    frames.append(b)
                    break
            except Exception as e:
                print(f"    batch retry {attempt + 1}: {e}", flush=True)
            time.sleep(15 * (attempt + 1))
        else:
            print(f"    WARNING: batch {batch[:3]}... failed after retries", flush=True)
        time.sleep(10)
    if not frames:
        raise SystemExit("No bar data downloaded — yfinance blocked? Try again later.")
    new = pd.concat(frames, axis=1)
    if df is not None:
        # merge new symbols into cached frame
        df = pd.concat([df, new], axis=1)
    else:
        df = new
    df.to_parquet(cache_path)
    print(f"  cached -> {cache_path}", flush=True)
    return {s: df.xs(s, axis=1, level=1).copy() for s in _universe() + ["SPY", "QQQ"]
            if s in df.columns.get_level_values(1)}


# ── Signals (point-in-time: only bars with date <= t may be used) ─────────────

def signal_52wh(hist: pd.DataFrame) -> float:
    """Proximity to trailing-252d high. hist: bars strictly before/at t, oldest->newest."""
    if len(hist) < LOOKBACK:
        return np.nan
    w = hist.iloc[-LOOKBACK:]
    hi = w["High"].max()
    px = hist["Close"].iloc[-1]
    return px / hi if hi > 0 else np.nan


def signal_residual(hist: pd.DataFrame, mkt: pd.Series) -> float:
    """Idiosyncratic drift over 252d (skip 21d): cumulative stock return minus the
    beta-implied market component, i.e. T * alpha from a market-model regression.

    NOTE (bug fixed 2026-10-06): the first version returned sum(residuals), which is
    identically ~0 when the regression (with intercept) is fit ON the formation
    window — the ranking was numerical dust, not a signal. Ranking on T*alpha is
    the economically meaningful quantity (BHM 2011 spirit, single-factor).
    """
    if len(hist) < LOOKBACK + SKIP:
        return np.nan
    w = hist.iloc[-(LOOKBACK + SKIP):-SKIP] if SKIP else hist.iloc[-LOOKBACK:]
    r = np.log(w["Close"] / w["Close"].shift(1)).dropna()
    m = mkt.reindex(w.index)
    m = np.log(m / m.shift(1)).dropna()
    idx = r.index.intersection(m.index)
    if len(idx) < 126:
        return np.nan
    r, m = r.loc[idx].values, m.loc[idx].values
    A = np.column_stack([np.ones_like(m), m])
    beta = np.linalg.lstsq(A, r, rcond=None)[0]
    return float(np.sum(r) - beta[1] * np.sum(m))  # == len(r) * alpha


def signal_mom12_1(hist: pd.DataFrame) -> float:
    """Raw 12-1 momentum (reference arm only)."""
    if len(hist) < LOOKBACK + SKIP:
        return np.nan
    w = hist.iloc[-(LOOKBACK + SKIP):-SKIP] if SKIP else hist.iloc[-LOOKBACK:]
    return float(w["Close"].iloc[-1] / w["Close"].iloc[0] - 1)


def dollar_vol_ok(hist: pd.DataFrame) -> bool:
    if len(hist) < VOL_WINDOW:
        return False
    w = hist.iloc[-VOL_WINDOW:]
    return float((w["Close"] * w["Volume"]).mean()) >= LIQ_MIN_DOLLAR_VOL


def ann_vol(hist: pd.DataFrame) -> float:
    if len(hist) < VOL_WINDOW:
        return np.nan
    r = np.log(hist["Close"].iloc[-VOL_WINDOW:] /
               hist["Close"].iloc[-VOL_WINDOW:].shift(1)).dropna()
    return float(r.std() * np.sqrt(252)) if len(r) > 20 else np.nan


# ── Backtest ──────────────────────────────────────────────────────────────────

def decide_target(bars, symbols, spy, t, signal, top_k, vol_target):
    """Target weights decided at close t from data <= t. Pure, no lookahead."""
    if signal == "universe_bh":
        # Equal-weight buy-and-hold of every name with enough history at t.
        # Not rebalanced by signal — measures universe/survivorship bias.
        elig = [s for s in symbols
                if len(bars[s][bars[s].index <= t]) >= LOOKBACK + SKIP
                and dollar_vol_ok(bars[s][bars[s].index <= t])]
        return {s: 1.0 / len(elig) for s in elig} if elig else {}
    sig_fn = {"52wh": signal_52wh, "residual": signal_residual,
              "mom12_1": signal_mom12_1}[signal]
    scored, vols = [], {}
    for s in symbols:
        h = bars[s]
        hcut = h[h.index <= t]
        if len(hcut) < LOOKBACK + SKIP or not dollar_vol_ok(hcut):
            continue
        sc = (sig_fn(hcut) if signal != "residual"
              else sig_fn(hcut, spy[spy.index <= t]))
        if np.isnan(sc):
            continue
        v = ann_vol(hcut)
        if np.isnan(v) or v <= 0:
            continue
        scored.append((sc, s))
        vols[s] = v
    scored.sort(reverse=True)
    picks = [s for _, s in scored[:top_k]]
    if not picks:
        return {}
    if vol_target:
        inv = np.array([1.0 / vols[s] for s in picks])
        w = inv / inv.sum()
        # diagonal approx of ex-ante portfolio vol at full gross; scale to 20%
        pv = float(np.sqrt(np.sum((w * np.array([vols[s] for s in picks])) ** 2)))
        gross = min(1.0, 0.20 / pv) if pv > 0 else 1.0
        return {s: wi * gross for s, wi in zip(picks, w)}
    return {s: 1.0 / len(picks) for s in picks}


def run_backtest(bars: dict[str, pd.DataFrame], symbols: list[str],
                 signal: str, top_k: int, vol_target: bool,
                 rebalance_days: int = 21,
                 start: str = "2023-01-01", end: str = "2026-09-30",
                 cost_bps: float = COST_BPS_ONE_WAY):
    spy = bars["SPY"]["Close"]
    dates = spy[(spy.index >= start) & (spy.index <= end)].index
    first_ok = spy.index[LOOKBACK + SKIP]
    reb_set = {d for i, d in enumerate(dates)
               if d >= first_ok and (i % rebalance_days == 0)}
    if rebalance_days >= 9999:
        # buy-and-hold diagnostic: single entry at first eligible date
        reb_set = {min(d for d in dates if d >= first_ok)}

    equity = 1.0
    curve, traded_notional = [], []
    weights: dict[str, float] = {}
    ref: dict[str, float] = {}       # price the current holding is measured from
    pending: dict[str, float] | None = None  # target decided yday close -> exec today open

    for d in dates:
        if pending is not None:
            # 1) overnight gap on OLD weights: prev close -> today's open
            for s, w in list(weights.items()):
                o = bars[s]["Open"]
                if d in o.index and ref.get(s) and not np.isnan(o.loc[d]):
                    equity *= (1 + w * (o.loc[d] / ref[s] - 1))
            # 2) rebalance at today's open; 5 bps one-way on traded notional
            turnover, new_w = 0.0, {}
            for s in set(weights) | set(pending):
                o = bars[s]["Open"]
                if d not in o.index or np.isnan(o.loc[d]):
                    if s in weights:
                        new_w[s] = weights[s]  # no print: carry at old ref
                    continue
                turnover += abs(pending.get(s, 0.0) - weights.get(s, 0.0))
                if pending.get(s, 0.0) > 0:
                    new_w[s] = pending[s]
                    ref[s] = o.loc[d]
            equity *= (1 - turnover * (cost_bps / 1e4))
            traded_notional.append(turnover)
            weights = new_w
            pending = None
            for s in list(weights):
                if s not in ref:
                    ref[s] = bars[s]["Open"].loc[d]
        # 3) accrue today's session: ref -> close
        for s, w in list(weights.items()):
            c = bars[s]["Close"]
            if d in c.index and ref.get(s) and not np.isnan(c.loc[d]):
                equity *= (1 + w * (c.loc[d] / ref[s] - 1))
                ref[s] = c.loc[d]
        curve.append((d, equity))

        if d in reb_set:
            t = decide_target(bars, symbols, spy, d, signal, top_k, vol_target)
            pending = t if t else None  # empty screen -> hold, never panic-liquidate

    return curve, traded_notional


def metrics(curve, traded_notional, label: str) -> dict:
    eq = pd.Series([e for _, e in curve],
                   index=pd.DatetimeIndex([d for d, _ in curve]))
    eq.index = pd.DatetimeIndex(eq.index)
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
    r = eq.pct_change().dropna()
    sharpe = (r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else 0.0
    max_dd = float(((eq / eq.cummax()) - 1).min() * 100)
    ann_turnover = (float(np.sum(traded_notional)) / yrs) if traded_notional else 0.0
    return {"label": label, "cagr": cagr * 100, "sharpe": sharpe,
            "maxdd": max_dd, "turnover": ann_turnover,
            "n": len(eq), "end_eq": float(eq.iloc[-1])}


def buy_hold(bars: dict[str, pd.DataFrame], sym: str, start: str, end: str):
    c = bars[sym]["Close"]
    c = c[(c.index >= start) & (c.index <= end)]
    curve = [(d, float(c.loc[d] / c.iloc[0])) for d in c.index]
    return curve, []


def report(all_m: list[dict], title: str):
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)
    print(f"  {'strategy':<28}{'CAGR':>8}{'Sharpe':>8}{'MaxDD':>8}{'Turn/yr':>9}")
    print("  " + "-" * 68)
    base = {m["label"]: m for m in all_m}
    for m in all_m:
        print(f"  {m['label']:<28}{m['cagr']:>+7.2f}%{m['sharpe']:>8.2f}"
              f"{m['maxdd']:>7.1f}%{m['turnover']:>8.1f}x")
    s = base.get("v2")
    if s:
        for b in ("SPY", "QQQ"):
            if b in base:
                print(f"  EXCESS vs {b:<3}: {s['cagr'] - base[b]['cagr']:+.2f}pp/yr   "
                      f"(Sharpe {s['sharpe'] - base[b]['sharpe']:+.2f})")
    print("=" * 72 + "\n")
    return all_m


def diagnose(bars: dict[str, pd.DataFrame], symbols: list[str]):
    """Holdings + drawdown anatomy for the residual K=20 config. Not a performance claim."""
    spy = bars["SPY"]["Close"]
    dates = spy[(spy.index >= "2023-01-01") & (spy.index <= "2026-09-30")].index
    first_ok = spy.index[LOOKBACK + SKIP]
    reb_dates = [d for i, d in enumerate(dates)
                 if d >= first_ok and (i % 21 == 0)]
    print("\n--- holdings at sample rebalances (residual, top-20, equal-weight) ---")
    for d in reb_dates:
        ds = d.strftime("%Y-%m")
        if ds in ("2023-06", "2024-01", "2024-06", "2024-12", "2025-04",
                  "2025-09", "2026-01", "2026-06") and d.day <= 7:
            t = decide_target(bars, symbols, spy, d, "residual", 20, False)
            print(f"  {d.date()}: {', '.join(sorted(t))}")
    # drawdown episodes on the OOS equity curve
    curve, _ = run_backtest(bars, symbols, "residual", 20, False, 21,
                            "2025-01-01", "2026-09-30")
    eq = pd.Series([e for _, e in curve],
                   index=pd.DatetimeIndex([d for d, _ in curve]))
    dd = (eq / eq.cummax() - 1) * 100
    print("\n--- OOS drawdown episodes (trough < -10%) ---")
    in_dd, start = False, None
    for d, v in dd.items():
        if not in_dd and v < -10:
            in_dd, start, trough, trough_d = True, d, v, d
        elif in_dd:
            if v < trough:
                trough, trough_d = v, d
            if v >= -2:
                print(f"  {start.date()} -> {d.date()}: trough {trough:.1f}% on "
                      f"{trough_d.date()}")
                in_dd = False
    if in_dd:
        print(f"  {start.date()} -> (still in) : trough {trough:.1f}% on "
              f"{trough_d.date()}")
    # monthly returns OOS: find the crash months
    m = eq.resample("ME").last().pct_change() * 100
    print("\n--- OOS worst months ---")
    for d, v in m.nsmallest(6).items():
        print(f"  {d.strftime('%Y-%m')}: {v:+.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal", default="52wh",
                    choices=["52wh", "residual", "mom12_1"])
    ap.add_argument("--top-k", type=int, default=25)
    ap.add_argument("--vol-target", action="store_true")
    ap.add_argument("--rebalance-days", type=int, default=21)
    ap.add_argument("--cost-bps", type=float, default=COST_BPS_ONE_WAY,
                    help="one-way transaction cost in bps (sensitivity analysis)")
    ap.add_argument("--universe-bh", action="store_true",
                    help="equal-weight buy-and-hold of the full universe "
                         "(quantifies universe/survivorship bias)")
    ap.add_argument("--sweep", action="store_true",
                    help="staged IS-only sweep over a small config grid")
    ap.add_argument("--diagnose", action="store_true",
                    help="holdings + drawdown anatomy for residual K=20")
    args = ap.parse_args()

    symbols = _universe()
    bars = download_bars(symbols + ["SPY", "QQQ"])
    symbols = [s for s in symbols if s in bars and len(bars[s]) > LOOKBACK + SKIP + 60]
    print(f"  universe with data: {len(symbols)} names", flush=True)

    if args.diagnose:
        diagnose(bars, symbols)
        return

    configs = []
    if args.universe_bh:
        configs.append(("universe_bh", 0, False, 9999))
    elif args.sweep:
        for sig in ("52wh", "residual"):
            for k in (20, 30):
                for vt in (False, True):
                    configs.append((sig, k, vt, 21))
        configs.append(("52wh", 25, False, 5))   # weekly rebalance hypothesis
    else:
        configs.append((args.signal, args.top_k, args.vol_target,
                        args.rebalance_days))

    for sig, k, vt, rd in configs:
        tag = (f"v2:{sig} K={k} {'vol20' if vt else 'eq'} r{rd}d")
        print(f"\n>>> {tag}", flush=True)
        # Full period
        curve, tn = run_backtest(bars, symbols, sig, k, vt, rd,
                                 "2023-01-01", "2026-09-30",
                                 cost_bps=args.cost_bps)
        m_full = metrics(curve, tn, "v2")
        # IS / OOS
        c_is, t_is = run_backtest(bars, symbols, sig, k, vt, rd,
                                  "2023-01-01", IS_END, cost_bps=args.cost_bps)
        c_oos, t_oos = run_backtest(bars, symbols, sig, k, vt, rd,
                                    OOS_START, "2026-09-30",
                                    cost_bps=args.cost_bps)
        m_is, m_oos = metrics(c_is, t_is, "v2-IS"), metrics(c_oos, t_oos, "v2-OOS")
        b_spy = metrics(*buy_hold(bars, "SPY", "2023-01-01", "2026-09-30"), "SPY")
        b_qqq = metrics(*buy_hold(bars, "QQQ", "2023-01-01", "2026-09-30"), "QQQ")
        report([m_full, b_spy, b_qqq], f"FULL 2023-01 -> 2026-09 — {tag}")
        b_spy_oos = metrics(*buy_hold(bars, "QQQ", OOS_START, "2026-09-30"), "QQQ")
        b_spy_o = metrics(*buy_hold(bars, "SPY", OOS_START, "2026-09-30"), "SPY")
        report([m_oos, b_spy_o, b_spy_oos], f"OOS 2025-01 -> 2026-09 — {tag}")
        m_is["label"] = "v2-IS"
        report([m_is,
                metrics(*buy_hold(bars, "SPY", "2023-01-01", IS_END), "SPY"),
                metrics(*buy_hold(bars, "QQQ", "2023-01-01", IS_END), "QQQ")],
               f"IS 2023-01 -> 2024-12 — {tag} (tuning window)")


if __name__ == "__main__":
    main()
