"""EXP-016 (2026-10-05 review §2.1D): watch/skip names were re-debated 20-29x in ~24 days
(AMD 29, SMCI 25, COP 23, DDOG 18) — optional stopping on a noisy PM. Max 1 debate per
symbol per WATCH_COOLDOWN_DAYS trading days unless a material trigger fired."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import config
import main
from core import journal


def cand(symbol="AMD", price=100.0, headline="AMD beats", earnings="2026-10-28", score=0.5):
    return SimpleNamespace(
        symbol=symbol, price=price, composite_score=score,
        signals={"top_headline": headline, "earnings_date": earnings},
    )


def ctx(price=100.0, headline="AMD beats", earnings="2026-10-28"):
    return {"price": price, "top_headline": headline, "earnings_date": earnings}


def seed(tmp_journal, symbol="AMD", decision="watch", days_ago=1, context=None):
    """One debate in the journal, `days_ago` calendar days back."""
    journal.log_debate(symbol, "bull", "bear", 6, 5, 6, decision, context=context)
    data = json.loads(tmp_journal.read_text())
    data["debate_logs"][-1]["ts"] = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    tmp_journal.write_text(json.dumps(data))


@pytest.fixture(autouse=True)
def cooldown_on(monkeypatch):
    monkeypatch.setattr(config, "WATCH_COOLDOWN_DAYS", 5)
    monkeypatch.setattr(config, "WATCH_COOLDOWN_MOVE_PCT", 0.05)


def test_second_debate_in_window_is_blocked_and_journaled(tmp_journal):
    seed(tmp_journal, context=ctx())
    out = main._watch_cooldown_filter([cand()])
    assert out == []
    rd = json.loads(tmp_journal.read_text())["risk_decisions"][-1]
    assert rd["action"] == "gated" and rd["symbol"] == "AMD"
    assert rd["reason"].startswith("watch_cooldown")


def test_same_day_redebate_is_blocked(tmp_journal):
    seed(tmp_journal, days_ago=0, context=ctx())
    assert main._watch_cooldown_filter([cand()]) == []


@pytest.mark.parametrize("fresh,why", [
    (dict(price=105.5), "move"),                     # +5.5% since the last debate
    (dict(price=94.0), "move"),                      # -6%
    (dict(headline="AMD wins OpenAI deal"), "news"),
    (dict(earnings="2027-01-27"), "earnings"),       # earnings date rolled = earnings happened
])
def test_material_trigger_bypasses_cooldown(tmp_journal, fresh, why):
    seed(tmp_journal, context=ctx())
    c = cand(**fresh)
    assert main._material_trigger(ctx(), c) == why
    assert main._watch_cooldown_filter([c]) == [c]


def test_sub_threshold_move_does_not_bypass(tmp_journal):
    seed(tmp_journal, context=ctx())
    assert main._watch_cooldown_filter([cand(price=103.0)]) == []


def test_window_expiry_allows_redebate(tmp_journal):
    seed(tmp_journal, days_ago=14, context=ctx())          # 10 weekdays > 5
    c = cand()
    assert main._watch_cooldown_filter([c]) == [c]


def test_prior_buy_that_never_executed_is_not_cooled(tmp_journal):
    seed(tmp_journal, decision="buy", context=ctx())       # lost fill, not a repeat
    c = cand()
    assert main._watch_cooldown_filter([c]) == [c]


def test_no_history_and_disabled_pass_through(tmp_journal, monkeypatch):
    c = cand()
    assert main._watch_cooldown_filter([c]) == [c]
    seed(tmp_journal, context=ctx())
    monkeypatch.setattr(config, "WATCH_COOLDOWN_DAYS", 0)
    assert main._watch_cooldown_filter([c]) == [c]


def test_legacy_debate_without_context_is_cooled(tmp_journal):
    seed(tmp_journal, context=None)                        # pre-EXP-016 record
    assert main._watch_cooldown_filter([cand()]) == []


def test_corrupt_journal_fails_open(tmp_journal):
    tmp_journal.write_text("{not json")
    c = cand()
    assert main._watch_cooldown_filter([c]) == [c]


def test_only_the_latest_debate_counts(tmp_journal):
    seed(tmp_journal, days_ago=14, context=ctx())
    seed(tmp_journal, days_ago=1, context=ctx())
    assert main._watch_cooldown_filter([cand()]) == []


def test_trading_days_between_skips_weekends():
    from datetime import date
    assert main._trading_days_between(date(2026, 10, 2), date(2026, 10, 5)) == 1   # Fri -> Mon
    assert main._trading_days_between(date(2026, 10, 5), date(2026, 10, 12)) == 5  # Mon -> Mon


def test_debate_context_round_trips_through_journal(tmp_journal):
    c = cand(price=123.4, headline="h", earnings="2026-11-01")
    journal.log_debate("AMD", "b", "b", 6, 5, 6, "watch", context=main._debate_context(c))
    stored = json.loads(tmp_journal.read_text())["debate_logs"][-1]["context"]
    assert stored == {"price": 123.4, "top_headline": "h", "earnings_date": "2026-11-01"}


def test_cooled_names_do_not_consume_top_n_slots(tmp_journal, monkeypatch):
    """Cooled AMD/SMCI must not starve fresh names of the few candidate slots."""
    seed(tmp_journal, "AMD", context=ctx())
    seed(tmp_journal, "SMCI", context=ctx())
    ranked = [cand("AMD"), cand("SMCI"), cand("NVDA"), cand("MSFT"), cand("CRM")]

    class FakeScreener:
        WATCHLIST = [c.symbol for c in ranked]
        fetcher = None

        def run(self, max_candidates, symbols):
            return [c for c in ranked if c.symbol in symbols][:max_candidates]

    monkeypatch.setattr(main, "_active_watchlist_entries", lambda: [])
    out = main._select_candidates(FakeScreener(), dynamic_max=3, trigger_reason="scheduled")
    assert [c.symbol for c in out] == ["NVDA", "MSFT", "CRM"]
