"""
agents/sentiment_v2.py — v2 LLM sentiment layer (REPOSITIONED role).

The v1 debate-to-conviction pipeline (agents/sentiment.py) is retired: conviction
proved uncalibrated (c6 avg -1.43%, c7 ~-2.5% ex-artifact), so it no longer picks
stocks and no longer sizes positions.

The evidence-backed role for LLMs (Lopez-Lira & Tang: daily news-drift horizon)
is as a SENTIMENT FEATURE feeding the deterministic engine: each day, score
news/earnings sentiment per held symbol in [-1, 1]; the v2 sizer may tilt
weights by a small, bounded amount.

STATUS: disabled until the ablation gate passes. The gate: on the Phase-1
backtest, sentiment-tilted weights must add Sharpe over the deterministic core
alone. Until then get_sentiment_scores() returns neutral without any API calls.
Set config.SENTIMENT_ENABLED=True only after the gate passes.

Interface (stable — the sizer codes against this, not an implementation):
    get_sentiment_scores(symbols) -> dict[str, float]  # [-1, 1], 0.0 = neutral
"""
import logging

import config

logger = logging.getLogger(__name__)

# Bound on the sentiment tilt when enabled: weights move at most this fraction
# toward/away from a name. Small by design — sentiment is a feature, not a picker.
MAX_TILT_PCT = 0.20


def get_sentiment_scores(symbols: list[str]) -> dict[str, float]:
    """Daily news/earnings sentiment per symbol in [-1, 1]. Neutral when disabled."""
    if not getattr(config, "SENTIMENT_ENABLED", False):
        return {}
    return _score_with_llm(symbols)


def _score_with_llm(symbols: list[str]) -> dict[str, float]:
    """LLM sentiment scorer. Not implemented until the ablation gate passes.

    Intended design (do not build until gated): fetch last-24h headlines per
    symbol via the existing news fetcher, batch-score with the LLM for
    directional sentiment, cache per (symbol, date), clamp to [-1, 1].
    """
    logger.warning("sentiment_v2: SENTIMENT_ENABLED=True but the LLM scorer is not "
                   "implemented (ablation gate not passed) — returning neutral")
    return {}


def apply_tilt(weights: dict[str, float],
               scores: dict[str, float]) -> dict[str, float]:
    """Tilt equal weights by sentiment, bounded by MAX_TILT_PCT. Renormalized.

    Pure function — unit-testable without any LLM. Not used while disabled.
    """
    if not scores:
        return dict(weights)
    tilted = {}
    for s, w in weights.items():
        sc = max(-1.0, min(1.0, scores.get(s, 0.0)))
        tilted[s] = w * (1.0 + MAX_TILT_PCT * sc)
    total = sum(tilted.values())
    if total <= 0:
        return dict(weights)
    return {s: w / total for s, w in tilted.items()}
