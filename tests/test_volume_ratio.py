"""WS-D (2026-10-05 review §2.1F): partial-session volume ratio.

Incident: agents/technical.py and agents/screener.py computed
`volumes[-1] / avg(volumes[-21:-1])` where volumes[-1] is TODAY's in-progress daily bar
(yfinance includes it). Intraday that reads ~0.3x on a perfectly normal day. 8 of 13
LLM thesis exits cited a sub-0.5x "volume tripwire" (0.12-0.34x) — an artifact.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import agents.technical as technical
from agents.screener import Screener
from core.volume import relative_volume, session_fraction

ET = ZoneInfo("America/New_York")
AVG = 1_000_000


def at(s):
    return datetime.fromisoformat(s).replace(tzinfo=ET)


def bars_with_today(today_volume, today="2026-10-05", n=40):
    """n-1 completed days at AVG volume (dated before `today`), then today's bar."""
    bars = []
    for i in range(n - 1):
        price = 100 + i * 0.3
        # dates only need to be strictly before `today`
        bars.append({"date": f"2026-08-{(i % 28) + 1:02d}",
                     "open": price, "high": price + 1, "low": price - 1,
                     "close": price, "volume": AVG})
    bars.append({"date": today, "open": 112, "high": 113, "low": 111,
                 "close": 112, "volume": today_volume})
    return bars


def test_session_fraction_shape():
    assert session_fraction(at("2026-10-05T09:00")) == 1.0      # pre-open: bar is complete
    assert session_fraction(at("2026-10-05T16:00")) == 1.0
    assert session_fraction(at("2026-10-05T20:00")) == 1.0
    f_open, f_11, f_noon, f_3 = (session_fraction(at(f"2026-10-05T{t}"))
                                 for t in ("09:35", "11:20", "12:00", "15:00"))
    assert 0 < f_open < f_11 < f_noon < f_3 < 1.0


def test_normal_day_at_1120_et_is_not_a_volume_tripwire_2026_10_05():
    """The incident: a normal day 11:20 ET reads ~0.3x raw, must read ~1.0x."""
    now = at("2026-10-05T11:20")
    partial_so_far = AVG * session_fraction(now)            # exactly a normal day so far
    bars = bars_with_today(partial_so_far)
    raw = bars[-1]["volume"] / AVG
    assert raw < 0.5                                         # what the old code reported
    assert relative_volume(bars, now) == pytest.approx(1.0, abs=0.02)


def test_technical_agent_uses_normalized_ratio_2026_10_05(monkeypatch):
    now_et = at("2026-10-05T11:20")
    monkeypatch.setattr("core.volume._now_et", lambda: now_et)
    monkeypatch.setattr(technical.config, "TECHNICAL_MODE", "deterministic")
    bars = bars_with_today(AVG * session_fraction(now_et))
    out = technical.analyse("NVDA", bars)
    assert out["indicators"]["volume_ratio"] == pytest.approx(1.0, abs=0.02)


def test_screener_uses_normalized_ratio_2026_10_05(monkeypatch):
    now_et = at("2026-10-05T11:20")
    monkeypatch.setattr("core.volume._now_et", lambda: now_et)
    # a genuine 2.5x-pace spike intraday must still register as a spike
    bars = bars_with_today(AVG * session_fraction(now_et) * 2.5)
    score, sig = Screener._score_volume(object(), bars)
    assert sig["volume_spike_ratio"] == pytest.approx(2.5, abs=0.05)
    assert sig["volume_spike"] is True
    # and a normal-pace day is NOT a spike and NOT read as ~0.3x
    bars = bars_with_today(AVG * session_fraction(now_et))
    _, sig = Screener._score_volume(object(), bars)
    assert sig["volume_spike_ratio"] == pytest.approx(1.0, abs=0.02)


def test_completed_bar_after_close_is_unchanged():
    """After 16:00 ET today's bar is complete: identical to the pre-fix formula."""
    now = at("2026-10-05T17:30")
    bars = bars_with_today(1_500_000)
    assert relative_volume(bars, now) == pytest.approx(1.5)


def test_last_bar_from_a_previous_day_is_complete():
    now = at("2026-10-05T11:20")
    bars = bars_with_today(900_000, today="2026-10-02")      # Friday's bar, Monday run
    assert relative_volume(bars, now) == pytest.approx(0.9)


def test_insufficient_history_returns_none():
    assert relative_volume(bars_with_today(1)[-10:], at("2026-10-05T17:30")) is None
    assert relative_volume([], at("2026-10-05T17:30")) is None


def test_sentinel_volume_spike_uses_normalized_ratio_2026_10_05(monkeypatch):
    """sentinel.check_volume_spikes ran raw off yfinance: a real 5x-pace spike at 11:20 ET
    read ~1.5x and never fired the 4x trigger."""
    import pandas as pd
    import sentinel

    now_et = at("2026-10-05T11:20")
    monkeypatch.setattr("core.volume._now_et", lambda: now_et)
    idx = pd.bdate_range(end="2026-10-05", periods=30)
    hist = pd.DataFrame({"Volume": [AVG] * 29 + [AVG * session_fraction(now_et) * 5]}, index=idx)

    class FakeTicker:
        def __init__(self, symbol):
            pass

        def history(self, period):
            return hist

    monkeypatch.setattr(sentinel.yf, "Ticker", FakeTicker)
    out = sentinel.check_volume_spikes([{"symbol": "NVDA"}])
    assert len(out) == 1 and out[0]["trigger_type"] == "volume_spike"
