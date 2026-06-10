"""EXP-006 dynamic universe discovery — the anti-blow-off guards are the point,
so most tests assert REJECTION."""

import json

import config
from agents.discovery import discover_universe
from tests.conftest import make_bars


def down_bars(n=60, start=150.0, step=-0.5):
    return make_bars(n=n, start=start, step=step)


def spike_bars():
    """Uptrend that goes parabolic in the last 5 sessions (+30%)."""
    bars = make_bars(n=55)
    last = bars[-1]["close"]
    for i in range(5):
        last *= 1.054   # ~+30% over 5 sessions
        bars.append({"date": f"2026-06-{i+1:02d}", "open": last, "high": last * 1.01,
                     "low": last * 0.99, "close": round(last, 2), "volume": 2_000_000})
    return bars


class FakeFetcher:
    def __init__(self, actives=None, gainers=None, quotes=None, bars=None):
        self.actives = actives or []
        self.gainers = gainers or []
        self.quotes = quotes or {}
        self.bars = bars or {}

    def get_most_actives(self, top=50):
        return self.actives

    def get_market_movers(self, top=50):
        return {"gainers": self.gainers, "losers": []}

    def get_quote(self, symbol):
        return self.quotes.get(symbol)

    def get_ohlcv(self, symbol, days=45):
        return self.bars.get(symbol, [])


GOOD_QUOTE = {"price": 50.0, "volume": 2_000_000, "market_cap": 10e9}
CORE = ["NVDA", "MSFT"]


def _discovery_record(tmp_journal):
    return json.loads(tmp_journal.read_text())["universe_discovery"][-1]


def test_accepts_clean_uptrend_name(tmp_journal):
    f = FakeFetcher(actives=[{"symbol": "GOOD"}],
                    quotes={"GOOD": GOOD_QUOTE}, bars={"GOOD": make_bars()})
    assert discover_universe(f, CORE) == ["GOOD"]
    rec = _discovery_record(tmp_journal)
    assert rec["accepted"][0]["symbol"] == "GOOD"
    assert rec["accepted"][0]["source"] == "most_actives"


def test_rejects_day_blowoff_from_movers_payload_without_fetching(tmp_journal):
    f = FakeFetcher(gainers=[{"symbol": "POP", "percent_change": 18.0}])
    assert discover_universe(f, CORE) == []
    rec = _discovery_record(tmp_journal)
    assert rec["rejected"][0]["symbol"] == "POP"
    assert "blowoff_day_gain" in rec["rejected"][0]["reason"]


def test_rejects_5d_parabolic(tmp_journal):
    f = FakeFetcher(actives=[{"symbol": "PARA"}],
                    quotes={"PARA": GOOD_QUOTE}, bars={"PARA": spike_bars()})
    assert discover_universe(f, CORE) == []
    assert "blowoff" in _discovery_record(tmp_journal)["rejected"][0]["reason"]


def test_rejects_downtrend_dead_cat(tmp_journal):
    f = FakeFetcher(gainers=[{"symbol": "KNIFE", "percent_change": 4.0}],
                    quotes={"KNIFE": GOOD_QUOTE}, bars={"KNIFE": down_bars()})
    assert discover_universe(f, CORE) == []
    assert _discovery_record(tmp_journal)["rejected"][0]["reason"] == "no_uptrend"


def test_rejects_small_caps_and_skips_core_names(tmp_journal):
    small = {"price": 50.0, "volume": 2_000_000, "market_cap": 5e8}
    f = FakeFetcher(actives=[{"symbol": "NVDA"}, {"symbol": "TINY"}],
                    quotes={"TINY": small}, bars={})
    assert discover_universe(f, CORE) == []
    rec = _discovery_record(tmp_journal)
    # NVDA is core — silently skipped, not a rejection; TINY fails the mcap floor.
    assert [r["symbol"] for r in rec["rejected"]] == ["TINY"]
    assert rec["rejected"][0]["reason"] == "mcap_floor"


def test_cap_respected(tmp_journal, monkeypatch):
    monkeypatch.setattr(config, "DISCOVERY_MAX_SYMBOLS", 2)
    syms = [f"S{i}" for i in range(5)]
    f = FakeFetcher(actives=[{"symbol": s} for s in syms],
                    quotes={s: GOOD_QUOTE for s in syms},
                    bars={s: make_bars() for s in syms})
    assert len(discover_universe(f, CORE)) == 2


def test_rejects_nan_bars(tmp_journal):
    bars = make_bars()
    bars[-3]["close"] = float("nan")   # NaN > threshold is False — must not pass
    f = FakeFetcher(actives=[{"symbol": "NANCO"}],
                    quotes={"NANCO": GOOD_QUOTE}, bars={"NANCO": bars})
    assert discover_universe(f, CORE) == []
    assert _discovery_record(tmp_journal)["rejected"][0]["reason"] == "bad_bars"


def test_disabled_returns_empty(tmp_journal, monkeypatch):
    monkeypatch.setattr(config, "DISCOVERY_ENABLED", False)
    f = FakeFetcher(actives=[{"symbol": "GOOD"}])
    assert discover_universe(f, CORE) == []
    assert not tmp_journal.exists()
