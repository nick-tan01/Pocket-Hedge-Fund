"""
scripts/check_heartbeat.py
Liveness alarm for the whole pipeline — the gap that let the 2026-07-06 outage run for a
full trading day unnoticed.

Why this exists: `check_llm_health` and `check_journal_sync` both read the LAST JOURNALED
run, so a crash that happens BEFORE anything is journaled (the get_account() TypeError took
down trading, the snapshot job and the health checks alike) leaves no trace in the journal —
those checks kept reporting "healthy" while the fund made zero decisions. The only signal
was a GitHub failure email.

This check inverts that: it asks "has the journal been written recently?" rather than "was
the last written run healthy?" — so silence itself is the alarm.

Deliberately dependency-free (stdlib only) and hosted in its own workflow so it stays alive
when the trading pipeline and the market tick are both dead.

False-alarm safety: it ONLY alerts while the market is actually open, using Alpaca's clock
endpoint (which knows holidays). Weekends, holidays and overnight are silent by construction.
If the broker is unreachable it exits 0 — a monitor must never be the thing that pages you.

Exit 0 = alive (or cannot verify); exit 2 = STALE (turns the job red -> owner email).

Usage:
  python scripts/check_heartbeat.py [--data dashboard/data.json]
    [--max-snapshot-age-h 3] [--max-run-age-h 26]
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

CLOCK_URL = "https://paper-api.alpaca.markets/v2/clock"


def _market_is_open() -> bool | None:
    """True/False from Alpaca's clock, or None if we can't tell (never alarm on None)."""
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        # Say this LOUDLY. A monitor that silently no-ops because its secrets are missing
        # is worse than no monitor — it reports success forever while watching nothing.
        print("⚠ check_heartbeat: ALPACA_API_KEY/SECRET not set — the liveness monitor is "
              "NOT actually checking anything. Fix the workflow secrets.")
        return None
    req = urllib.request.Request(
        CLOCK_URL,
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return bool(json.loads(r.read().decode()).get("is_open"))
    except (urllib.error.URLError, ValueError, KeyError, TimeoutError, OSError) as e:
        print(f"check_heartbeat: clock unreachable ({e}) — cannot verify, treating as OK")
        return None


def _age_hours(ts: str) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except (ValueError, TypeError):
        return None


def evaluate_staleness(snap_ts: str, run_ts: str,
                       max_snapshot_age_h: float = 3.0,
                       max_run_age_h: float = 26.0) -> list[str]:
    """Pure staleness decision — returns a list of problems (empty == alive).

    Split out from main() so the alarm logic is unit-testable without network or clock.
    """
    snap_age, run_age = _age_hours(snap_ts), _age_hours(run_ts)
    problems = []
    if snap_age is None or snap_age > max_snapshot_age_h:
        problems.append(
            f"last SNAPSHOT {snap_ts[:19] or 'never'} "
            f"({'unparseable' if snap_age is None else f'{snap_age:.1f}h ago'}; "
            f"limit {max_snapshot_age_h}h) — the market tick / journal writer looks dead"
        )
    if run_age is None or run_age > max_run_age_h:
        problems.append(
            f"last RUN {run_ts[:19] or 'never'} "
            f"({'unparseable' if run_age is None else f'{run_age:.1f}h ago'}; "
            f"limit {max_run_age_h}h) — the trading pipeline looks dead"
        )
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="dashboard/data.json")
    # The market tick writes a snapshot hourly; >3h of silence in-session means the
    # journal writer is dead. Trade runs are ~2x/day, so >26h spans a full session
    # (and the market-open gate keeps weekends/holidays from tripping it).
    ap.add_argument("--max-snapshot-age-h", type=float, default=3.0)
    ap.add_argument("--max-run-age-h", type=float, default=26.0)
    args = ap.parse_args()

    is_open = _market_is_open()
    if is_open is None:
        return 0
    if not is_open:
        print("check_heartbeat: market closed — liveness not evaluated (OK)")
        return 0

    try:
        with open(args.data) as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        print(f"🔴 HEARTBEAT: cannot read {args.data} ({e}) — the journal is unreadable.")
        return 2

    snaps, runs = d.get("snapshots") or [], d.get("runs") or []
    snap_ts = snaps[-1].get("ts", "") if snaps else ""
    run_ts = runs[-1].get("ts", "") if runs else ""
    snap_age, run_age = _age_hours(snap_ts), _age_hours(run_ts)
    problems = evaluate_staleness(snap_ts, run_ts,
                                  args.max_snapshot_age_h, args.max_run_age_h)

    if problems:
        print("🔴 HEARTBEAT STALE — the market is open but the journal is not advancing:")
        for p in problems:
            print(f"   - {p}")
        print("   Check the Actions tab: a run is probably crashing BEFORE it journals "
              "anything (the 2026-07-06 failure mode). check_llm_health cannot see this.")
        return 2

    print(f"check_heartbeat: alive — snapshot {snap_age:.1f}h ago, run {run_age:.1f}h ago — OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
