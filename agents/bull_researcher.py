"""
agents/bull_researcher.py
Round 1: Bull presents opening thesis from analyst reports.
Round 2: Bull responds to bear's rebuttals.
"""

import json
import logging
import config
from agents.performance_context import get_performance_context
from core.llm_json import complete_json, get_client

logger = logging.getLogger(__name__)


def opening_argument(
    symbol: str,
    technical: dict,
    fundamental: dict,
    sentiment: dict,
) -> dict:
    """
    Round 1 — build the opening bull case from analyst reports.
    """
    prompt = f"""You are Zealot, a conviction-driven equity analyst whose reputation rests on finding breakouts before consensus forms. You are making the opening case to BUY {symbol}. Your job is not to be balanced — it is to identify the single most compelling reason this trade works and preempt the bear's strongest objection.

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

    perf_ctx = get_performance_context(lookback=5)
    prompt = prompt.replace(
        "Build the strongest honest bull case.",
        f"{perf_ctx}\nBuild the strongest honest bull case.",
    )

    try:
        result = complete_json(
            get_client(), model=config.DEBATE_MODEL, max_tokens=config.DEBATE_MAX_TOKENS,
            prompt=prompt, label=f"bull_r1:{symbol}",
        )
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

Respond to the bear's strongest points. Classify each bear argument before responding:
  TYPE A — TRADE-SPECIFIC: A near-term catalyst that could cause this stock to fall in
  the next 2-4 weeks (earnings tonight, regulatory decision this week, contract loss
  just announced, confirmed technical breakdown with volume). Lower conviction for
  strong TYPE A arguments — they represent genuine near-term risk.
  TYPE B — SYSTEMIC: Debt ratios, P/E premiums, competitive pressure, macro concerns.
  These apply to every comparable stock and are already priced in by the market.
  Acknowledge them and move on — do NOT lower conviction for TYPE B alone.
Be honest about TYPE A. Hold firm against TYPE B.

MOMENTUM DEFENSE: If the bear's case rests primarily on high RSI (>70), you are permitted to argue momentum continuation — cite the ADX reading and volume ratio as evidence. In trending markets (ADX > 25), RSI staying above 70 for extended periods is normal and bullish. Only concede on RSI if volume is declining or ADX is weakening below 20.

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
        result = complete_json(
            get_client(), model=config.DEBATE_MODEL, max_tokens=config.DEBATE_MAX_TOKENS,
            prompt=prompt, label=f"bull_r2:{symbol}",
        )
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
