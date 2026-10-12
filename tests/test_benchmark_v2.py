"""v2 (EXP-018) alpha measurement: beta/alpha/t/CI + IR vs SPY/QQQ/MTUM/EW universe.

Measurement only — added 2026-10-11 so v2 is judged on beta-adjusted alpha with honest
uncertainty, not on raw return vs SPY (the v1 "beat SPY" reading was low exposure in a
down month).
"""

import json
import random
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from core import benchmark
from core.benchmark import (V2_MIN_DAYS, _t_crit_975, compute_v2_alpha,
                            ew_buy_hold_levels, relative_stats, v2_era_start)


def _synthetic(n, alpha_d, beta, noise_sd, seed=7):
    rng = random.Random(seed)
    mkt = [rng.gauss(0.0004, 0.01) for _ in range(n)]
    fund = [alpha_d + beta * m + rng.gauss(0, noise_sd) for m in mkt]
    return fund, mkt


# ── pure regression math ──────────────────────────────────────────────────────

def test_t_crit_matches_student_t_table():
    assert _t_crit_975(18) == pytest.approx(2.1009, abs=1e-3)
    assert _t_crit_975(60) == pytest.approx(2.0003, abs=1e-3)
    assert _t_crit_975(1000) == pytest.approx(1.9623, abs=1e-3)


def test_recovers_known_beta_and_alpha():
    # 10.08%/yr alpha (0.04%/day), beta 1.3, 0.2% daily idiosyncratic noise, 500 days.
    fund, mkt = _synthetic(500, alpha_d=0.0004, beta=1.3, noise_sd=0.002)
    out = relative_stats(fund, mkt)
    assert out["n"] == 500
    assert out["beta"] == pytest.approx(1.3, abs=0.03)
    true_alpha_pct = 0.0004 * 252 * 100
    lo, hi = out["alpha_ci95_pct"]
    assert lo < true_alpha_pct < hi
    assert out["alpha_ann_pct"] == pytest.approx(true_alpha_pct, abs=3.0)
    assert out["alpha_t"] > 2.0          # a real edge at this n is significant
    assert out["r2"] > 0.9
    assert out["information_ratio"] is not None


def test_zero_alpha_is_not_significant():
    fund, mkt = _synthetic(250, alpha_d=0.0, beta=0.9, noise_sd=0.004, seed=11)
    out = relative_stats(fund, mkt)
    lo, hi = out["alpha_ci95_pct"]
    assert lo < 0 < hi
    assert abs(out["alpha_t"]) < 2.0


def test_insufficient_sample_reports_n_not_numbers():
    fund, mkt = _synthetic(V2_MIN_DAYS - 1, 0.001, 1.0, 0.002)
    out = relative_stats(fund, mkt)
    assert out == {"n": V2_MIN_DAYS - 1,
                   "status": f"insufficient data (n={V2_MIN_DAYS - 1})"}


def test_degenerate_series_does_not_crash():
    flat = [0.0] * 40
    out = relative_stats([0.001] * 40, flat)       # zero benchmark variance
    assert out["status"].startswith("degenerate")
    perfect = relative_stats([0.0001 + 1.2 * m for m in _synthetic(40, 0, 1, 0)[1]],
                             _synthetic(40, 0, 1, 0)[1])
    json.dumps(perfect, allow_nan=False)           # no inf/NaN reaches data.json
    assert perfect["beta"] == pytest.approx(1.2, abs=1e-9)


# ── v2-era anchoring ──────────────────────────────────────────────────────────

def test_era_starts_at_first_v2_book_not_clock_selftest():
    # 2026-10-12 04:42Z: a v2 clock self-test ran (market closed, v1 book still held).
    # That must NOT start the v2 era — the v2 book is born at the first rebalance.
    data = {
        "runs": [
            {"ts": "2026-10-12T04:42:49+00:00", "strategy": "v2"},
            {"ts": "2026-10-12T14:05:00+00:00", "strategy": "v2", "v2_rebalanced": True},
        ],
        "trades": [], "open_trades": [],
    }
    assert v2_era_start(data) == "2026-10-12T14:05:00+00:00"


