"""Local (launchd) dispatcher: which slots are due right now. Pure logic, no gh/network."""
from datetime import datetime, timezone

from scripts.local_dispatch import due_slots


def T(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_nothing_due_before_first_slot():
    assert due_slots(T("2026-10-12T14:04"), set()) == []


def test_first_slot_due_at_its_minute_with_compute_slot_format():
    assert due_slots(T("2026-10-12T14:05"), set()) == ["2026-10-12T14:05"]


def test_already_dispatched_slot_not_repeated():
    assert due_slots(T("2026-10-12T14:30"), {"2026-10-12T14:05"}) == []


def test_mac_woke_late_dispatches_every_missed_slot_in_order():
    assert due_slots(T("2026-10-12T17:30"), set()) == ["2026-10-12T14:05", "2026-10-12T17:00"]


def test_no_dispatch_after_the_close():
    # 20:00 UTC = 16:00 EDT. A post-close run can't trade; let the GH cron backup cover it.
    assert due_slots(T("2026-10-12T20:00"), set()) == []


def test_weekends_never_dispatch():
    assert due_slots(T("2026-10-11T15:00"), set()) == []   # Sunday
    assert due_slots(T("2026-10-10T15:00"), set()) == []   # Saturday
