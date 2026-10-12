"""
core/benchmark.py
Benchmark statistics from journal snapshots (audit §7.6 — this file was empty
while the dashboard compared raw cumulative return of a ≤60%-deployed book
against 100% SPY).

Computes, from dashboard/data.json snapshots alone (no network):
  - fund vs SPY total return, plus an SPY/cash blend at MAX_PORTFOLIO_EXPOSURE
    (the exposure-honest benchmark; 60/40 until EXP-017 raised gross to 90%)
  - peak drawdown for fund and SPY (close-to-close on daily last snapshots;
    intraday DD is not observable from run-time sampling)
  - daily-return Sharpe (annualized, rf=0) and beta vs SPY

v2 (EXP-018) alpha block — `compute_v2_alpha` / `publish_v2_benchmark`:
  v2 is judged on beta-adjusted alpha with honest uncertainty, not raw return vs
  SPY (v1's early "beat SPY" was low exposure in a down month). Daily fund returns
  (last snapshot per US/Eastern trading day, v2 era only) are OLS-regressed on each
  benchmark's daily returns -> beta, annualized alpha, its t-stat and 95% CI, plus
  the information ratio. Benchmarks: SPY (market), QQQ (the backtest's bar), MTUM
  (a buyable momentum factor — the real question for a momentum strategy) and an
  equal-weight buy-and-hold of the v2 universe (Screener.WATCHLIST, Alpaca bars).
  Below V2_MIN_DAYS paired returns, every block reports "insufficient data (n=…)"
  instead of numbers. rf = 0 (alpha vs excess-return alpha differs by rf*(1-beta),
  negligible at beta ~1). The result is written to data.json["v2_benchmark"] (a
  single overwritten dict, not an array) for the dashboard.

Usage:
  python3 -m core.benchmark            # print report
  from core.benchmark import compute_benchmark, compute_v2_alpha
"""

import json
import logging
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")


def _daily_series(snapshots: list[dict]) -> list[dict]:
    """Collapse run-time snapshots to one per calendar day (last value wins)."""
    by_day: dict[str, dict] = {}
    for s in snapshots:
        ts = str(s.get("ts", ""))[:10]
        if ts and s.get("portfolio_value") and s.get("spy_price"):
            by_day[ts] = s
    return [by_day[d] for d in sorted(by_day)]


def _returns(values: list[float]) -> list[float]:
    return [(b - a) / a for a, b in zip(values, values[1:]) if a]


def _peak_drawdown(values: list[float]) -> float:
    peak, mdd = 0.0, 0.0
    for v in values:
        peak = max(peak, v)
        if peak:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def _sharpe(returns: list[float], periods_per_year: int = 252) -> float | None:
    if len(returns) < 5:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return round(mean / sd * math.sqrt(periods_per_year), 2)


def _beta(fund_rets: list[float], spy_rets: list[float]) -> float | None:
    n = min(len(fund_rets), len(spy_rets))
    if n < 5:
        return None
    f, s = fund_rets[-n:], spy_rets[-n:]
    mf, ms = sum(f) / n, sum(s) / n
    cov = sum((a - mf) * (b - ms) for a, b in zip(f, s)) / (n - 1)
    var = sum((b - ms) ** 2 for b in s) / (n - 1)
    if var == 0:
        return None
    return round(cov / var, 2)


