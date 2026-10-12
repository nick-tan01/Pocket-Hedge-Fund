"""
scripts/local_dispatch.py — on-time trade dispatch from this Mac (launchd stand-in for the
Cloudflare Worker in docs/EXTERNAL_DISPATCH_SETUP.md).

GitHub delivers the trade.yml crons 3-8 h late (every run 2026-10-06..09 landed post-gate).
launchd runs this every 5 min; it fires `gh workflow run trade.yml -f slot=<slot>` for each
of today's slots that has passed and has not been dispatched yet. The slot string matches
scripts/compute_slot.py exactly, so the late GH-cron backup for the same slot exits 0
(main.py slot idempotency) — first arrival wins.

Limits: needs the Mac awake and `gh` authenticated. A sleeping Mac dispatches on wake
(still hours earlier than GH); a powered-off Mac falls back to the GH cron.
Rollback: launchctl unload ~/Library/LaunchAgents/com.nicktan.phf-dispatch.plist
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SLOTS = ("14:05", "17:00")          # keep in sync with trade.yml crons + heartbeat --slots
CUTOFF = "20:00"                     # UTC; no dispatch once the market has closed
REPO = "nick-tan01/Pocket-Hedge-Fund"
GH = "/opt/homebrew/bin/gh"
STATE = Path.home() / ".phf_dispatch_state.json"


def due_slots(now: datetime, dispatched: set[str]) -> list[str]:
    now = now.astimezone(timezone.utc)
    if now.weekday() >= 5 or now.strftime("%H:%M") >= CUTOFF:
        return []
    day = now.strftime("%Y-%m-%d")
    return [f"{day}T{s}" for s in SLOTS
            if now.strftime("%H:%M") >= s and f"{day}T{s}" not in dispatched]


def _load_state() -> set[str]:
    try:
        return set(json.loads(STATE.read_text()))
    except Exception:
        return set()


def main() -> int:
    now = datetime.now(timezone.utc)
    dispatched = _load_state()
    rc = 0
    for slot in due_slots(now, dispatched):
        res = subprocess.run(
            [GH, "workflow", "run", "trade.yml", "--repo", REPO, "--ref", "main",
             "-f", f"slot={slot}", "-f", "reason=external_dispatch"],
            capture_output=True, text=True, timeout=60)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        if res.returncode == 0:
            dispatched.add(slot)
            print(f"{stamp} dispatched slot {slot}", flush=True)
        else:   # leave undispatched -> retried on the next 5-min tick
            print(f"{stamp} FAILED slot {slot}: {res.stderr.strip()[:300]}", flush=True)
            rc = 1
    # keep the state file small: only this week's labels matter
    keep = sorted(dispatched)[-20:]
    STATE.write_text(json.dumps(keep))
    return rc


if __name__ == "__main__":
    sys.exit(main())
