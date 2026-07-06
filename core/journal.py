"""
core/journal.py
Writes all trade activity, debate logs, and portfolio snapshots to a single
JSON file that the Vercel dashboard reads. Append-only — never overwrites history.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# Caps for the append-heavy observability arrays (audit 2026-07-06: debate_logs /
# runs / risk_decisions / position_reviews were unbounded and dominated data.json's
# 5.9 MB — a wholesale-rewritten multi-MB JSON committed 10-14x/day). Sized to keep
# months of history at current run rates. Consumers only need recent windows
# (sentinel cooldowns, open-trade debate lookups, forward-return scoring). The
# closed-trades ledger ("trades") is the real P&L record and is NEVER capped;
# full history stays recoverable from git either way.
_ARRAY_CAPS = {
    "debate_logs":      500,
    "runs":             1000,
    "risk_decisions":   1500,
    "position_reviews": 1500,
}


def _cap(data: dict, key: str):
    cap = _ARRAY_CAPS.get(key)
    if cap and len(data.get(key, [])) > cap:
        data[key] = data[key][-cap:]


class JournalCorrupt(RuntimeError):
    """Raised when the journal exists but cannot be parsed. NEVER auto-reset."""


def _load() -> dict:
    """
    Load existing journal or return fresh structure.

    A4 (audit): a corrupt/truncated journal must HALT the pipeline, not silently
    return a fresh structure — the very next _save() would have overwritten the
    entire trading history with an empty file. Recovery is manual: restore from
    the .bak sibling or from git (every run is committed).
    """
    path = Path(config.JOURNAL_PATH)
    if path.exists() and path.stat().st_size > 0:
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            raise JournalCorrupt(
                f"{path} exists but failed to parse ({e}). Refusing to reset the "
                f"journal — restore from {path}.bak or git history."
            ) from e
    return {
        "meta": {
            "created":          datetime.now(timezone.utc).isoformat(),
            "benchmark":        config.BENCHMARK_TICKER,
            "starting_capital": config.STARTING_CAPITAL,
            "max_positions":    config.MAX_POSITIONS,
        },
        "snapshots":  [],   # portfolio value over time
        "trades":     [],   # completed trades (entry + exit)
        "open_trades":[],   # currently open positions
        "runs":       [],   # each pipeline run summary
        "debate_logs":[],   # full bull/bear transcripts
        "watchlists": [],   # after-close market memory records
    }


def _save(data: dict):
    """
    Atomic write (tmp + os.replace) so a killed process can never leave a
    truncated journal, plus a .bak of the previous good file for manual recovery.
    """
    path = Path(config.JOURNAL_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    if path.exists():
        try:
            os.replace(path, path.with_suffix(".json.bak"))
        except OSError:
            pass
    os.replace(tmp, path)


# ── Public API ────────────────────────────────────────────────────────────────

def log_snapshot(portfolio_value: float, cash: float, spy_price: float):
    """Record a portfolio value snapshot for the equity curve."""
    data = _load()
    # Keep config-derived display values fresh so the dashboard reflects them with no code
    # edit (e.g. the "of N max" positions sub-line). Refreshed every run from config.
    data.setdefault("meta", {})["max_positions"] = config.MAX_POSITIONS
    data["snapshots"].append({
        "ts":              datetime.now(timezone.utc).isoformat(),
        "portfolio_value": round(portfolio_value, 2),
        "cash":            round(cash, 2),
        "spy_price":       round(spy_price, 2),
    })
    # Cap snapshot history so the frequent dashboard-snapshot job (snapshot.yml, ~every
    # 15 min in market hours) can't grow data.json without bound — 20000 keeps well over a
    # year of market-hours points for the equity curve.
    if len(data["snapshots"]) > 20000:
        data["snapshots"] = data["snapshots"][-20000:]
    _save(data)
    logger.debug("Snapshot logged: $%.2f", portfolio_value)


def log_trade_open(
    symbol: str, side: str, qty: float, entry_price: float,
    stop_price: float, conviction: int, debate_id: str,
    key_risk: str = "", portfolio_value: float = 0.0, sector: str = "",
    stop_order_id: str = "",
):
    """Record when a new position is opened. entry_price should be the FILL price."""
    data = _load()
    position_usd = round(entry_price * qty, 2)
    position_pct = round(position_usd / portfolio_value, 4) if portfolio_value > 0 else 0.0
    trade = {
        "id":            f"{symbol}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "symbol":        symbol,
        "side":          side,
        "qty":           qty,
        "entry_price":   round(entry_price, 2),
        "entry_ts":      datetime.now(timezone.utc).isoformat(),
        "stop_price":    round(stop_price, 2),
        "stop_order_id": stop_order_id,      # S1: resting broker stop, "" if none
        "stop_ratcheted": False,             # A8: flips True on first trailing ratchet
        "conviction":    conviction,
        "debate_id":     debate_id,
        "status":        "open",
        "key_risk":      key_risk,
        "position_usd":  position_usd,
        "position_pct":  position_pct,
        "sector":        sector,
        "trim_pnl":      0.0,                # A5: realized P&L booked on trims
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
    # A5: total_pnl = final-leg P&L + realized P&L booked on earlier trims, so the
    # whole-position outcome is visible (pnl alone covers only the remaining shares).
    trim_pnl  = round(float(trade.get("trim_pnl", 0) or 0), 2)
    total_pnl = round(pnl + trim_pnl, 2)

    trade.update({
        "exit_price":  exit_p,
        "exit_ts":     datetime.now(timezone.utc).isoformat(),
        "exit_reason": exit_reason,
        "pnl":         pnl,
        "pnl_pct":     pnl_pct,
        "trim_pnl":    trim_pnl,
        "total_pnl":   total_pnl,
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
    debate_id = f"debate_{symbol}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    data["debate_logs"].append({
        "id":               debate_id,
        "symbol":           symbol,
        "ts":               datetime.now(timezone.utc).isoformat(),
        "bull_case":        bull_case,
        "bear_case":        bear_case,
        "bull_score":       bull_score,
        "bear_score":       bear_score,
        "final_conviction": final_conviction,
        "decision":         decision,
    })
    _cap(data, "debate_logs")
    _save(data)
    return debate_id


def log_run(
    run_type: str, candidates: list[str], trades_executed: int,
    skipped_reason: str = "", regime: str = "", vix_regime: str = "",
    reason: str = "scheduled", event_symbols: list[str] | None = None,
    event_details: list[dict] | None = None,
    candidate_details: list[dict] | None = None,
    llm_failures: dict | None = None,
):
    """Log a summary of each pipeline run."""
    data = _load()
    entry = {
        "ts":               datetime.now(timezone.utc).isoformat(),
        "run_type":         run_type,
        "candidates":       candidates,
        "trades_executed":  trades_executed,
        "skipped_reason":   skipped_reason,
        "reason":           reason,
    }
    # C3-OBS: persist the screener composite_score + per-factor signals for each
    # candidate so signal-quality post-mortems and the C18 data-driven score floor
    # become computable. Additive/append-only — the dashboard's `candidates` list is
    # unchanged; this is an extra optional field.
    if candidate_details:
        entry["candidate_details"] = candidate_details
    # Observability (audit §7.4): a run where the analysts all fell back to
    # neutral-5 is a data/LLM outage, not analysis — make that visible per run.
    if llm_failures and any(llm_failures.values()):
        entry["llm_failures"] = llm_failures
    if event_symbols:
        entry["event_symbols"] = event_symbols
    if event_details:
        entry["event_details"] = event_details
    if regime:
        entry["regime"] = regime
    if vix_regime:
        entry["vix_regime"] = vix_regime
    data["runs"].append(entry)
    _cap(data, "runs")
    _save(data)


def log_risk_decision(
    symbol: str,
    action: str,
    reason: str,
    conviction: int,
    rotate_from: str = "",
    **extra,
):
    """Append a deterministic risk-manager decision for audit/debugging."""
    data = _load()
    data.setdefault("risk_decisions", [])
    entry = {
        "ts":          datetime.now(timezone.utc).isoformat(),
        "symbol":      symbol,
        "action":      action,
        "reason":      reason,
        "conviction":  conviction,
        "rotate_from": rotate_from,
    }
    entry.update({k: v for k, v in extra.items() if v not in (None, "")})
    data["risk_decisions"].append(entry)
    _cap(data, "risk_decisions")
    _save(data)


def _append_capped(key: str, entry: dict, cap: int = 500):
    """Append entry to data[key] (created if absent), keeping only the last `cap`."""
    data = _load()
    data.setdefault(key, [])
    data[key].append(entry)
    if len(data[key]) > cap:
        data[key] = data[key][-cap:]
    _save(data)


def log_tech_shadow(symbol: str, llm_signal, llm_strength, det_signal,
                    det_strength, agree: bool):
    """C13-TECH shadow mode: record one LLM-vs-deterministic technical comparison."""
    _append_capped("tech_shadow", {
        "ts":           datetime.now(timezone.utc).isoformat(),
        "symbol":       symbol,
        "llm_signal":   llm_signal,
        "llm_strength": llm_strength,
        "det_signal":   det_signal,
        "det_strength": det_strength,
        "agree":        bool(agree),
    })


def log_pre_debate_gate(symbol: str, would_gate: bool, reason: str):
    """C18 watch mode: record what the pre-debate gate WOULD have done (no enforcement)."""
    _append_capped("pre_debate_gates", {
        "ts":         datetime.now(timezone.utc).isoformat(),
        "symbol":     symbol,
        "would_gate": bool(would_gate),
        "reason":     reason,
    })


def log_would_have_traded(record: dict):
    """
    Phase 1 options shadow: log a would-be spread alongside the real equity trade.
    Record should include: ts, symbol, structure, long_leg, short_leg, net_debit,
    max_loss, max_profit, breakeven, qty, total_premium, pct_of_portfolio, net_greeks,
    dte, expiry_date, conviction, catalyst, veto_reason. Append-only, capped at 200.
    """
    entry = {"ts": datetime.now(timezone.utc).isoformat()}
    entry.update(record)
    _append_capped("would_have_traded", entry, cap=200)


def log_greeks_snapshot(symbol: str, occ_symbol: str, greeks: dict):
    """
    Periodic greeks snapshot for an open option position (Phase 2+).
    Used for dashboard P&L display and theta/vega monitoring. Capped at 1000.
    """
    _append_capped("greeks_snapshots", {
        "ts":         datetime.now(timezone.utc).isoformat(),
        "symbol":     symbol,
        "occ_symbol": occ_symbol,
        "greeks":     greeks,
    }, cap=1000)


def get_open_trades() -> list[dict]:
    return _load().get("open_trades", [])


def get_all_trades() -> list[dict]:
    return _load().get("trades", [])


def get_snapshots() -> list[dict]:
    return _load().get("snapshots", [])


def get_debate_by_id(debate_id: str) -> dict | None:
    """Return a stored debate log by its id, or None."""
    if not debate_id:
        return None
    return next(
        (d for d in _load().get("debate_logs", []) if d.get("id") == debate_id),
        None,
    )


def log_watchlist(record: dict) -> str:
    """Append an after-close watchlist record and return its id."""
    data = _load()
    data.setdefault("watchlists", [])
    generated = record.get("generated_at") or datetime.now(timezone.utc).isoformat()
    watchlist_id = record.get("id") or f"watchlist_{generated[:19].replace('-', '').replace(':', '').replace('T', '_')}"
    record["id"] = watchlist_id
    data["watchlists"].append(record)
    history_limit = getattr(config, "WATCHLIST_HISTORY_LIMIT", 30)
    if history_limit and len(data["watchlists"]) > history_limit:
        data["watchlists"] = sorted(
            data["watchlists"],
            key=lambda w: w.get("generated_at", ""),
        )[-history_limit:]
    _save(data)
    logger.info("Watchlist logged: %s entries=%s", watchlist_id, record.get("entry_count", 0))
    return watchlist_id


def get_latest_watchlist(source: str = "after_close") -> dict | None:
    """Return the most recent watchlist record for a source."""
    records = [
        w for w in _load().get("watchlists", [])
        if w.get("source") == source
    ]
    if not records:
        return None
    return max(records, key=lambda w: w.get("generated_at", ""))


def log_position_review(
    trade_id: str,
    symbol: str,
    action: str,
    conviction: int,
    thesis_status: str,
    rationale: str,
    original_thesis_check: str,
    key_risk_to_monitor: str,
    **extra,
):
    """Append a position review record; creates position_reviews array if absent."""
    data = _load()
    data.setdefault("position_reviews", [])
    entry = {
        "trade_id":             trade_id,
        "symbol":               symbol,
        "ts":                   datetime.now(timezone.utc).isoformat(),
        "action":               action,
        "conviction":           conviction,
        "thesis_status":        thesis_status,
        "rationale":            rationale,
        "original_thesis_check": original_thesis_check,
        "key_risk_to_monitor":  key_risk_to_monitor,
    }
    entry.update({k: v for k, v in extra.items() if v is not None})
    data["position_reviews"].append(entry)
    _cap(data, "position_reviews")
    _save(data)
    logger.info("Position review logged: %s → %s (%s)", symbol, action, thesis_status)


def update_open_trade(trade_id: str, updates: dict):
    """Apply a dict of field updates to an open trade in-place."""
    data = _load()
    for trade in data["open_trades"]:
        if trade["id"] == trade_id:
            trade.update(updates)
            break
    _save(data)


def log_trade_trim(
    trade_id: str,
    trim_qty: float,
    new_qty: float,
    price: float,
    reason: str,
    basis: float | None = None,
):
    """
    Record a partial sell on an open trade and update its qty.
    A5 (audit): realized P&L on the trimmed shares is booked against `basis`
    (broker avg_entry preferred, journal entry_price fallback) and accumulated on
    the trade's trim_pnl so trim proceeds stop vanishing from the ledger.
    """
    data = _load()
    for trade in data["open_trades"]:
        if trade["id"] == trade_id:
            old_qty = float(trade.get("qty", 0) or 0)
            old_pct = float(trade.get("position_pct", 0) or 0)
            cost_basis = float(basis if basis else trade.get("entry_price", 0) or 0)
            realized = round((price - cost_basis) * trim_qty, 2) if cost_basis > 0 else 0.0
            trade.setdefault("trim_history", [])
            trade["trim_history"].append({
                "ts":           datetime.now(timezone.utc).isoformat(),
                "trim_qty":     round(trim_qty, 4),
                "new_qty":      round(new_qty, 4),
                "price":        round(price, 2),
                "basis":        round(cost_basis, 4),
                "realized_pnl": realized,
                "reason":       reason,
            })
            trade["trim_pnl"] = round(float(trade.get("trim_pnl", 0) or 0) + realized, 2)
            trade["qty"] = round(new_qty, 4)
            trade["position_usd"] = round(price * new_qty, 2)
            if old_qty > 0:
                trade["position_pct"] = round(old_pct * (new_qty / old_qty), 4)
            break
    _save(data)
    logger.info("Trim logged: trade=%s sold=%.4f remaining=%.4f @ $%.2f",
                trade_id, trim_qty, new_qty, price)


def log_universe_discovery(record: dict):
    """
    EXP-006: per-run audit of dynamic universe discovery — accepted symbols (with
    source + momentum stats) and rejected symbols (with the guard that fired).
    Capped at 300.
    """
    entry = {"ts": datetime.now(timezone.utc).isoformat()}
    entry.update(record)
    _append_capped("universe_discovery", entry, cap=300)


def log_baseline_shadow(record: dict):
    """
    S2 (audit): log what the deterministic baseline twin WOULD have bought this run
    (screener top-N + same risk caps + fixed size + same stop math). Never executed.
    Forward returns of these records vs the live pipeline's decisions measure whether
    the LLM debate layer adds selection value. Capped at 500.
    """
    entry = {"ts": datetime.now(timezone.utc).isoformat()}
    entry.update(record)
    _append_capped("baseline_shadow", entry, cap=500)


def set_queued_action(trade_id: str, action: str, reason: str):
    """
    Record that a trim/exit was recommended but market was closed.
    The next run re-reviews fresh — this is for audit purposes only.
    """
    data = _load()
    for trade in data["open_trades"]:
        if trade["id"] == trade_id:
            trade["queued_action"] = {
                "action":    action,
                "reason":    reason,
                "queued_ts": datetime.now(timezone.utc).isoformat(),
            }
            break
    _save(data)
    logger.info("Queued action: trade=%s action=%s (market closed)", trade_id, action)


def clear_queued_action(trade_id: str):
    """Remove any stale queued action from an open trade."""
    data = _load()
    for trade in data["open_trades"]:
        if trade["id"] == trade_id:
            trade.pop("queued_action", None)
            break
    _save(data)


# ── GitHub auto-push ──────────────────────────────────────────────────────────

def push_to_github(commit_message: str = None):
    """
    Commit and push the updated data.json to GitHub after every run.
    Requires the repo to already have a remote origin configured (git push -u origin main).
    Called automatically at the end of each pipeline run.
    """
    import subprocess
    import os

    # C5: under GitHub Actions the workflow's "Push updated dashboard data" step is the
    # single push authority (it serializes via the concurrency group and rebases safely).
    # Doing a second commit+push here had no rebase and raced the workflow step, so we
    # defer entirely in CI. Local/scheduled runs still commit+push as before.
    if os.getenv("GITHUB_ACTIONS"):
        logger.info("GitHub push: under GitHub Actions — deferring to workflow push step")
        return

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    journal   = os.path.join(repo_root, config.JOURNAL_PATH)
    msg       = commit_message or f"Auto: pipeline run {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"

    try:
        # Stage only the dashboard data file — nothing else
        subprocess.run(
            ["git", "add", journal],
            cwd=repo_root, check=True, capture_output=True,
        )
        # Check if there's actually anything to commit
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repo_root, capture_output=True,
        )
        if result.returncode == 0:
            logger.info("GitHub push: no changes to data.json, skipping")
            return

        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=repo_root, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "push"],
            cwd=repo_root, check=True, capture_output=True,
        )
        logger.info("GitHub push: data.json pushed — Vercel will redeploy shortly")
    except subprocess.CalledProcessError as e:
        logger.warning("GitHub push failed: %s", e.stderr.decode() if e.stderr else str(e))
    except Exception as e:
        logger.warning("GitHub push failed: %s", e)
