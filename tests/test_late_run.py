"""WS-A (2026-10-05 review): late-run policy + slot idempotency.

Incident: 7 of 14 PM buys since 2026-09-02 died because the run that approved
them started after the entry gate closed (GitHub `schedule` delivers hours late),
and those runs still burned full LLM debates on candidates that could not trade.
"""

import json

import pytest

import config
import main
from core import journal


class FakeAlpaca:
    def __init__(self, market_open=True, since_open=120, to_close=120):
        self.market_open, self.since_open, self.to_close = market_open, since_open, to_close

    def is_market_open(self): return self.market_open
    def minutes_since_open(self): return self.since_open
    def minutes_to_close(self): return self.to_close
    def get_account(self): return {"portfolio_value": 100_000.0, "cash": 80_000.0}
    def get_latest_price(self, sym): return 700.0


class FakeFetcher:
    def get_ohlcv(self, sym, days=2): return [{"close": 700.0}]


def _bump(calls, key, ret=None):
    calls[key] += 1
    return ret


@pytest.fixture
def harness(tmp_journal, monkeypatch):
    calls = {"review": 0, "screener": 0, "analyse": 0, "baseline": 0}
    state = {"alpaca": FakeAlpaca()}

    monkeypatch.setattr(main, "AlpacaClient", lambda: state["alpaca"])
    monkeypatch.setattr(main, "DataFetcher", FakeFetcher)
    monkeypatch.setattr(main, "check_hard_stops", lambda a, f: (True, "", "bull", "normal"))
    monkeypatch.setattr(main, "check_open_positions", lambda a: None)
    monkeypatch.setattr(main, "_llm_preflight", lambda: None)
    monkeypatch.setattr("core.reconcile.reconcile_untracked", lambda *a, **k: [])
    monkeypatch.setattr("core.baseline.log_baseline_decisions",
                        lambda **k: _bump(calls, "baseline"))
    monkeypatch.setattr(main, "_should_run_thesis_review", lambda *a: True)
    monkeypatch.setattr(main, "review_open_positions",
                        lambda *a, **k: _bump(calls, "review"))

    class Cand:
        symbol = "AMD"
        composite_score = 1.0
        signals = {}

    class FakeScreener:
        WATCHLIST = ["AMD"]

        def __init__(self, fetcher):
            pass

        def format_for_log(self, c):
            return ""

    monkeypatch.setattr(main, "Screener", FakeScreener)
    monkeypatch.setattr(main, "_select_candidates",
                        lambda **k: _bump(calls, "screener", [Cand()]))
    monkeypatch.setattr(main, "analyse_symbol",
                        lambda **k: _bump(calls, "analyse", False))
    monkeypatch.setattr(main, "push_to_github", lambda *a, **k: None)
    monkeypatch.setenv("GITHUB_RUN_ID", "123456789")
    return calls, state


def _runs():
    with open(config.JOURNAL_PATH) as f:
        return json.load(f)["runs"]


def test_market_closed_run_skips_debates_but_still_reviews_2026_10_05(harness):
    """10/05: both runs landed after 20:00 UTC and debated 6 candidates each."""
    calls, state = harness
    state["alpaca"] = FakeAlpaca(market_open=False, since_open=0, to_close=0)
    main.run_pipeline(dry_run=False, slot="2026-10-05T17:00")
    assert calls["analyse"] == 0          # zero debate LLM calls
    assert calls["screener"] == 0
    assert calls["review"] == 1           # position reviews still run
    run = _runs()[-1]
    assert run["skipped_reason"] == "market_closed"
    assert run["slot"] == "2026-10-05T17:00"
    assert run["scheduled_for"].startswith("2026-10-05T17:00")
    assert run["github_run_id"] == "123456789"
    assert run["started_at"]
    with open(config.JOURNAL_PATH) as f:
        assert json.load(f)["snapshots"]   # snapshot still written


def test_past_entry_gate_is_late_run(harness):
    calls, state = harness
    state["alpaca"] = FakeAlpaca(market_open=True, since_open=400, to_close=10)
    main.run_pipeline(dry_run=False, slot="2026-09-03T17:00")
    assert calls["analyse"] == 0 and calls["review"] == 1
    assert _runs()[-1]["skipped_reason"] == "late_run"


def test_tradable_run_still_debates_and_records_meta(harness):
    calls, _ = harness
    main.run_pipeline(dry_run=False, slot="2026-10-06T14:05")
    assert calls["analyse"] == 1
    run = _runs()[-1]
    assert run["skipped_reason"] == ""
    assert run["slot"] == "2026-10-06T14:05"
    assert run["github_run_id"] == "123456789"


def test_gate_closing_mid_run_stops_further_debates(harness, monkeypatch):
    """A run that starts tradable but outlasts the entry gate stops debating."""
    calls, state = harness
    seen = []

    def analyse(**k):
        seen.append(k["symbol"])
        state["alpaca"].since_open, state["alpaca"].to_close = 400, 5
        return False

    class C:
        def __init__(self, s):
            self.symbol, self.composite_score, self.signals = s, 1.0, {}

    monkeypatch.setattr(main, "_select_candidates",
                        lambda **k: [C("AMD"), C("NVDA"), C("MU")])
    monkeypatch.setattr(main, "analyse_symbol", analyse)
    main.run_pipeline(dry_run=False, slot="2026-10-06T17:00")
    assert seen == ["AMD"]


def test_duplicate_slot_exits_without_work(harness):
    calls, _ = harness
    main.run_pipeline(dry_run=False, slot="2026-10-06T14:05")
    n_runs = len(_runs())
    before = dict(calls)
    main.run_pipeline(dry_run=False, slot="2026-10-06T14:05")   # returns (exit 0)
    assert len(_runs()) == n_runs
    assert calls == before


def test_failed_run_does_not_burn_the_slot(harness):
    """A data_unavailable / preflight-failed record must not block the retry."""
    journal.log_run("midday", [], 0, skipped_reason="data_unavailable",
                    run_meta={"slot": "2026-10-06T17:00"})
    assert not main._slot_already_completed("2026-10-06T17:00")
    journal.log_run("midday", [], 0, skipped_reason="llm_preflight_failed: boom",
                    run_meta={"slot": "2026-10-06T17:00"})
    assert not main._slot_already_completed("2026-10-06T17:00")
    journal.log_run("midday", [], 0, skipped_reason="late_run",
                    run_meta={"slot": "2026-10-06T17:00"})
    assert main._slot_already_completed("2026-10-06T17:00")


def test_no_slot_is_never_idempotent(harness):
    main.run_pipeline(dry_run=False)
    main.run_pipeline(dry_run=False)
    assert len(_runs()) == 2


def test_early_return_paths_carry_run_meta(harness, monkeypatch):
    """Every log_run path in run_pipeline must thread slot/started_at/run id."""
    monkeypatch.setattr(main, "check_hard_stops",
                        lambda a, f: (False, "dd breaker", "bull", "normal"))
    main.run_pipeline(dry_run=False, slot="2026-10-06T14:05")
    run = _runs()[-1]
    assert run["skipped_reason"] == "dd breaker"
    assert run["slot"] == "2026-10-06T14:05" and run["github_run_id"] == "123456789"
    assert run["started_at"] and run["scheduled_for"]
