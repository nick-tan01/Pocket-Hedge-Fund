"""Pure-logic helpers in main.py: review normalization, exit-reason split,
target sizing, pre-debate gate, TYPE-B cooldown."""

import json
from datetime import datetime, timezone

import pytest

import config
import main
from core import journal


def test_safe_review_decision_normalizes_garbage():
    action, conv = main._safe_review_decision({"action": "LIQUIDATE", "conviction": "x"}, 6)
    assert action == "hold"
    assert conv == 6


def test_safe_review_decision_trim_low_conviction_becomes_exit():
    action, conv = main._safe_review_decision({"action": "trim", "conviction": 2}, 7)
    assert action == "exit"
    assert conv == 2


def test_review_target_pct_bands():
    assert main._review_target_pct(8) == config.CONVICTION_SIZE_MAP[8]
    assert main._review_target_pct(5) == 0.02
    assert main._review_target_pct(3) == 0.01
    assert main._review_target_pct(1) == 0.0


def test_stop_exit_reason_split():
    assert main._stop_exit_reason({"stop_ratcheted": True}) == "trailing_stop"
    assert main._stop_exit_reason({}) == "initial_stop"


class _Cand:
    def __init__(self, sector):
        self.signals = {"sector": sector}
        self.symbol = "X"


def test_pre_debate_gate_exposure_maxed():
    positions = [{"position_pct": 0.57, "sector": "Other"}]
    gated, reason = main._pre_debate_gate(_Cand("Technology"), positions)
    assert gated and "exposure_maxed" in reason


def test_pre_debate_gate_sector_saturated():
    positions = [{"position_pct": 0.23, "sector": "Technology"}]
    gated, reason = main._pre_debate_gate(_Cand("Technology"), positions)
    assert gated and "sector_saturated" in reason


def test_pre_debate_gate_open():
    positions = [{"position_pct": 0.10, "sector": "Technology"}]
    gated, _ = main._pre_debate_gate(_Cand("Healthcare"), positions)
    assert not gated


def test_cooled_down_symbols_matches_typeb_pm_skips(tmp_journal, monkeypatch):
    monkeypatch.setattr(config, "TYPEB_SKIP_COOLDOWN_DAYS", 3)
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "risk_decisions": [
            {"ts": now, "symbol": "DDOG", "action": "skip",
             "reason": "PM decision: skip — valuation premium already priced in"},
            {"ts": now, "symbol": "AMD", "action": "skip",
             "reason": "PM decision: skip — earnings tonight"},          # TYPE-A: no kw
            {"ts": "2026-01-01T00:00:00+00:00", "symbol": "OLD", "action": "skip",
             "reason": "PM decision: skip — overvalued"},                 # expired
            {"ts": now, "symbol": "GS", "action": "skip",
             "reason": "Sector cap: Financials"},                         # not a PM skip
        ]
    }
    tmp_journal.write_text(json.dumps(data))
    cooled = main._cooled_down_symbols()
    assert cooled == {"DDOG"}
