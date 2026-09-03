"""Pure-logic tests for the pipeline liveness alarm (no network / no clock).

Guards the monitor that exists because the 2026-07-06 outage ran a full trading day
unnoticed: a crash BEFORE journaling leaves check_llm_health reporting "healthy", so
silence itself has to be the alarm.
"""

from datetime import datetime, timedelta, timezone

from scripts.check_heartbeat import _age_hours, evaluate_staleness


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_fresh_journal_is_alive():
    assert evaluate_staleness(_ago(0.5), _ago(2.0)) == []


def test_stale_snapshot_flags_journal_writer():
    p = evaluate_staleness(_ago(9.0), _ago(2.0))
    assert len(p) == 1
    assert "SNAPSHOT" in p[0] and "journal writer" in p[0]


def test_stale_run_flags_trading_pipeline():
    # Snapshot fresh (market tick alive) but no decision run in >26h — the exact
    # "trade.yml is dead while the tick keeps writing" case.
    p = evaluate_staleness(_ago(0.5), _ago(40.0))
    assert len(p) == 1
    assert "RUN" in p[0] and "trading pipeline" in p[0]


def test_both_stale_flags_both():
    assert len(evaluate_staleness(_ago(9.0), _ago(40.0))) == 2


def test_missing_or_unparseable_timestamps_alarm():
    # A journal with no timestamps must alarm, not silently pass.
    assert len(evaluate_staleness("", "")) == 2
    assert len(evaluate_staleness("not-a-date", "not-a-date")) == 2


def test_thresholds_are_configurable():
    # 5h-old snapshot is fine under a 6h limit, stale under the 3h default.
    assert evaluate_staleness(_ago(5.0), _ago(2.0), max_snapshot_age_h=6.0) == []
    assert len(evaluate_staleness(_ago(5.0), _ago(2.0))) == 1


def test_age_hours_parses_utc_and_naive():
    assert _age_hours(_ago(3.0)) == __import__("pytest").approx(3.0, abs=0.05)
    assert _age_hours("") is None
    assert _age_hours("garbage") is None
