"""Table-driven tests for the deterministic risk manager (audit §7.3 — the most
dangerous untested logic: sizing, caps, top-up credits, regime multipliers)."""

import pytest

import config
from agents import risk_manager
from tests.conftest import make_bars


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(risk_manager, "_get_sector", lambda s: "Technology")
    monkeypatch.setattr(risk_manager, "_get_beta", lambda s: 1.0)


def pm(action="buy", conviction=7, bull_r2=7, bear_r2=6):
    return {
        "action": action, "final_conviction": conviction,
        "key_risk_to_monitor": "kr", "deciding_factor": "df",
        "bull_r2_conviction": bull_r2, "bear_r2_conviction": bear_r2,
    }


def evaluate(pm_verdict, open_positions=None, regime="bull", vix="normal",
             price=100.0, bars=None):
    return risk_manager.evaluate(
        symbol="TEST", pm_verdict=pm_verdict, current_price=price,
        bars=bars or make_bars(), portfolio_value=100_000.0,
        open_positions=open_positions or [], regime=regime, vix_regime=vix,
        fetcher=None,
    )


def test_conviction_below_min_skips():
    p = evaluate(pm(conviction=5))
    assert p.action == "skip"
    assert "below minimum" in p.reason


def test_bear_regime_requires_8():
    assert evaluate(pm(conviction=7), regime="bear").action == "skip"
    assert evaluate(pm(conviction=8), regime="bear").action == "buy"


def test_conviction_7_sizes_6pct_bull_normal():
    p = evaluate(pm(conviction=7))
    assert p.action == "buy"
    assert p.position_usd == pytest.approx(6000, abs=1)


def test_vix_and_regime_multipliers_shrink_size():
    p = evaluate(pm(conviction=7), regime="caution", vix="elevated_vix")
    # 6% * 0.8 * 0.75 = 3.6%
    assert p.position_usd == pytest.approx(3600, abs=1)


def test_bear_spread_shading_half_size():
    p = evaluate(pm(conviction=7, bull_r2=5, bear_r2=9))
    assert p.position_usd == pytest.approx(3000, abs=1)


def test_already_holding_full_position_holds():
    held = [{"symbol": "TEST", "position_pct": 0.06, "sector": "Technology"}]
    assert evaluate(pm(), open_positions=held).action == "hold"


def test_exposure_cap_blocks_buy():
    held = [{"symbol": f"P{i}", "position_pct": 0.085, "sector": "Other"} for i in range(7)]
    p = evaluate(pm(conviction=7), open_positions=held)  # deployed 59.5%
    assert p.action == "skip"
    assert "exposure cap" in p.reason.lower()


def test_sector_cap_blocks_buy():
    held = [
        {"symbol": "A", "position_pct": 0.10, "sector": "Technology"},
        {"symbol": "B", "position_pct": 0.10, "sector": "Technology"},
        {"symbol": "C", "position_pct": 0.04, "sector": "Technology"},
    ]
    p = evaluate(pm(conviction=7), open_positions=held)
    assert p.action == "skip"
    assert "sector cap" in p.reason.lower()


def test_max_positions_blocks_buy():
    held = [{"symbol": f"P{i}", "position_pct": 0.05, "sector": "Other"} for i in range(8)]
    p = evaluate(pm(conviction=7), open_positions=held)
    assert p.action == "skip"
    assert "max positions" in p.reason.lower()


def test_remnant_topup_buys_only_the_gap(monkeypatch):
    monkeypatch.setattr(config, "REMNANT_REBUY", True)
    held = [{"symbol": "TEST", "position_pct": 0.02, "sector": "Technology"}]
    p = evaluate(pm(conviction=7), open_positions=held)
    assert p.action == "buy"
    # target 6%, held 2% → buy 4% = $4,000
    assert p.position_usd == pytest.approx(4000, abs=1)


def test_stop_never_below_hard_stop():
    p = evaluate(pm(conviction=7), price=100.0)
    assert p.stop_price >= 100.0 * (1 - config.HARD_STOP_PCT) - 0.01
    assert p.stop_price < 100.0
