"""
core/signal_v2.py — v2 deterministic signal engine (residual momentum).

The Phase-1 backtest (scripts/backtest_v2.py) validated this signal:
  residual (T*alpha from a market-model regression, 252d formation skip 21d),
  top-25 equal-weight, monthly rebalance ->
  +35.96% CAGR 2023-01->2026-09 (+14.0pp vs SPY, +3.6pp vs QQQ, maxDD -24.9%),
  OOS 2025-01->2026-09: +37.77% (+20.0pp/+13.4pp).
52-week-high was also tested and FAILED everywhere (-6 to -8pp vs SPY) — not used.

Sizing: flat equal-weight. The vol-targeted variant was backtested and
underperformed by ~8pp/yr (1/vol weighting fights the signal: the highest-alpha
names tend to be high-vol). Conviction plays no role — dead by operator order.

All functions are pure w.r.t. the passed fetcher and never raise on bad data:
a symbol that cannot be scored is skipped, not crashed on (Principle: never let
external payloads crash a run).
"""
import logging
import math

import numpy as np

import config

logger = logging.getLogger(__name__)

LOOKBACK = 252
SKIP = 21
VOL_WINDOW = 63
FETCH_DAYS = 320  # lookback + skip + buffer for weekends/holidays


def _bars_to_arrays(bars: list[dict]):
    """Split bar dicts into (closes, highs, dollar_vols) numpy arrays, oldest first."""
    closes = np.array([float(b["close"]) for b in bars], dtype=float)
    highs = np.array([float(b["high"]) for b in bars], dtype=float)
    vols = np.array([float(b.get("volume") or 0) for b in bars], dtype=float)
    return closes, highs, vols


def residual_score(closes: np.ndarray, mkt_closes: np.ndarray,
                   lookback: int = LOOKBACK, skip: int = SKIP) -> float:
    """T*alpha: idiosyncratic drift over `lookback` log-return days, skipping `skip`.

    Rank on economic drift, NOT sum-of-residuals (which is identically ~0 when the
    regression is fit on the formation window — see tests/test_backtest_v2.py).
    Returns nan when there is insufficient clean data.
    """
    n = lookback + skip
    if len(closes) < n or len(mkt_closes) < n:
        return float("nan")
    c = closes[-n:-skip] if skip else closes[-n:]
    m = mkt_closes[-n:-skip] if skip else mkt_closes[-n:]
    if np.any(c <= 0) or np.any(m <= 0) or np.any(~np.isfinite(c)):
        return float("nan")
    r = np.log(c[1:] / c[:-1])
    mm = np.log(m[1:] / m[:-1])
    if len(r) < 126 or np.std(mm) == 0:
        return float("nan")
    A = np.column_stack([np.ones_like(mm), mm])
    try:
        beta = np.linalg.lstsq(A, r, rcond=None)[0]
    except np.linalg.LinAlgError:
        return float("nan")
    return float(np.sum(r) - beta[1] * np.sum(mm))  # == len(r) * alpha


def dollar_volume_ok(closes: np.ndarray, vols: np.ndarray,
                     min_dollar_vol: float) -> bool:
    if len(closes) < VOL_WINDOW:
        return False
    c, v = closes[-VOL_WINDOW:], vols[-VOL_WINDOW:]
    return bool(np.mean(c * v) >= min_dollar_vol)


def compute_scores(fetcher, symbols: list[str],
                   market_symbol: str = "SPY") -> dict[str, float]:
    """Score every symbol's residual momentum. Skips unscorable symbols silently."""
    try:
        mkt_bars = fetcher.get_ohlcv(market_symbol, days=FETCH_DAYS)
    except Exception as e:
        logger.warning("signal_v2: market bars failed (%s) — no scores", e)
        return {}
    if not mkt_bars:
        return {}
    mkt_closes, _, _ = _bars_to_arrays(mkt_bars)
    min_dv = float(getattr(config, "V2_MIN_DOLLAR_VOL", 10_000_000))
    scores: dict[str, float] = {}
    for s in symbols:
        try:
            bars = fetcher.get_ohlcv(s, days=FETCH_DAYS)
        except Exception as e:
            logger.debug("signal_v2: bars failed for %s (%s)", s, e)
            continue
        if not bars or len(bars) < LOOKBACK + SKIP:
            continue
        closes, _, vols = _bars_to_arrays(bars)
        if not dollar_volume_ok(closes, vols, min_dv):
            continue
        sc = residual_score(closes, mkt_closes)
        if math.isnan(sc):
            continue
        scores[s] = sc
    logger.info("signal_v2: scored %d/%d symbols", len(scores), len(symbols))
    return scores


def select_top(scores: dict[str, float], top_k: int) -> list[str]:
    """Top-K by score, descending. Deterministic tiebreak by symbol."""
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [s for s, _ in ranked[:max(0, top_k)]]


def equal_weights(symbols: list[str], gross: float = 1.0) -> dict[str, float]:
    """Flat equal-weight allocation. Conviction-independent by design."""
    if not symbols:
        return {}
    w = gross / len(symbols)
    return {s: w for s in symbols}