def compute_benchmark(snapshots: list[dict]) -> dict:
    daily = _daily_series(snapshots)
    if len(daily) < 2:
        return {"error": "not enough snapshots"}

    fund = [float(s["portfolio_value"]) for s in daily]
    spy  = [float(s["spy_price"]) for s in daily]
    fund_rets, spy_rets = _returns(fund), _returns(spy)

    fund_ret  = fund[-1] / fund[0] - 1
    spy_ret   = spy[-1] / spy[0] - 1
    # Exposure-honest baseline: SPY at the CONFIGURED gross cap, cash at 0. Was a hardcoded
    # 0.60 — EXP-017 (gross 0.60 → 0.90) made that a silently wrong benchmark (Principle 1).
    # The legacy key name "blend_60_40_return_pct" is kept for consumers; it now means
    # "blend at MAX_PORTFOLIO_EXPOSURE" (see `blend_exposure`).
    blend_ret = config.MAX_PORTFOLIO_EXPOSURE * spy_ret

    out = {
        "start": daily[0]["ts"][:10],
        "end":   daily[-1]["ts"][:10],
        "n_days": len(daily),
        "fund_return_pct":   round(fund_ret * 100, 2),
        "spy_return_pct":    round(spy_ret * 100, 2),
        "blend_60_40_return_pct": round(blend_ret * 100, 2),
        "blend_exposure": config.MAX_PORTFOLIO_EXPOSURE,
        "excess_vs_spy_pct":   round((fund_ret - spy_ret) * 100, 2),
        "excess_vs_blend_pct": round((fund_ret - blend_ret) * 100, 2),
        "fund_peak_drawdown_pct": round(_peak_drawdown(fund) * 100, 2),
        "spy_peak_drawdown_pct":  round(_peak_drawdown(spy) * 100, 2),
        "fund_sharpe": _sharpe(fund_rets),
        "spy_sharpe":  _sharpe(spy_rets),
        "beta_vs_spy": _beta(fund_rets, spy_rets),
        "note": "daily-close sampling from run-time snapshots; intraday DD not observable",
    }
    # v2 (EXP-018): QQQ benchmark. Snapshots written by the v2 pipeline carry
    # qqq_price; older snapshots don't — report QQQ stats only when present.
    qqq_days = [s for s in daily if s.get("qqq_price")]
    if len(qqq_days) >= 2:
        qqq = [float(s["qqq_price"]) for s in qqq_days]
        qqq_rets = _returns(qqq)
        f2 = [float(s["portfolio_value"]) for s in qqq_days]
        fund_ret_q = f2[-1] / f2[0] - 1
        qqq_ret = qqq[-1] / qqq[0] - 1
        out["qqq_return_pct"] = round(qqq_ret * 100, 2)
        out["excess_vs_qqq_pct"] = round((fund_ret_q - qqq_ret) * 100, 2)
        out["qqq_peak_drawdown_pct"] = round(_peak_drawdown(qqq) * 100, 2)
        out["qqq_sharpe"] = _sharpe(qqq_rets)
        out["beta_vs_qqq"] = _beta(_returns(f2), qqq_rets)
    return out


# ── v2 (EXP-018) alpha measurement ───────────────────────────────────────────

V2_MIN_DAYS = 20          # paired daily returns (~one rebalance month) before any number is shown
PERIODS_PER_YEAR = 252
V2_BENCHMARKS = {"SPY": "spy_price", "QQQ": "qqq_price", "MTUM": "mtum_price"}


def _t_crit_975(df: int) -> float:
    """Two-sided 95% Student-t critical value (Cornish-Fisher expansion; |err| < 1e-3 for df >= 10)."""
    z = 1.959963985
    v = float(max(df, 1))
    return (z + (z**3 + z) / (4 * v)
            + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * v**2)
            + (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / (384 * v**3))


def _insufficient(n: int) -> dict:
    return {"n": n, "status": f"insufficient data (n={n})"}


def relative_stats(fund_rets: list[float], bench_rets: list[float],
                   periods_per_year: int = PERIODS_PER_YEAR,
                   min_n: int = V2_MIN_DAYS) -> dict:
    """OLS fund = a + b*bench + e on paired periodic returns, plus the information ratio.

    Returns beta, annualized alpha (%), alpha t-stat and 95% CI (%), R^2, and IR /
    annualized excess / tracking error — or {"n", "status": "insufficient data (n=…)"}.
    """
    n = min(len(fund_rets), len(bench_rets))
    if n < max(min_n, 3):
        return _insufficient(n)
    f, b = fund_rets[-n:], bench_rets[-n:]
    mf, mb = sum(f) / n, sum(b) / n
    sxx = sum((x - mb) ** 2 for x in b)
    if sxx <= 0:
        return {"n": n, "status": "degenerate (zero benchmark variance)"}
    beta = sum((x - mb) * (y - mf) for x, y in zip(b, f)) / sxx
    alpha = mf - beta * mb
    sse = sum((y - alpha - beta * x) ** 2 for x, y in zip(b, f))
    sst = sum((y - mf) ** 2 for y in f)
    se_a = math.sqrt(sse / (n - 2) * (1 / n + mb ** 2 / sxx))
    half = _t_crit_975(n - 2) * se_a
    p = periods_per_year
    diffs = [y - x for x, y in zip(b, f)]
    md = sum(diffs) / n
    sd = math.sqrt(sum((d - md) ** 2 for d in diffs) / (n - 1))
    return {
        "n": n,
        "beta": round(beta, 3),
        "alpha_ann_pct": round(alpha * p * 100, 2),
        "alpha_t": round(alpha / se_a, 2) if se_a > 0 else None,
        "alpha_ci95_pct": [round((alpha - half) * p * 100, 2),
                           round((alpha + half) * p * 100, 2)],
        "r2": round(1 - sse / sst, 3) if sst > 0 else None,
        "information_ratio": round(md / sd * math.sqrt(p), 2) if sd > 0 else None,
        "excess_ann_pct": round(md * p * 100, 2),
        "tracking_error_ann_pct": round(sd * math.sqrt(p) * 100, 2),
    }


