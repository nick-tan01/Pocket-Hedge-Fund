"""EXP-017 (operator directive 2026-10-05): gross cap 0.60 -> 0.90 and guarded pyramiding.

Pyramiding was held under EXP-007 until its four guardrails could be enforced in code:
  G1 breakeven stop on the blended basis BEFORE the add, G2 position <= 8% NAV after,
  G3 no add near/past the 52-week high (journaled), G4 every existing cap still applies.
"""

import json

import pytest

import config
import main
from agents import risk_manager
from core import journal, pyramid
from tests.conftest import make_bars

NAV = 100_000.0


def plan(trade=None, **over):
    """evaluate_add on a healthy winner: +10% over a 4% position, 30% deployed."""
    kw = dict(
        price=110.0, avg_entry=100.0, qty=40.0, market_value=4_400.0,
        portfolio_value=NAV, deployed_pct=0.30, sector_pct=0.044,
        conviction=7, required_conviction=6, thesis_status="intact", trend="up",
        size_mult=1.0, high_52w_fn=lambda: 150.0,
    )
    kw.update(over)
    return pyramid.evaluate_add(trade if trade is not None else {"stop_price": 92.0}, **kw)


# ── gross cap 0.90 ───────────────────────────────────────────────────────────

@pytest.fixture
def no_network(monkeypatch):
    monkeypatch.setattr(risk_manager, "_get_sector", lambda s: "Technology")
    monkeypatch.setattr(risk_manager, "_get_beta", lambda s: 1.0)


def _evaluate(held, conviction=9):
    return risk_manager.evaluate(
        symbol="TEST",
        pm_verdict={"action": "buy", "final_conviction": conviction,
                    "key_risk_to_monitor": "k", "deciding_factor": "d",
                    "bull_r2_conviction": conviction, "bear_r2_conviction": 5},
        current_price=100.0, bars=make_bars(), portfolio_value=NAV,
        open_positions=held, regime="bull", vix_regime="normal", fetcher=None,
    )


def test_gross_cap_is_90():
    assert config.MAX_PORTFOLIO_EXPOSURE == 0.90


def test_gross_cap_binds_at_90_for_new_entries(no_network):
    held = [{"symbol": f"P{i}", "position_pct": 0.14, "sector": "Other"} for i in range(6)]
    p = _evaluate(held)                                  # 84% deployed; conv-9 wants 10%
    assert p.action == "buy"
    assert p.position_usd == pytest.approx(6_000, abs=1)  # clipped to the 6% of headroom
    held.append({"symbol": "P6", "position_pct": 0.06, "sector": "Other"})   # now 90%
    assert _evaluate(held).action == "skip"


def test_gross_cap_binds_at_90_for_pyramid_adds():
    p = plan(deployed_pct=0.885)                         # 1.5% headroom (shaved) -> shrunk add
    assert p["ok"] and p["binding_cap"] == "gross"
    assert p["add_pct"] < 0.015
    assert 0.885 + p["add_pct"] <= 0.90
    b = plan(deployed_pct=0.895)                         # < 1% headroom -> no add
    assert not b["ok"] and b["reason"] == "cap_binds:gross"


# ── G1: breakeven stop on the blended basis ──────────────────────────────────

def test_g1_plan_carries_a_breakeven_stop_on_the_blended_basis():
    p = plan()
    assert p["ok"]
    qty_add = p["add_qty"]
    blended = (40 * 100.0 + qty_add * 110.0) / (40 + qty_add)
    assert p["blended_basis"] == pytest.approx(blended, abs=1e-3)
    assert p["new_stop"] >= blended                       # never below breakeven
    assert p["new_stop"] > 92.0                           # the stop had to MOVE
    assert p["new_stop"] < 110.0 * (1 - config.PYRAMID_MIN_STOP_GAP_PCT)


def test_g1_existing_higher_stop_is_kept():
    p = plan({"stop_price": 106.0})
    assert p["ok"] and p["new_stop"] == 106.0


def test_g1_no_room_for_a_breakeven_stop_blocks():
    # tiny position + a 3% NAV add => blended basis sits just under price
    b = plan(qty=1.0, market_value=108.0, price=108.0, avg_entry=100.0)
    assert not b["ok"] and b["reason"] == "no_room_for_breakeven_stop"


# ── G2: position <= 8% NAV after the add ─────────────────────────────────────

def test_g2_add_is_clipped_so_position_stays_under_8pct():
    p = plan(market_value=6_500.0, qty=59.0, sector_pct=0.065)   # 6.5% held
    assert p["ok"] and p["binding_cap"] == "position_8pct"
    assert p["position_pct_after"] <= config.PYRAMID_MAX_POSITION_PCT


