"""
agents/fundamental.py
Fetches key fundamental metrics via yfinance and asks Claude to
interpret them relative to sector context.

Output schema:
{
  "symbol": "NVDA",
  "signal": "bullish" | "bearish" | "neutral",
  "strength": 1-10,
  "rationale": "2 sentences max",
  "metrics": { "pe_ttm": 35.2, ... }
}
"""

import json
import logging
import anthropic
import yfinance as yf

import config
from core.llm_json import complete_json

logger = logging.getLogger(__name__)
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _get_metrics(symbol: str) -> dict:
    """Pull fundamental metrics from yfinance."""
    try:
        info = yf.Ticker(symbol).info
        return {
            "pe_ttm":          info.get("trailingPE"),
            "pe_forward":      info.get("forwardPE"),
            "peg_ratio":       info.get("pegRatio"),
            "eps_growth":      info.get("earningsGrowth"),
            "revenue_growth":  info.get("revenueGrowth"),
            "profit_margin":   info.get("profitMargins"),
            "debt_equity":     info.get("debtToEquity"),
            "free_cashflow":   info.get("freeCashflow"),
            "return_on_equity":info.get("returnOnEquity"),
            "sector":          info.get("sector", "Unknown"),
            "market_cap":      info.get("marketCap"),
            "52w_high":        info.get("fiftyTwoWeekHigh"),
            "52w_low":         info.get("fiftyTwoWeekLow"),
            "analyst_target":  info.get("targetMeanPrice"),
            "recommendation":  info.get("recommendationKey"),
        }
    except Exception as e:
        logger.warning("Metrics fetch failed %s: %s", symbol, e)
        return {}


def _fmt(val, prefix="", suffix="", pct=False, decimals=1) -> str:
    """Format a metric value for the prompt, handling None cleanly."""
    if val is None:
        return "N/A"
    if pct:
        return f"{round(val * 100, decimals)}%"
    return f"{prefix}{round(val, decimals)}{suffix}"


def analyse(symbol: str) -> dict:
    """
    Run fundamental analysis and return structured JSON.
    """
    metrics = _get_metrics(symbol)
    if not metrics:
        return _fallback(symbol, "metrics_unavailable")

    price     = metrics.get("52w_low")
    high_52w  = metrics.get("52w_high")
    low_52w   = metrics.get("52w_low")

    # Distance from 52w high/low
    pct_from_high = None
    pct_from_low  = None
    if high_52w and low_52w:
        try:
            ticker_info  = yf.Ticker(symbol).fast_info
            current      = float(ticker_info.last_price)
            pct_from_high = round((current - high_52w) / high_52w * 100, 1)
            pct_from_low  = round((current - low_52w)  / low_52w  * 100, 1)
        except Exception:
            pass

    prompt = f"""You are a fundamental analyst at a hedge fund. Evaluate {symbol} and return JSON only.

FUNDAMENTALS:
- Sector: {metrics.get('sector')}
- P/E (TTM): {_fmt(metrics.get('pe_ttm'))} | Forward P/E: {_fmt(metrics.get('pe_forward'))}
- PEG ratio: {_fmt(metrics.get('peg_ratio'))}
- EPS growth (YoY): {_fmt(metrics.get('eps_growth'), pct=True)}
- Revenue growth (YoY): {_fmt(metrics.get('revenue_growth'), pct=True)}
- Profit margin: {_fmt(metrics.get('profit_margin'), pct=True)}
- Debt/Equity: {_fmt(metrics.get('debt_equity'))}
- Return on equity: {_fmt(metrics.get('return_on_equity'), pct=True)}
- 52w range: ${_fmt(low_52w)} – ${_fmt(high_52w)} | From high: {_fmt(pct_from_high, suffix='%')} | From low: {_fmt(pct_from_low, suffix='%')}
- Analyst consensus: {metrics.get('recommendation', 'N/A')} | Target: ${_fmt(metrics.get('analyst_target'))}

Assess valuation, growth quality, and financial health. Consider sector norms.

Return ONLY this JSON, no other text:
{{
  "symbol": "{symbol}",
  "signal": "bullish" or "bearish" or "neutral",
  "strength": <1-10 integer>,
  "rationale": "<max 2 sentences focusing on the most important factor>",
  "flags": ["<any red flags, e.g. high debt, slowing growth — empty list if none>"]
}}"""

    try:
        result = complete_json(
            client, model=config.ANALYST_MODEL, max_tokens=config.MAX_TOKENS,
            prompt=prompt, label=f"fundamental:{symbol}",
        )
        result["metrics"] = metrics
        logger.info("Fundamental | %s | signal=%s strength=%s",
                    symbol, result.get("signal"), result.get("strength"))
        return result
    except Exception as e:
        logger.warning("Fundamental agent failed for %s: %s", symbol, e)
        return _fallback(symbol, str(e), metrics)


def _fallback(symbol: str, reason: str, metrics: dict = None) -> dict:
    return {
        "symbol":    symbol,
        "signal":    "neutral",
        "strength":  5,
        "rationale": f"Fundamental analysis unavailable: {reason}",
        "flags":     [],
        "metrics":   metrics or {},
    }
