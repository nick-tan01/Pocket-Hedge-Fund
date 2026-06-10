"""
scripts/migrate_journal_v2.py
One-time journal repair + backfill from the 2026-06 strategy audit. Idempotent.

What it does (all offline — touches only dashboard/data.json, never the broker):
  A3: restore journal records deleted by commit 0f2b260 ("Funnel reopening") by
      union-merging records from a given git ref's data.json (default 15121c7,
      the last commit that still contains the 2026-06-06..06-09 records).
  A8: split historical exit_reason="stop_loss" into initial_stop / trailing_stop
      (a closed trade whose final stop sat ABOVE entry was a ratcheted trailing
      stop harvesting profit, not a failed entry).
  A5: backfill realized_pnl on historical trim_history rows (basis = entry_price,
      the only basis recorded at the time) and trim_pnl / total_pnl on trades.
      Marks orphan_close P&L as estimated.

Usage:
  python scripts/migrate_journal_v2.py             # dry-run report
  python scripts/migrate_journal_v2.py --apply     # write dashboard/data.json
  python scripts/migrate_journal_v2.py --ref <sha> # merge from a different ref
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_PATH = "dashboard/data.json"

# How to identify a unique record per journal array, for union-merging.
MERGE_KEYS = {
    "snapshots":        lambda r: r.get("ts"),
    "runs":             lambda r: r.get("ts"),
    "debate_logs":      lambda r: r.get("id"),
    "risk_decisions":   lambda r: (r.get("ts"), r.get("symbol")),
    "position_reviews": lambda r: (r.get("ts"), r.get("trade_id")),
    "tech_shadow":      lambda r: (r.get("ts"), r.get("symbol")),
    "pre_debate_gates": lambda r: (r.get("ts"), r.get("symbol")),
    "watchlists":       lambda r: r.get("id"),
    "trades":           lambda r: r.get("id"),
}
SORT_KEYS = {
    "debate_logs": lambda r: r.get("ts", ""),
    "watchlists":  lambda r: r.get("generated_at", ""),
    "trades":      lambda r: r.get("exit_ts", "") or r.get("entry_ts", ""),
}


def load_ref_journal(ref: str) -> dict:
    raw = subprocess.run(
        ["git", "show", f"{ref}:{DATA_PATH}"],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(raw)


def union_merge(current: dict, old: dict) -> dict[str, int]:
    """Add records present in `old` but missing from `current`. Returns counts added."""
    added = {}
    for key, keyfn in MERGE_KEYS.items():
        cur_list = current.setdefault(key, [])
        seen = {keyfn(r) for r in cur_list}
        missing = [r for r in old.get(key, []) if keyfn(r) not in seen]
        if missing:
            cur_list.extend(missing)
            sort_key = SORT_KEYS.get(key, lambda r: r.get("ts", ""))
            cur_list.sort(key=sort_key)
            added[key] = len(missing)
    return added


def backfill_exit_reasons(data: dict) -> int:
    """A8: relabel legacy 'stop_loss' closes as initial_stop / trailing_stop."""
    changed = 0
    for t in data.get("trades", []):
        if t.get("exit_reason") != "stop_loss":
            continue
        entry = float(t.get("entry_price", 0) or 0)
        stop = float(t.get("stop_price", 0) or 0)
        # A final stop above entry can only exist after a trailing ratchet.
        t["exit_reason"] = "trailing_stop" if (entry > 0 and stop > entry) else "initial_stop"
        t["exit_reason_backfilled"] = True
        changed += 1
    return changed


def backfill_trim_pnl(data: dict) -> int:
    """A5: book realized P&L on historical trims (basis = recorded entry_price)."""
    changed = 0
    for t in data.get("trades", []) + data.get("open_trades", []):
        trims = t.get("trim_history") or []
        if not trims:
            continue
        entry = float(t.get("entry_price", 0) or 0)
        total = 0.0
        dirty = False
        for h in trims:
            if "realized_pnl" not in h:
                basis = float(h.get("basis", 0) or entry)
                h["basis"] = round(basis, 4)
                h["realized_pnl"] = round((float(h["price"]) - basis) * float(h["trim_qty"]), 2)
                h["basis_estimated"] = True   # entry_price, not true broker avg_entry
                dirty = True
            total += float(h["realized_pnl"])
        if dirty or "trim_pnl" not in t:
            t["trim_pnl"] = round(total, 2)
            if t.get("status") == "closed":
                t["total_pnl"] = round(float(t.get("pnl", 0) or 0) + t["trim_pnl"], 2)
            changed += 1
        if t.get("exit_reason") == "orphan_close" and not t.get("pnl_estimated"):
            t["pnl_estimated"] = True
            changed += 1
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="15121c7",
                    help="git ref whose data.json holds the deleted records")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()

    with open(DATA_PATH) as f:
        data = json.load(f)

    old = load_ref_journal(args.ref)
    added = union_merge(data, old)
    relabeled = backfill_exit_reasons(data)
    trims = backfill_trim_pnl(data)

    print(f"Restored from {args.ref}: " + (json.dumps(added) if added else "nothing missing"))
    print(f"Exit reasons relabeled (A8): {relabeled}")
    print(f"Trades with trim P&L backfilled / flags set (A5): {trims}")
    realized = sum(t.get("pnl", 0) or 0 for t in data["trades"])
    trim_pnl = sum(t.get("trim_pnl", 0) or 0 for t in data["trades"])
    print(f"Ledger after backfill: final-leg P&L ${realized:,.2f} + trim P&L "
          f"${trim_pnl:,.2f} = ${realized + trim_pnl:,.2f}")

    if not args.apply:
        print("\nDRY-RUN — re-run with --apply to write dashboard/data.json")
        return
    with open(DATA_PATH + ".pre_migration_backup", "w") as f:
        json.dump(json.load(open(DATA_PATH)), f, indent=2)
    with open(DATA_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"\nWrote {DATA_PATH} (backup at {DATA_PATH}.pre_migration_backup)")


if __name__ == "__main__":
    main()