def test_era_survives_runs_cap_via_trade_ledger():
    # runs[] is capped at 1000 — the first rebalance run can roll off. The uncapped
    # trades ledger / open_trades still carry the first v2 entry.
    data = {
        "runs": [{"ts": "2027-03-02T14:05:00+00:00", "strategy": "v2",
                  "v2_rebalanced": True}],
        "trades": [{"strategy": "v2", "entry_ts": "2026-10-12T14:06:00+00:00"},
                   {"strategy": "v1", "entry_ts": "2026-05-01T14:06:00+00:00"}],
        "open_trades": [{"entry_ts": "2026-04-01T14:00:00+00:00"}],
    }
    assert v2_era_start(data) == "2026-10-12T14:06:00+00:00"


def test_no_v2_book_yet_is_insufficient():
    out = compute_v2_alpha({"runs": [], "snapshots": []})
    assert out["era_start"] is None
    assert out["status"] == "insufficient data (n=0)"


# ── end-to-end on snapshots ───────────────────────────────────────────────────

def _days(n, start=date(2026, 10, 12)):
    d, out = start, []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _journal(n_days=120, alpha_d=0.0003, beta=1.1):
    fund_r, spy_r = _synthetic(n_days - 1, alpha_d, beta, 0.002, seed=3)
    rng = random.Random(5)
    days = _days(n_days)
    pv, spy, qqq, mtum = 100_000.0, 700.0, 600.0, 250.0
    snaps = [
        # v1-era junk that must be ignored (a -40% "cliff" day before the v2 book).
        {"ts": "2026-10-09T20:00:00+00:00", "portfolio_value": 160_000.0,
         "spy_price": 900.0, "qqq_price": 1.0, "mtum_price": 1.0},
    ]
    for i, d in enumerate(days):
        if i:
            pv *= 1 + fund_r[i - 1]
            spy *= 1 + spy_r[i - 1]
            qqq *= 1 + spy_r[i - 1] * 1.2 + rng.gauss(0, 0.003)
            mtum *= 1 + spy_r[i - 1] * 1.05 + rng.gauss(0, 0.003)
        # an intraday tick snapshot without qqq/mtum, then the closing one with them
        snaps.append({"ts": f"{d}T15:00:00+00:00", "portfolio_value": pv * 0.99,
                      "spy_price": spy * 0.99})
        snaps.append({"ts": f"{d}T20:30:00+00:00", "portfolio_value": pv,
                      "spy_price": spy, "qqq_price": qqq, "mtum_price": mtum})
    data = {
        "snapshots": snaps,
        "runs": [{"ts": "2026-10-12T04:42:00+00:00", "strategy": "v2"},
                 {"ts": "2026-10-12T14:05:00+00:00", "strategy": "v2",
                  "v2_rebalanced": True}],
        "trades": [], "open_trades": [],
    }
    return data, days


def test_compute_v2_alpha_recovers_beta_from_snapshots_and_ignores_v1():
    data, days = _journal()
    out = compute_v2_alpha(data)
    assert out["era_start"] == "2026-10-12T14:05:00+00:00"
    spy = out["vs"]["SPY"]
    assert spy["n"] == len(days) - 1     # one return per v2 day; v1 snapshot excluded
    assert spy["beta"] == pytest.approx(1.1, abs=0.05)
    lo, hi = spy["alpha_ci95_pct"]
    assert lo < 0.0003 * 252 * 100 < hi
    for k in ("QQQ", "MTUM"):
        assert out["vs"][k]["n"] == len(days) - 1
        assert out["vs"][k]["information_ratio"] is not None
    assert out["fund_return_pct"] is not None
    json.dumps(out, allow_nan=False)


def test_compute_v2_alpha_early_days_are_insufficient():
    data, _ = _journal(n_days=8)
    out = compute_v2_alpha(data)
    assert out["vs"]["SPY"]["status"] == "insufficient data (n=7)"
    assert out["status"] == "insufficient data (n=7)"


def test_missing_benchmark_key_degrades_to_insufficient():
    data, _ = _journal(n_days=40)
    for s in data["snapshots"]:
        s.pop("mtum_price", None)
    out = compute_v2_alpha(data)
    assert out["vs"]["MTUM"]["status"] == "insufficient data (n=0)"
    assert "beta" in out["vs"]["SPY"]


def test_garbage_snapshot_fields_never_crash():
    data, _ = _journal(n_days=30)
    data["snapshots"] += [{"ts": None}, {"ts": "2027-01-01T20:00:00+00:00",
                                         "portfolio_value": "abc", "spy_price": None},
                          {"portfolio_value": 1}]
    compute_v2_alpha(data)


