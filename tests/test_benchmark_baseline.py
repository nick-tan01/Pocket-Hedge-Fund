"""core/benchmark.py math and the S2 baseline shadow logger."""

import json

import pytest

import config
from core.benchmark import compute_benchmark, _peak_drawdown
from core.baseline import log_baseline_decisions
from agents.screener import ScreenerCandidate
from tests.conftest import make_bars


def test_peak_drawdown_from_peak_not_inception():
    # 100 → 120 → 95: inception loss 5%, peak DD ~20.8%
    assert _peak_drawdown([100, 120, 95]) == pytest.approx((120 - 95) / 120)


def test_compute_benchmark_basic():
    snaps = []
    fund, spy = 100_000.0, 700.0
    for d in range(1, 11):
        fund *= 1.01
        spy *= 1.005
        snaps.append({"ts": f"2026-06-{d:02d}T20:00:00",
                      "portfolio_value": fund, "spy_price": spy})
    out = compute_benchmark(snaps)
    assert out["n_days"] == 10
    assert out["fund_return_pct"] > out["spy_return_pct"] > 0
    assert out["excess_vs_blend_pct"] > out["excess_vs_spy_pct"]
    assert out["beta_vs_spy"] is not None


class _FakeFetcher:
    def get_ohlcv(self, symbol, days=60):
        return make_bars()


def _cand(sym, score, sector="Technology", price=100.0):
    return ScreenerCandidate(symbol=sym, price=price, market_cap=1e10,
                             composite_score=score, signals={"sector": sector})


def test_baseline_logs_topN_respecting_caps(tmp_journal, monkeypatch):
    monkeypatch.setattr(config, "BASELINE_SHADOW_ENABLED", True)
    monkeypatch.setattr(config, "BASELINE_SHADOW_TOP_N", 3)
    candidates = [
        _cand("AAA", 0.9), _cand("BBB", 0.8, sector="Healthcare"),
        _cand("CCC", 0.7), _cand("DDD", 0.6, sector="Healthcare"),
    ]
    open_positions = [
        {"symbol": "HELD", "position_pct": 0.06, "sector": "Technology"},
        {"symbol": "AAA",  "position_pct": 0.06, "sector": "Technology"},  # held → skip
        {"symbol": "T2",   "position_pct": 0.10, "sector": "Technology"},  # tech at 22%
    ]
    log_baseline_decisions(candidates, open_positions, 100_000.0, _FakeFetcher(),
                           regime="bull", vix_regime="normal", run_reason="test")
    data = json.loads(tmp_journal.read_text())
    rec = data["baseline_shadow"][0]
    picks = [b["symbol"] for b in rec["would_buy"]]
    # AAA held; CCC blocked by 25% tech sector cap (22% + 6% > 25%); BBB+DDD pass.
    assert picks == ["BBB", "DDD"]
    reasons = {s["symbol"]: s["reason"] for s in rec["skipped"]}
    assert reasons["AAA"] == "held"
    assert reasons["CCC"].startswith("sector_cap")
    for b in rec["would_buy"]:
        assert 0 < b["stop_price"] < b["price"]
        assert b["size_pct"] == config.BASELINE_SHADOW_SIZE_PCT


def test_baseline_disabled_writes_nothing(tmp_journal, monkeypatch):
    monkeypatch.setattr(config, "BASELINE_SHADOW_ENABLED", False)
    log_baseline_decisions([_cand("AAA", 0.9)], [], 100_000.0, _FakeFetcher(),
                           "bull", "normal")
    assert not tmp_journal.exists()
