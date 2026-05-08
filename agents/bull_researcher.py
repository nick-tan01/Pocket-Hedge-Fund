"""
agents/bull_researcher.py
Round 1: Bull presents opening thesis from analyst reports.
Round 2: Bull responds to bear's rebuttals.
"""

import json
import logging
import anthropic
import config

logger = logging.getLogger(__name__)
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def opening_argument(
    symbol: str,
    technical: dict,
    fundamental: dict,
    sentiment: dict,
) -> dict:
    """
    Round 1 — build the opening bull case from analyst reports.
    """
    prompt = f"""You are a bullish research analyst making the opening case to BUY {symbol}.

ANALYST REPORTS:
Technical: signal={technical.get('signal')} strength={technical.get('strength')}/10
  → {technical.get('rationale')}
  → RSI: {technical.get('indicators', {}).get('rsi')} | Trend: {technical.get('indicators', {}).get('trend')} | Vol ratio: {technical.get('indicators', {}).get('volume_ratio')}x

Fundamental: signal={fundamental.get('signal')} strength={fundamental.get('strength')}/10
  → {fundamental.get('rationale')}
  → Flags: {fundamental.get('flags', [])}

Sentiment: signal={sentiment.get('signal')} strength={sentiment.get('strength')}/10
  → {sentiment.get('rationale')}
  → Themes: {sentiment.get('top_themes', [])}

Build the strongest honest bull case. Reference specific data above.
If evidence is weak, conviction should reflect that (1-4).
Do NOT fabricate data not in the reports above.

Return ONLY this JSON:
{{
  "symbol": "{symbol}",
  "stance": "bullish",
  "conviction": <1-10>,
  "thesis": "<1 sentence core argument>",
  "arguments": [
    "<argument 1 with specific data>",
    "<argument 2 with specific data>",
    "<argument 3 with specific data>"
  ],
  "catalyst": "<primary near-term upside catalyst>",
  "target_timeframe": "days" or "weeks"
}}"""

    try:
        r = client.messages.create(
            model=config.DEBATE_MODEL,
            max_tokens=config.MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = _clean(r.content[0].text)
        result = json.loads(raw)
        logger.info("Bull R1 | %s | conviction=%s | %s",
                    symbol, result.get("conviction"), result.get("thesis"))
        return result
    except Exception as e:
        logger.warning("Bull R1 failed %s: %s", symbol, e)
        return _fallback(symbol)


def rebuttal(
    symbol: str,
    opening: dict,
    bear_case: dict,
) -> dict:
    """
    Round 2 — bull responds to bear's rebuttals.
    Receives its own opening + bear's full argument.
    """
    bear_args = "\n".join(f"  - {a}" for a in bear_case.get("arguments", []))
    bear_rebuttals = "\n".join(f"  - {a}" for a in bear_case.get("bull_rebuttals", []))

    prompt = f"""You are the bullish analyst for {symbol}. The bear has responded to your opening case.

YOUR OPENING THESIS: {opening.get('thesis')}
YOUR OPENING CONVICTION: {opening.get('conviction')}/10

BEAR'S COUNTER-ARGUMENTS:
{bear_args}

BEAR'S REBUTTALS TO YOUR POINTS:
{bear_rebuttals}

BEAR'S PRIMARY RISK IDENTIFIED: {bear_case.get('primary_risk')}

Respond to the bear's strongest points. Where they raise valid concerns, acknowledge them
and adjust your conviction accordingly. Where their arguments are weak or speculative,
explain why. Be intellectually honest — if the bear has changed your mind, say so.

Return ONLY this JSON:
{{
  "symbol": "{symbol}",
  "stance": "bullish",
  "conviction": <1-10, updated after hearing bear>,
  "conviction_change": "<increased/decreased/unchanged and why in 1 sentence>",
  "strongest_bear_point": "<the bear argument you find most compelling>",
  "rebuttal_to_strongest": "<your response to their best point>",
  "final_thesis": "<updated 1-sentence bull thesis after debate>",
  "concessions": ["<any valid bear point you accept, empty list if none>"]
}}"""

    try:
        r = client.messages.create(
            model=config.DEBATE_MODEL,
            max_tokens=config.MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = _clean(r.content[0].text)
        result = json.loads(raw)
        logger.info("Bull R2 | %s | updated conviction=%s | %s",
                    symbol, result.get("conviction"), result.get("conviction_change"))
        return result
    except Exception as e:
        logger.warning("Bull R2 failed %s: %s", symbol, e)
        return {
            "symbol": symbol, "stance": "bullish",
            "conviction": opening.get("conviction", 5),
            "conviction_change": "unchanged (rebuttal failed)",
            "strongest_bear_point": "", "rebuttal_to_strongest": "",
            "final_thesis": opening.get("thesis", ""),
            "concessions": [],
        }


def _clean(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.startswith("json"):
            t = t[4:]
    return t.strip()


def _fallback(symbol: str) -> dict:
    return {
        "symbol": symbol, "stance": "bullish", "conviction": 1,
        "thesis": "Bull case unavailable", "arguments": [],
        "catalyst": "unknown", "target_timeframe": "weeks",
    }
