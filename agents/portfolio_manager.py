"""
agents/portfolio_manager.py
The final decision-maker. Reads the complete 2-round debate transcript
and renders a verdict: buy / skip / watch.

This is the only agent with full context of both sides across both rounds.
It synthesizes a final conviction score and action recommendation.
The risk_manager then uses this verdict for position sizing.

Output schema:
{
  "symbol": "NVDA",
  "action": "buy" | "skip" | "watch",
  "final_conviction": 1-10,
  "verdict": "1-2 sentence synthesis",
  "deciding_factor": "what ultimately tipped the decision",
  "key_risk_to_monitor": "if buying, what to watch for stop review"
}
"""

import json
import logging
import anthropic
import config

logger = logging.getLogger(__name__)
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def decide(
    symbol: str,
    bull_r1: dict,
    bear_r1: dict,
    bull_r2: dict,
    bear_r2: dict,
    technical: dict,
    fundamental: dict,
    sentiment: dict,
) -> dict:
    """
    Read the full debate transcript and render a final verdict.
    """

    prompt = f"""You are the Portfolio Manager for a disciplined hedge fund.
You've observed a full 2-round debate on {symbol}. Render your final verdict.

═══ ANALYST CONSENSUS ═══
Technical:   {technical.get('signal')} ({technical.get('strength')}/10) — {technical.get('rationale')}
Fundamental: {fundamental.get('signal')} ({fundamental.get('strength')}/10) — {fundamental.get('rationale')}
Sentiment:   {sentiment.get('signal')} ({sentiment.get('strength')}/10) — {sentiment.get('rationale')}

═══ ROUND 1 ═══
BULL (conviction {bull_r1.get('conviction')}/10): {bull_r1.get('thesis')}
  Arguments: {" | ".join(bull_r1.get('arguments', []))}
  Catalyst: {bull_r1.get('catalyst')}

BEAR (conviction {bear_r1.get('conviction')}/10): {bear_r1.get('thesis')}
  Primary risk: {bear_r1.get('primary_risk')}

═══ ROUND 2 ═══
BULL updated (conviction {bull_r2.get('conviction')}/10): {bull_r2.get('final_thesis')}
  Conviction change: {bull_r2.get('conviction_change')}
  Bull concedes: {bull_r2.get('concessions', [])}

BEAR updated (conviction {bear_r2.get('conviction')}/10): {bear_r2.get('final_thesis')}
  Conviction change: {bear_r2.get('conviction_change')}
  Unresolved risks: {bear_r2.get('unresolved_risks', [])}
  Bear concedes: {bear_r2.get('concessions', [])}

═══ YOUR DECISION FRAMEWORK ═══
- Minimum conviction to BUY: {config.MIN_CONVICTION_SCORE}/10
- Prefer "skip" over "watch" when there is no clear catalyst
- Use "watch" only when the thesis is sound but timing is wrong (e.g. wait for pullback)
- Weight unresolved risks heavily — a strong unresolved bear risk should lower conviction
- If both sides made meaningful concessions, that is a sign of a nuanced situation

MOMENTUM REGIME RULE:
RSI above 70 is NOT automatically bearish. In a confirmed uptrend (ADX > 25, price above key EMAs), overbought RSI signals momentum continuation, not exhaustion. Apply the "overbought = bearish" interpretation ONLY when: (a) ADX is declining below 20, indicating trend weakness, OR (b) volume is declining as price rises (divergence), OR (c) there are genuine fundamental deterioration signals.
A bear argument that rests SOLELY on elevated RSI — without structural breakdown, volume divergence, or fundamental deterioration — should be discounted significantly in a strong trending market. If ADX > 25 and volume is above average, a BUY with conviction 7-8 is appropriate for fundamentally sound stocks showing momentum continuation.

Return ONLY this JSON:
{{
  "symbol": "{symbol}",
  "action": "buy" or "skip" or "watch",
  "final_conviction": <1-10>,
  "verdict": "<1-2 sentence synthesis of why you made this call>",
  "deciding_factor": "<the single most important thing that tipped your decision>",
  "key_risk_to_monitor": "<if buying: what condition would make you exit early>"
}}"""

    try:
        r = client.messages.create(
            model=config.DEBATE_MODEL,
            max_tokens=config.DEBATE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = r.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        logger.info(
            "PM | %s | action=%s conviction=%s | %s",
            symbol, result.get("action"),
            result.get("final_conviction"),
            result.get("deciding_factor"),
        )
        return result
    except Exception as e:
        logger.warning("Portfolio manager failed %s: %s", symbol, e)
        return _fallback(symbol)


def _fallback(symbol: str) -> dict:
    return {
        "symbol":             symbol,
        "action":             "skip",
        "final_conviction":   1,
        "verdict":            "PM decision unavailable — defaulting to skip",
        "deciding_factor":    "error",
        "key_risk_to_monitor": "",
    }
