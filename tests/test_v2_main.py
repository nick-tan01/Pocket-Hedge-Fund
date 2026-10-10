"""Tests for v2_main.py — the v2 pipeline orchestration.

Heavy broker/journal interactions use fakes; the journal itself uses the
tmp_journal fixture so dashboard/data.json is never touched.
"""
import pytest

import config
import v2_main
from v2_main import v2_slot, _last_v2_rebalance_month, _v2_positions
from core.journal import (get_open_trades, log_run, log_trade_open,
                          update_open_trade)


def test_v2_slot_namespace():
    assert v2_slot("2026-10-07T14:05") == "v2-2026-10-07T14:05"
    assert v2_slot("  ") == ""
    assert v2_slot("") == ""
    # distinct from the v1 label for the same wall-clock slot
    assert v2_slot("2026-10-07T14:05") != "2026-10-07T14:05"


def test_last_v2_rebalance_month(tmp_journal):
    assert _last_v2_rebalance_month() is None
    log_run("v2", ["A"], 1, run_meta={"strategy": "v2", "v2_rebalanced": False,
                                      "slot": "v2-x"})
    assert _last_v2_rebalance_month() is None  # not a rebalance run
    log_run("v2", ["A"], 1, run_meta={"strategy": "v2", "v2_rebalanced": True,
                                      "slot": "v2-y"})
    assert _last_v2_rebalance_month() is not None
    # v1 runs never count
    log_run("pre_market", ["B"], 1, run_meta={"slot": "2026-10-07T14:05"})
    assert _last_v2_rebalance_month() is not None


def test_v2_positions_filters_by_strategy(tmp_journal):
    t1 = log_trade_open("AAA", "buy", 10, 100.0, 90.0, 0, "", portfolio_value=100000)
    update_open_trade(t1, {"strategy": "v2"})
    log_trade_open("BBB", "buy", 10, 100.0, 90.0, 0, "", portfolio_value=100000)
    got = _v2_positions()
    assert [t["symbol"] for t in got] == ["AAA"]


class _FakeAlpaca:
    def __init__(self, positions=None, pv=100000.0, cash=100000.0):
        self._positions = positions or []
        self.orders = []
        self.closed = []

    def get_account(self):
        return {"portfolio_value": self._pv, "cash": self._cash,
                "equity": self._pv, "buying_power": self._cash}

    @property
    def _pv(self):
        return 100000.0

    @property
    def _cash(self):
        return 100000.0

    def get_positions(self):
        return self._positions

    def get_latest_price(self, symbol):
        return 100.0

    def close_position(self, symbol, reason=""):
        self.closed.append(symbol)
        return {"id": "close-1", "symbol": symbol}

    def submit_market_order(self, symbol, qty, side, reason="", ref_price=None):
        self.orders.append((symbol, qty, side))
        return {"id": "ord-1", "symbol": symbol, "side": side, "qty": qty}


class _FakeFetcher:
    def get_ohlcv(self, symbol, days=60):
        return []

    def get_quote(self, symbol):
        return {"symbol": symbol, "price": 100.0, "volume": 1_000_000,
                "market_cap": 1e10}


def _canned_scores(*a, **k):
    return {"AAA": 0.9, "BBB": 0.8, "CCC": 0.7}


def test_rebalance_dry_run_sells_drops_buys_adds(tmp_journal, monkeypatch):
    monkeypatch.setattr(v2_main.signal_v2, "compute_scores", _canned_scores)
    monkeypatch.setattr(config, "V2_TOP_K", 2)  # target: AAA, BBB
    monkeypatch.setattr(config, "V2_MAX_GROSS", 1.0)
    # book holds AAA (keep) and ZZZ (drop)
    t1 = log_trade_open("AAA", "buy", 100, 100.0, 90.0, 0, "",
                        portfolio_value=100000)
    update_open_trade(t1, {"strategy": "v2"})
    t2 = log_trade_open("ZZZ", "buy", 100, 100.0, 90.0, 0, "",
                        portfolio_value=100000)
    update_open_trade(t2, {"strategy": "v2"})
    alpaca = _FakeAlpaca(positions=[
        {"symbol": "AAA", "qty": 100, "avg_entry": 100.0, "current_price": 100.0,
         "market_value": 10000.0},
        {"symbol": "ZZZ", "qty": 100, "avg_entry": 100.0, "current_price": 100.0,
         "market_value": 10000.0},
    ])
    trades, picks = v2_main._execute_rebalance(alpaca, _FakeFetcher(),
                                               dry_run=True, reason="test")
    assert picks == ["AAA", "BBB"]
    assert trades == 0  # dry run executes nothing
    assert alpaca.orders == [] and alpaca.closed == []
    # book untouched in dry run
    assert {t["symbol"] for t in _v2_positions()} == {"AAA", "ZZZ"}


