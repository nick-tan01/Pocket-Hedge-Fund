"""
agents/bear_researcher.py
Round 1: Bear presents opening counter-case after reading bull's argument.
Round 2: Bear responds to bull's rebuttal.
"""

import json
import logging
import anthropic
import config
from agents.performance_context import get_performance_context

logger = logging.getLogger(__name__)
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def opening_argument(
    symbol: str,
    technical: dict,
    fundamental: dict,
    sentiment: dict,
    bull_case: dict,
) -> dict:
    """
    Round 1 — bear presents counter-case, directly rebutting the bull opening.
    """
    bull_args = "\n".join(f"  - {a}" for a in bull_case.get("arguments", []))

    prompt = f"""You are Reaper, a risk-focused analyst whose job is to protect the fund from trades that sound compelling but quietly destroy capital. You are making the case AGAINST buying {symbol}. Find the fatal flaw the bull glossed over — the risk they called 'manageable' that actually isn't.

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

BULL'S OPENING CASE (conviction={bull_case.get('conviction')}/10):
Thesis: {bull_case.get('thesis')}
Arguments:
{bull_args}
Catalyst: {bull_case.get('catalyst')}

Build the strongest honest bear case. Directly rebut the bull's arguments where weak.
Identify risks they ignored. If evidence genuinely favours the bull, conviction should be low (1-4).
Do NOT fabricate data not in the reports above.

Return ONLY this JSON:
{{
  "symbol": "{symbol}",
  "stance": "bearish",
  "conviction": <1-10>,
  "thesis": "<1 sentence core bear argument>",
  "arguments": [
    "<bear argument 1 with specific data>",
    "<bear argument 2>",
    "<bear argument 3>"
  ],
  "bull_rebuttals": [
    "<rebuttal to bull argument 1>",
    "<rebuttal to bull argument 2>",
    "<rebuttal to bull argument 3>"
  ],
  "primary_risk": "<single biggest downside risk if we enter this trade>"
}}"""

    perf_ctx = get_performance_context(lookback=5)
    prompt = prompt.replace(
        "Build the strongest honest bear case.",
        f"{perf_ctx}\nBuild the strongest honest bear case.",
    )

    try:
        r = client.messages.create(
            model=config.DEBATE_MODEL,
            max_tokens=config.DEBATE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = _clean(r.content[0].text)
        result = json.loads(raw)
        logger.info("Bear R1 | %s | conviction=%s | %s",
                    symbol, result.get("conviction"), result.get("thesis"))
        return result
    except Exception as e:
        logger.warning("Bear R1 failed %s: %s", symbol, e)
        return _fallback(symbol)


def rebuttal(
    symbol: str,
    opening: dict,
    bull_r2: dict,
) -> dict:
    """
    Round 2 — bear responds to bull's rebuttal of the bear case.
    """
    bull_concessions = "\n".join(f"  - {c}" for c in bull_r2.get("concessions", []))

    prompt = f"""You are the bearish analyst for {symbol}. The bull has responded to your opening case.

YOUR OPENING THESIS: {opening.get('thesis')}
YOUR OPENING CONVICTION: {opening.get('conviction')}/10
YOUR PRIMARY RISK: {opening.get('primary_risk')}

BULL'S RESPONSE:
Updated thesis: {bull_r2.get('final_thesis')}
Updated conviction: {bull_r2.get('conviction')}/10
Their rebuttal to your strongest point: {bull_r2.get('rebuttal_to_strongest')}
Concessions the bull made: 
{bull_concessions if bull_concessions else "  (none)"}

After hearing the bull's rebuttal, give your final bear conviction.
Has the bull addressed your primary risk adequately? Where do you still disagree?
Be intellectually honest — if the bull has genuinely addressed your concerns, lower your conviction.

Return ONLY this JSON:
{{
  "symbol": "{symbol}",
  "stance": "bearish",
  "conviction": <1-10, updated after hearing bull rebuttal>,
  "conviction_change": "<increased/decreased/unchanged and why in 1 sentence>",
  "unresolved_risks": ["<risk the bull did NOT adequately address>"],
  "final_thesis": "<updated 1-sentence bear thesis after full debate>",
  "concessions": ["<any bull point you now accept, empty list if none>"]
}}"""

    try:
        r = client.messages.create(
            model=config.DEBATE_MODEL,
            max_tokens=config.DEBATE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = _clean(r.content[0].text)
        result = json.loads(raw)
        logger.info("Bear R2 | %s | updated conviction=%s | %s",
                    symbol, result.get("conviction"), result.get("conviction_change"))
        return result
    except Exception as e:
        logger.warning("Bear R2 failed %s: %s", symbol, e)
        return {
            "symbol": symbol, "stance": "bearish",
            "conviction": opening.get("conviction", 5),
            "conviction_change": "unchanged (rebuttal failed)",
            "unresolved_risks": [opening.get("primary_risk", "")],
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
        "symbol": symbol, "stance": "bearish", "conviction": 1,
        "thesis": "Bear case unavailable", "arguments": [],
        "bull_rebuttals": [], "primary_risk": "unknown",
    }
