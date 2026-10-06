"""Pure-logic tests for the pipeline liveness alarm (no network / no clock).

Guards the monitor that exists because the 2026-07-06 outage ran a full trading day
unnoticed: a crash BEFORE journaling leaves check_llm_health reporting "healthy", so
silence itself has to be the alarm.

WS-C (2026-10-05): liveness is measured against the EXPECTED TRADE SLOTS, not wall-clock
age. The old `--max-run-age-h 26` check was red every Monday (Friday 21:30 UTC ->
Monday 14:35 UTC = 65h) and trained the operator to ignore red heartbeats.
"""

from datetime import datetime, timezone

import pytest

from scripts.check_heartbeat import _age_hours, evaluate_slot_liveness


def T(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def run(ts, slot=None, skipped=""):
    r = {"ts": T(ts).isoformat(), "skipped_reason": skipped}
    if slot:
        r["slot"] = slot
    return r


FRIDAY_RUNS = [run("2026-10-02T14:30"), run("2026-10-02T17:25"), run("2026-10-02T21:30")]


def test_friday_evening_to_monday_morning_does_not_alarm_2026_10_05():
    """The Monday false-red: 65h gap, but no slot is due yet at 14:35 / 15:00 UTC."""
    assert evaluate_slot_liveness(FRIDAY_RUNS, T("2026-10-05T14:35")) == []
    assert evaluate_slot_liveness(FRIDAY_RUNS, T("2026-10-05T15:00")) == []


def test_day_after_holiday_does_not_alarm():
    # Monday market holiday: no runs Monday. Tuesday 14:35: nothing due yet.
    runs = [run("2026-10-02T21:30")]
    assert evaluate_slot_liveness(runs, T("2026-10-06T14:35")) == []


def test_truly_dead_tuesday_alarms():
    monday = [run("2026-10-05T14:40"), run("2026-10-05T17:20")]
    problems = evaluate_slot_liveness(monday, T("2026-10-06T15:40"))   # 14:05 + 90m
    assert len(problems) == 1
    assert "2026-10-06T14:05" in problems[0]


def test_dead_afternoon_slot_alarms_even_though_morning_ran():
    runs = [run("2026-10-06T14:20")]
    assert evaluate_slot_liveness(runs, T("2026-10-06T18:00")) == []   # 17:00 + 90m not yet
    problems = evaluate_slot_liveness(runs, T("2026-10-06T18:35"))
    assert len(problems) == 1 and "2026-10-06T17:00" in problems[0]


def test_late_run_still_counts_as_alive():
    """A 14:05 run delivered 2h late is journaled -> not silence."""
    runs = [run("2026-10-06T16:05", slot="2026-10-06T14:05")]
    assert evaluate_slot_liveness(runs, T("2026-10-06T16:40")) == []


def test_late_run_of_earlier_slot_does_not_cover_later_slot():
    # slot-labelled run for 14:05 finishing at 17:10 must NOT mask a dead 17:00 slot.
    runs = [run("2026-10-06T17:10", slot="2026-10-06T14:05")]
    problems = evaluate_slot_liveness(runs, T("2026-10-06T18:40"))
    assert len(problems) == 1 and "2026-10-06T17:00" in problems[0]


def test_unlabelled_legacy_and_sentinel_runs_cover_by_timestamp():
    runs = [run("2026-10-06T14:50")]          # no slot field (sentinel / pre-WS-A)
    assert evaluate_slot_liveness(runs, T("2026-10-06T15:40")) == []


def test_run_before_slot_does_not_cover_it():
    runs = [run("2026-10-06T13:50")]
    assert len(evaluate_slot_liveness(runs, T("2026-10-06T15:40"))) == 1


def test_grace_min_is_configurable():
    # slot 14:05; due at slot + grace
    assert len(evaluate_slot_liveness([], T("2026-10-06T14:50"), grace_min=30)) == 1
    assert evaluate_slot_liveness([], T("2026-10-06T14:50"), grace_min=60) == []
    assert evaluate_slot_liveness([], T("2026-10-06T14:30"), grace_min=30) == []


def test_custom_slots():
    problems = evaluate_slot_liveness([], T("2026-10-06T12:00"), slots=["10:00"], grace_min=60)
    assert len(problems) == 1 and "2026-10-06T10:00" in problems[0]


def test_garbage_run_records_never_crash():
    runs = [{"ts": "not-a-date"}, {"ts": None}, {}, "junk", run("2026-10-06T14:30")]
    assert evaluate_slot_liveness(runs, T("2026-10-06T15:40")) == []
    assert len(evaluate_slot_liveness(runs[:-1], T("2026-10-06T15:40"))) == 1


def test_age_hours_parses_utc_and_naive():
    assert _age_hours("") is None
    assert _age_hours("garbage") is None
    assert _age_hours("2020-01-01T00:00:00") > 1000
    assert pytest.approx(0, abs=0.05) == _age_hours(datetime.now(timezone.utc).isoformat())
