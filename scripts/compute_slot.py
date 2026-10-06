"""
Slot label for a trade run (WS-A, 2026-10-05).

The slot is the intended wall-clock time of the cron that fired, in UTC
("2026-10-06T14:05") — NOT "the latest slot <= now". A 14:05 cron delayed to
17:20 is still the 14:05 slot, so the 17:00 run is never mistaken for a
duplicate of it. main.py uses the slot as an idempotency key.

Usage (trade.yml):  python scripts/compute_slot.py --cron "$GITHUB_EVENT_SCHEDULE"
Stdlib only.
"""

import argparse
from datetime import datetime, timedelta, timezone


def slot_for_cron(cron: str, now: datetime) -> str:
    """Slot label for a 'M H * * *'-style cron that fired at `now` (UTC-aware).

    The scheduled day is the latest day whose M:H is <= now, so a run delayed
    across UTC midnight keeps the day it was scheduled for.
    """
    parts = cron.split()
    minute, hour = int(parts[0]), int(parts[1])
    now = now.astimezone(timezone.utc)
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot > now:
        slot -= timedelta(days=1)
    return slot.strftime("%Y-%m-%dT%H:%M")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cron", required=True)
    ap.add_argument("--now", default="", help="ISO UTC override (tests)")
    args = ap.parse_args()
    now = datetime.fromisoformat(args.now).replace(tzinfo=timezone.utc) \
        if args.now else datetime.now(timezone.utc)
    print(slot_for_cron(args.cron, now))


if __name__ == "__main__":
    main()
