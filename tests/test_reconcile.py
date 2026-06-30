"""Tests for core.reconcile — self-healing journal↔broker reconciliation.

The pipeline relies on this to adopt any broker position whose open_trade record was lost
to a write failure (push race, crash, timeout), so the journal converges to the broker
every run. All broker access is faked; no network.
"""

from core import reconcile


class FakeAlpaca:
    def __init__(self, positions, account=None, stops=None, fill=None):
        self._positions = positions
        self._account = account or {"portfolio_value": 100_000}
        self._stops = stops or {}
        self._fill = fill

    def get_positions(self):
        return self._positions

    def get_account(self):
        return self._account

    def get_open_stop_orders(self, sym):
        return self._stops.get(sym, [])

    def get_last_filled_buy(self, sym):
        return self._fill


def test_find_untracked_skips_journaled_shorts_and_zero(monkeypatch):
    monkeypatch.setattr(reconcile, "get_open_trades", lambda: [{"symbol": "AMD"}])
    alp = FakeAlpaca([
        {"symbol": "AMD", "qty": "5", "avg_entry": "500"},          # tracked -> skip
        {"symbol": "MU",  "qty": "3.9294", "avg_entry": "1043.20"}, # untracked -> adopt
        {"symbol": "SH",  "qty": "-2", "avg_entry": "30"},          # short -> skip
        {"symbol": "ZZ",  "qty": "0", "avg_entry": "10"},           # zero qty -> skip
    ])
    assert [p["symbol"] for p in reconcile.find_untracked(alp)] == ["MU"]


def test_reconcile_adopts_from_broker_truth(monkeypatch):
    monkeypatch.setattr(reconcile, "get_open_trades", lambda: [{"symbol": "AMD"}])
    monkeypatch.setattr(reconcile, "_sector", lambda s: "Technology")
    saved = {}
    monkeypatch.setattr(reconcile, "_load", lambda: {"open_trades": [{"symbol": "AMD"}]})
    monkeypatch.setattr(reconcile, "_save", lambda d: saved.update(d))
    alp = FakeAlpaca(
        [{"symbol": "AMD", "qty": "5", "avg_entry": "500"},
         {"symbol": "MU",  "qty": "3.9294", "avg_entry": "1043.20"}],
        stops={"MU": [{"id": "o1", "stop_price": 959.74}]},
        fill={"filled_at": "2026-06-24T15:56:43+00:00", "filled_avg_price": 1043.2, "qty": 3.9294},
    )
    recs = reconcile.reconcile_untracked(alp, apply=True)
    assert len(recs) == 1
    r = recs[0]
    assert r["symbol"] == "MU"
    assert r["reconciled_from_broker"] is True
    assert r["entry_ts"] == "2026-06-24T15:56:43+00:00"   # recovered from order history
    assert r["stop_price"] == 959.74
    assert r["stop_order_id"] == "o1"
    assert r["conviction"] == 6
    assert r["position_pct"] == round(1043.2 * 3.9294 / 100_000, 4)
    assert {t["symbol"] for t in saved["open_trades"]} == {"AMD", "MU"}   # written through


def test_reconcile_noop_when_in_sync(monkeypatch):
    monkeypatch.setattr(reconcile, "get_open_trades", lambda: [{"symbol": "MU"}])
    wrote = {"n": 0}
    monkeypatch.setattr(reconcile, "_load", lambda: {"open_trades": [{"symbol": "MU"}]})
    monkeypatch.setattr(reconcile, "_save", lambda d: wrote.__setitem__("n", wrote["n"] + 1))
    alp = FakeAlpaca([{"symbol": "MU", "qty": "3.9294", "avg_entry": "1043.20"}])
    assert reconcile.reconcile_untracked(alp, apply=True) == []
    assert wrote["n"] == 0   # nothing written when already in sync


def test_reconcile_dry_run_writes_nothing(monkeypatch):
    monkeypatch.setattr(reconcile, "get_open_trades", lambda: [])
    monkeypatch.setattr(reconcile, "_sector", lambda s: "Tech")
    monkeypatch.setattr(reconcile, "_load", lambda: {"open_trades": []})
    wrote = {"n": 0}
    monkeypatch.setattr(reconcile, "_save", lambda d: wrote.__setitem__("n", wrote["n"] + 1))
    alp = FakeAlpaca([{"symbol": "MU", "qty": "1", "avg_entry": "1000"}], fill=None)
    recs = reconcile.reconcile_untracked(alp, apply=False)
    assert len(recs) == 1   # record built
    assert wrote["n"] == 0   # but not persisted
