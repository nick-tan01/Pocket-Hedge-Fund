"""
scripts/reconcile_untracked_positions.py
CLI wrapper around core.reconcile — adopt broker positions missing from the journal.

The reconciliation logic now lives in core/reconcile.py and runs AUTOMATICALLY at the
top of every pipeline cycle (main.run_pipeline), so the journal self-heals each run and
manual use is rarely needed. This script remains for ad-hoc inspection (dry-run) and
one-off repairs. Journal-write only — it NEVER touches the broker.

Usage:
  python scripts/reconcile_untracked_positions.py            # dry-run report
  python scripts/reconcile_untracked_positions.py --apply    # write into journal
  python scripts/reconcile_untracked_positions.py --apply --push
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.alpaca_client import AlpacaClient
from core.reconcile import reconcile_untracked
from core.journal import push_to_github


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default dry-run)")
    ap.add_argument("--push", action="store_true", help="git push after writing (with --apply)")
    args = ap.parse_args()

    alpaca = AlpacaClient()
    recs = reconcile_untracked(alpaca, apply=args.apply)

    if not recs:
        print("No untracked positions — journal matches the broker.")
        return

    for r in recs:
        print(f"  {r['symbol']}: qty={r['qty']} entry=${r['entry_price']} "
              f"stop=${r['stop_price']} stop_oid={'Y' if r['stop_order_id'] else 'N'} "
              f"pos_pct={r['position_pct']*100:.1f}% entry_ts={r['entry_ts'] or 'UNKNOWN'}")

    if not args.apply:
        print("\nDRY-RUN — re-run with --apply to write these into open_trades.")
        return

    print(f"\nWrote {len(recs)} reconciled position(s) into the journal.")
    if args.push:
        push_to_github("Reconcile untracked broker position(s): "
                       f"{', '.join(r['symbol'] for r in recs)}")
        print("Pushed to GitHub.")


if __name__ == "__main__":
    main()
