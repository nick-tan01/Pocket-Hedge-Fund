"""
Manual Adaptive-OPRO prompt optimizer.

Reads recent debates and trade outcomes, asks Claude for a revised Portfolio
Manager static instruction block, and saves it to prompts/pm_vN.txt for human
review. It never edits live agent code or config.

Run only after there are at least 20 closed trades:
    python scripts/prompt_optimizer.py
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

import anthropic

DATA_PATH = Path("dashboard/data.json")
PROMPTS_DIR = Path("prompts")
MIN_TRADES = 20
LOOKBACK = 20


def load_data() -> dict:
    if not DATA_PATH.exists():
        raise SystemExit(f"Missing {DATA_PATH}")
    with open(DATA_PATH) as f:
        return json.load(f)


def get_regime_tag() -> str:
    try:
        import yfinance as yf
        spy = yf.Ticker("SPY").history(period="1mo")
        ret = (spy["Close"].iloc[-1] - spy["Close"].iloc[0]) / spy["Close"].iloc[0]
        if ret > 0.04:
            return "bullish"
        if ret < -0.04:
            return "bearish"
        return "sideways"
    except Exception:
        return "unknown"


def match_debates_to_trades(data: dict) -> list[dict]:
    debates = {d.get("id"): d for d in data.get("debate_logs", [])}
    matched = []
    for trade in data.get("trades", []):
        debate = debates.get(trade.get("debate_id"))
        if not debate:
            symbol_debates = [
                d for d in data.get("debate_logs", [])
                if d.get("symbol") == trade.get("symbol")
            ]
            debate = symbol_debates[-1] if symbol_debates else None
        if not debate:
            continue
        matched.append({
            "symbol": trade.get("symbol"),
            "entry_ts": trade.get("entry_ts"),
            "exit_ts": trade.get("exit_ts"),
            "pnl_pct": trade.get("pnl_pct"),
            "pnl": trade.get("pnl"),
            "exit_reason": trade.get("exit_reason"),
            "final_conviction": debate.get("final_conviction"),
            "decision": debate.get("decision"),
            "bull_score": debate.get("bull_score"),
            "bear_score": debate.get("bear_score"),
            "bull_case": debate.get("bull_case", "")[:700],
            "bear_case": debate.get("bear_case", "")[:700],
        })
    return matched


def summarize_outcomes(rows: list[dict]) -> str:
    if not rows:
        return "No matched debate/trade outcomes."
    wins = [r for r in rows if (r.get("pnl") or 0) > 0]
    avg_pnl = sum((r.get("pnl_pct") or 0) for r in rows) / len(rows)
    by_exit = {}
    for r in rows:
        by_exit[r.get("exit_reason", "unknown")] = by_exit.get(r.get("exit_reason", "unknown"), 0) + 1
    return (
        f"Trades analyzed: {len(rows)}\n"
        f"Win rate: {len(wins) / len(rows) * 100:.1f}%\n"
        f"Average P&L pct: {avg_pnl:.2f}%\n"
        f"Exit reason counts: {by_exit}"
    )


def current_pm_instruction() -> str:
    path = Path("agents/portfolio_manager.py")
    text = path.read_text()
    match = re.search(r'prompt = f"""(.*?)═══ ANALYST CONSENSUS ═══', text, re.S)
    if not match:
        return "Could not extract current PM instruction block."
    return match.group(1).strip()


def next_prompt_path() -> Path:
    PROMPTS_DIR.mkdir(exist_ok=True)
    versions = []
    for path in PROMPTS_DIR.glob("pm_v*.txt"):
        match = re.search(r"pm_v(\d+)\.txt$", path.name)
        if match:
            versions.append(int(match.group(1)))
    return PROMPTS_DIR / f"pm_v{(max(versions) + 1) if versions else 1}.txt"


def build_optimizer_prompt(rows: list[dict]) -> str:
    recent = rows[-LOOKBACK:]
    regime = get_regime_tag()
    outcome_lines = "\n".join(
        json.dumps(r, default=str) for r in recent
    )
    return f"""You are optimizing the static instruction block for a Portfolio Manager agent.

Current market regime (SPY 1-month): {regime}

Hard constraints:
- Do NOT suggest changing MIN_CONVICTION_SCORE or any config parameters.
- Do NOT remove structured JSON output requirements.
- Do NOT change data slots, variable names, or schema assumptions.
- Optimize only the PM's static behavioral instruction block.
- The output is for human review; do not claim it has been applied.

Current PM instruction block:
{current_pm_instruction()}

Recent performance summary:
{summarize_outcomes(recent)}

Recent matched debate/trade outcomes:
{outcome_lines}

Return ONLY a revised static instruction block for the PM prompt.
Keep it concise, operational, and robust across regimes.
"""


def main():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY is required")

    data = load_data()
    trades = data.get("trades", [])
    if len(trades) < MIN_TRADES:
        raise SystemExit(f"Need at least {MIN_TRADES} closed trades; found {len(trades)}")

    rows = match_debates_to_trades(data)
    if len(rows) < MIN_TRADES:
        raise SystemExit(f"Need at least {MIN_TRADES} matched debate/trade rows; found {len(rows)}")

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1200,
        messages=[{"role": "user", "content": build_optimizer_prompt(rows)}],
    )
    text = response.content[0].text.strip()
    out = next_prompt_path()
    out.write_text(
        f"# PM prompt candidate generated {datetime.utcnow().isoformat()} UTC\n\n{text}\n"
    )
    print(f"Saved prompt candidate for human review: {out}")


if __name__ == "__main__":
    main()
