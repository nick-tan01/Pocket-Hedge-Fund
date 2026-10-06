"""Regression tests for the v2 backtest signal math (scripts/backtest_v2.py).

Uses synthetic bars — no network. The key guard: signal_residual must rank on
ECONOMIC idiosyncratic drift, not numerical dust (2026-10-06 bug: sum(residuals)
from a regression fit on the formation window is identically ~0, so the ranking
was floating-point noise).
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from backtest_v2 import signal_52wh, signal_residual, LOOKBACK, SKIP


def _bars(n: int, daily_ret: float, vol: float = 0.0, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = daily_ret + rng.normal(0, vol, n)
    close = 100 * np.exp(np.cumsum(rets))
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame({"Open": close * 0.999, "High": close * 1.001,
                         "Low": close * 0.999, "Close": close,
                         "Volume": 1_000_000}, index=idx)


def _mkt(n: int, seed: int = 7) -> pd.Series:
    rng = np.random.default_rng(seed + 1)
    rets = 0.0004 + rng.normal(0, 0.01, n)
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.Series(100 * np.exp(np.cumsum(rets)), index=idx)


N = LOOKBACK + SKIP + 30


def test_residual_ranks_idiosyncratic_drift():
    """Alpha stock must outrank a pure market tracker by an ECONOMIC margin."""
    mkt = _mkt(N)
    mkt_rets = np.log(mkt / mkt.shift(1)).fillna(0).values
    # A: beta 1.0 + 15bp/day idiosyncratic drift; B: beta 1.0, no drift.
    rng = np.random.default_rng(3)
    eps_a = rng.normal(0, 0.005, N)
    close_a = 100 * np.exp(np.cumsum(mkt_rets + 0.0015 + eps_a))
    close_b = 100 * np.exp(np.cumsum(mkt_rets + rng.normal(0, 0.005, N)))
    idx = pd.bdate_range("2020-01-01", periods=N)
    ha = pd.DataFrame({"Open": close_a, "High": close_a, "Low": close_a,
                       "Close": close_a, "Volume": 1e6}, index=idx)
    hb = pd.DataFrame({"Open": close_b, "High": close_b, "Low": close_b,
                       "Close": close_b, "Volume": 1e6}, index=idx)
    sa, sb = signal_residual(ha, mkt), signal_residual(hb, mkt)
    assert sa > 0.10, f"drift stock signal should be clearly positive, got {sa}"
    assert sa > sb + 0.10, f"drift must outrank tracker: {sa} vs {sb}"


def test_residual_not_dust():
    """Sum-of-residuals bug guard: signal magnitude must be economic, not ~1e-15."""
    mkt = _mkt(N)
    h = _bars(N, 0.001, vol=0.01)
    s = signal_residual(h, mkt)
    assert abs(s) > 1e-6, f"signal is numerical dust: {s}"


def test_52wh_proximity():
    n = LOOKBACK + 10
    idx = pd.bdate_range("2020-01-01", periods=n)
    at_high = pd.DataFrame({"Open": 100.0, "High": 100.0, "Low": 99.0,
                            "Close": 100.0, "Volume": 1e6}, index=idx)
    off = at_high.copy()
    off.loc[idx[-1], "Close"] = 50.0
    assert signal_52wh(at_high) == pytest.approx(1.0)
    assert signal_52wh(off) == pytest.approx(0.5)


def test_signals_need_history():
    short = _bars(100, 0.001)
    assert np.isnan(signal_52wh(short))
    assert np.isnan(signal_residual(short, _mkt(100)))


def test_no_lookahead():
    """Appending future bars must not change the signal evaluated at t."""
    mkt = _mkt(N + 60)
    h = _bars(N + 60, 0.001, vol=0.01)
    s1 = signal_residual(h.iloc[:N], mkt.iloc[:N])
    s2 = signal_residual(h.iloc[:N], mkt)  # mkt longer, but hist cut at N
    assert s1 == s2
