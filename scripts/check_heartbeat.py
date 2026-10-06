"""
scripts/check_heartbeat.py
Liveness alarm for the whole pipeline — the gap that let the 2026-07-06 outage run for a
full trading day unnoticed.

Why this exists: `check_llm_health` and `check_journal_sync` both read the LAST JOURNALED
run, so a crash that happens BEFORE anything is journaled (the get_account() TypeError took
down trading, the snapshot job and the health checks alike) leaves no trace in the journal —
those checks kept reporting "healthy" while the fund made zero decisions. The only signal
was a GitHub failure email.

This check inverts that: it asks "was a run journaled for the slot that is now due?" rather
than "was the last written run healthy?" — so silence itself is the alarm.

SLOT-BASED (WS-C, 2026-10-05). The old check compared wall-clock age (`--max-run-age-h 26`)
and was red EVERY Monday (Friday 21:30 UTC -> Monday 14:35 UTC = 65h), training the operator
to ignore it. Now: the trade workflow has expected slots (14:05 / 17:00 UTC on weekdays). A
slot is DUE once `grace` minutes have passed since it. We alarm only if the latest due slot
of TODAY has no journaled run. Because the check only runs while Alpaca says the market is
open (weekends/holidays are silent by construction) and only looks at today's slots, there is
no Monday / post-holiday gap to false-alarm on. The tick's snapshot cadence is no longer an
alarm condition (it is regenerable and its thresholds never matched reality) — its age is
printed for information only.

A run "covers" a slot if its `slot` field equals the slot label, or — for runs with no slot
label (sentinel / manual / pre-WS-A) — if it was journaled at or after the slot time. A
late-delivered run labelled for an EARLIER slot does not mask a dead later slot.

Deliberately dependency-free (stdlib only) and hosted in its own workflow so it stays alive
when the trading pipeline and the market tick are both dead. READ-ONLY.
If the broker is unreachable it exits 0 — a monitor must never be the thing that pages you.

Exit 0 = alive (or cannot verify); exit 2 = SILENT (turns the job red -> owner email).

Usage:
  python scripts/check_heartbeat.py [--data dashboard/data.json]
    [--slots 14:05,17:00] [--grace-min 90]
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

CLOCK_URL = "https://paper-api.alpaca.markets/v2/clock"

# Keep in sync with the trade.yml cron lines (UTC).
DEFAULT_SLOTS = ("14:05", "17:00")
DEFAULT_GRACE_MIN = 90


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


def _parse_ts(ts) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _age_hours(ts: str) -> float | None:
    dt = _parse_ts(ts)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def _slot_label(day: datetime, hhmm: str) -> tuple[str, datetime]:
    h, m = (int(x) for x in hhmm.split(":"))
    at = day.replace(hour=h, minute=m, second=0, microsecond=0)
    return at.strftime("%Y-%m-%dT%H:%M"), at


def evaluate_slot_liveness(runs: list, now: datetime,
                           slots=DEFAULT_SLOTS,
                           grace_min: float = DEFAULT_GRACE_MIN) -> list[str]:
    """Pure liveness decision — returns a list of problems (empty == alive).

    Looks only at today's (UTC) slots whose due time (slot + grace) has passed and checks
    the LATEST such slot for a covering journaled run. Split out from main() so the alarm
    logic is unit-testable without network or clock; tolerant of garbage run records
    (external payloads must never crash the monitor).
    """
    now = now.astimezone(timezone.utc)
    due = []
    for hhmm in slots:
        label, at = _slot_label(now, hhmm)
        if now >= at + timedelta(minutes=grace_min):
            due.append((at, label))
    if not due:
        return []                       # nothing due yet today (Monday morning, post-holiday)
    slot_at, label = max(due)

    for r in runs or []:
        if not isinstance(r, dict):
            continue
        if r.get("slot") == label:
            return []
        ts = _parse_ts(r.get("ts"))
        if ts is not None and not r.get("slot") and ts >= slot_at:
            return []
    return [
        f"no journaled run for slot {label} UTC (due {int(grace_min)} min after the slot; "
        f"checked {now.strftime('%H:%M')} UTC) — the trading pipeline looks silent"
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="dashboard/data.json")
    ap.add_argument("--slots", default=",".join(DEFAULT_SLOTS),
                    help="Expected trade slots, UTC HH:MM comma-separated (keep in sync "
                         "with trade.yml cron)")
    ap.add_argument("--grace-min", type=float, default=DEFAULT_GRACE_MIN,
                    help="Minutes after a slot before its absence is an alarm "
                         "(GH schedule delivers late; default 90)")
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
    slots = [s.strip() for s in args.slots.split(",") if s.strip()]
    problems = evaluate_slot_liveness(runs, datetime.now(timezone.utc), slots, args.grace_min)

    snap_ts = snaps[-1].get("ts", "") if snaps and isinstance(snaps[-1], dict) else ""
    snap_age = _age_hours(snap_ts)
    snap_info = f"{snap_age:.1f}h ago" if snap_age is not None else "never/unparseable"

    if problems:
        print("🔴 HEARTBEAT SILENT — the market is open but an expected trade slot has no run:")
        for p in problems:
            print(f"   - {p}")
        print(f"   (info: last snapshot {snap_info})")
        print("   Check the Actions tab: a run is probably crashing BEFORE it journals "
              "anything (the 2026-07-06 failure mode), or GH schedule dropped the slot. "
              "check_llm_health cannot see this.")
        return 2

    print(f"check_heartbeat: alive — expected slots covered (last snapshot {snap_info}) — OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
