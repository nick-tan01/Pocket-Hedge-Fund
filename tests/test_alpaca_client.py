"""Pure-logic tests for core.alpaca_client helpers (no network / no API keys)."""

from enum import Enum
from types import SimpleNamespace

import pytest
import requests

from core.alpaca_client import _account_dict, _position_dict, _retry_read


def test_account_dict_tolerates_none_fields():
    # Regression: Alpaca returns daytrade_count=None on paper accounts, and int(None)
    # crashed get_account() — the first broker call every run makes — taking down the
    # trading pipeline, snapshot job, and health checks on 2026-07-06. Nulls -> 0.
    acct = SimpleNamespace(
        portfolio_value="103557.71", cash="58562.78", equity="103557.71",
        buying_power="360236.91", daytrade_count=None, pattern_day_trader=None,
    )
    d = _account_dict(acct)
    assert d["daytrade_count"] == 0
    assert d["portfolio_value"] == 103557.71
    assert d["cash"] == 58562.78
    assert d["equity"] == 103557.71
    assert d["buying_power"] == 360236.91


def test_account_dict_parses_normal_values():
    acct = SimpleNamespace(portfolio_value="100000", cash="50000", equity="100000",
                           buying_power="200000", daytrade_count=3)
    assert _account_dict(acct) == {
        "portfolio_value": 100000.0, "cash": 50000.0, "equity": 100000.0,
        "buying_power": 200000.0, "daytrade_count": 3,
    }


class _Side(Enum):
    LONG = "long"


def test_position_dict_tolerates_none_fields():
    # Regression guard for the get_positions() sibling of the 2026-07-06 get_account()
    # outage: a degraded position payload (None numeric fields) must coerce to 0,
    # not raise TypeError — get_positions feeds the pipeline, the snapshot job and
    # the reconcile self-healer.
    p = SimpleNamespace(
        symbol="MU", qty=None, avg_entry_price=None, current_price=None,
        market_value=None, unrealized_pl=None, unrealized_plpc=None, side=None,
    )
    d = _position_dict(p)
    assert d["symbol"] == "MU"
    assert d["qty"] == 0.0
    assert d["avg_entry"] == 0.0
    assert d["current_price"] == 0.0
    assert d["side"] == "long"


def test_position_dict_parses_normal_values():
    p = SimpleNamespace(
        symbol="AMD", qty="10", avg_entry_price="100.5", current_price="110.25",
        market_value="1102.50", unrealized_pl="97.5", unrealized_plpc="0.097",
        side=_Side.LONG,
    )
    d = _position_dict(p)
    assert d == {
        "symbol": "AMD", "qty": 10.0, "avg_entry": 100.5, "current_price": 110.25,
        "market_value": 1102.5, "unrealized_pl": 97.5, "unrealized_plpc": 0.097,
        "side": "long",
    }


def test_retry_read_retries_transient_then_succeeds(monkeypatch):
    # Regression guard for the Jun 11/19/30 outages: a single connect timeout on a
    # read call must be retried, not kill the run.
    monkeypatch.setattr("core.alpaca_client.time.sleep", lambda s: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.ConnectTimeout("connect timed out")
        return "ok"

    assert _retry_read(flaky, "test") == "ok"
    assert calls["n"] == 3


def test_retry_read_does_not_retry_non_transient(monkeypatch):
    # Auth/validation errors are not transient — retrying them just delays the
    # real failure. One attempt only.
    monkeypatch.setattr("core.alpaca_client.time.sleep", lambda s: None)
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise ValueError("bad request")

    with pytest.raises(ValueError):
        _retry_read(broken, "test")
    assert calls["n"] == 1


def test_retry_read_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr("core.alpaca_client.time.sleep", lambda s: None)
    calls = {"n": 0}

    def always_down():
        calls["n"] += 1
        raise requests.exceptions.ConnectionError("down")

    with pytest.raises(requests.exceptions.ConnectionError):
        _retry_read(always_down, "test")
    assert calls["n"] == 3
