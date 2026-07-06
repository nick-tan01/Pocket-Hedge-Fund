"""
agents/sentiment.py
Reads recent news headlines via Alpaca News API and asks Claude to
score market sentiment for the symbol.

Output schema:
{
  "symbol": "NVDA",
  "signal": "bullish" | "bearish" | "neutral",
  "strength": 1-10,
  "rationale": "2 sentences max",
  "headline_count": 8,
  "top_themes": ["AI demand", "earnings beat"]
}
"""

import json
import logging

import config
from core.llm_json import complete_json, get_client

logger = logging.getLogger(__name__)


def analyse(symbol: str, news: list[dict]) -> dict:
    """
    Score sentiment from news headlines.
    news: list of dicts with keys headline/source/datetime
    """
    if not news:
        logger.info("Sentiment | %s | no headlines — returning neutral", symbol)
        return {
            "symbol":          symbol,
            "signal":          "neutral",
            "strength":        5,
            "rationale":       "No recent news found for this symbol.",
            "headline_count":  0,
            "top_themes":      [],
        }

    # Format headlines for prompt — titles only, no full articles
    headlines_text = "\n".join(
        f"- [{item['datetime']}] {item['headline']}"
        for item in news[:10]
    )

    prompt = f"""You are a market sentiment analyst. Assess sentiment for {symbol} from these headlines.

RECENT HEADLINES:
{headlines_text}

Evaluate overall tone, materiality of news, and likely short-term price impact.
Ignore noise (analyst upgrades/downgrades without new info, minor price target changes).
Focus on: earnings surprises, product launches, regulatory risk, macro impact, M&A.

Return ONLY this JSON, no other text:
{{
  "symbol": "{symbol}",
  "signal": "bullish" or "bearish" or "neutral",
  "strength": <1-10 integer>,
  "rationale": "<max 2 sentences on the most impactful news>",
  "top_themes": ["<theme1>", "<theme2>"]
}}"""

    try:
        result = complete_json(
            get_client(), model=config.ANALYST_MODEL, max_tokens=config.MAX_TOKENS,
            prompt=prompt, label=f"sentiment:{symbol}",
        )
        result["headline_count"] = len(news)
        logger.info("Sentiment | %s | signal=%s strength=%s",
                    symbol, result.get("signal"), result.get("strength"))
        return result
    except Exception as e:
        logger.warning("Sentiment agent failed for %s: %s", symbol, e)
        return _fallback(symbol, str(e), len(news))


def _fallback(symbol: str, reason: str, count: int = 0) -> dict:
    return {
        "symbol":         symbol,
        "signal":         "neutral",
        "strength":       5,
        "rationale":      f"Sentiment analysis unavailable: {reason}",
        "headline_count": count,
        "top_themes":     [],
    }
