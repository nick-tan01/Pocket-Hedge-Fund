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
from datetime import datetime, timezone

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
    log_risk_decision, get_latest_watchlist,
)
from agents.screener import Screener
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

    account  = alpaca.get_account()
    drawdown = (config.STARTING_CAPITAL - account["portfolio_value"]) / config.STARTING_CAPITAL
    if drawdown > config.MAX_PORTFOLIO_DD:
        return False, f"Portfolio drawdown {drawdown*100:.1f}% exceeds limit", regime, vix_regime
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

def check_open_positions(alpaca: AlpacaClient):
    if not alpaca.is_market_open():
        return
    from core.journal import log_trade_close
    positions      = {p["symbol"]: p for p in alpaca.get_positions()}
    journal_trades = get_open_trades()

    for trade in journal_trades:
        symbol = trade["symbol"]
        if symbol not in positions:
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
                "ORPHAN CLOSE | %s not in Alpaca after %.1fh — closing at entry price.",
                symbol, age_hours,
            )
            log_trade_close(trade["id"], trade["entry_price"], "orphan_close")
            continue
        pos           = positions[symbol]
        current_price = pos["current_price"]
        stop_price    = trade["stop_price"]

        if current_price <= stop_price:
            logger.warning("STOP TRIGGERED | %s $%.2f <= $%.2f",
                           symbol, current_price, stop_price)
            order = alpaca.close_position(symbol, reason="stop_loss")
            if order:
                log_trade_close(trade["id"], current_price, "stop_loss")

        pnl_pct = pos["unrealized_plpc"]
        if pnl_pct >= config.TRAILING_STOP_TRIGGER:
            peak     = trade["entry_price"] * (1 + pnl_pct)
            new_stop = round(peak * (1 - config.TRAILING_STOP_PCT), 2)
            if new_stop > stop_price:
                trade["stop_price"] = new_stop
                logger.info("Trailing stop → %s $%.2f", symbol, new_stop)

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
        if action == "hold":
            update_open_trade(trade_id, {
                "last_review_ts":         datetime.now(ET).isoformat(),
                "last_review_conviction": conviction,
                "thesis_status":          review.get("thesis_status"),
                "key_risk":               review.get("key_risk_to_monitor"),
                "queued_action":           None,
                "time_stop_flagged":       False,
            })
            clear_queued_action(trade_id)

        elif action == "exit":
            if dry_run:
                logger.info("DRY RUN — would EXIT %s | %s",
                            symbol, review.get("exit_reason", "thesis broken"))
                continue

            if not ok_to_trade:
                logger.info("Market closed — EXIT queued for %s (will re-review next run)", symbol)
                set_queued_action(trade_id, "exit", review.get("exit_reason", "thesis_broken"))
                update_open_trade(trade_id, {
                    "last_review_ts": datetime.now(ET).isoformat(),
                    "thesis_status":  review.get("thesis_status"),
                    "time_stop_flagged": False,
                })
                continue

            order = alpaca.close_position(symbol, reason="thesis_broken")
            if order:
                log_trade_close(trade_id, current_price, "thesis_broken")
                logger.info("EXIT executed | %s @ $%.2f", symbol, current_price)

        elif action == "trim":
            if dry_run:
                logger.info("DRY RUN — would TRIM %s | %s",
                            symbol, review.get("trim_reason", "conviction reduced"))
                continue

            if not ok_to_trade:
                logger.info("Market closed — TRIM queued for %s (will re-review next run)", symbol)
                set_queued_action(trade_id, "trim", review.get("trim_reason", "conviction_reduced"))
                update_open_trade(trade_id, {
                    "last_review_ts":         datetime.now(ET).isoformat(),
                    "last_review_conviction": conviction,
                    "thesis_status":          review.get("thesis_status"),
                    "time_stop_flagged":      False,
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
                    "last_review_ts":         datetime.now(ET).isoformat(),
                    "last_review_conviction": conviction,
                    "thesis_status":          review.get("thesis_status"),
                    "key_risk":               review.get("key_risk_to_monitor"),
                    "queued_action":           None,
                    "time_stop_flagged":       False,
                })
                clear_queued_action(trade_id)
                continue

            trim_qty = round(excess_value / current_price, 4)
            new_qty  = round(current_qty_act - trim_qty, 4)

            if trim_qty < 0.01:
                logger.info("TRIM %s — trim qty %.4f below minimum, treating as hold",
                            symbol, trim_qty)
                update_open_trade(trade_id, {
                    "last_review_ts":         datetime.now(ET).isoformat(),
                    "last_review_conviction": conviction,
                    "thesis_status":          review.get("thesis_status"),
                    "key_risk":               review.get("key_risk_to_monitor"),
                    "queued_action":           None,
                    "time_stop_flagged":       False,
                })
                clear_queued_action(trade_id)
                continue

            order = alpaca.submit_market_order(
                symbol=symbol, qty=trim_qty, side="sell",
                reason=(f"review_trim | conviction={conviction} | "
                        f"{review.get('trim_reason', '')}"),
            )
            if order:
                log_trade_trim(trade_id, trim_qty, new_qty, current_price,
                               review.get("trim_reason", ""))
                update_open_trade(trade_id, {
                    "qty":                    new_qty,
                    "last_review_ts":         datetime.now(ET).isoformat(),
                    "last_review_conviction": conviction,
                    "thesis_status":          review.get("thesis_status"),
                    "key_risk":               review.get("key_risk_to_monitor"),
                    "queued_action":           None,
                    "time_stop_flagged":       False,
                })
                clear_queued_action(trade_id)
                logger.info(
                    "TRIM executed | %s sold %.4f shares @ $%.2f | remaining qty=%.4f",
                    symbol, trim_qty, current_price, new_qty,
                )


