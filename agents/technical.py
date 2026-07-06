"""
agents/technical.py
Computes technical indicators in pure pandas, then asks Claude to
interpret them and return a structured JSON signal.

Output schema:
{
  "symbol": "NVDA",
  "signal": "bullish" | "bearish" | "neutral",
  "strength": 1-10,
  "rationale": "2 sentences max",
  "indicators": { "rsi": 66.2, "macd_crossover": true, "bb_pct": 0.82, "trend": "up" }
}
"""

import json
import logging
import pandas as pd

import config
from core.journal import log_tech_shadow
from core.llm_json import complete_json, get_client

logger = logging.getLogger(__name__)


def _deterministic_signal(ind: dict) -> dict:
    """
    C13-TECH: map the already-computed indicators to a signal/strength with a plain
    rule — no LLM. Mirrors the prompt's logic (trend gates RSI interpretation; MACD,
    EMA trend, Bollinger, ADX strength, volume confirmation). Used in shadow mode for
    comparison and, once validated, as the live replacement for the LLM narration call.
    """
    score = 0
    rsi   = ind.get("rsi")
    adx   = ind.get("adx")
    trend = ind.get("trend", "unknown")
    macd  = ind.get("macd", {}) or {}
    bb    = ind.get("bollinger", {}) or {}
    vol   = ind.get("volume_ratio", 1.0) or 1.0
    strong_trend = adx is not None and adx > 25

    if rsi is not None:
        if rsi > 70:   score += 2 if strong_trend else -1   # continuation vs reversal
        elif rsi > 55: score += 1
        elif rsi < 30: score += 1                            # oversold bounce (long-only)
        elif rsi < 45: score -= 1
    if macd.get("crossover"):       score += 2
    elif macd.get("above_signal"):  score += 1
    else:                           score -= 1
    score += {"up": 2, "sideways": 0, "down": -2, "unknown": 0}.get(trend, 0)
    bbp = bb.get("pct_b")
    if bbp is not None:
        if bbp > 0.85:   score += 1 if strong_trend else -1
        elif bbp < 0.15: score += 1
    if strong_trend and score > 0: score += 1
    if vol >= 1.5 and score > 0:   score += 1

    signal   = "bullish" if score >= 3 else "bearish" if score <= -2 else "neutral"
    strength = max(1, min(10, 5 + score))
    return {"signal": signal, "strength": strength, "score": score}


# ── Pure-pandas TA (same as screener, kept local to avoid circular imports) ───

def _rsi(closes: pd.Series, period: int = 14) -> float | None:
    try:
        delta    = closes.diff()
        gain     = delta.clip(lower=0)
        loss     = (-delta).clip(lower=0)
        avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
        rs  = avg_gain / avg_loss.replace(0, float("inf"))
        rsi = 100 - (100 / (1 + rs))
        val = rsi.iloc[-1]
        return round(float(val), 1) if not pd.isna(val) else None
    except Exception:
        return None


def _macd(closes: pd.Series) -> dict:
    try:
        ema12  = closes.ewm(span=12, adjust=False).mean()
        ema26  = closes.ewm(span=26, adjust=False).mean()
        macd   = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        crossover = (
            macd.iloc[-1] > signal.iloc[-1] and
            macd.iloc[-2] <= signal.iloc[-2]
        )
        return {
            "macd_value":    round(float(macd.iloc[-1]), 3),
            "signal_value":  round(float(signal.iloc[-1]), 3),
            "crossover":     crossover,
            "above_signal":  bool(macd.iloc[-1] > signal.iloc[-1]),
        }
    except Exception:
        return {}


def _bollinger(closes: pd.Series, period: int = 20) -> dict:
    try:
        sma   = closes.rolling(period).mean()
        std   = closes.rolling(period).std()
        upper = sma + (2 * std)
        lower = sma - (2 * std)
        width = float(upper.iloc[-1] - lower.iloc[-1])
        pct   = (float(closes.iloc[-1]) - float(lower.iloc[-1])) / width if width else 0.5
        return {
            "upper":   round(float(upper.iloc[-1]), 2),
            "lower":   round(float(lower.iloc[-1]), 2),
            "pct_b":   round(pct, 2),
            "squeeze": width < float(std.rolling(period).mean().iloc[-1]) * 3,
        }
    except Exception:
        return {}


