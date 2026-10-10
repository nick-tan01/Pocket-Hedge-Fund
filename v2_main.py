"""
v2_main.py — Pocket Hedge Fund v2 pipeline entrypoint (EXP-018).

Deterministic residual-momentum pipeline. Replaces the v1 debate funnel when
config.V2_ENABLED is True; the dispatcher slot contract, journal, snapshot and
broker conventions are unchanged.

Pipeline (per run):
  1. Slot idempotency (v2 namespace: "v2-<slot>") — duplicate slots exit 0.
  2. Account-level hard-stop check (reused from v1).
  3. Entry gate (market hours) — early/closed/late runs do snapshot+journal only.
  4. Monthly rebalance (first run of each calendar month):
       residual-momentum top-K -> equal-weight targets -> diff vs v2 book
       -> sell drops, buy adds, re-equalize held names, journal everything.
  5. Snapshot (with qqq_price) + run record.

Sizing is flat equal-weight; conviction plays no role (dead by operator order).
The sentiment tilt is wired but disabled (config.SENTIMENT_ENABLED=False) until
the ablation gate passes.

Cutover from v1 is EXPLICIT: `python v2_main.py --cutover` liquidates non-v2
positions (journaled). The regular pipeline never auto-liquidates v1 trades.

Usage (same contract as main.py):
  python v2_main.py --now --reason scheduled --slot 2026-10-07T14:05
  python v2_main.py --test          # dry run: no orders, still journals (like v1)
  python v2_main.py --cutover       # one-time v1 -> v2 book cutover (explicit)
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timezone

# Import main FIRST: it configures root logging at import (basicConfig side
# effect). We intentionally inherit that config for CI consistency.
import main as v1
import config
from agents import sentiment_v2
from agents.screener import Screener
from core import signal_v2
from core.alpaca_client import AlpacaClient
from core.data_fetcher import DataFetcher
from core.journal import (clear_queued_action, get_open_trades, get_runs,
                          log_run, log_trade_close, log_trade_open,
                          log_trade_trim, push_to_github, update_open_trade)

logger = logging.getLogger(__name__)

V2_SLOT_PREFIX = "v2-"
REBAL_TOL_PCT = 0.015  # re-equalize held names drifting >1.5pp from target


def v2_slot(slot: str) -> str:
    """v2 idempotency namespace — never collides with v1 slot labels."""
    s = (slot or "").strip()
    return f"{V2_SLOT_PREFIX}{s}" if s else ""


def _last_v2_rebalance_month() -> str | None:
    """YYYY-MM of the last run that performed a v2 rebalance (None if never).

    Note: log_run() flattens run_meta into the record and drops falsy values,
    so v2_rebalanced=False runs are indistinguishable from unset — we only
    look for truthy v2_rebalanced.
    """
    for r in reversed(get_runs()):
        if r.get("strategy") == "v2" and r.get("v2_rebalanced"):
            ts = str(r.get("ts", ""))[:7]
            return ts or None
    return None


def _v2_positions() -> list[dict]:
    return [t for t in get_open_trades() if t.get("strategy") == "v2"]


def _v2_run_meta(slot_v2: str, rebalanced: bool, **extra) -> dict:
    meta = {
        "strategy": "v2",
        "slot": slot_v2,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "github_run_id": os.getenv("GITHUB_RUN_ID", ""),
        "v2_rebalanced": rebalanced,
        "v2_signal": "residual",
        "v2_top_k": config.V2_TOP_K,
    }
    meta.update({k: v for k, v in extra.items() if v})
    return meta


def _snapshot_v2(alpaca: AlpacaClient, fetcher: DataFetcher, account: dict) -> None:
    """Snapshot with QQQ price (benchmark.py picks it up when present)."""
    from core.journal import log_snapshot
    spy_px = alpaca.get_latest_price("SPY") or 0.0
    qqq_px = alpaca.get_latest_price("QQQ") or 0.0
    log_snapshot(account["portfolio_value"], account["cash"], spy_px,
                 qqq_price=qqq_px)


def _execute_rebalance(alpaca: AlpacaClient, fetcher: DataFetcher, dry_run: bool,
                       reason: str) -> tuple[int, list[str]]:
    """Monthly rebalance. Returns (trades_executed, target_symbols)."""
    universe = sorted(set(Screener.WATCHLIST))
    scores = signal_v2.compute_scores(fetcher, universe)
    if not scores:
        logger.warning("v2: no scores computed — skipping rebalance")
        return 0, []
    picks = signal_v2.select_top(scores, config.V2_TOP_K)
    weights = signal_v2.equal_weights(picks, gross=config.V2_MAX_GROSS)
    # sentiment tilt (disabled until the ablation gate passes -> no-op)
    weights = sentiment_v2.apply_tilt(
        weights, sentiment_v2.get_sentiment_scores(picks))

    account = alpaca.get_account()
    pv = float(account.get("portfolio_value") or 0)
    if pv <= 0:
        logger.warning("v2: no portfolio value — skipping rebalance")
        return 0, []

    # DD breaker: halt NEW entries (sells still allowed) below 90% of peak.
    dd_halt = False
    try:
        from core.journal import get_snapshots
        snaps = get_snapshots()
        peak = max([float(s.get("portfolio_value") or 0) for s in snaps] +
                   [config.STARTING_CAPITAL, pv])
        if pv < peak * (1 - config.V2_DD_BREAKER_PCT):
            dd_halt = True
            logger.warning("v2: drawdown breaker — %.1f%% below peak, new entries halted",
                           (1 - pv / peak) * 100)
    except Exception as e:
        logger.warning("v2: DD check failed (%s) — proceeding", e)

    current = {t["symbol"]: t for t in _v2_positions()}
    live = {p["symbol"]: p for p in alpaca.get_positions()}
    trades = 0

    # 1) SELL drops (in book, not in target)
    for sym, trade in list(current.items()):
        if sym in weights:
            continue
        if dry_run:
            logger.info("DRY RUN — v2 would SELL %s (drop)", sym)
            continue
        pos = live.get(sym)
        qty = float((pos or {}).get("qty") or trade.get("qty") or 0)
        if qty <= 0:
            log_trade_close(trade["id"], 0.0, "v2_rebalance_drop:no_position")
            continue
        order = alpaca.close_position(sym, reason="v2_rebalance_drop")
        px = v1._await_fill(alpaca, (order or {}).get("id"))[0] if order else None
        px = px or float((pos or {}).get("current_price") or trade.get("current_price") or 0)
        log_trade_close(trade["id"], px, "v2_rebalance_drop")
        clear_queued_action(trade["id"])
        trades += 1

    # 2) BUY adds (in target, not in book) — skipped under DD halt
    for sym, w in weights.items():
        if sym in current or dd_halt:
            continue
        notional = pv * w
        px = alpaca.get_latest_price(sym)
        if not px or px <= 0:
            logger.warning("v2: no price for %s — skipping buy", sym)
            continue
        qty = notional / px
        if qty < 1e-6:
            continue
        if dry_run:
            logger.info("DRY RUN — v2 would BUY %s %.4f @ ~$%.2f", sym, qty, px)
            continue
        order = alpaca.submit_market_order(symbol=sym, qty=qty, side="buy",
                                           reason=f"v2_rebalance_add | {reason}",
                                           ref_price=px)
        if not order:
            logger.warning("v2: buy failed for %s", sym)
            continue
        fill_px, fill_qty = v1._await_fill(alpaca, order.get("id"))
        fill_px = fill_px or px
        fill_qty = fill_qty or qty
        tid = log_trade_open(symbol=sym, side="buy", qty=fill_qty,
                             entry_price=fill_px, stop_price=0.0, conviction=0,
                             debate_id="", key_risk="v2 residual momentum",
                             portfolio_value=pv, sector="",
                             stop_order_id="")
        update_open_trade(tid, {"strategy": "v2", "v2_entry_reason": "rebalance_add"})
        trades += 1

    # 3) RE-EQUALIZE held names drifting >1.5pp from target
    for sym, w in weights.items():
        if sym not in current:
            continue
        trade = current[sym]
        pos = live.get(sym)
        mv = float((pos or {}).get("market_value") or 0)
        cur_w = mv / pv if pv else 0
        if abs(cur_w - w) < REBAL_TOL_PCT:
            continue
        if dry_run:
            logger.info("DRY RUN — v2 would re-equalize %s %.1f%% -> %.1f%%",
                        sym, cur_w * 100, w * 100)
            continue
        px = float((pos or {}).get("current_price") or 0) or alpaca.get_latest_price(sym)
        if not px:
            continue
        if cur_w > w:
            # trim the excess
            trim_notional = (cur_w - w) * pv
            trim_qty = trim_notional / px
            live_qty = float((pos or {}).get("qty") or 0)
            trim_qty = min(trim_qty, live_qty)
            if trim_qty * px < 1.0:
                continue
            order = alpaca.submit_market_order(symbol=sym, qty=trim_qty,
                                               side="sell",
                                               reason="v2_rebalance_reequalize")
            if order:
                new_qty = live_qty - trim_qty
                log_trade_trim(trade["id"], trim_qty, new_qty, px,
                               "v2_rebalance_reequalize",
                               basis=float(trade.get("avg_entry") or trade.get("entry_price") or px))
                update_open_trade(trade["id"], {"qty": new_qty})
                trades += 1
        elif not dd_halt:
            add_notional = (w - cur_w) * pv
            add_qty = add_notional / px
            if add_qty * px < 1.0:
                continue
            order = alpaca.submit_market_order(symbol=sym, qty=add_qty,
                                               side="buy",
                                               reason="v2_rebalance_reequalize",
                                               ref_price=px)
            if order:
                fill_px, fill_qty = v1._await_fill(alpaca, order.get("id"))
                fill_px, fill_qty = fill_px or px, fill_qty or add_qty
                old_qty = float(trade.get("qty") or 0)
                old_basis = float(trade.get("avg_entry") or trade.get("entry_price") or px)
                new_qty = old_qty + fill_qty
                new_basis = ((old_qty * old_basis + fill_qty * fill_px) / new_qty
                             if new_qty else px)
                update_open_trade(trade["id"], {"qty": new_qty, "avg_entry": round(new_basis, 4)})
                trades += 1

    return trades, picks


def run_v2_pipeline(dry_run: bool = False, slot: str = "",
                    reason: str = "scheduled") -> None:
    """v2 pipeline entry — mirrors main.run_pipeline's contract."""
    slot_v2 = v2_slot(slot)
    run_start = datetime.now(timezone.utc)

    # 1) slot idempotency (v2 namespace)
    if slot_v2 and v1._slot_already_completed(slot_v2):
        logger.info("v2: slot %s already completed — exiting", slot_v2)
        return

    alpaca = AlpacaClient()
    fetcher = DataFetcher()

    # 1b) self-heal the journal: broker is source of truth (same as v1).
    # Non-fatal — a prior run may have filled without persisting.
    try:
        from core.reconcile import reconcile_untracked
        adopted = reconcile_untracked(alpaca, apply=not dry_run)
        if adopted:
            logger.warning("v2 AUTO-RECONCILE | adopted %d untracked position(s)",
                           len(adopted))
    except Exception as e:
        logger.error("v2 auto-reconcile failed (non-fatal): %s", e)

    # 2) account-level hard stops (reuse v1)
    ok, skipped_reason, regime, vix_regime = v1.check_hard_stops(alpaca, fetcher)
    if not ok:
        account = alpaca.get_account()
        _snapshot_v2(alpaca, fetcher, account)
        log_run("v2", [], 0, skipped_reason=skipped_reason,
                run_meta=_v2_run_meta(slot_v2, False))
        return

    # 3) entry gate — early/closed runs do snapshot + journal only
    gate = v1._entry_gate_skip_reason(alpaca)
    account = alpaca.get_account()
    if gate:
        _snapshot_v2(alpaca, fetcher, account)
        log_run("v2", [], 0, skipped_reason=gate, reason=reason,
                run_meta=_v2_run_meta(slot_v2, False))
        logger.info("v2: entry gate '%s' — review-only run", gate)
        return

    # 4) monthly rebalance on the first run of each calendar month
    month = run_start.strftime("%Y-%m")
    last = _last_v2_rebalance_month()
    trades, picks = 0, []
    rebalanced = False
    if last != month:
        can_trade, why = v1.can_execute_trades(alpaca)
        if can_trade:
            trades, picks = _execute_rebalance(alpaca, fetcher, dry_run, reason)
            rebalanced = True
        else:
            logger.info("v2: rebalance due but cannot trade (%s)", why)

    # 5) snapshot + run record
    _snapshot_v2(alpaca, fetcher, account)
    log_run("v2", picks, trades, reason=reason,
            run_meta=_v2_run_meta(slot_v2, rebalanced))
    logger.info("v2: run complete — rebalanced=%s trades=%d", rebalanced, trades)
    if not dry_run:
        push_to_github()


