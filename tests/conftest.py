import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402


@pytest.fixture
def tmp_journal(tmp_path, monkeypatch):
    """Point the journal at a throwaway file so tests never touch dashboard/data.json."""
    path = tmp_path / "data.json"
    monkeypatch.setattr(config, "JOURNAL_PATH", str(path))
    return path


def make_bars(n=60, start=100.0, step=0.5, vol=1_000_000):
    """Synthetic gently-uptrending OHLCV bars, oldest first."""
    bars = []
    price = start
    for i in range(n):
        price += step
        bars.append({
            "date":   f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            "open":   round(price - 0.3, 2),
            "high":   round(price + 1.0, 2),
            "low":    round(price - 1.0, 2),
            "close":  round(price, 2),
            "volume": vol,
        })
    return bars
