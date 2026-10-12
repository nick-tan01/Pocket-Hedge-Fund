"""GitHub-only trade clock: decide dispatch / wait / chain / done. Pure logic, no gh."""
from datetime import datetime, timedelta, timezone

from scripts.trade_clock import next_action, parse_dispatched


def T(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_early_clock_waits_when_slot_is_inside_its_lifetime():
    now = T("2026-10-12T09:00")
    assert next_action(now, now + timedelta(minutes=340), set()) == ("wait", [])


def test_dispatches_due_slot():
    now = T("2026-10-12T14:05")
    assert next_action(now, now + timedelta(hours=3), set()) == ("dispatch", ["2026-10-12T14:05"])


def test_slot_beyond_lifetime_waits_then_chains_at_deadline():
    start = T("2026-10-12T05:00")
    deadline = start + timedelta(minutes=340)        # 10:40 < 14:05
    assert next_action(start, deadline, set()) == ("wait", [])
    assert next_action(deadline, deadline, set()) == ("chain", [])


def test_done_after_last_slot_dispatched_no_chain():
    now = T("2026-10-12T17:01")
    got = next_action(now, now + timedelta(hours=1), {"2026-10-12T14:05", "2026-10-12T17:00"})
    assert got == ("done", [])


def test_weekend_is_done_immediately():
    now = T("2026-10-11T09:00")
    assert next_action(now, now + timedelta(hours=5), set()) == ("done", [])


def test_after_close_is_done_even_with_undispatched_slots():
    now = T("2026-10-12T20:30")
    assert next_action(now, now + timedelta(hours=5), set()) == ("done", [])


def test_parse_dispatched_reads_slot_from_run_titles():
    titles = ["Trading Pipeline · slot 2026-10-12T14:05",
              "Pocket Hedge Fund — Trading Pipeline", "garbage", None]
    assert parse_dispatched(titles) == {"2026-10-12T14:05"}
