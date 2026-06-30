"""
scripts/reconcile_untracked_positions.py
Reconcile broker positions that are missing from the journal (A9 follow-on).

Background: the broker order and the journal commit are NOT atomic. A pipeline run
can successfully buy a stock (and place its broker stop) but then fail to persist the
open_trade record to dashboard/data.json if a concurrent run's commit wins the git
race. The result is an UNTRACKED position: held + stopped at the broker, invisible to
the journal — so the pipeline never reviews it, never trails its stop, and never logs
its close. (Discovered 2026-06-13: AMAT, bought 2026-06-12 16:37:38, lost to a
data.json merge conflict.)

This script finds live Alpaca positions absent from open_trades and rebuilds the
missing open_trade record from broker truth (real avg_entry, qty, the resting stop
order's id/price). Journal write only — it NEVER touches the broker.

Usage:
  python scripts/reconcile_untracked_positions.py            # dry-run report
  python scripts/reconcile_untracked_positions.py --apply    # write + (optional) push
  python scripts/reconcile_untracked_positions.py --apply --push
"""

import argparse
import json
import os
import sys
from datetime import timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from core.alpaca_client import AlpacaClient
from core.journal import _load, _save, push_to_github

# Known buy timestamps recovered from Alpaca order history, so the rebuilt record
# carries the true entry time rather than "now". Extend as needed.
KNOWN_ENTRY_TS = {
    "AMAT": "2026-06-12T16:37:38+00:00",
    "MU":   "2026-06-24T15:56:43+00:00",  # recovered from Alpaca order history; buy lost to push/merge race
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default dry-run)")
    ap.add_argument("--push", action="store_true", help="git push after writing (with --apply)")
    args = ap.parse_args()

    alpaca = AlpacaClient()
    data = _load()
    journal_syms = {t["symbol"] for t in data.get("open_trades", [])}
    positions = alpaca.get_positions()
    pv = alpaca.get_account().get("portfolio_value", 0) or 0

    untracked = [p for p in positions if p["symbol"] not in journal_syms]
    if not untracked:
        print("No untracked positions — journal matches the broker.")
        return

    rebuilt = []
    for p in untracked:
        sym = p["symbol"]
        stops = alpaca.get_open_stop_orders(sym)
        stop = stops[0] if stops else {}
        entry = float(p["avg_entry"])
        qty = float(p["qty"])
        stop_price = float(stop.get("stop_price") or round(entry * (1 - config.HARD_STOP_PCT), 2))
        # Derive conviction from the stop distance: an 8% stop == hard-stop floor
        # (the lost run's true conviction is unrecoverable). Mark it reconciled.
        position_usd = round(entry * qty, 2)
        record = {
            "id":              f"{sym}_RECONCILED_{KNOWN_ENTRY_TS.get(sym, '')[:10].replace('-', '')}",
            "symbol":          sym,
            "side":            "buy",
            "qty":             round(qty, 4),
            "entry_price":     round(entry, 2),
            "entry_ts":        KNOWN_ENTRY_TS.get(sym, ""),
            "stop_price":      round(stop_price, 2),
            "stop_order_id":   stop.get("id", ""),
            "stop_ratcheted":  False,
            "conviction":      6,            # unrecoverable; honest lower bound (buy floor)
            "debate_id":       "",           # lost with the un-pushed run
            "status":          "open",
            "key_risk":        "RECONCILED from broker — original debate/run record lost to a "
                               "data.json merge race; conviction/debate_id unknown",
            "position_usd":    position_usd,
            "position_pct":    round(position_usd / pv, 4) if pv else 0.0,
            "sector":          _sector(sym),
            "trim_pnl":        0.0,
            "reconciled_from_broker": True,
        }
        rebuilt.append(record)
        print(f"  {sym}: qty={qty} entry=${entry} stop=${stop_price} "
              f"stop_oid={'Y' if stop.get('id') else 'N'} pos_pct={record['position_pct']*100:.1f}%")

    if not args.apply:
        print("\nDRY-RUN — re-run with --apply to write these into open_trades.")
        return

    data.setdefault("open_trades", []).extend(rebuilt)
    _save(data)
    print(f"\nWrote {len(rebuilt)} reconciled position(s) into the journal.")
    if args.push:
        push_to_github(f"Reconcile untracked broker position(s): "
                       f"{', '.join(r['symbol'] for r in rebuilt)}")
        print("Pushed to GitHub.")


def _sector(symbol: str) -> str:
    try:
        import yfinance as yf
        return yf.Ticker(symbol).info.get("sector", "Unknown") or "Unknown"
    except Exception:
        return "Unknown"


if __name__ == "__main__":
    main()