def test_rebalance_live_executes_diff(tmp_journal, monkeypatch):
    monkeypatch.setattr(v2_main.signal_v2, "compute_scores", _canned_scores)
    monkeypatch.setattr(v2_main.v1, "_await_fill",
                        lambda alpaca, oid: (100.0, 500.0))
    monkeypatch.setattr(config, "V2_TOP_K", 2)
    monkeypatch.setattr(config, "V2_MAX_GROSS", 1.0)
    t1 = log_trade_open("AAA", "buy", 100, 100.0, 90.0, 0, "",
                        portfolio_value=100000)
    update_open_trade(t1, {"strategy": "v2"})
    t2 = log_trade_open("ZZZ", "buy", 100, 100.0, 90.0, 0, "",
                        portfolio_value=100000)
    update_open_trade(t2, {"strategy": "v2"})
    alpaca = _FakeAlpaca(positions=[
        {"symbol": "AAA", "qty": 100, "avg_entry": 100.0, "current_price": 100.0,
         "market_value": 10000.0},
        {"symbol": "ZZZ", "qty": 100, "avg_entry": 100.0, "current_price": 100.0,
         "market_value": 10000.0},
    ])
    trades, picks = v2_main._execute_rebalance(alpaca, _FakeFetcher(),
                                               dry_run=False, reason="test")
    # ZZZ dropped -> closed; BBB added -> bought; AAA held (weight ~10% vs 50%
    # target -> re-equalize buy)
    assert "ZZZ" in alpaca.closed
    buys = [s for s, q, d in alpaca.orders if d == "buy"]
    assert "BBB" in buys
    assert {t["symbol"] for t in _v2_positions()} == {"AAA", "BBB"}
    assert all(t.get("strategy") == "v2" for t in _v2_positions())
    assert trades >= 2


def _cutover_alpaca():
    a = _FakeAlpaca()
    a.get_order = lambda oid: {"status": "filled", "filled_avg_price": 101.0,
                              "filled_qty": 10}
    return a


def test_cutover_closes_only_non_v2_positions(tmp_journal, monkeypatch):
    t1 = log_trade_open("AAA", "buy", 10, 100.0, 90.0, 0, "",
                        portfolio_value=100000)  # v1: no strategy tag
    t2 = log_trade_open("BBB", "buy", 10, 100.0, 90.0, 0, "",
                        portfolio_value=100000)
    update_open_trade(t2, {"strategy": "v2"})
    alpaca = _cutover_alpaca()
    monkeypatch.setattr(v2_main, "AlpacaClient", lambda: alpaca)
    v2_main.run_cutover(slot="2026-10-12T14:05")
    assert alpaca.closed == ["AAA"]
    remaining = {t["symbol"] for t in get_open_trades()}
    assert remaining == {"BBB"}


def test_cutover_idempotent_per_slot(tmp_journal, monkeypatch):
    log_trade_open("AAA", "buy", 10, 100.0, 90.0, 0, "", portfolio_value=100000)
    alpaca = _cutover_alpaca()
    monkeypatch.setattr(v2_main, "AlpacaClient", lambda: alpaca)
    v2_main.run_cutover(slot="2026-10-12T14:05")
    assert alpaca.closed == ["AAA"]
    v2_main.run_cutover(slot="2026-10-12T14:05")  # same slot: no-op
    assert alpaca.closed == ["AAA"]


def test_cutover_dry_run_closes_nothing(tmp_journal, monkeypatch):
    log_trade_open("AAA", "buy", 10, 100.0, 90.0, 0, "", portfolio_value=100000)
    alpaca = _cutover_alpaca()
    monkeypatch.setattr(v2_main, "AlpacaClient", lambda: alpaca)
    v2_main.run_cutover(dry_run=True, slot="2026-10-12T14:05")
    assert alpaca.closed == []
    assert {t["symbol"] for t in get_open_trades()} == {"AAA"}