def test_g2_position_near_8pct_gets_no_add():
    b = plan(market_value=7_500.0, qty=68.0, sector_pct=0.075)
    assert not b["ok"] and b["reason"] == "cap_binds:position_8pct"


def test_per_name_10pct_clamp_enforced_under_pyramiding(monkeypatch):
    # Even if PYRAMID_MAX_POSITION_PCT were raised past it, MAX_POSITION_PCT (10%) holds.
    monkeypatch.setattr(config, "PYRAMID_MAX_POSITION_PCT", 0.15)
    p = plan(market_value=8_800.0, qty=80.0, sector_pct=0.088)
    assert p["ok"] and p["binding_cap"] == "position_8pct"
    assert p["position_pct_after"] <= config.MAX_POSITION_PCT
    b = plan(market_value=9_800.0, qty=89.0, sector_pct=0.098)
    assert not b["ok"] and b["reason"].startswith("cap_binds")


# ── G3: 52-week-high extension ───────────────────────────────────────────────

# boundary: block iff price >= 0.95 * high, i.e. high <= 115.789 at price 110
@pytest.mark.parametrize("high", [112.0, 115.7, 108.0])   # within 5%, just inside, past it
def test_g3_blocks_near_or_past_the_52w_high(high):
    b = plan(high_52w_fn=lambda: high)
    assert not b["ok"] and b["reason"] == "extended_52w_high"
    assert "pct_from_52w_high" in b and b["journal"]


def test_g3_just_outside_the_block_band_is_allowed():
    assert plan(high_52w_fn=lambda: 115.9)["ok"]


def test_g3_unknown_52w_high_fails_closed():
    assert plan(high_52w_fn=lambda: None)["reason"] == "52w_high_unavailable"


def test_g3_clear_of_the_high_is_allowed():
    p = plan(high_52w_fn=lambda: 130.0)                    # price is 15% under the high
    assert p["ok"] and p["pct_from_52w_high"] == pytest.approx(-15.38, abs=0.01)


def test_g3_52w_lookup_is_lazy():
    calls = []
    plan(thesis_status="weakened", high_52w_fn=lambda: calls.append(1) or 150.0)
    plan(price=104.0, high_52w_fn=lambda: calls.append(1) or 150.0)
    assert calls == []                                     # cheap gates fail first: no network


# ── G4: the other existing caps / floors ─────────────────────────────────────

def test_g4_sector_cap_binds():
    b = plan(sector_pct=0.24)
    assert not b["ok"] and b["reason"] == "cap_binds:sector"


def test_g4_regime_floor_and_size_scaling():
    assert plan(conviction=7, required_conviction=8)["reason"] == "conviction_below_required"
    assert plan(size_mult=0.6)["add_pct"] == pytest.approx(0.03 * 0.6, abs=1e-4)


def test_trigger_and_thesis_gates():
    assert plan(price=105.0)["reason"] == "below_trigger"           # +5% < +8%
    assert plan(thesis_status="weakened")["reason"] == "thesis_not_intact"
    assert plan(trend="flat")["reason"] == "trend_not_up"
    assert plan({"stop_price": 92.0, "pyramid_adds": 1})["reason"] == "already_added"


# ── execution order: stop BEFORE buy; journaling ─────────────────────────────

class FakeAlpaca:
    def __init__(self, replace_ok=True, stop_resting=True):
        self.calls = []
        self.replace_ok = replace_ok
        self.stop_resting = stop_resting

    def get_order(self, oid):
        if oid.startswith("stop"):
            return {"status": "new" if self.stop_resting else "canceled",
                    "qty": 40, "filled_qty": 0, "filled_avg_price": 0}
        return {"status": "filled", "filled_avg_price": 110.0, "filled_qty": 27.2727}

    def get_open_stop_orders(self, symbol):
        return []

    def submit_stop_order(self, *a, **k):
        self.calls.append(("place_stop",))
        return None

    def replace_stop_order(self, oid, price, qty=None):
        self.calls.append(("replace_stop", price, qty))
        return {"id": f"stop{len(self.calls)}"} if self.replace_ok else None

    def submit_market_order(self, symbol, qty, side, reason="", ref_price=None):
        self.calls.append(("buy", qty))
        return {"id": "buy1"}


class FakeFetcher:
    def __init__(self, high=150.0):
        self.high = high

    def get_52w_high(self, symbol):
        return self.high


