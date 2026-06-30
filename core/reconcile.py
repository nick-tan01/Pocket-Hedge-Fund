"""
core/reconcile.py
Self-healing journal↔broker reconciliation.

The broker is the SOURCE OF TRUTH; the committed journal (dashboard/data.json) is a
derived cache that can miss a write. A pipeline run can fill an order (and place its
broker stop) but then fail to persist the open_trade record — a push/merge race, a run
crash, a GitHub Actions timeout, or an Alpaca network blip between the fill and the
commit. The result is an UNTRACKED position: held + stopped at the broker, invisible to
the journal, so the pipeline never reviews it and the dashboard/reviews under-report the
book (this is how MU, HOOD, and AMAT became untracked).

You can't make a broker fill and a git commit atomic, so instead of trying to guarantee
every write, we CONVERGE the journal to the broker every cycle. `reconcile_untracked` is
called at the top of every pipeline run (main.run_pipeline) and is also the engine behind
scripts/reconcile_untracked_positions.py for manual/CLI use.

Journal-write only — this module NEVER submits or cancels a broker order.
"""

import logging

import config
from core.journal import _load, _save, get_open_trades

logger = logging.getLogger(__name__)

# Entry timestamps recovered by hand for positions whose order history predates the
# Alpaca lookback window. get_last_filled_buy() supersedes this for any recent fill;
# the map is only a fallback so old reconciliations stay accurate.
KNOWN_ENTRY_TS = {
    "AMAT": "2026-06-12T16:37:38+00:00",
    "MU":   "2026-06-24T15:56:43+00:00",
}


def _sector(symbol: str) -> str:
    try:
        import yfinance as yf
        return yf.Ticker(symbol).info.get("sector", "Unknown") or "Unknown"
    except Exception:
        return "Unknown"


def find_untracked(alpaca) -> list[dict]:
    """Live broker positions (long equity only) absent from the journal's open_trades."""
    journal_syms = {t.get("symbol") for t in get_open_trades()}
    untracked = []
    for p in alpaca.get_positions():
        try:
            if float(p.get("qty", 0)) <= 0:   # long-only fund; skip shorts / zero qty
                continue
        except (TypeError, ValueError):
            continue
        if p.get("symbol") and p["symbol"] not in journal_syms:
            untracked.append(p)
    return untracked


def build_reconciled_record(alpaca, position: dict, portfolio_value: float) -> dict:
    """Build one open_trade record for an untracked broker position, from broker truth."""
    sym   = position["symbol"]
    entry = float(position["avg_entry"])
    qty   = float(position["qty"])

    stops = alpaca.get_open_stop_orders(sym)
    stop  = stops[0] if stops else {}
    stop_price = float(stop.get("stop_price") or round(entry * (1 - config.HARD_STOP_PCT), 2))

    # Recover the true entry timestamp: live order history first, then the known map.
    entry_ts = KNOWN_ENTRY_TS.get(sym, "")
    try:
        fill = alpaca.get_last_filled_buy(sym)
    except Exception:
        fill = None
    if fill and fill.get("filled_at"):
        entry_ts = fill["filled_at"]

    position_usd = round(entry * qty, 2)
    return {
        "id":             f"{sym}_RECONCILED_{entry_ts[:10].replace('-', '')}",
        "symbol":         sym,
        "side":           "buy",
        "qty":            round(qty, 4),
        "entry_price":    round(entry, 2),
        "entry_ts":       entry_ts,
        "stop_price":     round(stop_price, 2),
        "stop_order_id":  stop.get("id", ""),
        "stop_ratcheted": False,
        "conviction":     6,            # original conviction unrecoverable; honest buy-floor
        "debate_id":      "",           # lost with the un-persisted run
        "status":         "open",
        "key_risk":       "RECONCILED from broker — original debate/run record lost to a "
                          "journal write failure; conviction/debate_id unknown",
        "position_usd":   position_usd,
        "position_pct":   round(position_usd / portfolio_value, 4) if portfolio_value else 0.0,
        "sector":         _sector(sym),
        "trim_pnl":       0.0,
        "reconciled_from_broker": True,
    }


def reconcile_untracked(alpaca, apply: bool = True) -> list[dict]:
    """Adopt untracked broker positions into the journal and return the rebuilt records.

    apply=False is a dry run: build the records but write nothing (used by the CLI's
    default dry-run and by the pipeline's dry_run mode).
    """
    untracked = find_untracked(alpaca)
    if not untracked:
        return []
    pv = alpaca.get_account().get("portfolio_value", 0) or 0
    rebuilt = [build_reconciled_record(alpaca, p, pv) for p in untracked]
    if apply:
        data = _load()
        data.setdefault("open_trades", []).extend(rebuilt)
        _save(data)
        for r in rebuilt:
            logger.warning("AUTO-RECONCILE | adopted %s (qty=%s entry=$%.2f pct=%.1f%%) "
                           "into journal", r["symbol"], r["qty"], r["entry_price"],
                           r["position_pct"] * 100)
    return rebuilt
