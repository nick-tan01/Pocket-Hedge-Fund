"""Pure-logic tests for core.alpaca_client helpers (no network / no API keys)."""

from types import SimpleNamespace

from core.alpaca_client import _account_dict


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