@pytest.fixture
def held_trade(tmp_journal, monkeypatch):
    monkeypatch.setattr(main, "can_execute_trades", lambda a: (True, ""))
    tid = journal.log_trade_open(
        symbol="NVDA", side="buy", qty=40.0, entry_price=100.0, stop_price=92.0,
        conviction=7, debate_id="d1", portfolio_value=NAV, sector="Technology",
        stop_order_id="stop0")
    live = {"qty": 40.0, "avg_entry": 100.0, "market_value": 4_400.0, "current_price": 110.0}
    return tid, live


def run_add(alpaca, fetcher, tid, live, **over):
    kw = dict(thesis_status="intact", trend="up", conviction=7, regime="bull",
              vix_regime="normal", dry_run=False)
    kw.update(over)
    return main._maybe_pyramid_add(alpaca, fetcher, tid, live, 110.0, NAV, **{
        "thesis_status": kw["thesis_status"], "trend": kw["trend"],
        "conviction": kw["conviction"], "regime": kw["regime"],
        "vix_regime": kw["vix_regime"], "dry_run": kw["dry_run"]})


def decisions(tmp_journal):
    return json.loads(tmp_journal.read_text()).get("pyramid_decisions", [])


def test_stop_is_ratcheted_before_the_buy_and_journal_is_updated(held_trade, tmp_journal):
    tid, live = held_trade
    alp = FakeAlpaca()
    assert run_add(alp, FakeFetcher(), tid, live) is True
    kinds = [c[0] for c in alp.calls]
    assert kinds.index("replace_stop") < kinds.index("buy")          # G1 ordering
    first_replace = next(c for c in alp.calls if c[0] == "replace_stop")
    t = journal.get_open_trades()[0]
    assert first_replace[1] >= t["avg_entry"] - 0.01                  # stop >= blended basis
    assert t["qty"] == pytest.approx(67.2727, abs=1e-3)
    assert t["pyramid_adds"] == 1 and t["pyramid_breakeven_stop"] is True
    assert t["stop_price"] >= t["avg_entry"] - 0.01
    # the resting stop was then resized to cover the grown position
    assert alp.calls[-1][0] == "replace_stop" and alp.calls[-1][2] == pytest.approx(67.2727, abs=1e-3)
    d = decisions(tmp_journal)[-1]
    assert d["outcome"] == "executed" and "pct_from_52w_high" in d


def test_failed_stop_ratchet_aborts_the_add_with_no_order(held_trade, tmp_journal):
    tid, live = held_trade
    alp = FakeAlpaca(replace_ok=False)
    assert run_add(alp, FakeFetcher(), tid, live) is False
    assert not any(c[0] == "buy" for c in alp.calls)
    assert decisions(tmp_journal)[-1]["outcome"] == "aborted:stop_ratchet_failed"
    assert journal.get_open_trades()[0]["stop_price"] == 92.0         # journal untouched


def test_no_resting_broker_stop_aborts_the_add(held_trade, tmp_journal):
    tid, live = held_trade
    alp = FakeAlpaca(stop_resting=False)
    assert run_add(alp, FakeFetcher(), tid, live) is False
    assert not any(c[0] == "buy" for c in alp.calls)
    assert decisions(tmp_journal)[-1]["outcome"] == "aborted:no_resting_stop"


def test_extended_name_is_blocked_and_journaled_with_distance(held_trade, tmp_journal):
    tid, live = held_trade
    alp = FakeAlpaca()
    assert run_add(alp, FakeFetcher(high=112.0), tid, live) is False
    assert alp.calls == []                                            # no stop move, no buy
    d = decisions(tmp_journal)[-1]
    assert d["outcome"] == "blocked:extended_52w_high"
    assert d["pct_from_52w_high"] == pytest.approx(-1.79, abs=0.01)
    assert d["block_within_pct"] == config.PYRAMID_52W_BLOCK_PCT * 100


def test_dry_run_sends_nothing(held_trade):
    tid, live = held_trade
    alp = FakeAlpaca()
    assert run_add(alp, FakeFetcher(), tid, live, dry_run=True) is False
    assert alp.calls == []


def test_second_add_is_refused(held_trade):
    tid, live = held_trade
    assert run_add(FakeAlpaca(), FakeFetcher(), tid, live) is True
    alp = FakeAlpaca()
    live2 = dict(live, qty=67.2727, market_value=7_400.0)
    assert run_add(alp, FakeFetcher(), tid, live2) is False
    assert alp.calls == []


def test_pyramid_array_is_capped_in_the_journal():
    assert journal._ARRAY_CAPS["pyramid_decisions"] == 500
