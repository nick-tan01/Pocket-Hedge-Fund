"""
core/journal.py
Writes all trade activity, debate logs, and portfolio snapshots to a single
JSON file that the Vercel dashboard reads. Append-only — never overwrites history.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import config

logger = logging.getLogger(__name__)


def _load() -> dict:
    """Load existing journal or return fresh structure."""
    path = Path(config.JOURNAL_PATH)
    if path.exists() and path.stat().st_size > 0:
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "meta": {
            "created":          datetime.utcnow().isoformat(),
            "benchmark":        config.BENCHMARK_TICKER,
            "starting_capital": config.STARTING_CAPITAL,
        },
        "snapshots":  [],   # portfolio value over time
        "trades":     [],   # completed trades (entry + exit)
        "open_trades":[],   # currently open positions
        "runs":       [],   # each pipeline run summary
        "debate_logs":[],   # full bull/bear transcripts
    }


def _save(data: dict):
    path = Path(config.JOURNAL_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


# ── Public API ────────────────────────────────────────────────────────────────

def log_snapshot(portfolio_value: float, cash: float, spy_price: float):
    """Record a portfolio value snapshot for the equity curve."""
    data = _load()
    data["snapshots"].append({
        "ts":              datetime.utcnow().isoformat(),
        "portfolio_value": round(portfolio_value, 2),
        "cash":            round(cash, 2),
        "spy_price":       round(spy_price, 2),
    })
    _save(data)
    logger.debug("Snapshot logged: $%.2f", portfolio_value)


def log_trade_open(
    symbol: str, side: str, qty: float, entry_price: float,
    stop_price: float, conviction: int, debate_id: str,
    key_risk: str = ""
):
    """Record when a new position is opened."""
    data = _load()
    trade = {
        "id":          f"{symbol}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
        "symbol":      symbol,
        "side":        side,
        "qty":         qty,
        "entry_price": round(entry_price, 2),
        "entry_ts":    datetime.utcnow().isoformat(),
        "stop_price":  round(stop_price, 2),
        "conviction":  conviction,
        "debate_id":   debate_id,
        "status":      "open",
        "key_risk":    key_risk,
    }
    data["open_trades"].append(trade)
    _save(data)
    logger.info("Trade opened: %s %s x%.2f @ $%.2f", side, symbol, qty, entry_price)
    return trade["id"]


def log_trade_close(
    trade_id: str, exit_price: float, exit_reason: str
):
    """Move a trade from open_trades to trades with P&L calculated."""
    data = _load()
    trade = next((t for t in data["open_trades"] if t["id"] == trade_id), None)
    if not trade:
        logger.warning("Could not find open trade id=%s to close", trade_id)
        return

    entry  = trade["entry_price"]
    exit_p = round(exit_price, 2)
    qty    = trade["qty"]
    pnl    = round((exit_p - entry) * qty, 2) if trade["side"] == "buy" else round((entry - exit_p) * qty, 2)
    pnl_pct = round((pnl / (entry * qty)) * 100, 2) if entry * qty > 0 else 0

    trade.update({
        "exit_price":  exit_p,
        "exit_ts":     datetime.utcnow().isoformat(),
        "exit_reason": exit_reason,
        "pnl":         pnl,
        "pnl_pct":     pnl_pct,
        "status":      "closed",
    })

    data["trades"].append(trade)
    data["open_trades"] = [t for t in data["open_trades"] if t["id"] != trade_id]
    _save(data)
    logger.info("Trade closed: %s P&L=$%.2f (%.2f%%)", trade["symbol"], pnl, pnl_pct)


def log_debate(
    symbol: str, bull_case: str, bear_case: str,
    bull_score: int, bear_score: int, final_conviction: int, decision: str
) -> str:
    """Store the full bull/bear debate transcript. Returns debate_id."""
    data = _load()
    debate_id = f"debate_{symbol}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    data["debate_logs"].append({
        "id":               debate_id,
        "symbol":           symbol,
        "ts":               datetime.utcnow().isoformat(),
        "bull_case":        bull_case,
        "bear_case":        bear_case,
        "bull_score":       bull_score,
        "bear_score":       bear_score,
        "final_conviction": final_conviction,
        "decision":         decision,
    })
    _save(data)
    return debate_id


def log_run(
    run_type: str, candidates: list[str], trades_executed: int,
    skipped_reason: str = ""
):
    """Log a summary of each pipeline run."""
    data = _load()
    data["runs"].append({
        "ts":               datetime.utcnow().isoformat(),
        "run_type":         run_type,
        "candidates":       candidates,
        "trades_executed":  trades_executed,
        "skipped_reason":   skipped_reason,
    })
    _save(data)


def get_open_trades() -> list[dict]:
    return _load().get("open_trades", [])


def get_all_trades() -> list[dict]:
    return _load().get("trades", [])


def get_snapshots() -> list[dict]:
    return _load().get("snapshots", [])