def _trend(closes: pd.Series) -> str:
    """Simple trend: compare 10-day EMA vs 30-day EMA."""
    try:
        ema10 = closes.ewm(span=10, adjust=False).mean().iloc[-1]
        ema30 = closes.ewm(span=30, adjust=False).mean().iloc[-1]
        if ema10 > ema30 * 1.02:
            return "up"
        elif ema10 < ema30 * 0.98:
            return "down"
        return "sideways"
    except Exception:
        return "unknown"


def _adx(bars: list[dict], period: int = 14) -> dict:
    """Average Directional Index — measures trend strength, not direction.
    ADX > 25 = strong trend (RSI overbought here means continuation).
    ADX < 20 = weak/absent trend (RSI overbought here means potential reversal).
    """
    try:
        if len(bars) < period + 2:
            return {}
        plus_dm, minus_dm, tr_list = [], [], []
        for i in range(1, len(bars)):
            high, low   = bars[i]["high"], bars[i]["low"]
            prev_high   = bars[i - 1]["high"]
            prev_low    = bars[i - 1]["low"]
            prev_close  = bars[i - 1]["close"]
            up_move   = high - prev_high
            down_move = prev_low - low
            plus_dm.append(up_move   if (up_move > down_move and up_move > 0)   else 0)
            minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0)
            tr_list.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

        s = pd.Series
        atr_s    = s(tr_list,    dtype=float).ewm(alpha=1/period, min_periods=period).mean()
        plus_s   = s(plus_dm,    dtype=float).ewm(alpha=1/period, min_periods=period).mean()
        minus_s  = s(minus_dm,   dtype=float).ewm(alpha=1/period, min_periods=period).mean()

        plus_di  = 100 * plus_s  / atr_s.replace(0, float("inf"))
        minus_di = 100 * minus_s / atr_s.replace(0, float("inf"))
        dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("inf"))
        adx_val  = float(dx.ewm(alpha=1/period, min_periods=period).mean().iloc[-1])

        return {
            "adx":          round(adx_val, 1),
            "trend_strong": adx_val > 25,
            "+di":          round(float(plus_di.iloc[-1]), 1),
            "-di":          round(float(minus_di.iloc[-1]), 1),
        }
    except Exception:
        return {}


def _support_resistance(bars: list[dict]) -> dict:
    """Identify recent support and resistance levels."""
    try:
        highs  = [b["high"] for b in bars[-20:]]
        lows   = [b["low"]  for b in bars[-20:]]
        return {
            "resistance": round(max(highs), 2),
            "support":    round(min(lows), 2),
        }
    except Exception:
        return {}


# ── Main agent function ───────────────────────────────────────────────────────

