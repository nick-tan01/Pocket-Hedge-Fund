"""
scripts/trade_clock.py — GitHub-only on-time trade dispatch.

Why: GitHub delivers `schedule` events for this repo 1-6 h late (median ~4 h in 2026-10,
0 of 14 on time) — every trade slot 2026-10-06..09 landed after the entry gate. But a
`workflow_dispatch` starts within seconds, and the built-in GITHUB_TOKEN is allowed to
fire one. So: trade_clock.yml is seeded by several early-morning crons; whichever arrives
first SLEEPS until each trade slot and dispatches trade.yml with that slot. If a slot lies
beyond this job's 6 h runner limit, it sleeps to its deadline and dispatches a fresh clock
(chain) — so one delivered seed covers the whole day. Finishes once both slots are out.

Dedupe: trade.yml's run-name carries the slot, so clocks (and the Mac backup) skip slots
already dispatched; main.py slot idempotency backstops any duplicate, and the late GH
trade cron for the same slot exits 0.
"""
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from scripts.local_dispatch import CUTOFF, SLOTS, due_slots

LIFETIME_MIN = 340                  # < trade_clock.yml timeout-minutes (355) < 6 h cap
POLL_SEC = 60
_SLOT_RE = re.compile(r"slot (\d{4}-\d{2}-\d{2}T\d{2}:\d{2})")


def _upcoming(now: datetime, dispatched: set[str]) -> list[datetime]:
    if now.weekday() >= 5 or now.strftime("%H:%M") >= CUTOFF:
        return []
    out = []
    for s in SLOTS:
        h, m = (int(x) for x in s.split(":"))
        at = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if at > now and at.strftime("%Y-%m-%dT%H:%M") not in dispatched:
            out.append(at)
    return out


def next_action(now: datetime, deadline: datetime, dispatched: set[str]) -> tuple[str, list[str]]:
    """('dispatch', slots) | ('wait', []) | ('chain', []) | ('done', [])."""
    now = now.astimezone(timezone.utc)
    due = due_slots(now, dispatched)
    if due:
        return "dispatch", due
    upcoming = _upcoming(now, dispatched)
    if not upcoming:
        return "done", []
    if now >= deadline:
        return "chain", []            # next slot is past our lifetime: hand off to a fresh clock
    return "wait", []


def parse_dispatched(titles) -> set[str]:
    out = set()
    for t in titles:
        m = _SLOT_RE.search(t or "")
        if m:
            out.add(m.group(1))
    return out


def _gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)


def _dispatched_from_github() -> set[str]:
    res = _gh("run", "list", "--workflow", "trade.yml", "--event", "workflow_dispatch",
              "--limit", "50", "--json", "displayTitle", "-q", ".[].displayTitle")
    if res.returncode != 0:
        print(f"WARN could not list trade runs: {res.stderr.strip()[:200]}", flush=True)
        return set()
    return parse_dispatched(res.stdout.splitlines())


def _selftest() -> int:
    """Prove GITHUB_TOKEN may dispatch trade.yml. Only off-hours: an unslotted in-hours
    run could trade, so refuse 13:00-21:00 UTC on weekdays."""
    now = datetime.now(timezone.utc)
    if now.weekday() < 5 and "13:00" <= now.strftime("%H:%M") < "21:00":
        print("selftest refused during market hours", flush=True)
        return 1
    res = _gh("workflow", "run", "trade.yml", "--ref", "main", "-f", "reason=clock_selftest")
    print(f"selftest dispatch rc={res.returncode} {res.stderr.strip()[:300]}", flush=True)
    return res.returncode


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()
    start = datetime.now(timezone.utc)
    deadline = start + timedelta(minutes=LIFETIME_MIN)
    dispatched = _dispatched_from_github()
    print(f"trade clock up at {start:%Y-%m-%d %H:%M} UTC; deadline {deadline:%H:%M}; "
          f"already dispatched: {sorted(dispatched) or 'none'}", flush=True)
    while True:
        now = datetime.now(timezone.utc)
        action, slots = next_action(now, deadline, dispatched)
        if action == "dispatch":
            dispatched |= _dispatched_from_github()          # another clock may have done it
            for slot in [s for s in slots if s not in dispatched]:
                res = _gh("workflow", "run", "trade.yml", "--ref", "main",
                          "-f", f"slot={slot}", "-f", "reason=external_dispatch")
                if res.returncode == 0:
                    dispatched.add(slot)
                    print(f"{now:%H:%M:%S} dispatched trade slot {slot}", flush=True)
                else:   # retried next poll; loud in the log
                    print(f"{now:%H:%M:%S} ERROR dispatching {slot}: {res.stderr.strip()[:300]}",
                          flush=True)
            if not set(slots) <= dispatched:
                time.sleep(POLL_SEC)
            continue
        if action == "done":
            print(f"{now:%H:%M:%S} nothing left to dispatch today — exiting", flush=True)
            return 0
        if action == "chain":
            res = _gh("workflow", "run", "trade_clock.yml", "--ref", "main")
            print(f"{now:%H:%M:%S} lifetime reached — chained a fresh clock "
                  f"(rc={res.returncode} {res.stderr.strip()[:200]})", flush=True)
            return 0 if res.returncode == 0 else 1
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    sys.exit(main())
