"""
scripts/refresh_snapshot.py
Lightweight dashboard refresh — pull the LIVE portfolio value, cash, SPY price, and
per-position mark-to-market from Alpaca and append a snapshot, WITHOUT running the LLM
pipeline or placing any orders.

The dashboard's top-line (portfolio value, total return, cash, open positions) is read
from the LAST snapshot in dashboard/data.json, which the full pipeline only writes a
couple of times a day. Between runs the dashboard freezes at a stale value. A short cron
(snapshot.yml, ~every 15 min during market hours) runs this so the displayed numbers stay
current. Read-from-broker + journal-write only — it NEVER trades.

Usage:
  python scripts/refresh_snapshot.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from core.alpaca_client import AlpacaClient
from core.reconcile import reconcile_untracked
from core.journal import get_open_trades, log_snapshot, update_open_trade, get_snapshots


def main():
    alpaca = AlpacaClient()

    # Self-heal first so the book is complete before we value it (adopts any untracked
    # broker position). No-op when journal already matches the broker.
    try:
        reconcile_untracked(alpaca, apply=True)
    except Exception as e:
        print(f"refresh_snapshot: reconcile skipped ({e})")

    acct = alpaca.get_account()
    pv   = float(acct.get("portfolio_value", 0) or 0)
    cash = float(acct.get("cash", 0) or 0)
    if pv <= 0:
        print("refresh_snapshot: no portfolio value from broker — skipping (non-fatal)")
        return

    # Mark-to-market refresh of open_trades so the positions panel is current too.
    positions = {p["symbol"]: p for p in alpaca.get_positions()}
    min_slot = getattr(config, "MIN_SLOT_PCT", 0.03)
    for t in get_open_trades():
        lp = positions.get(t["symbol"])
        if not lp:
            continue
        live_pct = round(float(lp.get("market_value", 0)) / pv, 4)
        update_open_trade(t["id"], {
            "position_pct":    live_pct,
            "is_remnant":      0 < live_pct < min_slot,
            "qty":             round(float(lp.get("qty", t.get("qty", 0))), 4),
            "current_price":   round(float(lp.get("current_price", 0)), 4),
            "unrealized_pl":   round(float(lp.get("unrealized_pl", 0)), 2),
            "unrealized_plpc": round(float(lp.get("unrealized_plpc", 0)), 6),
            "avg_entry":       round(float(lp.get("avg_entry", 0)), 4),
        })

    # SPY for the benchmark curve; fall back to the last snapshot's value if unavailable.
    spy = alpaca.get_latest_price("SPY")
    if not spy:
        snaps = get_snapshots()
        spy = snaps[-1].get("spy_price", 0) if snaps else 0

    log_snapshot(pv, cash, float(spy or 0))
    print(f"refresh_snapshot: pv=${pv:,.2f} cash=${cash:,.2f} spy={spy} "
          f"positions={len(positions)}")


if __name__ == "__main__":
    config.validate_required_env(require_llm=False)
    main()
