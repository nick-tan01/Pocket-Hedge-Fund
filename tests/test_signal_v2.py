"""Unit tests for core/signal_v2.py — the v2 residual-momentum signal engine.

Synthetic bars only; no network. Complements tests/test_backtest_v2.py, which
covers the same math inside the backtest harness.
"""
import numpy as np
import pytest

from core import signal_v2
from core.signal_v2 import (dollar_volume_ok, equal_weights, residual_score,
                            select_top)


def _closes(n, drift=0.0, vol=0.01, seed=11):
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(drift + rng.normal(0, vol, n)))


def test_residual_score_ranks_alpha():
    n = 300
    mkt = _closes(n, drift=0.0004, vol=0.01, seed=11)
    rng = np.random.default_rng(12)
    # A: market beta 1 + strong idiosyncratic drift; B: beta 1, no drift.
    mret = np.log(mkt[1:] / mkt[:-1])
    a = 100 * np.exp(np.cumsum(mret + 0.002 + rng.normal(0, 0.005, n - 1)))
    b = 100 * np.exp(np.cumsum(mret + rng.normal(0, 0.005, n - 1)))
    a = np.concatenate([[100.0], a])
    b = np.concatenate([[100.0], b])
    sa = residual_score(a, mkt)
    sb = residual_score(b, mkt)
    assert sa > 0.2, sa
    assert sa > sb + 0.2, (sa, sb)


def test_residual_score_insufficient_history():
    mkt = _closes(100, seed=1)
    assert np.isnan(residual_score(_closes(100, seed=2), mkt))


def test_residual_score_never_raises_on_garbage():
    mkt = _closes(300, seed=1)
    bad = np.full(300, np.nan)
    assert np.isnan(residual_score(bad, mkt))
    zero = np.zeros(300)
    assert np.isnan(residual_score(zero, mkt))


def test_select_top_deterministic():
    scores = {"B": 0.5, "A": 0.9, "C": 0.9, "D": -0.2}
    assert select_top(scores, 2) == ["A", "C"]  # tie -> symbol order
    assert select_top(scores, 0) == []
    assert select_top({}, 5) == []


def test_equal_weights_flat():
    w = equal_weights(["A", "B", "C", "D"], gross=1.0)
    assert w == {"A": 0.25, "B": 0.25, "C": 0.25, "D": 0.25}
    assert equal_weights([], gross=1.0) == {}


def test_dollar_volume_gate():
    closes = np.full(63, 100.0)
    vols = np.full(63, 200_000)  # $20M/day -> passes $10M gate
    assert dollar_volume_ok(closes, vols, 10_000_000)
    thin = np.full(63, 50_000)   # $5M/day -> fails
    assert not dollar_volume_ok(closes, thin, 10_000_000)


class _FakeFetcher:
    def __init__(self, bars):
        self.bars = bars

    def get_ohlcv(self, symbol, days=60):
        if symbol == "BOOM":
            raise RuntimeError("feed exploded")
        return self.bars.get(symbol, [])[-days:]


def _mkbars(n, px=100.0, vol_px=100.0, volume=1_000_000):
    return [{"date": f"2024-01-{(i % 28) + 1:02d}", "open": px, "high": px,
             "low": px, "close": vol_px if False else px, "volume": volume}
            for i in range(n)]


def test_compute_scores_skips_failures():
    n = 320
    good = _mkbars(n, px=100.0)
    # upward drift -> positive residual vs flat market
    drifted = _mkbars(n, px=100.0)
    for i, b in enumerate(drifted):
        px = 100 * (1.001 ** i)
        b.update(open=px, high=px, low=px, close=px)
    import numpy as _np
    _rng = _np.random.default_rng(99)
    _mwalk = 500 * _np.exp(_np.cumsum(0.0002 + _rng.normal(0, 0.01, n)))
    mkt = _mkbars(n, px=500.0)
    for i, b in enumerate(mkt):
        px = float(_mwalk[i])
        b.update(open=px, high=px, low=px, close=px)
    fetcher = _FakeFetcher({"AAA": drifted, "BBB": good, "SPY": mkt,
                            "THIN": _mkbars(n, volume=1_000)})
    scores = signal_v2.compute_scores(fetcher, ["AAA", "BBB", "BOOM", "THIN", "NODATA"])
    assert set(scores) == {"AAA", "BBB"}  # BOOM raised, THIN illiquid, NODATA missing
    assert scores["AAA"] > scores["BBB"]
