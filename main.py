"""
main.py — Pocket Hedge Fund orchestrator
Full pipeline: Screener → Analysts → 2-Round Debate → PM Verdict → Risk → Execute

Usage:
    python3 main.py          # scheduled (8:30am + 1:00pm ET)
    python3 main.py --now    # run once immediately
    python3 main.py --test   # dry run, no orders submitted
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import pytz
import schedule

import config
from core.alpaca_client import AlpacaClient
from core.data_fetcher import DataFetcher
from core.journal import (
    log_run, log_snapshot, log_debate,
    log_trade_open, log_trade_close, get_open_trades, push_to_github,
    get_debate_by_id, log_position_review, update_open_trade,
    log_trade_trim, set_queued_action, clear_queued_action,
    log_risk_decision, get_latest_watchlist, log_pre_debate_gate,
    log_would_have_traded, get_snapshots, log_baseline_shadow,
)
from agents.screener import Screener, ScreenerDataUnavailable
from agents.after_close_watchlist import LONG_SETUP_TYPES
import agents.technical        as technical_agent
import agents.fundamental      as fundamental_agent
import agents.sentiment        as sentiment_agent
import agents.bull_researcher  as bull_agent
import agents.bear_researcher  as bear_agent
import agents.portfolio_manager as pm_agent
import agents.risk_manager      as risk_agent
import agents.position_reviewer as reviewer_agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"{config.LOG_DIR}system.log"),
    ],
)
logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")


# ── Circuit breakers / regime ─────────────────────────────────────────────────

def check_hard_stops(alpaca: AlpacaClient, fetcher: DataFetcher) -> tuple[bool, str, str, str]:
    vix_regime = "normal"
    vix = fetcher.get_vix()
    if vix and vix > config.VIX_HIGH_THRESHOLD:
        vix_regime = "high_vix"
    elif vix and vix > config.VIX_ELEVATED_THRESHOLD:
        vix_regime = "elevated_vix"

    regime = "bull"
    spy_bars = fetcher.get_ohlcv("SPY", days=220)
    if spy_bars and len(spy_bars) >= 200:
        spy_200d_ma = sum(b["close"] for b in spy_bars[-200:]) / 200
        spy_current = spy_bars[-1]["close"]
        if spy_current < spy_200d_ma * 0.97:
            regime = "bear"
        elif spy_current < spy_200d_ma:
            regime = "caution"

    account = alpaca.get_account()
    value   = account["portfolio_value"]
    # A2 (audit): drawdown must be measured from the PEAK equity, not from starting
    # capital — loss-from-inception lets a 21% fall from a run-up read as 5%. Peak is
    # reconstructed from journal snapshots (plus current value and starting capital).
    # The legacy from-inception number is still logged during the transition.
    try:
        snap_peak = max((s.get("portfolio_value", 0) for s in get_snapshots()), default=0)
    except Exception:
        snap_peak = 0
    peak = max(config.STARTING_CAPITAL, snap_peak, value)
    drawdown_from_peak  = (peak - value) / peak if peak > 0 else 0.0
    drawdown_inception  = (config.STARTING_CAPITAL - value) / config.STARTING_CAPITAL
    logger.info("Drawdown | from_peak=%.2f%% (peak=$%.0f) | from_inception=%.2f%%",
                drawdown_from_peak * 100, peak, drawdown_inception * 100)
    if drawdown_from_peak > config.MAX_PORTFOLIO_DD:
        return False, (f"Portfolio drawdown {drawdown_from_peak*100:.1f}% from peak "
                       f"${peak:,.0f} exceeds limit"), regime, vix_regime
    return True, "", regime, vix_regime


def can_execute_trades(alpaca: AlpacaClient) -> tuple[bool, str]:
    if not alpaca.is_market_open():
        return False, "Market closed — analysis complete, no orders submitted"
    if alpaca.minutes_since_open() < config.MARKET_OPEN_BUFFER:
        return False, "Too close to market open — waiting for price discovery"
    if alpaca.minutes_to_close() < config.MARKET_CLOSE_BUFFER:
        return False, "Too close to market close — orders held"
    return True, ""


# ── Stop loss monitor ─────────────────────────────────────────────────────────

def _stop_exit_reason(trade: dict) -> str:
    """
    A8 (audit): distinguish a protective initial stop from a trailing-stop harvest.
    Lumping both under 'stop_loss' made +24% trailing exits look like failed entries
    in every downstream feedback loop.
    """
    return "trailing_stop" if trade.get("stop_ratcheted") else "initial_stop"


_STOP_OPEN_STATES = ("new", "accepted", "held", "pending_new", "partially_filled")


def _ensure_broker_stop(alpaca: AlpacaClient, trade: dict) -> bool:
    """
    S1 reconciliation: make sure a journal-tracked position has a live stop order
    resting at the broker matching the journal's stop_price/qty. Places one for
    legacy positions opened before broker-native stops existed.

    Returns True only if a stop is ACTUALLY resting — callers must use this (not
    the mere presence of stop_order_id) to decide whether the software fallback
    fires. A filled/cancelled order id is cleared so the fallback re-engages.
    """
    if not getattr(config, "BROKER_NATIVE_STOPS", False):
        return False
    symbol = trade["symbol"]
    stop_price = trade.get("stop_price") or 0
    qty = trade.get("qty") or 0
    if stop_price <= 0 or qty <= 0:
        return False

    stop_order_id = trade.get("stop_order_id")
    if stop_order_id:
        order = alpaca.get_order(stop_order_id)
        if order and order["status"] in _STOP_OPEN_STATES:
            # Resting — fix qty drift (e.g. a failed trim left it oversized/undersized).
            want = int(qty)
            if want >= 1 and abs(order.get("qty", 0) - want) >= 1:
                replaced = alpaca.replace_stop_order(stop_order_id, stop_price, want)
                if replaced:
                    update_open_trade(trade["id"], {"stop_order_id": replaced["id"]})
                    trade["stop_order_id"] = replaced["id"]
            return True
        # Dead order — clear so fallback logic engages. If it DIED BY FILLING while
        # a fractional dust position remains (the floored whole-share leg sold),
        # book those shares as a realized trim so the ledger stays complete; the
        # software fallback then closes the dust.
        if order and order["status"] == "filled" and order.get("filled_qty") \
                and order.get("filled_avg_price"):
            basis = trade.get("avg_entry") or trade.get("entry_price", 0)
            logger.warning(
                "BROKER STOP FILLED (partial position) | %s %.0f sh @ $%.2f — "
                "booked as realized trim; dust remains",
                symbol, order["filled_qty"], order["filled_avg_price"],
            )
            log_trade_trim(
                trade["id"], order["filled_qty"], trade.get("qty", 0),
                order["filled_avg_price"],
                reason=f"broker_stop_fill_{_stop_exit_reason(trade)}",
                basis=basis,
            )
        update_open_trade(trade["id"], {"stop_order_id": ""})
        trade["stop_order_id"] = ""

    live = alpaca.get_open_stop_orders(symbol)
    if live:
        # A stop exists but the journal lost the id — re-adopt it.
        update_open_trade(trade["id"], {"stop_order_id": live[0]["id"]})
        trade["stop_order_id"] = live[0]["id"]
        return True

    placed = alpaca.submit_stop_order(symbol, qty, stop_price,
                                      reason="s1_reconcile_protective_stop")
    if placed:
        update_open_trade(trade["id"], {"stop_order_id": placed["id"]})
        trade["stop_order_id"] = placed["id"]
        return True
    return False


def check_open_positions(alpaca: AlpacaClient):
    if not alpaca.is_market_open():
        return
    from core.journal import log_trade_close
    positions      = {p["symbol"]: p for p in alpaca.get_positions()}
    journal_trades = get_open_trades()

    # Refresh position_pct and is_remnant from live Alpaca market values.
    # position_pct is stored at entry time and updated on trims, but it drifts
    # as the portfolio value changes. Stale pct causes the dashboard to show
    # remnants (ARM 1.3%, QCOM 2%, DDOG 2%) as if they're full positions.
    # We recompute every run so the slot-counting logic always works from fresh data.
    min_slot_pct = getattr(config, "MIN_SLOT_PCT", 0.03)
    portfolio_value = alpaca.get_account().get("portfolio_value", 0)
    if portfolio_value > 0:
        for trade in journal_trades:
            live_pos = positions.get(trade["symbol"])
            if not live_pos:
                continue
            live_pct = round(float(live_pos.get("market_value", 0)) / portfolio_value, 4)
            is_remnant = live_pct < min_slot_pct and live_pct > 0
            # Persist Alpaca's REAL qty / price / unrealized P&L as the source of truth.
            # Previously only position_pct was refreshed while qty stayed at the original
            # entry value; if Alpaca holds more shares than the journal recorded, the
            # dashboard valued real market value against a stale cost basis and showed
            # wildly inflated per-share price and P&L% (e.g. MS +83% vs real +6.7%).
            update_open_trade(trade["id"], {
                "position_pct":    live_pct,
                "is_remnant":      is_remnant,
                "qty":             round(float(live_pos.get("qty", trade.get("qty", 0))), 4),
                "current_price":   round(float(live_pos.get("current_price", 0)), 4),
                "unrealized_pl":   round(float(live_pos.get("unrealized_pl", 0)), 2),
                "unrealized_plpc": round(float(live_pos.get("unrealized_plpc", 0)), 6),
                "avg_entry":       round(float(live_pos.get("avg_entry", 0)), 4),
            })

    for trade in journal_trades:
        symbol = trade["symbol"]
        if symbol not in positions:
            # S1: the position may be gone because the BROKER stop filled between
            # runs. Check the resting stop order first — closing at the real fill
            # price beats the legacy orphan-close-at-entry fabrication.
            stop_order_id = trade.get("stop_order_id")
            if stop_order_id:
                order = alpaca.get_order(stop_order_id)
                if order and order["status"] == "filled" and order["filled_avg_price"]:
                    reason = _stop_exit_reason(trade)
                    logger.warning(
                        "BROKER STOP FILLED | %s @ $%.2f → closing journal trade (%s)",
                        symbol, order["filled_avg_price"], reason,
                    )
                    log_trade_close(trade["id"], order["filled_avg_price"], reason)
                    continue
            try:
                entry_dt = datetime.fromisoformat(trade.get("entry_ts", ""))
                if entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                age_hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
            except (ValueError, TypeError):
                age_hours = 999
            if age_hours < 24:
                logger.warning(
                    "ORPHAN CHECK | %s in journal but not in Alpaca — "
                    "opened %.1fh ago, may be fill delay. Will re-check next run.",
                    symbol, age_hours,
                )
                continue
            logger.warning(
                "ORPHAN CLOSE | %s not in Alpaca after %.1fh — closing at entry price "
                "(P&L is an ESTIMATE, no fill data).",
                symbol, age_hours,
            )
            log_trade_close(trade["id"], trade["entry_price"], "orphan_close")
            continue

        # S1: make sure a protective stop is resting at the broker for this position.
        # Software stop check stays as a FALLBACK: it fires only if no live broker
        # stop is actually resting (flag off, submit failed, fractional-only position,
        # or the order filled/was cancelled).
        has_broker_stop = _ensure_broker_stop(alpaca, trade)

        pos           = positions[symbol]
        current_price = pos["current_price"]
        stop_price    = trade["stop_price"]
        if current_price <= stop_price and not has_broker_stop:
            reason = _stop_exit_reason(trade)
            logger.warning("STOP TRIGGERED (software) | %s $%.2f <= $%.2f",
                           symbol, current_price, stop_price)
            order = alpaca.close_position(symbol, reason=reason)
            if order:
                log_trade_close(trade["id"], current_price, reason)
                continue

        pnl_pct = pos["unrealized_plpc"]
        if pnl_pct >= config.TRAILING_STOP_TRIGGER:
            # A7 (audit): trail off the LIVE price, not entry_price*(1+plpc) — plpc is
            # computed by Alpaca against avg_entry (which includes top-ups), so the old
            # mixed-base math produced wrong ratchet levels on topped-up positions.
            # The ratchet only ever moves the stop UP, so peak-tracking still emerges.
            new_stop = round(current_price * (1 - config.TRAILING_STOP_PCT), 2)
            if new_stop > stop_price:
                updates = {"stop_price": new_stop, "stop_ratcheted": True}
                # S1: move the resting broker stop with the ratchet.
                if has_broker_stop:
                    replaced = alpaca.replace_stop_order(trade["stop_order_id"], new_stop)
                    if replaced:
                        updates["stop_order_id"] = replaced["id"]
                    else:
                        logger.warning("Trailing ratchet: broker replace failed for %s — "
                                       "journal stop updated, software fallback covers", symbol)
                update_open_trade(trade["id"], updates)
                logger.info("Trailing stop → %s $%.2f (persisted%s)",
                            symbol, new_stop, " + broker" if has_broker_stop else "")

        try:
            entry_dt = datetime.fromisoformat(trade.get("entry_ts", ""))
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            calendar_days = (datetime.now(timezone.utc) - entry_dt).days
            trading_days = int(calendar_days * 5 / 7)
        except (TypeError, ValueError):
            trading_days = 0
        if trading_days > 20 and pnl_pct < 0.03 and not trade.get("time_stop_flagged"):
            logger.warning(
                "TIME STOP FLAG | %s held %d trading days, P&L=%.1f%% — forcing thesis review",
                symbol, trading_days, pnl_pct * 100,
            )
            update_open_trade(trade["id"], {"time_stop_flagged": True})


# ── Position review (thesis-driven hold / trim / exit) ───────────────────────

def _safe_review_decision(review: dict, fallback_conviction) -> tuple[str, int]:
    """Normalize LLM review output before it can affect execution."""
    action = str(review.get("action", "hold")).strip().lower()
    if action not in {"hold", "trim", "exit"}:
        logger.warning("Unknown review action '%s' — defaulting to HOLD", action)
        action = "hold"

    try:
        conviction = int(review.get("conviction", fallback_conviction))
    except (TypeError, ValueError):
        logger.warning("Invalid review conviction '%s' — using previous conviction",
                       review.get("conviction"))
        try:
            conviction = int(fallback_conviction)
        except (TypeError, ValueError):
            conviction = 5
    conviction = max(1, min(10, conviction))

    if action == "trim" and conviction < 3:
        logger.info("Review trim with conviction=%d converted to EXIT", conviction)
        action = "exit"

    return action, conviction


def _review_target_pct(conviction: int) -> float:
    """
    Convert a review conviction into target exposure.
    Existing buy sizing only maps 7-10; review trims need lower-conviction
    targets so weakened-but-not-broken theses can become smaller holdings.
    """
    if conviction >= config.MIN_CONVICTION_SCORE:
        size_map = config.CONVICTION_SIZE_MAP
        capped   = min(conviction, max(size_map.keys()))
        floored  = max(capped, min(size_map.keys()))
        return size_map.get(floored, 0.03)
    if conviction >= 5:
        return 0.02
    if conviction >= 3:
        return 0.01
    return 0.0

def review_open_positions(
    alpaca: AlpacaClient,
    fetcher: DataFetcher,
    dry_run: bool,
    symbols: set[str] | None = None,
    regime: str = "bull",
    vix_regime: str = "normal",
):
    """
    Re-analyse every open position and decide hold / trim / exit based on whether
    the original entry thesis is still intact. Runs before the screener each pipeline.
    Market-closed trim/exit actions are queued for audit but NOT executed; the next
    run re-reviews from scratch rather than blindly executing stale queues.
    """
    open_trades = get_open_trades()
    if symbols:
        open_trades = [t for t in open_trades if t.get("symbol") in symbols]
    if not open_trades:
        return

    logger.info("── Position Review: %d open trade(s) ──", len(open_trades))

    market_open      = alpaca.is_market_open()
    ok_to_trade      = market_open and alpaca.minutes_to_close() >= config.MARKET_CLOSE_BUFFER
    account          = alpaca.get_account()
    portfolio_value  = account["portfolio_value"]
    alpaca_positions = {p["symbol"]: p for p in alpaca.get_positions()}

    for trade in open_trades:
        symbol   = trade["symbol"]
        trade_id = trade["id"]

        # ── Fetch current data (same path as analyse_symbol) ──────────────────
        bars  = fetcher.get_ohlcv(symbol, days=60)
        news  = fetcher.get_news(symbol, days=3)
        quote = fetcher.get_quote(symbol)

        if not bars or not quote:
            logger.warning("Position review: no data for %s — skipping", symbol)
            continue

        current_price = quote["price"]
        live_pos      = alpaca_positions.get(symbol)
        unrealized_plpc = live_pos["unrealized_plpc"] if live_pos else None

        # ── Re-run full analyst + debate pipeline ──────────────────────────────
        tech = technical_agent.analyse(symbol, bars)
        fund = fundamental_agent.analyse(symbol)
        sent = sentiment_agent.analyse(symbol, news)

        bull_r1 = bull_agent.opening_argument(symbol, tech, fund, sent)
        bear_r1 = bear_agent.opening_argument(symbol, tech, fund, sent, bull_r1)
        bull_r2 = bull_agent.rebuttal(symbol, bull_r1, bear_r1)
        bear_r2 = bear_agent.rebuttal(symbol, bear_r1, bull_r2)

        original_debate = get_debate_by_id(trade.get("debate_id", ""))

        # ── Call position reviewer ─────────────────────────────────────────────
        review = reviewer_agent.review(
            symbol=symbol,
            trade=trade,
            original_debate=original_debate,
            tech=tech, fund=fund, sent=sent,
            bull_r1=bull_r1, bear_r1=bear_r1,
            bull_r2=bull_r2, bear_r2=bear_r2,
            current_price=current_price,
            unrealized_plpc=unrealized_plpc,
        )

        action, conviction = _safe_review_decision(
            review, trade.get("conviction", 5)
        )

        # Fix 2 (proper): Consecutive weakened exit.
        # Track how many consecutive reviews return weakened/broken thesis — reset to 0
        # the moment thesis recovers to intact. Force exit at 3 consecutive.
        # This is more precise than counting total trims: a position that recovered once
        # and then weakened again starts its counter fresh, not from historical trims.
        thesis_status_raw = review.get("thesis_status", "intact")
        consec_weakened = int(trade.get("consecutive_weakened_count", 0))
        if thesis_status_raw in ("weakened", "broken"):
            consec_weakened += 1
        else:
            consec_weakened = 0  # thesis recovered — counter resets

        # C13-EXIT (gated by WEAKENED_EXIT_REQUIRE_PRICE, default on): require PRICE
        # confirmation before a thesis-"weakened" streak forces an exit. A daily-bar
        # backtest showed the old unconditional rule cut RKLB at +5.4% (it ran to
        # +15.3%) and DOCS at +9.6% (it ran to +21.5%) while both were still winning —
        # a thesis-only weakened streak dumped the fund's biggest winners before the
        # trailing stop could engage. A "broken" thesis still exits unconditionally (a
        # real signal); a "weakened" streak only forces exit when price confirms
        # weakness: below entry OR the EMA10/30 trend (the same trend the debate uses,
        # with a 2% deadband that avoids single-day whipsaw — agents/technical.py:_trend)
        # is no longer "up". When price is strong the position is left to the persisted
        # trailing stop (C2); the streak counter is NOT reset, so the exit fires the
        # moment the trend rolls over. Set WEAKENED_EXIT_REQUIRE_PRICE=False to restore
        # the legacy unconditional exit.
        if consec_weakened >= 3 and action in ("trim", "hold"):
            require_price = getattr(config, "WEAKENED_EXIT_REQUIRE_PRICE", True)
            entry_px      = trade.get("entry_price", current_price)
            trend         = (tech.get("indicators") or {}).get("trend", "unknown")
            price_weak    = current_price < entry_px or trend != "up"
            if not require_price or thesis_status_raw == "broken" or price_weak:
                logger.warning(
                    "CONSECUTIVE WEAKENED EXIT | %s thesis=%s for %d reviews "
                    "(trend=%s, px=%.2f vs entry=%.2f) → EXIT",
                    symbol, thesis_status_raw, consec_weakened, trend,
                    current_price, entry_px,
                )
                action = "exit"
                review = dict(review)
                review["exit_reason"] = (
                    f"consecutive_weakened_exit: thesis={thesis_status_raw} for "
                    f"{consec_weakened} reviews, price confirmed weak "
                    f"(px={current_price:.2f}, entry={entry_px:.2f}, trend={trend})"
                )
            else:
                logger.info(
                    "WEAKENED-EXIT SUPPRESSED | %s weakened x%d but price strong "
                    "(px=%.2f >= entry=%.2f, trend=up) — holding; trailing stop manages",
                    symbol, consec_weakened, current_price, entry_px,
                )

        # C12: clean up DEAD remnants. A position trimmed below MIN_SLOT_PCT whose thesis
        # has weakened/broken for >=2 consecutive reviews is dead weight (e.g. ARM) — exit
        # to free the cash/slot. Intact remnants are left alone (C7 rebuys them to full).
        # Lower threshold (2) than the meaningful-position exit (3): a sub-slot position
        # has negligible upside convexity, so the cost of an early exit is low.
        pos_pct = trade.get("position_pct", 0)
        if (getattr(config, "REMNANT_FORCE_EXIT", True)
                and action in ("trim", "hold")
                and 0 < pos_pct < getattr(config, "MIN_SLOT_PCT", 0.03)
                and thesis_status_raw in ("weakened", "broken")
                and consec_weakened >= 2):
            logger.warning(
                "REMNANT CLEANUP | %s remnant %.1f%% thesis=%s x%d → EXIT",
                symbol, pos_pct * 100, thesis_status_raw, consec_weakened,
            )
            action = "exit"
            review = dict(review)
            review["exit_reason"] = (
                f"remnant_cleanup: {pos_pct*100:.1f}% position, thesis="
                f"{thesis_status_raw} for {consec_weakened} consecutive reviews"
            )

        # ── Regime-conditional trim discipline (TRIM-DISC) ───────────────────
        # Problem: the reviewer re-trims winners on STATIC TYPE-B concerns (debt,
        # valuation, P/E) that were already known at entry — e.g. AMD's 6.0 debt/equity
        # got trimmed 4x in 2 days while the stock ran +26%. That bleeds the winners a
        # momentum book depends on (Shefrin-Statman disposition effect; trim drag
        # measured at ~1.1pp of NAV).
        #
        # Fix: in a CALM bull regime, do NOT trim a green, technically-intact position
        # on a merely-"weakened" thesis. Let the trailing/ATR/hard stops be the downside
        # control. A trim is still allowed when ANY of these hold:
        #   - the thesis is BROKEN (not just weakened) — real deterioration,
        #   - price confirms weakness (below entry OR EMA10/30 trend no longer "up"),
        #   - the weakened read PERSISTS across >=2 consecutive reviews (hysteresis —
        #     kills the intraday oscillation from multiple daily + sentinel reviews),
        #   - the regime is RISK-OFF (caution/bear OR elevated/high VIX) — this is when
        #     momentum crashes happen (Daniel-Moskowitz), so de-risking is warranted.
        # Set REGIME_CONDITIONAL_TRIMS=False to restore legacy (trim on every weakened).
        if getattr(config, "REGIME_CONDITIONAL_TRIMS", True) and action == "trim":
            risk_off       = regime in ("caution", "bear") or vix_regime in ("elevated_vix", "high_vix")
            entry_px       = trade.get("entry_price", current_price)
            trend          = (tech.get("indicators") or {}).get("trend", "unknown")
            price_strong   = current_price >= entry_px and trend == "up"
            thesis_broken  = thesis_status_raw == "broken"
            persistent     = consec_weakened >= 2
            if (not risk_off) and price_strong and (not thesis_broken) and (not persistent):
                logger.info(
                    "TRIM SUPPRESSED | %s calm-bull, price strong (px=%.2f >= entry=%.2f, "
                    "trend=up), thesis=%s consec_weakened=%d — holding full size; "
                    "stops are the downside control",
                    symbol, current_price, entry_px, thesis_status_raw, consec_weakened,
                )
                action = "hold"
                review = dict(review)
                review["trim_suppressed"] = True

        logger.info(
            "Position Review | %s → %s | conviction=%d | thesis=%s | %s",
            symbol, action.upper(), conviction,
            review.get("thesis_status"), review.get("rationale", "")[:80],
        )

        # ── Log review record ──────────────────────────────────────────────────
        log_position_review(
            trade_id=trade_id,
            symbol=symbol,
            action=action,
            conviction=conviction,
            thesis_status=review.get("thesis_status"),
            rationale=review.get("rationale"),
            original_thesis_check=review.get("original_thesis_check"),
            key_risk_to_monitor=review.get("key_risk_to_monitor"),
            trim_reason=review.get("trim_reason"),
            exit_reason=review.get("exit_reason"),
            current_bull_case=(
                f"R1 Bull({bull_r1.get('conviction')}): {bull_r1.get('thesis', '')} | "
                f"R2 Bull({bull_r2.get('conviction')}): {bull_r2.get('final_thesis', '')}"
            ),
            current_bear_case=(
                f"R1 Bear({bear_r1.get('conviction')}): {bear_r1.get('thesis', '')} | "
                f"R2 Bear({bear_r2.get('conviction')}): {bear_r2.get('final_thesis', '')}"
            ),
        )

        # ── Execute decision ───────────────────────────────────────────────────
        exit_reason_str = review.get("exit_reason", "thesis_broken")

        if action == "hold":
            update_open_trade(trade_id, {
                "last_review_ts":            datetime.now(timezone.utc).isoformat(),
                "last_review_conviction":    conviction,
                "thesis_status":             review.get("thesis_status"),
                "key_risk":                  review.get("key_risk_to_monitor"),
                "queued_action":             None,
                "time_stop_flagged":         False,
                "consecutive_weakened_count": consec_weakened,
            })
            clear_queued_action(trade_id)

        elif action == "exit":
            if dry_run:
                logger.info("DRY RUN — would EXIT %s | %s", symbol, exit_reason_str)
                continue

            if not ok_to_trade:
                logger.info("Market closed — EXIT queued for %s (will re-review next run)", symbol)
                set_queued_action(trade_id, "exit", exit_reason_str)
                update_open_trade(trade_id, {
                    "last_review_ts":            datetime.now(timezone.utc).isoformat(),
                    "thesis_status":             review.get("thesis_status"),
                    "time_stop_flagged":         False,
                    "consecutive_weakened_count": consec_weakened,
                })
                continue

            order = alpaca.close_position(symbol, reason=exit_reason_str)
            if order:
                log_trade_close(trade_id, current_price, exit_reason_str)
                logger.info("EXIT executed | %s @ $%.2f | %s", symbol, current_price, exit_reason_str)

        elif action == "trim":
            if dry_run:
                logger.info("DRY RUN — would TRIM %s | %s",
                            symbol, review.get("trim_reason", "conviction reduced"))
                continue

            if not ok_to_trade:
                logger.info("Market closed — TRIM queued for %s (will re-review next run)", symbol)
                set_queued_action(trade_id, "trim", review.get("trim_reason", "conviction_reduced"))
                update_open_trade(trade_id, {
                    "last_review_ts":            datetime.now(timezone.utc).isoformat(),
                    "last_review_conviction":    conviction,
                    "thesis_status":             review.get("thesis_status"),
                    "time_stop_flagged":         False,
                    "consecutive_weakened_count": consec_weakened,
                })
                continue

            # Conviction-resize: map new conviction to target position %
            target_pct = _review_target_pct(conviction)

            target_mkt_val  = portfolio_value * target_pct
            current_qty_act = live_pos["qty"]         if live_pos else trade["qty"]
            current_mkt_val = live_pos["market_value"] if live_pos else (current_qty_act * current_price)

            excess_value = current_mkt_val - target_mkt_val
            if excess_value <= 0:
                logger.info("TRIM %s — position already at or below target size, treating as hold",
                            symbol)
                update_open_trade(trade_id, {
                    "last_review_ts":            datetime.now(timezone.utc).isoformat(),
                    "last_review_conviction":    conviction,
                    "thesis_status":             review.get("thesis_status"),
                    "key_risk":                  review.get("key_risk_to_monitor"),
                    "queued_action":             None,
                    "time_stop_flagged":         False,
                    "consecutive_weakened_count": consec_weakened,
                })
                clear_queued_action(trade_id)
                continue

            trim_qty = round(excess_value / current_price, 4)
            new_qty  = round(current_qty_act - trim_qty, 4)

            if trim_qty < 0.01:
                logger.info("TRIM %s — trim qty %.4f below minimum, treating as hold",
                            symbol, trim_qty)
                update_open_trade(trade_id, {
                    "last_review_ts":            datetime.now(timezone.utc).isoformat(),
                    "last_review_conviction":    conviction,
                    "thesis_status":             review.get("thesis_status"),
                    "key_risk":                  review.get("key_risk_to_monitor"),
                    "queued_action":             None,
                    "time_stop_flagged":         False,
                    "consecutive_weakened_count": consec_weakened,
                })
                clear_queued_action(trade_id)
                continue

            # S1: shrink the resting broker stop FIRST — Alpaca holds the full qty
            # against the open sell stop, so the trim sell would otherwise be
            # rejected for insufficient available shares.
            if getattr(config, "BROKER_NATIVE_STOPS", False) and trade.get("stop_order_id"):
                replaced = alpaca.replace_stop_order(
                    trade["stop_order_id"], trade.get("stop_price", 0), new_qty)
                if replaced:
                    update_open_trade(trade_id, {"stop_order_id": replaced["id"]})
                else:
                    # Could not shrink — cancel so the trim can execute; reconciler
                    # re-places a correctly-sized stop next run.
                    alpaca.cancel_stop_orders(symbol)
                    update_open_trade(trade_id, {"stop_order_id": ""})

            order = alpaca.submit_market_order(
                symbol=symbol, qty=trim_qty, side="sell",
                reason=(f"review_trim | conviction={conviction} | "
                        f"{review.get('trim_reason', '')}"),
            )
            if order:
                # A5 (audit): book realized P&L on the trimmed shares against the
                # position's basis so trim proceeds stop vanishing from the ledger.
                basis = (live_pos or {}).get("avg_entry") or trade.get("entry_price", 0)
                log_trade_trim(trade_id, trim_qty, new_qty, current_price,
                               review.get("trim_reason", ""), basis=basis)
                update_open_trade(trade_id, {
                    "qty":                       new_qty,
                    "last_review_ts":            datetime.now(timezone.utc).isoformat(),
                    "last_review_conviction":    conviction,
                    "thesis_status":             review.get("thesis_status"),
                    "key_risk":                  review.get("key_risk_to_monitor"),
                    "queued_action":             None,
                    "time_stop_flagged":         False,
                    "consecutive_weakened_count": consec_weakened,
                })
                clear_queued_action(trade_id)
                logger.info(
                    "TRIM executed | %s sold %.4f shares @ $%.2f | remaining qty=%.4f",
                    symbol, trim_qty, current_price, new_qty,
                )


# ── Per-symbol analysis ───────────────────────────────────────────────────────

def _pre_debate_gate(candidate, open_positions: list[dict]) -> tuple[bool, str]:
    """
    C18: would a BUY be structurally impossible for this candidate regardless of the
    debate outcome? Returns (would_gate, reason). Mirrors deterministic risk-manager
    caps so the (expensive) debate can be skipped when the answer is already 'no'.
    Note: a same-sector rotation could still admit a saturated-sector name, so in
    'watch' mode we only LOG this and never skip — the scorecard reveals over-firing
    before we ever enforce.
    """
    min_size = min(config.CONVICTION_SIZE_MAP.values())  # smallest possible new entry
    deployed = sum(p.get("position_pct", 0) for p in open_positions)
    if config.MAX_PORTFOLIO_EXPOSURE - deployed < 0.04:
        return True, (f"exposure_maxed (deployed={deployed*100:.1f}%, "
                      f"cap={config.MAX_PORTFOLIO_EXPOSURE*100:.0f}%)")
    sector = (getattr(candidate, "signals", {}) or {}).get("sector")
    if sector:
        sector_deployed = sum(
            p.get("position_pct", 0) for p in open_positions
            if p.get("sector", "") == sector
        )
        if sector_deployed + min_size > config.MAX_SECTOR_PCT:
            return True, (f"sector_saturated ({sector} at {sector_deployed*100:.1f}%, "
                          f"cap={config.MAX_SECTOR_PCT*100:.0f}%)")
    return False, ""


def _risk_category(proposal) -> str:
    reason = (proposal.reason or "").lower()
    if proposal.action == "buy":
        return "approved"
    if proposal.action == "hold":
        return "already_holding"
    if "conviction" in reason:
        return "conviction"
    if "correlation tournament" in reason:
        return "correlation"
    if "sector cap" in reason:
        return "sector_cap"
    if "portfolio exposure" in reason:
        return "portfolio_exposure"
    if "max positions" in reason:
        return "max_positions"
    if proposal.action in {"skip", "watch"}:
        return "pm_decision"
    return "other"


def _count_llm_fallbacks(diag: dict | None, tech: dict, fund: dict, sent: dict,
                         pm: dict) -> None:
    """Observability (audit §7.4): tally agent fallbacks so a degraded run is
    distinguishable from a genuine no-trade analysis in the run record."""
    if diag is None:
        return
    for name, result in (("technical", tech), ("fundamental", fund), ("sentiment", sent)):
        if "unavailable" in str(result.get("rationale", "")).lower():
            diag["analyst_fallbacks"] = diag.get("analyst_fallbacks", 0) + 1
            diag.setdefault("fallback_agents", []).append(name)
    if pm.get("deciding_factor") == "error":
        diag["pm_failures"] = diag.get("pm_failures", 0) + 1


def analyse_symbol(
    symbol: str,
    fetcher: DataFetcher,
    portfolio_value: float,
    open_positions: list[dict],
    dry_run: bool,
    ok_to_trade: bool,
    alpaca: AlpacaClient,
    regime: str,
    vix_regime: str,
    llm_diag: dict | None = None,
) -> bool:
    logger.info("── Analysing %s ──", symbol)

    bars  = fetcher.get_ohlcv(symbol, days=60)
    news  = fetcher.get_news(symbol, days=3)
    quote = fetcher.get_quote(symbol)

    if not bars or not quote:
        logger.warning("No data for %s — skipping", symbol)
        return False

    current_price = quote["price"]

    # ── Analysts ──────────────────────────────────────────────────────────────
    tech = technical_agent.analyse(symbol, bars)
    fund = fundamental_agent.analyse(symbol)
    sent = sentiment_agent.analyse(symbol, news)

    logger.info("%s analysts → tech=%s(%d) fund=%s(%d) sent=%s(%d)",
                symbol,
                tech["signal"], tech["strength"],
                fund["signal"], fund["strength"],
                sent["signal"], sent["strength"])

    # ── Debate Round 1 ────────────────────────────────────────────────────────
    bull_r1 = bull_agent.opening_argument(symbol, tech, fund, sent)
    bear_r1 = bear_agent.opening_argument(symbol, tech, fund, sent, bull_r1)

    logger.info("%s R1 → bull=%d bear=%d",
                symbol, bull_r1["conviction"], bear_r1["conviction"])

    # ── Debate Round 2 ────────────────────────────────────────────────────────
    bull_r2 = bull_agent.rebuttal(symbol, bull_r1, bear_r1)
    bear_r2 = bear_agent.rebuttal(symbol, bear_r1, bull_r2)

    logger.info("%s R2 → bull=%d (%s) bear=%d (%s)",
                symbol,
                bull_r2["conviction"], bull_r2.get("conviction_change", ""),
                bear_r2["conviction"], bear_r2.get("conviction_change", ""))

    # ── Portfolio Manager verdict ─────────────────────────────────────────────
    pm = pm_agent.decide(
        symbol, bull_r1, bear_r1, bull_r2, bear_r2,
        tech, fund, sent,
    )

    logger.info("%s PM verdict → action=%s conviction=%d | %s",
                symbol, pm["action"], pm["final_conviction"], pm["verdict"])

    _count_llm_fallbacks(llm_diag, tech, fund, sent, pm)

    # Enrich pm_verdict with R2 debate scores so the risk manager can apply
    # bear spread shading (Fix 4): a contested debate (high bear R2 vs low bull R2)
    # warrants a smaller initial position even at the same final conviction.
    pm["bull_r2_conviction"] = bull_r2.get("conviction", pm.get("final_conviction", 7))
    pm["bear_r2_conviction"] = bear_r2.get("conviction", 0)

    # ── Options shadow logger (Phase 1 — inert unless OPTIONS_ENABLED=True) ───
    # Completely skipped when flag is off; never affects the risk/execution path.
    if getattr(config, "OPTIONS_ENABLED", False) and \
            getattr(config, "OPTIONS_MODE", "shadow") == "shadow":
        _options_shadow_log(symbol, pm, tech, fetcher, alpaca, portfolio_value)

    # ── Risk manager ──────────────────────────────────────────────────────────
    proposal = risk_agent.evaluate(
        symbol=symbol,
        pm_verdict=pm,
        current_price=current_price,
        bars=bars,
        portfolio_value=portfolio_value,
        open_positions=open_positions,
        regime=regime,
        vix_regime=vix_regime,
        fetcher=fetcher,
    )
    log_risk_decision(
        symbol=symbol,
        action=proposal.action,
        reason=proposal.reason,
        conviction=proposal.conviction,
        rotate_from=proposal.rotate_from,
        position_usd=proposal.position_usd,
        shares=proposal.shares,
        entry_price=proposal.entry_price,
        stop_price=proposal.stop_price,
        stop_pct=proposal.stop_pct,
        sector=proposal.sector,
        correlation=proposal.correlation,
        rotation_reason=proposal.rotation_reason,
        pm_action=pm.get("action"),
        pm_verdict=pm.get("verdict"),
        risk_category=_risk_category(proposal),
    )

    # ── Log full debate to journal ────────────────────────────────────────────
    debate_summary = (
        f"R1 Bull({bull_r1['conviction']}): {bull_r1.get('thesis','')} | "
        f"R1 Bear({bear_r1['conviction']}): {bear_r1.get('thesis','')} | "
        f"R2 Bull({bull_r2['conviction']}): {bull_r2.get('final_thesis','')} | "
        f"R2 Bear({bear_r2['conviction']}): {bear_r2.get('final_thesis','')}"
    )
    debate_id = log_debate(
        symbol=symbol,
        bull_case=debate_summary,
        bear_case=f"Primary risk: {bear_r1.get('primary_risk','')} | "
                  f"Unresolved: {bear_r2.get('unresolved_risks',[])}",
        bull_score=bull_r2["conviction"],
        bear_score=bear_r2["conviction"],
        final_conviction=pm["final_conviction"],
        decision=pm["action"],
    )

    # ── Execute ───────────────────────────────────────────────────────────────
    if proposal.action != "buy":
        logger.info("%s → %s | %s", symbol, proposal.action.upper(), proposal.reason)
        return False

    if dry_run:
        logger.info("DRY RUN — would BUY %s: %.4f shares @ $%.2f | stop=$%.2f | risk: %s",
                    symbol, proposal.shares, current_price,
                    proposal.stop_price, proposal.key_risk)
        return False

    if not ok_to_trade:
        logger.info("MARKET CLOSED — %s queued for next open", symbol)
        return False

    if proposal.rotate_from:
        rotate_trade = next(
            (t for t in open_positions if t.get("symbol") == proposal.rotate_from),
            None,
        )
        if not rotate_trade:
            logger.warning(
                "Rotation target %s not found — skipping %s to avoid stacking correlated exposure",
                proposal.rotate_from, symbol,
            )
            return False

        rotate_pos = alpaca.get_position(proposal.rotate_from)
        rotate_price = (
            rotate_pos["current_price"]
            if rotate_pos else rotate_trade.get("entry_price", current_price)
        )
        if dry_run:
            logger.info(
                "DRY RUN — would ROTATE %s → %s | %s",
                proposal.rotate_from, symbol, proposal.rotation_reason,
            )
            return False

        close_order = alpaca.close_position(
            proposal.rotate_from,
            reason=f"correlation_rotation_to_{symbol}",
        )
        if not close_order:
            logger.warning("Rotation close failed for %s — skipping %s",
                           proposal.rotate_from, symbol)
            return False
        log_trade_close(
            rotate_trade["id"], rotate_price,
            f"correlation_rotation_to_{symbol}",
        )
        logger.info("ROTATION | closed %s before buying %s | %s",
                    proposal.rotate_from, symbol, proposal.rotation_reason)

    # C7: detect a remnant top-up — the symbol is already held below MIN_SLOT_PCT. We
    # grow the EXISTING position record rather than opening a duplicate open_trade.
    min_slot_pct = getattr(config, "MIN_SLOT_PCT", 0.03)
    topup_trade = None
    if getattr(config, "REMNANT_REBUY", False):
        topup_trade = next(
            (t for t in open_positions
             if t.get("symbol") == symbol and 0 < t.get("position_pct", 0) < min_slot_pct),
            None,
        )

    # ── DUP-GUARD (broker-level idempotency) ──────────────────────────────────
    # Fire ONLY on the dangerous case: Alpaca holds this symbol but the JOURNAL has no
    # record of it. That untracked-duplicate state (from the old push-race, a fill
    # delay, or a pre-existing paper position) is what let MS/SMCI accumulate ~2x the
    # intended shares. We refuse to stack a second buy on a position we don't even know
    # we hold.
    #
    # Crucially, this does NOT limit deliberately ADDING to a position the journal is
    # tracking. A remnant top-up (C7) or any future "scale into a winner" add passes
    # straight through, because the journal already lists the symbol. The guard only
    # blocks buying something the broker holds that our own books never recorded.
    journal_has_symbol = any(t.get("symbol") == symbol for t in open_positions)
    live_broker_pos = alpaca.get_position(symbol)
    if live_broker_pos and not journal_has_symbol:
        try:
            broker_qty = float(live_broker_pos.get("qty", 0))
        except (TypeError, ValueError):
            broker_qty = 0.0
        logger.warning(
            "DUP-GUARD | %s held at broker (%.4f sh) but ABSENT from the journal — "
            "refusing BUY to avoid doubling an untracked position. Reconcile the journal "
            "with the paper account for this symbol.",
            symbol, broker_qty,
        )
        log_risk_decision(symbol=symbol, action="skip", conviction=proposal.conviction,
                          reason="dup_guard: held at broker but not in journal")
        return False

    order = alpaca.submit_market_order(
        symbol=symbol, qty=proposal.shares, side="buy",
        reason=f"conviction={proposal.conviction} | {pm.get('verdict','')}",
    )

    if order:
        # A6 (audit): reconcile to the actual FILL, not the pre-order quote. The quote
        # was logged as entry_price, so slippage was invisible and stop math ran off a
        # price the fund never paid. Brief poll — market orders fill near-instantly.
        fill_price, fill_qty = _await_fill(alpaca, order.get("id"))
        entry_price = fill_price or current_price
        filled_qty  = fill_qty or proposal.shares
        if fill_price and abs(fill_price - current_price) / current_price > 0.001:
            logger.info("Fill reconcile | %s quote=$%.2f fill=$%.2f (%.2f%% slippage)",
                        symbol, current_price, fill_price,
                        (fill_price - current_price) / current_price * 100)
        # Keep the stop's percentage distance anchored to the actual fill.
        stop_price = round(entry_price * (1 - proposal.stop_pct / 100), 2) \
            if proposal.stop_pct else proposal.stop_price

        if topup_trade:
            # Grow the existing remnant: add shares, keep the original entry/stop/debate.
            new_qty = round(topup_trade.get("qty", 0) + filled_qty, 4)
            updates = {
                "qty":             new_qty,
                "last_topup_ts":   datetime.now(timezone.utc).isoformat(),
                "last_topup_qty":  filled_qty,
                "conviction":      proposal.conviction,
            }
            # S1: resize the resting stop to cover the grown position.
            if getattr(config, "BROKER_NATIVE_STOPS", False):
                sid = topup_trade.get("stop_order_id")
                if sid:
                    replaced = alpaca.replace_stop_order(
                        sid, topup_trade.get("stop_price", stop_price), new_qty)
                    if replaced:
                        updates["stop_order_id"] = replaced["id"]
            update_open_trade(topup_trade["id"], updates)
            logger.info("✅ REMNANT TOP-UP | %s +%.4f sh → qty=%.4f @ $%.2f (conviction=%d)",
                        symbol, filled_qty, new_qty, entry_price, proposal.conviction)
        else:
            stop_order_id = ""
            if getattr(config, "BROKER_NATIVE_STOPS", False):
                placed = alpaca.submit_stop_order(
                    symbol, filled_qty, stop_price,
                    reason=f"protective_stop conviction={proposal.conviction}",
                )
                if placed:
                    stop_order_id = placed["id"]
                else:
                    logger.warning("S1: protective stop submit failed for %s — "
                                   "software stop fallback active", symbol)
            log_trade_open(
                symbol=symbol, side="buy", qty=filled_qty,
                entry_price=entry_price, stop_price=stop_price,
                conviction=proposal.conviction, debate_id=debate_id,
                key_risk=proposal.key_risk, portfolio_value=portfolio_value,
                sector=proposal.sector, stop_order_id=stop_order_id,
            )
            logger.info("✅ ORDER FILLED | %s %.4f @ $%.2f stop=$%.2f%s",
                        symbol, filled_qty, entry_price, stop_price,
                        " (broker stop resting)" if stop_order_id else "")
        return True

    return False


def _await_fill(alpaca: AlpacaClient, order_id: str | None,
                attempts: int = 5, delay_s: float = 1.0) -> tuple[float | None, float | None]:
    """Poll an order briefly for its fill price/qty. Returns (price, qty) or (None, None)."""
    if not order_id:
        return None, None
    for _ in range(attempts):
        info = alpaca.get_order(order_id)
        if info and info["status"] == "filled" and info["filled_avg_price"]:
            return info["filled_avg_price"], info["filled_qty"] or None
        time.sleep(delay_s)
    logger.warning("Fill poll: order %s not confirmed filled after %.0fs — "
                   "falling back to quote price", order_id, attempts * delay_s)
    return None, None


# ── Options shadow logger (Phase 0/1 — never runs unless OPTIONS_ENABLED=True) ──

def _options_shadow_log(
    symbol: str,
    pm: dict,
    tech: dict,
    fetcher,
    alpaca,
    portfolio_value: float,
) -> None:
    """
    Phase 1 shadow: when the debate produces a buy with conviction >= OPTION_MIN_CONVICTION
    and a named catalyst, resolve the would-be call debit spread and log it alongside
    the real equity trade. NO orders are ever submitted here.

    This function is only reached when OPTIONS_ENABLED=True AND OPTIONS_MODE="shadow".
    With OPTIONS_ENABLED=False (the default) the caller never invokes it.
    """
    try:
        from core.options_selector import select_call_debit_spread

        min_conv = getattr(config, "OPTION_MIN_CONVICTION", 8)
        action   = pm.get("action", "skip")
        conv     = int(pm.get("final_conviction", 0))
        catalyst = pm.get("deciding_factor") or pm.get("key_risk_to_monitor") or ""

        # Only log for genuine buys at the conviction threshold with a named catalyst.
        if action != "buy" or conv < min_conv:
            return
        if not catalyst or catalyst in ("error", "unknown", ""):
            logger.debug("options_shadow: skipping %s — no named catalyst in PM verdict", symbol)
            return

        # Get the current spot price for contract selection.
        quote = fetcher.get_quote(symbol)
        if not quote:
            return
        spot = float(quote["price"])

        proposal = select_call_debit_spread(
            symbol=symbol,
            spot=spot,
            portfolio_value=portfolio_value,
            conviction=conv,
            catalyst=catalyst,
            fetcher=fetcher,
            alpaca_client=alpaca,
        )

        if proposal is None:
            logger.info("options_shadow | %s — no valid spread found", symbol)
            log_would_have_traded({
                "symbol": symbol, "structure": "call_debit_spread",
                "conviction": conv, "catalyst": catalyst,
                "veto_reason": "no_valid_spread_found",
            })
            return

        # Serialize the proposal for logging (dataclasses → dicts).
        def _leg_dict(leg):
            if leg is None:
                return None
            return {
                "occ_symbol":  leg.occ_symbol,
                "strike":      leg.strike,
                "expiry":      leg.expiry,
                "option_type": leg.option_type,
                "greeks": {
                    "delta": leg.greeks.delta, "gamma": leg.greeks.gamma,
                    "theta": leg.greeks.theta, "vega":  leg.greeks.vega,
                    "iv":    leg.greeks.iv,    "price": leg.greeks.price,
                },
                "market_mid": leg.market_mid,
            }

        record = {
            "symbol":           proposal.underlying,
            "structure":        proposal.structure,
            "conviction":       proposal.conviction,
            "catalyst":         proposal.catalyst,
            "veto_reason":      proposal.veto_reason,
            "long_leg":         _leg_dict(proposal.long_leg),
            "short_leg":        _leg_dict(proposal.short_leg),
            "net_debit":        proposal.net_debit,
            "max_loss":         proposal.max_loss,
            "max_profit":       proposal.max_profit,
            "breakeven":        proposal.breakeven,
            "qty":              proposal.qty,
            "total_premium":    proposal.total_premium,
            "pct_of_portfolio": proposal.pct_of_portfolio,
            "net_greeks":       proposal.net_greeks,
            "dte":              proposal.dte,
            "expiry_date":      proposal.expiry_date,
            "spot_at_log":      spot,
        }
        log_would_have_traded(record)

        if proposal.veto_reason:
            logger.info("options_shadow | %s → VETOED: %s", symbol, proposal.veto_reason)
        else:
            logger.info(
                "options_shadow | %s | conv=%d | debit=$%.2f max_loss=$%.2f "
                "max_profit=$%.2f breakeven=%.2f dte=%d | catalyst=%s",
                symbol, conv, proposal.net_debit, proposal.max_loss,
                proposal.max_profit, proposal.breakeven, proposal.dte, catalyst[:60],
            )

    except Exception as exc:
        # Never let the shadow logger affect the live pipeline.
        logger.warning("options_shadow: exception for %s (ignored): %s", symbol, exc)


# ── Main pipeline ─────────────────────────────────────────────────────────────

def _should_run_thesis_review(run_start: datetime, reason: str) -> bool:
    """Full LLM thesis review runs Mondays or when event-triggered by sentinel."""
    if reason == "sentinel_trigger":
        return True
    if any(t.get("time_stop_flagged") for t in get_open_trades()):
        return True
    return run_start.weekday() == 0   # 0 = Monday


def _parse_event_symbols(raw: str = "") -> set[str]:
    return {
        s.strip().upper()
        for s in (raw or "").replace(";", ",").split(",")
        if s.strip()
    }


def _parse_event_details(raw: str = "") -> list[dict]:
    try:
        parsed = json.loads(raw or "[]")
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, dict)]
    except json.JSONDecodeError:
        logger.warning("Could not parse event details JSON: %s", raw[:200])
        return []


def _is_broad_market_event(event_symbols: set[str]) -> bool:
    market_symbols = set(getattr(config, "SENTINEL_MARKET_SYMBOLS", []))
    return bool(event_symbols & market_symbols)


def _parse_iso_utc(raw: str = "") -> datetime | None:
    if not raw:
        return None
    try:
        value = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _cooled_down_symbols() -> set[str]:
    """
    Return symbols that had a TYPE-B systemic PM skip within TYPEB_SKIP_COOLDOWN_DAYS.
    TYPE-B: structural concerns (valuation, leverage, macro, competition, sector) that
    will not resolve in a 4-week window and should not trigger re-debate every run.
    """
    cooldown_days = getattr(config, "TYPEB_SKIP_COOLDOWN_DAYS", 0)
    if cooldown_days <= 0:
        return set()

    try:
        import json as _json
        with open(config.JOURNAL_PATH) as f:
            data = _json.load(f)
    except Exception:
        return set()

    type_b_keywords = [
        "valuation", "p/e", "pe ratio", "multiple", "overvalued",
        "debt", "leverage", "debt/equity",
        "macro", "competition", "competitive", "sector headwind",
        "growth indefinitely", "priced in", "market recognition",
    ]
    cutoff = datetime.now(timezone.utc) - timedelta(days=cooldown_days)
    cooled: set[str] = set()
    for rd in data.get("risk_decisions", []):
        if rd.get("action") != "skip":
            continue
        reason = rd.get("reason", "").lower()
        if "pm decision" not in reason:
            continue
        try:
            ts = datetime.fromisoformat(str(rd.get("ts", "")).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        if any(kw in reason for kw in type_b_keywords):
            sym = rd.get("symbol", "").upper()
            if sym:
                cooled.add(sym)
    if cooled:
        logger.info("TYPE-B cooldown suppressing %d symbol(s): %s", len(cooled), sorted(cooled))
    return cooled


def _active_watchlist_entries() -> list[dict]:
    latest = get_latest_watchlist("after_close")
    if not latest:
        return []

    expires_at = _parse_iso_utc(latest.get("expires_at", ""))
    if expires_at and expires_at < datetime.now(timezone.utc):
        logger.info("After-close watchlist expired at %s", latest.get("expires_at"))
        return []

    entries = latest.get("entries", [])
    eligible = []
    for entry in entries:
        side = entry.get("side", "")
        flags = entry.get("risk_flags", {})

        # Only explicit long_watch entries with a known long setup type are eligible.
        # C19: the after-close generator only ever emits side in
        # {long_watch, weakness_or_hedge_watch, position_alert} — there is no "long"
        # side, so the previous `elif side == "long"` branch was dead code (removed).
        if not (side == "long_watch" and entry.get("setup_type") in LONG_SETUP_TYPES):
            continue

        if flags.get("earnings_within_3d"):
            continue
        eligible.append(entry)
    return eligible


def _watchlist_position_alerts() -> set[str]:
    """
    Read overnight position alerts and weakness signals for open positions.
    Returns symbols that had a red flag overnight — these get an early thesis
    review even on non-Monday runs, closing the overnight feedback loop.
    """
    latest = get_latest_watchlist("after_close")
    if not latest:
        return set()

    expires_at = _parse_iso_utc(latest.get("expires_at", ""))
    if expires_at and expires_at < datetime.now(timezone.utc):
        return set()

    open_symbols = {t.get("symbol") for t in get_open_trades() if t.get("symbol")}
    if not open_symbols:
        return set()

    alert_symbols = set()
    for entry in latest.get("entries", []):
        sym = entry.get("symbol")
        if sym not in open_symbols:
            continue
        side = entry.get("side", "")
        setup = entry.get("setup_type", "")
        flags = entry.get("risk_flags", {})
        score = entry.get("score", 0)
        # Trigger early review if:
        #   - Explicit position alert (existing_position_alert)
        #   - Weakness/hedge watch on an open position
        #   - Failed breakout flag on an open position
        #   - Bearish news overnight with score >= 0.50
        if side == "position_alert":
            alert_symbols.add(sym)
            logger.info("Overnight alert | %s: position_alert (score=%.3f)", sym, score)
        elif side == "weakness_or_hedge_watch":
            alert_symbols.add(sym)
            logger.info("Overnight alert | %s: weakness_watch/%s (score=%.3f)", sym, setup, score)
        elif flags.get("failed_breakout") and sym in open_symbols:
            alert_symbols.add(sym)
            logger.info("Overnight alert | %s: failed_breakout detected (score=%.3f)", sym, score)
        elif flags.get("bearish_news") and score >= 0.50:
            alert_symbols.add(sym)
            logger.info("Overnight alert | %s: bearish_news flag (score=%.3f)", sym, score)

    return alert_symbols


def _memory_bonus(entry: dict) -> float:
    tier = str(entry.get("tier", "")).upper()
    base = getattr(config, "WATCHLIST_MEMORY_BONUS", 0.06)
    if tier == "A":
        return base
    if tier == "B":
        return base * 0.6
    return base * 0.25


def _select_candidates(
    screener: Screener,
    dynamic_max: int,
    trigger_reason: str,
    event_universe: list[str] | None = None,
) -> list:
    if event_universe is not None:
        return screener.run(max_candidates=dynamic_max, symbols=event_universe)

    # TYPE-B cooldown: suppress names recently skipped on systemic/structural grounds.
    # These re-enter the screener once the cooldown expires — so the suppression is
    # temporary and we don't need to maintain a separate denylist.
    cooled = _cooled_down_symbols()

    # C1: Exclude names we already hold at meaningful size (>= MIN_SLOT_PCT) from the
    # new-entry candidate pool. The screener scores the whole universe and routinely
    # returns held momentum leaders in the top-N; they then burn a full LLM debate only
    # to be vetoed at risk_manager ("Already holding"), starving real new ideas of the
    # few candidate slots. Held names are still managed via review_open_positions().
    # Remnants (< MIN_SLOT_PCT) are intentionally NOT excluded so they remain eligible
    # for a deliberate rebuy-to-full. The risk_manager held-check stays as a backstop.
    min_slot_pct = getattr(config, "MIN_SLOT_PCT", 0.03)
    held_full = {
        t["symbol"] for t in get_open_trades()
        if t.get("symbol") and t.get("position_pct", 0) >= min_slot_pct
    }

    def _drop_held(cands: list) -> list:
        kept = [c for c in cands if c.symbol not in held_full]
        dropped = [c.symbol for c in cands if c.symbol in held_full]
        if dropped:
            logger.info("Excluded held positions from candidate pool: %s", dropped)
        return kept

    # EXP-006: extend the static core list with dynamically discovered names
    # (market-wide most-actives + filtered gainers). Discovery is skipped for
    # sentinel runs (those use a narrow event universe) and is fail-safe — any
    # error leaves the core watchlist untouched.
    from agents.discovery import discover_universe
    discovered = discover_universe(screener.fetcher, screener.WATCHLIST) \
        if trigger_reason != "sentinel_trigger" else []
    base_universe = list(dict.fromkeys(list(screener.WATCHLIST) + discovered))
    universe = [s for s in base_universe if s not in cooled] if cooled else base_universe

    candidates = _drop_held(screener.run(max_candidates=dynamic_max, symbols=universe))

    # Tag discovered candidates so EXP-006 can compare their forward outcomes
    # against core-list candidates in the journal.
    disc_set = set(discovered)
    for c in candidates:
        if c.symbol in disc_set:
            c.signals["discovered"] = True

    if trigger_reason == "sentinel_trigger":
        return candidates

    watch_entries = _active_watchlist_entries()
    if not watch_entries:
        return candidates

    watch_by_symbol = {e["symbol"]: e for e in watch_entries if e.get("symbol")}
    watch_symbols = sorted(watch_by_symbol)
    revalidated = _drop_held(
        screener.run(max_candidates=len(watch_symbols), symbols=watch_symbols)
    )
    if not revalidated:
        logger.info("After-close watchlist had no symbols pass next-run revalidation")
        return candidates

    merged = {c.symbol: c for c in candidates}
    for candidate in revalidated:
        entry = watch_by_symbol.get(candidate.symbol)
        if not entry:
            continue
        bonus = _memory_bonus(entry)
        candidate.signals.update({
            "watchlist_memory": True,
            "watchlist_setup_type": entry.get("setup_type"),
            "watchlist_tier": entry.get("tier"),
            "watchlist_score": entry.get("score"),
            "watchlist_reason": entry.get("reason"),
            "watchlist_memory_bonus": round(bonus, 4),
        })
        candidate.composite_score = round(candidate.composite_score + bonus, 4)
        if candidate.symbol not in merged or candidate.composite_score > merged[candidate.symbol].composite_score:
            merged[candidate.symbol] = candidate

    selected = sorted(merged.values(), key=lambda c: c.composite_score, reverse=True)[:dynamic_max]
    logger.info(
        "After-close memory revalidated %d/%d symbols; selected candidates=%s",
        len(revalidated), len(watch_symbols), [c.symbol for c in selected],
    )
    return selected


def run_pipeline(
    dry_run: bool = False,
    reason: str = "scheduled",
    event_symbols: set[str] | None = None,
    event_details: list[dict] | None = None,
):
    run_start = datetime.now(ET)
    run_type  = "pre_market" if run_start.hour < 12 else "midday"
    trigger_reason = reason
    event_symbols = event_symbols or set()
    event_details = event_details or []
    if not event_symbols and event_details:
        event_symbols = {
            str(evt.get("symbol", "")).upper()
            for evt in event_details if evt.get("symbol")
        }
    logger.info(
        "══ Pipeline starting | %s | dry_run=%s | reason=%s | symbols=%s | events=%s ══",
        run_type, dry_run, trigger_reason, sorted(event_symbols), event_details,
    )

    alpaca  = AlpacaClient()
    fetcher = DataFetcher()

    ok, skipped_reason, regime, vix_regime = check_hard_stops(alpaca, fetcher)
    logger.info("Regime | SPY=%s | VIX=%s", regime, vix_regime)
    if not ok:
        logger.info("Hard stop: %s", skipped_reason)
        log_run(run_type, [], 0, skipped_reason=skipped_reason,
                regime=regime, vix_regime=vix_regime, reason=trigger_reason,
                event_symbols=sorted(event_symbols), event_details=event_details)
        return

    check_open_positions(alpaca)
    broad_event = trigger_reason == "sentinel_trigger" and _is_broad_market_event(event_symbols)
    review_symbols = None
    if trigger_reason == "sentinel_trigger" and event_symbols and not broad_event:
        review_symbols = event_symbols

    # Fix 1 (precise): Sentinel stop-proximity feedback loop guard.
    # The sentinel emits trigger_type per event in event_details:
    #   "near_stop"      → position is within 85% of its stop distance
    #   "intraday_move"  → watchlist or position moved >3.5% from prev close
    #   "earnings_today" → earnings event on a watched symbol
    #   "volume_spike"   → unusual volume on an open position
    #   "position_move"  → position moved >2.5% from entry
    # "near_stop" is a mechanical condition — check_open_positions() already handles
    # it. Running a full LLM thesis review on near_stop creates the feedback loop:
    # review → trim → still near stop → sentinel fires again → repeat.
    # Any other trigger type is a genuine signal change warranting LLM review.
    skip_llm_review_for_sentinel = False
    if trigger_reason == "sentinel_trigger" and event_details and not broad_event:
        near_stop_only = all(
            isinstance(evt, dict) and evt.get("trigger_type") == "near_stop"
            for evt in event_details
        )
        if near_stop_only:
            skip_llm_review_for_sentinel = True
            logger.info(
                "Sentinel: all %d event(s) are near_stop — "
                "mechanical check sufficient, skipping LLM review",
                len(event_details),
            )

    # Check overnight watchlist for position alerts — close the after-hours feedback loop
    overnight_alerts = _watchlist_position_alerts()
    if overnight_alerts:
        logger.info("Overnight position alerts for: %s — forcing targeted thesis review",
                    sorted(overnight_alerts))

    if skip_llm_review_for_sentinel:
        logger.info("Sentinel stop-proximity run — mechanical checks only, no LLM review")
    elif _should_run_thesis_review(run_start, trigger_reason):
        review_open_positions(alpaca, fetcher, dry_run=dry_run, symbols=review_symbols,
                              regime=regime, vix_regime=vix_regime)
    elif overnight_alerts:
        # Not Monday and not sentinel, but overnight weakness detected on open positions
        # Run a targeted review only on the flagged symbols — don't review the whole book
        logger.info("Non-Monday early review triggered by overnight alerts: %s",
                    sorted(overnight_alerts))
        review_open_positions(alpaca, fetcher, dry_run=dry_run, symbols=overnight_alerts,
                              regime=regime, vix_regime=vix_regime)
    else:
        logger.info("Skipping full thesis review (not Monday) — mechanical stops only")

    all_open     = get_open_trades()
    min_slot_pct = getattr(config, "MIN_SLOT_PCT", 0.03)
    # Meaningful positions are above MIN_SLOT_PCT — these count against MAX_POSITIONS.
    # Remnants (trimmed below 3%) don't block new full-size entries.
    meaningful_count = sum(1 for p in all_open if p.get("position_pct", 0) >= min_slot_pct)
    available   = config.MAX_POSITIONS - meaningful_count
    dynamic_max = max(
        config.SCREENER_MIN_CANDIDATES,
        min(available + 2, config.SCREENER_MAX_CANDIDATES),
    )
    screener = Screener(fetcher)
    event_universe = None
    if trigger_reason == "sentinel_trigger" and event_symbols and not broad_event:
        event_universe = sorted(
            s for s in event_symbols
            if s not in set(getattr(config, "SENTINEL_MARKET_SYMBOLS", []))
        )
        dynamic_max = min(dynamic_max, config.SENTINEL_EVENT_MAX_CANDIDATES)
        logger.info("Sentinel event mode — narrowed screener to %s", event_universe)

    try:
        candidates = _select_candidates(
            screener=screener,
            dynamic_max=dynamic_max,
            trigger_reason=trigger_reason,
            event_universe=event_universe,
        )
    except ScreenerDataUnavailable as exc:
        # Quote data is unavailable (yfinance rate-limit / network down).
        # Log as 'data_unavailable' — distinct from 'no_candidates' — so the
        # dashboard can surface the right diagnostic rather than implying the
        # screener ran and found nothing.
        logger.warning("Screener data unavailable — skipping this run: %s", exc)
        log_run(run_type, [], 0, skipped_reason="data_unavailable",
                regime=regime, vix_regime=vix_regime, reason=trigger_reason,
                event_symbols=sorted(event_symbols), event_details=event_details)
        return

    if not candidates:
        logger.info("No screener candidates")
        log_run(run_type, [], 0, skipped_reason="no_candidates",
                regime=regime, vix_regime=vix_regime, reason=trigger_reason,
                event_symbols=sorted(event_symbols), event_details=event_details)
        return

    logger.info(screener.format_for_log(candidates))

    # S2 (audit): log the deterministic baseline twin's would-buy decisions for this
    # run BEFORE any LLM judgment — measurement only, never executes anything.
    from core.baseline import log_baseline_decisions
    log_baseline_decisions(
        candidates=candidates,
        open_positions=get_open_trades(),
        portfolio_value=alpaca.get_account()["portfolio_value"],
        fetcher=fetcher,
        regime=regime,
        vix_regime=vix_regime,
        run_reason=trigger_reason,
    )

    ok_to_trade, trade_reason = can_execute_trades(alpaca)
    if not ok_to_trade:
        logger.info("Execution gate: %s", trade_reason)

    account         = alpaca.get_account()
    portfolio_value = account["portfolio_value"]
    open_positions  = get_open_trades()

    trades_executed = 0
    llm_diag = {"analyst_fallbacks": 0, "pm_failures": 0}
    for candidate in candidates:
        meaningful_open = sum(1 for p in open_positions if p.get("position_pct", 0) >= min_slot_pct)
        if meaningful_open + trades_executed >= config.MAX_POSITIONS:
            logger.info("Max positions reached — stopping analysis")
            break

        # C18: pre-debate gate. In 'watch' mode we log what we WOULD have skipped but
        # still run the debate (data collection). In 'enforce' mode we skip it for real.
        gate_mode = getattr(config, "PRE_DEBATE_GATE_MODE", "watch")
        if gate_mode in ("watch", "enforce"):
            would_gate, gate_reason = _pre_debate_gate(candidate, open_positions)
            if would_gate:
                log_pre_debate_gate(candidate.symbol, True, gate_reason)
                if gate_mode == "enforce":
                    logger.info("PRE-DEBATE GATE | %s skipped — %s", candidate.symbol, gate_reason)
                    log_risk_decision(symbol=candidate.symbol, action="gated",
                                      reason=f"pre_debate_gate: {gate_reason}", conviction=0)
                    continue
                logger.info("PRE-DEBATE GATE (watch) | %s would skip (%s) — running debate anyway",
                            candidate.symbol, gate_reason)

        # C3: isolate each candidate so one unexpected error (data, API, agent)
        # can't abort the run before log_snapshot/log_run/push_to_github below,
        # which would lose the run record and break append-only auditability.
        try:
            executed = analyse_symbol(
                symbol=candidate.symbol,
                fetcher=fetcher,
                portfolio_value=portfolio_value,
                open_positions=open_positions,
                dry_run=dry_run,
                ok_to_trade=ok_to_trade,
                alpaca=alpaca,
                regime=regime,
                vix_regime=vix_regime,
                llm_diag=llm_diag,
            )
        except Exception as exc:
            logger.exception("analyse_symbol failed for %s — skipping: %s",
                             candidate.symbol, exc)
            executed = False
        if executed:
            trades_executed += 1

    spy_bars  = fetcher.get_ohlcv("SPY", days=2)
    spy_price = spy_bars[-1]["close"] if spy_bars else 0.0
    log_snapshot(account["portfolio_value"], account["cash"], spy_price)
    log_run(run_type, [c.symbol for c in candidates], trades_executed,
            regime=regime, vix_regime=vix_regime, reason=trigger_reason,
            event_symbols=sorted(event_symbols), event_details=event_details,
            candidate_details=[
                {"symbol": c.symbol, "composite_score": c.composite_score,
                 "signals": c.signals}
                for c in candidates
            ],
            llm_failures=llm_diag)
    if llm_diag.get("analyst_fallbacks") or llm_diag.get("pm_failures"):
        logger.warning("LLM DEGRADATION | %d analyst fallback(s), %d PM failure(s) this run",
                       llm_diag.get("analyst_fallbacks", 0), llm_diag.get("pm_failures", 0))

    logger.info("══ Pipeline complete | portfolio=$%.2f | trades=%d ══",
                account["portfolio_value"], trades_executed)

    # Auto-push updated dashboard data to GitHub → triggers Vercel redeploy
    if dry_run:
        logger.info("DRY RUN — skipping GitHub push")
    else:
        push_to_github(f"Auto: {run_type} run — {trades_executed} trade(s) executed")


# ── Scheduler ─────────────────────────────────────────────────────────────────

def run_scheduled(dry_run: bool = False):
    for t in config.RUN_TIMES_ET:
        schedule.every().day.at(t).do(run_pipeline, dry_run=dry_run)
        logger.info("Scheduled at %s ET", t)
    logger.info("Running. Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--now",    action="store_true")
    parser.add_argument("--test",   action="store_true")
    parser.add_argument("--reason", default="scheduled",
                        help="Run trigger reason (scheduled / sentinel_trigger)")
    parser.add_argument("--symbols", default="",
                        help="Comma-separated symbols that triggered this run")
    parser.add_argument("--event-details", default="[]",
                        help="JSON event metadata from sentinel")
    args = parser.parse_args()
    if args.now or args.test:
        run_pipeline(
            dry_run=args.test,
            reason=args.reason,
            event_symbols=_parse_event_symbols(args.symbols),
            event_details=_parse_event_details(args.event_details),
        )
    else:
        run_scheduled()
