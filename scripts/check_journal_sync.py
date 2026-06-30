"""
scripts/check_journal_sync.py
Alert when the committed journal (dashboard/data.json) disagrees with the live Alpaca
book — i.e. a trade executed at the broker but its open_trade record didn't get
persisted this run. Wired as a final workflow step AFTER the journal push, mirroring
check_llm_health.py: it turns the GitHub Actions job RED (→ owner email) on drift while
the data still commits.

The next run's auto-reconcile (core/reconcile.reconcile_untracked) self-heals the
journal, so this is the loud EARLY SIGNAL that a write was lost — not the fix.

Exit 0 = in sync (or broker unreachable — non-fatal); exit 2 = a broker position is
missing from the journal (the dangerous untracked case).

Usage:
  python scripts/check_journal_sync.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_PATH = "dashboard/data.json"


def main() -> int:
    try:
        from core.alpaca_client import AlpacaClient
        alpaca = AlpacaClient()
        broker = {p["symbol"] for p in alpaca.get_positions() if float(p.get("qty", 0)) > 0}
    except Exception as e:
        print(f"check_journal_sync: could not reach broker ({e}) — skipping, non-fatal")
        return 0

    try:
        with open(DATA_PATH) as f:
            journal = {t["symbol"] for t in json.load(f).get("open_trades", [])}
    except Exception as e:
        print(f"check_journal_sync: could not read {DATA_PATH}: {e}")
        return 0

    missing = broker - journal      # held at broker, NOT in journal — the dangerous case
    phantom = journal - broker      # in journal, not at broker — orphan_close handles this

    if missing:
        print(f"🔴 JOURNAL DRIFT — {len(missing)} broker position(s) missing from the "
              f"journal: {', '.join(sorted(missing))}. A fill did not persist this run. "
              f"The next run's auto-reconcile will adopt it, but investigate the write path "
              f"(push race, crash, timeout).")
        return 2

    if phantom:
        print(f"⚠ journal lists {len(phantom)} position(s) not at the broker "
              f"({', '.join(sorted(phantom))}) — orphan-close should resolve next run.")

    print(f"check_journal_sync: in sync ({len(broker)} broker / {len(journal)} journal) — OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