# ── Per-symbol analysis ───────────────────────────────────────────────────────

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

    order = alpaca.submit_market_order(
        symbol=symbol, qty=proposal.shares, side="buy",
        reason=f"conviction={proposal.conviction} | {pm.get('verdict','')}",
    )

    if order:
        log_trade_open(
            symbol=symbol, side="buy", qty=proposal.shares,
            entry_price=current_price, stop_price=proposal.stop_price,
            conviction=proposal.conviction, debate_id=debate_id,
            key_risk=proposal.key_risk, portfolio_value=portfolio_value,
            sector=proposal.sector,
        )
        logger.info("✅ ORDER FILLED | %s %.4f @ $%.2f stop=$%.2f",
                    symbol, proposal.shares, current_price, proposal.stop_price)
        return True

    return False


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
        if entry.get("side") != "long_watch":
            continue
        if entry.get("setup_type") not in LONG_SETUP_TYPES:
            continue
        flags = entry.get("risk_flags", {})
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

    candidates = screener.run(max_candidates=dynamic_max)
    if trigger_reason == "sentinel_trigger":
        return candidates

    watch_entries = _active_watchlist_entries()
    if not watch_entries:
        return candidates

    watch_by_symbol = {e["symbol"]: e for e in watch_entries if e.get("symbol")}
    watch_symbols = sorted(watch_by_symbol)
    revalidated = screener.run(max_candidates=len(watch_symbols), symbols=watch_symbols)
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

    # Check overnight watchlist for position alerts — close the after-hours feedback loop
    overnight_alerts = _watchlist_position_alerts()
    if overnight_alerts:
        logger.info("Overnight position alerts for: %s — forcing targeted thesis review",
                    sorted(overnight_alerts))

    if _should_run_thesis_review(run_start, trigger_reason):
        review_open_positions(alpaca, fetcher, dry_run=dry_run, symbols=review_symbols)
    elif overnight_alerts:
        # Not Monday and not sentinel, but overnight weakness detected on open positions
        # Run a targeted review only on the flagged symbols — don't review the whole book
        logger.info("Non-Monday early review triggered by overnight alerts: %s",
                    sorted(overnight_alerts))
        review_open_positions(alpaca, fetcher, dry_run=dry_run, symbols=overnight_alerts)
    else:
        logger.info("Skipping full thesis review (not Monday) — mechanical stops only")

    open_count  = len(get_open_trades())
    available   = config.MAX_POSITIONS - open_count
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

    candidates = _select_candidates(
        screener=screener,
        dynamic_max=dynamic_max,
        trigger_reason=trigger_reason,
        event_universe=event_universe,
    )

    if not candidates:
        logger.info("No screener candidates")
        log_run(run_type, [], 0, skipped_reason="no_candidates",
                regime=regime, vix_regime=vix_regime, reason=trigger_reason,
                event_symbols=sorted(event_symbols), event_details=event_details)
        return

    logger.info(screener.format_for_log(candidates))

    ok_to_trade, trade_reason = can_execute_trades(alpaca)
    if not ok_to_trade:
        logger.info("Execution gate: %s", trade_reason)

    account         = alpaca.get_account()
    portfolio_value = account["portfolio_value"]
    open_positions  = get_open_trades()

    trades_executed = 0
    for candidate in candidates:
        if len(open_positions) + trades_executed >= config.MAX_POSITIONS:
            logger.info("Max positions reached — stopping analysis")
            break

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
        )
        if executed:
            trades_executed += 1

    spy_bars  = fetcher.get_ohlcv("SPY", days=2)
    spy_price = spy_bars[-1]["close"] if spy_bars else 0.0
    log_snapshot(account["portfolio_value"], account["cash"], spy_price)
    log_run(run_type, [c.symbol for c in candidates], trades_executed,
            regime=regime, vix_regime=vix_regime, reason=trigger_reason,
            event_symbols=sorted(event_symbols), event_details=event_details)

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