def test_ew_universe_levels_pair_with_fund():
    data, days = _journal(n_days=30)
    closes = {
        "AAA": {str(d): 100.0 * (1.01 ** i) for i, d in enumerate(days)},
        "BBB": {str(d): 50.0 for d in days},
    }
    levels = ew_buy_hold_levels(closes, str(days[0]))
    out = compute_v2_alpha(data, ew_levels=levels)
    assert out["vs"]["EW_UNIVERSE"]["n"] == len(days) - 1


# ── equal-weight buy-and-hold of the universe ─────────────────────────────────

def test_ew_buy_hold_levels():
    closes = {
        "UP":   {"2026-10-12": 10.0, "2026-10-13": 20.0, "2026-10-14": 20.0},
        "FLAT": {"2026-10-12": 5.0,  "2026-10-14": 5.0},          # missing 10-13 → ffill
        "LATE": {"2026-10-13": 7.0, "2026-10-14": 70.0},          # no base close → dropped
        "BAD":  {"2026-10-12": 0.0, "2026-10-13": 1.0},           # non-positive base
    }
    lv = ew_buy_hold_levels(closes, "2026-10-12")
    assert lv == {"2026-10-12": 1.0, "2026-10-13": 1.5, "2026-10-14": 1.5}


def test_ew_buy_hold_levels_empty():
    assert ew_buy_hold_levels({}, "2026-10-12") == {}


# ── broker boundary + publish never crash a run ───────────────────────────────

def _client_with(data_client):
    from core.alpaca_client import AlpacaClient
    c = object.__new__(AlpacaClient)
    c.data = data_client
    return c


def test_get_daily_closes_tolerates_none_and_missing():
    from datetime import datetime, timezone
    ts = datetime(2026, 10, 12, 4, tzinfo=timezone.utc)
    bars = SimpleNamespace(data={
        "AAA": [SimpleNamespace(timestamp=ts, close="101.5"),
                SimpleNamespace(timestamp=ts + timedelta(days=1), close=None)],
        "BBB": [],
    })
    c = _client_with(SimpleNamespace(get_stock_bars=lambda req: bars))
    out = c.get_daily_closes(["AAA", "BBB", "CCC"], "2026-10-12")
    assert out == {"AAA": {"2026-10-12": 101.5}}


def test_get_daily_closes_returns_empty_on_failure():
    def boom(req):
        raise ValueError("schema drift")
    c = _client_with(SimpleNamespace(get_stock_bars=boom))
    assert c.get_daily_closes(["AAA"], "2026-10-12") == {}


def test_publish_writes_dict_and_survives_broker_failure(tmp_journal):
    data, _ = _journal(n_days=25)
    tmp_journal.write_text(json.dumps(data))

    class _Broken:
        def get_daily_closes(self, symbols, start):
            raise RuntimeError("alpaca down")

    benchmark.publish_v2_benchmark(_Broken())       # must not raise
    saved = json.loads(tmp_journal.read_text())
    assert isinstance(saved["v2_benchmark"], dict)  # dict, overwritten — not a growing array
    assert "beta" in saved["v2_benchmark"]["vs"]["SPY"]
    assert saved["v2_benchmark"]["vs"]["EW_UNIVERSE"]["status"].startswith("insufficient")


def test_log_snapshot_stores_mtum(tmp_journal):
    from core.journal import log_snapshot
    log_snapshot(100_000.0, 5_000.0, 700.0, qqq_price=600.0, mtum_price=250.0)
    log_snapshot(100_100.0, 5_000.0, 701.0)
    snaps = json.loads(tmp_journal.read_text())["snapshots"]
    assert snaps[0]["mtum_price"] == 250.0
    assert "mtum_price" not in snaps[1]


def test_snapshot_v2_records_mtum_and_benchmark_failure_is_non_fatal(tmp_journal, monkeypatch):
    import v2_main

    class _Alpaca:
        def get_latest_price(self, sym):
            return {"SPY": 700.0, "QQQ": 600.0, "MTUM": 250.0}[sym]

    def boom(alpaca):
        raise RuntimeError("stats bug")
    monkeypatch.setattr(v2_main.benchmark, "publish_v2_benchmark", boom)
    v2_main._snapshot_v2(_Alpaca(), None, {"portfolio_value": 100_000.0, "cash": 1.0})
    snap = json.loads(tmp_journal.read_text())["snapshots"][-1]
    assert snap["mtum_price"] == 250.0 and snap["qqq_price"] == 600.0
