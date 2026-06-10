"""Journal round-trip, trim P&L booking (A5), atomic save + .bak, and the
no-silent-reset guarantee (A4)."""

import json

import pytest

from core import journal


def test_open_trim_close_roundtrip_books_trim_pnl(tmp_journal):
    tid = journal.log_trade_open(
        symbol="ABC", side="buy", qty=10.0, entry_price=100.0, stop_price=92.0,
        conviction=7, debate_id="d1", portfolio_value=100_000.0,
        sector="Technology", stop_order_id="so_1",
    )
    trade = journal.get_open_trades()[0]
    assert trade["stop_order_id"] == "so_1"
    assert trade["stop_ratcheted"] is False
    assert trade["trim_pnl"] == 0.0

    # Trim 4 shares at 110 against a 100 basis → +40 realized
    journal.log_trade_trim(tid, trim_qty=4.0, new_qty=6.0, price=110.0,
                           reason="test", basis=100.0)
    trade = journal.get_open_trades()[0]
    assert trade["trim_pnl"] == pytest.approx(40.0)
    assert trade["qty"] == 6.0
    assert trade["trim_history"][0]["realized_pnl"] == pytest.approx(40.0)

    # Close remaining 6 at 120 → +120 final leg; total = 160
    journal.log_trade_close(tid, exit_price=120.0, exit_reason="trailing_stop")
    closed = journal.get_all_trades()[0]
    assert closed["pnl"] == pytest.approx(120.0)
    assert closed["trim_pnl"] == pytest.approx(40.0)
    assert closed["total_pnl"] == pytest.approx(160.0)
    assert journal.get_open_trades() == []


def test_corrupt_journal_raises_instead_of_resetting(tmp_journal):
    tmp_journal.write_text("{ this is not json")
    with pytest.raises(journal.JournalCorrupt):
        journal.get_open_trades()
    # File must be untouched — no silent overwrite.
    assert tmp_journal.read_text() == "{ this is not json"


def test_save_is_atomic_and_keeps_bak(tmp_journal):
    journal.log_snapshot(100_000.0, 50_000.0, 700.0)
    journal.log_snapshot(101_000.0, 50_000.0, 701.0)
    bak = tmp_journal.with_suffix(".json.bak")
    assert bak.exists()
    prev = json.loads(bak.read_text())
    cur = json.loads(tmp_journal.read_text())
    assert len(cur["snapshots"]) == len(prev["snapshots"]) + 1


def test_baseline_shadow_capped_append(tmp_journal):
    journal.log_baseline_shadow({"would_buy": [{"symbol": "ABC"}], "skipped": []})
    data = json.loads(tmp_journal.read_text())
    assert len(data["baseline_shadow"]) == 1
    assert data["baseline_shadow"][0]["would_buy"][0]["symbol"] == "ABC"