def run_cutover(dry_run: bool = False, slot: str = "") -> None:
    """EXPLICIT one-time v1 -> v2 cutover: liquidate non-v2 positions.

    Never runs as part of the regular pipeline. Journaled as v2_cutover.
    Idempotent per v2 slot namespace: a completed cutover slot exits 0.
    """
    slot_v2 = v2_slot(slot)
    if slot_v2 and v1._slot_already_completed(slot_v2):
        logger.info("v2 cutover: slot %s already completed — exiting", slot_v2)
        return
    alpaca = AlpacaClient()
    n = 0
    for trade in get_open_trades():
        if trade.get("strategy") == "v2":
            continue
        sym = trade["symbol"]
        logger.info("%s v2 cutover — closing non-v2 position %s",
                    "DRY RUN:" if dry_run else "", sym)
        if dry_run:
            continue
        order = alpaca.close_position(sym, reason="v2_cutover")
        px = v1._await_fill(alpaca, (order or {}).get("id"))[0] if order else None
        px = px or float(trade.get("current_price") or 0)
        log_trade_close(trade["id"], px, "v2_cutover")
        n += 1
    log_run("v2_cutover", [], n, reason="manual",
            run_meta=_v2_run_meta(slot_v2, False, cutover_closed=n))
    logger.info("v2 cutover complete — closed %d non-v2 positions", n)


def main() -> None:
    ap = argparse.ArgumentParser(description="Pocket Hedge Fund v2 pipeline")
    ap.add_argument("--now", action="store_true")
    ap.add_argument("--test", action="store_true",
                    help="dry run: no orders, still journals")
    ap.add_argument("--reason", default="scheduled")
    ap.add_argument("--slot", default="")
    ap.add_argument("--cutover", action="store_true",
                    help="one-time: liquidate non-v2 positions for the cutover")
    args = ap.parse_args()

    config.validate_required_env(require_llm=False)  # v2 needs no LLM (sentiment off)

    if args.cutover:
        run_cutover(dry_run=args.test)
    elif args.now or args.test:
        run_v2_pipeline(dry_run=args.test, slot=args.slot.strip(),
                        reason=args.reason)
    else:
        print("v2_main.py: use --now, --test, or --cutover (no scheduler here)")


if __name__ == "__main__":
    main()
