"""EXP-014: the performance-context injection must be fully disableable by one flag.

It is injected into the bull, the bear AND the PM, so the flag has to work at the shared
choke point (get_performance_context) or the doom loop survives in whichever agent was
missed.
"""

import config
from agents import performance_context
from agents.performance_context import get_performance_context


def test_flag_off_returns_empty_for_every_consumer(tmp_journal, monkeypatch):
    monkeypatch.setattr(config, "PERFORMANCE_CONTEXT_ENABLED", False)
    # Same call the bull/bear (lookback=5) and the PM (default) each make.
    assert get_performance_context() == ""
    assert get_performance_context(lookback=5) == ""


def test_flag_on_still_injects(tmp_journal, monkeypatch):
    # Rollback path must keep working: with the flag on we get a real block back.
    monkeypatch.setattr(config, "PERFORMANCE_CONTEXT_ENABLED", True)
    monkeypatch.setattr(performance_context, "_get_regime_tag", lambda: "neutral")
    out = get_performance_context()
    assert isinstance(out, str) and out.strip() != ""


def test_flag_off_short_circuits_before_touching_the_journal(monkeypatch):
    # No tmp_journal fixture here: if the flag were checked after the journal read,
    # this would hit the real dashboard/data.json. It must return before that.
    monkeypatch.setattr(config, "PERFORMANCE_CONTEXT_ENABLED", False)

    def _boom():
        raise AssertionError("journal must not be read when the injection is disabled")

    monkeypatch.setattr(performance_context, "get_all_trades", _boom)
    assert get_performance_context() == ""
