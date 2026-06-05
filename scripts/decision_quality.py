"""
scripts/decision_quality.py
Decision-quality scorecard — run during the daily/periodic review.

Measures whether the system's SKIPS and TRIMS actually panned out, by checking the
forward (SPY-relative) price action of every skipped/trimmed name. This produces real
signal from the fast-accumulating process data (187 risk_decisions, 255 reviews) instead
of waiting for the scarce closed-trade count.

MEASUREMENT ONLY — never changes trading behavior. Read-only by default; pass --save to
append the scorecard to dashboard/data.json under "decision_quality" for history.

Usage:
  python scripts/decision_quality.py                 # print scorecard (no file change)
  python scripts/decision_quality.py --lookback 60   # wider window
  python scripts/decision_quality.py --window 20     # judge on 20-day forward returns
  python scripts/decision_quality.py --save          # also append to journal history
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.counterfactuals import analyze_decisions
from core.data_fetcher import DataFetcher

DATA_PATH = "dashboard/data.json"


def _fmt_excess(exc: dict) -> str:
    return " ".join(f"{w}d={v:+.1f}%" if v is not None else f"{w}d=…"
                    for w, v in exc.items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=45,
                    help="only analyze decisions newer than N days (default 45)")
    ap.add_argument("--window", type=int, default=10,
                    help="forward window in trading days to headline (default 10)")
    ap.add_argument("--save", action="store_true",
                    help="append the scorecard to dashboard/data.json history")
    ap.add_argument("--verbose", action="store_true",
                    help="list every skip/trim row, not just the summary")
    args = ap.parse_args()

    with open(DATA_PATH) as f:
        data = json.load(f)

    fetcher = DataFetcher()
    sc = analyze_decisions(data, fetcher, lookback_days=args.lookback,
                           primary_window=args.window)

    sk, tr = sc["skip_quality"], sc["trim_quality"]
    print("═" * 60)
    print(f"  DECISION QUALITY SCORECARD — {sc['as_of']}")
    print(f"  lookback={sc['lookback_days']}d | judged on {sc['primary_window_days']}-day "
          f"forward return vs SPY | {sc['closed_trades']} closed trades")
    print("═" * 60)

    print("\nSKIP QUALITY  (did we correctly avoid names that underperformed SPY?)")
    if sk["n_decided"]:
        print(f"  precision: {sk['precision_pct']}%  "
              f"({sk['good']} good avoid / {sk['missed']} missed alpha; "
              f"{sk['n_pending']} pending)")
    else:
        print(f"  no decided skips yet ({sk['n_pending']} pending forward data)")

    print("\nTRIM QUALITY  (did we cut winners — names that then BEAT SPY?)")
    if tr["n_decided"]:
        print(f"  precision: {tr['precision_pct']}%  "
              f"({tr['good']} good de-risk / {tr['premature']} premature; "
              f"{tr['n_pending']} pending)")
        if tr["premature"] > tr["good"]:
            print("  ⚠ more premature trims than good ones — trim discipline is cutting winners")
    else:
        print(f"  no decided trims yet ({tr['n_pending']} pending forward data)")

    print("\nCONVICTION CALIBRATION  (does higher conviction earn higher P&L?)")
    cal = sc["conviction_calibration"]
    if cal:
        for c, v in cal.items():
            print(f"  conv {c}: n={v['n']:2d}  avg P&L={v['avg_pnl_pct']:+.1f}%  "
                  f"win={v['win_rate']:.0f}%")
        convs = sorted(cal)
        if len(convs) >= 2:
            mono = all(cal[convs[i]]["avg_pnl_pct"] <= cal[convs[i+1]]["avg_pnl_pct"]
                       for i in range(len(convs)-1))
            print(f"  monotonic (higher conv -> higher P&L)? {'yes ✅' if mono else 'NO ⚠ — scale not discriminating'}")
    else:
        print("  not enough closed trades with conviction labels yet")

    if args.verbose:
        print("\n— skip rows —")
        for r in sk["rows"]:
            print(f"  {r['date']} {r['symbol']:6} [{r['verdict']:7}] {_fmt_excess(r['fwd_excess_vs_spy'])} | {r['reason']}")
        print("\n— trim rows —")
        for r in tr["rows"]:
            print(f"  {r['date']} {r['symbol']:6} [{r['verdict']:9}] {_fmt_excess(r['fwd_excess_vs_spy'])} | thesis={r['thesis']}")

    if args.save:
        sc_lite = {k: v for k, v in sc.items() if k not in ("skip_quality", "trim_quality")}
        sc_lite["skip_precision_pct"] = sk["precision_pct"]
        sc_lite["trim_precision_pct"] = tr["precision_pct"]
        sc_lite["ts"] = datetime.now(timezone.utc).isoformat()
        data.setdefault("decision_quality", [])
        data["decision_quality"].append(sc_lite)
        if len(data["decision_quality"]) > 200:
            data["decision_quality"] = data["decision_quality"][-200:]
        with open(DATA_PATH, "w") as f:
            json.dump(data, f, indent=2)
        print(f"\nSaved scorecard to {DATA_PATH} (decision_quality history)")
    else:
        print("\n(print-only — pass --save to append to journal history)")


if __name__ == "__main__":
    main()
