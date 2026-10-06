"""Unit tests for agents/sentiment_v2.py — the (disabled) v2 sentiment layer."""
import pytest

import config
from agents import sentiment_v2
from agents.sentiment_v2 import apply_tilt, get_sentiment_scores


def test_disabled_returns_neutral_without_api_calls(monkeypatch):
    monkeypatch.setattr(config, "SENTIMENT_ENABLED", False)
    assert get_sentiment_scores(["NVDA", "AAPL"]) == {}


def test_apply_tilt_bounded_and_renormalized():
    w = {"A": 0.5, "B": 0.5}
    out = apply_tilt(w, {"A": 1.0, "B": -1.0})
    assert out["A"] > 0.5 > out["B"]
    assert abs(sum(out.values()) - 1.0) < 1e-9
    # tilt bound: even max sentiment moves a 50% weight by <= 20% relatively
    assert out["A"] <= 0.5 * 1.2 + 1e-9


def test_apply_tilt_no_scores_is_identity():
    w = {"A": 0.5, "B": 0.5}
    assert apply_tilt(w, {}) == w


def test_apply_tilt_clamps_extreme_scores():
    w = {"A": 1.0}
    out = apply_tilt(w, {"A": 999.0})
    assert out == {"A": 1.0}