def _parse_ts(ts) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _pos(v) -> float | None:
    """Positive finite float or None — snapshot fields are external payloads."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def v2_era_start(data: dict) -> str | None:
    """Timestamp the v2 BOOK was born: the earliest v2 trade entry or v2 rebalance run.

    Deliberately not "the first strategy=v2 run": v2 clock self-tests ran on
    2026-10-12 04:42Z with the v1 book still held. The trades ledger is uncapped, so
    the anchor survives runs[] rolling past its cap.
    """
    cands = [t.get("entry_ts") for key in ("trades", "open_trades")
             for t in (data.get(key) or []) if isinstance(t, dict) and t.get("strategy") == "v2"]
    cands += [r.get("ts") for r in (data.get("runs") or [])
              if isinstance(r, dict) and r.get("strategy") == "v2" and r.get("v2_rebalanced")]
    parsed = [(dt, ts) for ts in cands if (dt := _parse_ts(ts))]
    return min(parsed)[1] if parsed else None


def _et_day(dt: datetime) -> str | None:
    d = dt.astimezone(_ET)
    return d.strftime("%Y-%m-%d") if d.weekday() < 5 else None


def _day_pairs(snapshots: list, since: datetime, key: str | None = None,
               levels: dict | None = None) -> list[tuple[str, float, float]]:
    """[(et_day, fund_value, bench_level)] — last snapshot per ET trading day at/after `since`.

    With `key`, fund and benchmark come from the SAME snapshot (last of the day that
    carries the key); with `levels` ({et_day: level}), the benchmark is the day's level.
    """
    by_day: dict[str, tuple[datetime, float, float | None]] = {}
    for s in snapshots or []:
        if not isinstance(s, dict):
            continue
        dt = _parse_ts(s.get("ts"))
        pv = _pos(s.get("portfolio_value"))
        if dt is None or dt < since or pv is None:
            continue
        day = _et_day(dt)
        if day is None:
            continue
        if key is not None:
            bench = _pos(s.get(key))
            if bench is None:
                continue
        else:
            bench = _pos((levels or {}).get(day))
            if bench is None:
                continue
        if day not in by_day or dt >= by_day[day][0]:
            by_day[day] = (dt, pv, bench)
    return [(d, by_day[d][1], by_day[d][2]) for d in sorted(by_day)]


def _block(pairs: list) -> dict:
    """relative_stats on a pair list, plus total period returns of each leg."""
    stats = relative_stats(_returns([p[1] for p in pairs]), _returns([p[2] for p in pairs]))
    if len(pairs) >= 2:
        fr = pairs[-1][1] / pairs[0][1] - 1
        br = pairs[-1][2] / pairs[0][2] - 1
        stats.update(start=pairs[0][0], end=pairs[-1][0],
                     fund_return_pct=round(fr * 100, 2),
                     bench_return_pct=round(br * 100, 2),
                     excess_return_pct=round((fr - br) * 100, 2))
    return stats


def ew_buy_hold_levels(closes_by_symbol: dict, start_date: str) -> dict:
    """Equal-weight buy-and-hold index of the universe, 1.0 on `start_date`.

    Symbols without a positive close ON start_date are dropped (can't be bought at
    the base); later gaps forward-fill the last close. {et_day: level}.
    """
    base, series = {}, {}
    for sym, closes in (closes_by_symbol or {}).items():
        clean = {d: c for d, v in (closes or {}).items() if (c := _pos(v)) and d >= start_date}
        if start_date in clean:
            base[sym], series[sym] = clean[start_date], clean
    if not base:
        return {}
    days = sorted({d for s in series.values() for d in s})
    last = dict(base)
    out = {}
    for d in days:
        for sym in base:
            if d in series[sym]:
                last[sym] = series[sym][d]
        out[d] = round(sum(last[s] / base[s] for s in base) / len(base), 6)
    return out


def compute_v2_alpha(data: dict, ew_levels: dict | None = None) -> dict:
    """v2-era alpha block vs SPY / QQQ / MTUM / equal-weight universe (see module doc)."""
    start = v2_era_start(data)
    out = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "era_start": start,
        "min_days": V2_MIN_DAYS,
        "rf": 0.0,
        "method": "OLS on daily returns (last snapshot per ET trading day, v2 era); "
                  "alpha/excess annualized x252; 95% CI uses Student-t",
    }
    labels = list(V2_BENCHMARKS) + ["EW_UNIVERSE"]
    if start is None:
        out.update(status=_insufficient(0)["status"], fund_return_pct=None,
                   vs={k: _insufficient(0) for k in labels})
        return out
    since = _parse_ts(start)
    snaps = data.get("snapshots") or []
    vs = {k: _block(_day_pairs(snaps, since, key=col)) for k, col in V2_BENCHMARKS.items()}
    vs["EW_UNIVERSE"] = (_block(_day_pairs(snaps, since, levels=ew_levels)) if ew_levels
                         else {**_insufficient(0), "note": "universe bars unavailable"})
    spy = vs["SPY"]
    out.update(status=spy.get("status", "ok"), n_days=spy["n"],
               fund_return_pct=spy.get("fund_return_pct"), vs=vs)
    return out


def publish_v2_benchmark(alpaca) -> None:
    """Compute the v2 alpha block and store it in the journal. NEVER raises —
    measurement must not crash a trading run (ENGINEERING_PRINCIPLES §2d)."""
    from core import journal
    try:
        data = journal._load()
        start = v2_era_start(data)
        ew = None
        if start:
            try:
                from agents.screener import Screener
                day = _parse_ts(start).astimezone(_ET).strftime("%Y-%m-%d")
                closes = alpaca.get_daily_closes(sorted(set(Screener.WATCHLIST)), day)
                ew = ew_buy_hold_levels(closes, day) or None
            except Exception as e:
                logger.warning("v2 benchmark: EW-universe bars unavailable (%s)", e)
        journal.set_v2_benchmark(compute_v2_alpha(data, ew))
    except Exception as e:
        logger.warning("v2 benchmark: publish failed (non-fatal): %s", e)


def main():
    with open(config.JOURNAL_PATH) as f:
        data = json.load(f)
    stats = compute_benchmark(data.get("snapshots", []))
    print("═" * 56)
    print(f"  BENCHMARK REPORT  {stats.get('start')} → {stats.get('end')} "
          f"({stats.get('n_days')} days)")
    print("═" * 56)
    for k in ("fund_return_pct", "spy_return_pct", "blend_60_40_return_pct",
              "excess_vs_spy_pct", "excess_vs_blend_pct",
              "fund_peak_drawdown_pct", "spy_peak_drawdown_pct",
              "fund_sharpe", "spy_sharpe", "beta_vs_spy"):
        print(f"  {k:26} {stats.get(k)}")
    print(f"  ({stats.get('note')})")

    v2 = compute_v2_alpha(data)
    print(f"\n  v2 ALPHA (era start {v2.get('era_start')}, min n={V2_MIN_DAYS})")
    for k, b in v2.get("vs", {}).items():
        if "beta" in b:
            lo, hi = b["alpha_ci95_pct"]
            print(f"  vs {k:12} n={b['n']:<4} beta={b['beta']:<6} alpha={b['alpha_ann_pct']:+.2f}%/yr "
                  f"[{lo:+.2f}, {hi:+.2f}] t={b['alpha_t']} IR={b['information_ratio']}")
        else:
            print(f"  vs {k:12} {b.get('status')}")


if __name__ == "__main__":
    main()
