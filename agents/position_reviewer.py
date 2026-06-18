"""
agents/position_reviewer.py
Reviews each open position against its original thesis using fresh current data.
Outputs a thesis-driven hold / trim / exit decision.

Output schema:
{
  "symbol": "...",
  "action": "hold" | "trim" | "exit",
  "conviction": 1-10,
  "thesis_status": "intact" | "weakened" | "broken",
  "rationale": "1-2 sentences",
  "original_thesis_check": "whether the reason we bought is still true",
  "key_risk_to_monitor": "...",
  "trim_reason": "only present if action=trim",
  "exit_reason": "only present if action=exit"
}
"""

import json
import logging
import anthropic
import config
from core.llm_json import complete_json

logger = logging.getLogger(__name__)
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def review(
    symbol: str,
    trade: dict,
    original_debate: dict | None,
    tech: dict,
    fund: dict,
    sent: dict,
    bull_r1: dict,
    bear_r1: dict,
    bull_r2: dict,
    bear_r2: dict,
    current_price: float,
    unrealized_plpc: float | None,
) -> dict:
    """
    Compare current evidence against original thesis and decide hold/trim/exit.
    Decision is thesis-driven; P&L is included as context only.
    """
    entry_price   = trade.get("entry_price", 0)
    stop_price    = trade.get("stop_price", 0)
    orig_conv     = trade.get("conviction", "unknown")
    orig_key_risk = trade.get("key_risk", "not recorded")

    orig_thesis = "not recorded"
    orig_debate_summary = "no original debate stored"
    if original_debate:
        orig_thesis = original_debate.get("bull_case", "not recorded")
        orig_debate_summary = (
            f"Bull case: {original_debate.get('bull_case', '')} | "
            f"Bear case: {original_debate.get('bear_case', '')} | "
            f"Final conviction: {original_debate.get('final_conviction', '?')}/10"
        )

    pnl_context = (
        f"{unrealized_plpc * 100:.1f}%" if unrealized_plpc is not None else "unavailable"
    )

    unresolved = bear_r2.get("unresolved_risks", [])

    prompt = f"""You are the Position Review Officer for a disciplined hedge fund.
You are reviewing an existing open position in {symbol}.

CRITICAL INSTRUCTION: Your decision MUST be thesis-driven, NOT profit-target-driven.
Do NOT recommend exit or trim merely because the position is up or down a certain percentage.
The ONLY valid exit criterion is: the original reason we bought this stock is no longer true.
The ONLY valid trim criterion is: the thesis has materially weakened but is not fully broken.

═══ ORIGINAL TRADE ═══
Entry price:         ${entry_price}
Hard stop:           ${stop_price}
Original conviction: {orig_conv}/10
Original key risk:   {orig_key_risk}
Original thesis:     {orig_thesis[:400]}
Original debate:     {orig_debate_summary[:500]}

═══ CURRENT P&L (informational only — NOT a decision input) ═══
Current price:   ${current_price}
Unrealized P&L:  {pnl_context}

═══ CURRENT ANALYST SIGNALS (fresh today) ═══
Technical:   {tech.get('signal')} ({tech.get('strength')}/10) — {tech.get('rationale')}
Fundamental: {fund.get('signal')} ({fund.get('strength')}/10) — {fund.get('rationale')}
Sentiment:   {sent.get('signal')} ({sent.get('strength')}/10) — {sent.get('rationale')}

═══ CURRENT DEBATE ═══
R1 Bull ({bull_r1.get('conviction')}/10): {bull_r1.get('thesis')}
R1 Bear ({bear_r1.get('conviction')}/10): {bear_r1.get('thesis')}
  Bear primary risk: {bear_r1.get('primary_risk')}

R2 Bull ({bull_r2.get('conviction')}/10): {bull_r2.get('final_thesis')}
  Bull concessions: {bull_r2.get('concessions', [])}

R2 Bear ({bear_r2.get('conviction')}/10): {bear_r2.get('final_thesis')}
  Unresolved risks: {unresolved}
  Bear concessions: {bear_r2.get('concessions', [])}

═══ DECISION FRAMEWORK ═══
HOLD  — thesis_status=intact:   Original thesis still holds; evidence still supports the position.
TRIM  — thesis_status=weakened: Thesis has materially softened but core case survives; reduce size.
EXIT  — thesis_status=broken:   The specific reason we entered this trade is no longer valid.

Key question to answer: "Is the specific catalyst or thesis that drove our original buy decision still intact?"

Return ONLY this JSON:
{{
  "symbol": "{symbol}",
  "action": "hold" or "trim" or "exit",
  "conviction": <1-10, your updated conviction in the remaining thesis>,
  "thesis_status": "intact" or "weakened" or "broken",
  "rationale": "<1-2 sentences explaining your decision>",
  "original_thesis_check": "<state whether the specific reason we bought is still true>",
  "key_risk_to_monitor": "<the most important thing to watch going forward>",
  "trim_reason": "<only include this key if action=trim>",
  "exit_reason": "<only include this key if action=exit>"
}}"""

    try:
        result = complete_json(
            client, model=config.DEBATE_MODEL, max_tokens=config.DEBATE_MAX_TOKENS,
            prompt=prompt, label=f"reviewer:{symbol}",
        )
        logger.info(
            "Reviewer | %s | action=%s conviction=%d thesis=%s | %s",
            symbol,
            result.get("action"),
            result.get("conviction", 0),
            result.get("thesis_status"),
            result.get("rationale", "")[:80],
        )
        return result
    except Exception as e:
        logger.warning("Position reviewer failed %s: %s", symbol, e)
        return _fallback(symbol)


def _clean(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.startswith("json"):
            t = t[4:]
    return t.strip()


def _fallback(symbol: str) -> dict:
    return {
        "symbol":               symbol,
        "action":               "hold",
        "conviction":           5,
        "thesis_status":        "intact",
        "rationale":            "Review agent unavailable — defaulting to hold",
        "original_thesis_check": "could not assess",
        "key_risk_to_monitor":  "manual review recommended",
    }
