"""
core/benchmark.py
Benchmark statistics from journal snapshots (audit §7.6 — this file was empty
while the dashboard compared raw cumulative return of a ≤60%-deployed book
against 100% SPY).

Computes, from dashboard/data.json snapshots alone (no network):
  - fund vs SPY total return, plus a 60/40 SPY/cash blend (the exposure-honest
    benchmark given MAX_PORTFOLIO_EXPOSURE=0.60)
  - peak drawdown for fund and SPY (close-to-close on daily last snapshots;
    intraday DD is not observable from run-time sampling)
  - daily-return Sharpe (annualized, rf=0) and beta vs SPY

Usage:
  python3 -m core.benchmark            # print report
  from core.benchmark import compute_benchmark
"""

import json
import math

import config


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
    blend_ret = 0.60 * spy_ret  # 60/40 SPY/cash, cash at 0 — exposure-honest baseline

    return {
        "start": daily[0]["ts"][:10],
        "end":   daily[-1]["ts"][:10],
        "n_days": len(daily),
        "fund_return_pct":   round(fund_ret * 100, 2),
        "spy_return_pct":    round(spy_ret * 100, 2),
        "blend_60_40_return_pct": round(blend_ret * 100, 2),
        "excess_vs_spy_pct":   round((fund_ret - spy_ret) * 100, 2),
        "excess_vs_blend_pct": round((fund_ret - blend_ret) * 100, 2),
        "fund_peak_drawdown_pct": round(_peak_drawdown(fund) * 100, 2),
        "spy_peak_drawdown_pct":  round(_peak_drawdown(spy) * 100, 2),
        "fund_sharpe": _sharpe(fund_rets),
        "spy_sharpe":  _sharpe(spy_rets),
        "beta_vs_spy": _beta(fund_rets, spy_rets),
        "note": "daily-close sampling from run-time snapshots; intraday DD not observable",
    }


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


if __name__ == "__main__":
    main()