def analyse(symbol: str, bars: list[dict]) -> dict:
    """
    Run technical analysis on OHLCV bars and return structured JSON.
    bars: list of dicts with keys date/open/high/low/close/volume, oldest first.
    """
    if len(bars) < 30:
        logger.warning("Not enough bars for %s (%d)", symbol, len(bars))
        return _fallback(symbol, "insufficient_data")

    closes  = pd.Series([b["close"] for b in bars], dtype=float)
    volumes = [b["volume"] for b in bars]

    # Compute all indicators
    rsi      = _rsi(closes)
    macd     = _macd(closes)
    bb       = _bollinger(closes)
    trend    = _trend(closes)
    adx      = _adx(bars)
    sr       = _support_resistance(bars)
    avg_vol  = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else 0
    vol_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol > 0 else 1.0

    indicators = {
        "rsi":           rsi,
        "trend":         trend,
        "macd":          macd,
        "bollinger":     bb,
        "adx":           adx.get("adx"),
        "trend_strong":  adx.get("trend_strong"),
        "volume_ratio":  vol_ratio,
        "price_now":     bars[-1]["close"],
        "price_5d_ago":  bars[-6]["close"] if len(bars) >= 6 else None,
        **sr,
    }

    # C13-TECH: deterministic mapping over the indicators above.
    det  = _deterministic_signal(indicators)
    mode = getattr(config, "TECHNICAL_MODE", "shadow")

    # "deterministic" mode: skip the LLM call entirely and use the rule.
    if mode == "deterministic":
        logger.info("Technical (deterministic) | %s | signal=%s strength=%s",
                    symbol, det["signal"], det["strength"])
        return {
            "symbol":     symbol,
            "signal":     det["signal"],
            "strength":   det["strength"],
            "rationale":  (f"RSI {rsi}, MACD "
                           f"{'cross' if macd.get('crossover') else 'above' if macd.get('above_signal') else 'below'}, "
                           f"trend {trend}, ADX {adx.get('adx')}."),
            "key_levels": {"support": sr.get("support"), "resistance": sr.get("resistance")},
            "indicators": indicators,
        }

    # Build compact prompt — numbers only, no prose data dumps
    prompt = f"""You are a technical analyst. Analyse these indicators for {symbol} and return JSON only.

INDICATORS:
- RSI(14): {rsi}
- ADX(14): {adx.get('adx')} | Trend strong (ADX>25): {adx.get('trend_strong')} | +DI: {adx.get('+di')} | -DI: {adx.get('-di')}
- Trend (EMA10 vs EMA30): {trend}
- MACD crossover today: {macd.get('crossover')} | MACD above signal: {macd.get('above_signal')}
- Bollinger %B: {bb.get('pct_b')} (0=lower band, 1=upper band)
- Volume today vs 20d avg: {vol_ratio}x
- Support: ${sr.get('support')} | Resistance: ${sr.get('resistance')}
- Price now: ${bars[-1]['close']} | 5d ago: ${indicators['price_5d_ago']}

IMPORTANT: RSI > 70 is only bearish when ADX < 20 (weak/absent trend). When ADX > 25, overbought RSI signals momentum continuation in a strong trend — interpret accordingly.

Return ONLY this JSON, no other text:
{{
  "symbol": "{symbol}",
  "signal": "bullish" or "bearish" or "neutral",
  "strength": <1-10 integer>,
  "rationale": "<max 2 sentences>",
  "key_levels": {{"support": <float>, "resistance": <float>}}
}}"""

    try:
        result = complete_json(
            get_client(), model=config.ANALYST_MODEL, max_tokens=config.MAX_TOKENS,
            prompt=prompt, label=f"technical:{symbol}",
        )
        result["indicators"] = indicators
        logger.info("Technical | %s | signal=%s strength=%s",
                    symbol, result.get("signal"), result.get("strength"))
    except Exception as e:
        logger.warning("Technical agent failed for %s: %s", symbol, e)
        result = _fallback(symbol, str(e), indicators)

    # C13-TECH shadow mode: record the LLM-vs-deterministic comparison (no behavior
    # change — we still return the LLM result). Used to decide when it's safe to flip
    # TECHNICAL_MODE to "deterministic".
    if mode == "shadow":
        agree = (result.get("signal") == det["signal"])
        logger.info("TECH_SHADOW | %s | llm=%s/%s det=%s/%s agree=%s",
                    symbol, result.get("signal"), result.get("strength"),
                    det["signal"], det["strength"], agree)
        try:
            log_tech_shadow(symbol, result.get("signal"), result.get("strength"),
                            det["signal"], det["strength"], agree)
        except Exception as exc:
            logger.debug("tech shadow log failed for %s: %s", symbol, exc)
    return result


def _fallback(symbol: str, reason: str, indicators: dict = None) -> dict:
    return {
        "symbol":     symbol,
        "signal":     "neutral",
        "strength":   5,
        "rationale":  f"Technical analysis unavailable: {reason}",
        "indicators": indicators or {},
    }
