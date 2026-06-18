"""Tolerant JSON extraction (recommendation #2) + LLM-health check (recommendation #1)."""

import json

import pytest

from core.llm_json import extract_json, complete_json


def test_plain_json():
    assert extract_json('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_markdown_fenced():
    assert extract_json('```json\n{"ok": true}\n```') == {"ok": True}


def test_prose_wrapped():
    assert extract_json('Here is the analysis:\n{"signal": "bullish"}\nThanks!') \
        == {"signal": "bullish"}


def test_trailing_comma():
    assert extract_json('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}


def test_empty_raises():
    with pytest.raises(ValueError):
        extract_json("")


def test_no_object_raises():
    with pytest.raises(ValueError):
        extract_json("no json here at all")


class _FakeResp:
    def __init__(self, text):
        self.content = [type("B", (), {"text": text})()]


class _FakeClient:
    """Returns a scripted sequence of raw response texts across calls."""
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        text = self.texts[min(self.calls, len(self.texts) - 1)]
        self.calls += 1
        return _FakeResp(text)


def test_complete_json_first_try():
    c = _FakeClient(['{"x": 1}'])
    assert complete_json(c, model="m", max_tokens=10, prompt="p") == {"x": 1}
    assert c.calls == 1


def test_complete_json_retries_then_succeeds():
    # first response truncated/garbled, retry returns valid JSON
    c = _FakeClient(['{"x": 1', '{"x": 2}'])
    assert complete_json(c, model="m", max_tokens=10, prompt="p", retries=1) == {"x": 2}
    assert c.calls == 2


def test_complete_json_raises_after_retries():
    c = _FakeClient(['garbage', 'still garbage'])
    with pytest.raises(Exception):
        complete_json(c, model="m", max_tokens=10, prompt="p", retries=1)
    assert c.calls == 2


def test_llm_health_check(tmp_path, monkeypatch):
    import scripts.check_llm_health as h
    # healthy run
    data = {"runs": [{"ts": "2026-06-19T16:00:00", "llm_failures": {"pm_failures": 0, "analyst_fallbacks": 0}}]}
    p = tmp_path / "data.json"
    p.write_text(json.dumps(data))
    monkeypatch.setattr(h, "DATA_PATH", str(p))
    monkeypatch.setattr("sys.argv", ["x", "--quiet"])
    assert h.main() == 0
    # outage run (pm failure)
    data["runs"][0]["llm_failures"] = {"pm_failures": 4, "analyst_fallbacks": 12}
    p.write_text(json.dumps(data))
    assert h.main() == 2
