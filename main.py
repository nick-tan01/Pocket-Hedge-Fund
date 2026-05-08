"""
main.py — Pocket Hedge Fund orchestrator
Full pipeline: Screener → Analysts → 2-Round Debate → PM Verdict → Risk → Execute

Usage:
    python3 main.py          # scheduled (8:30am + 1:00pm ET)
    python3 main.py --now    # run once immediately
    python3 main.py --test   # dry run, no orders submitted
"""

import argparse
import logging
import sys
import time
from datetime import datetime

import pytz
import schedule

import config
from core.alpaca_client import AlpacaClient
from core.data_fetcher import DataFetcher
from core.journal import (
    log_run, log_snapshot, log_debate,
    log_trade_open, get_open_trades,
)
from agents.screener import Screener
import agents.technical        as technical_agent
import agents.fundamental      as fundamental_agent
import agents.sentiment        as sentiment_agent
import agents.bull_researcher  as bull_agent
import agents.bear_researcher  as bear_agent
import agents.portfolio_manager as pm_agent
import agents.risk_manager      as risk_agent

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


# ── Circuit breakers ──────────────────────────────────────────────────────────

def check_hard_stops(alpaca: AlpacaClient, fetcher: DataFetcher) -> tuple[bool, str]:
    vix = fetcher.get_vix()
    if vix and vix > config.VIX_PAUSE_THRESHOLD:
        return False, f"VIX={vix:.1f} exceeds threshold {config.VIX_PAUSE_THRESHOLD}"
    account  = alpaca.get_account()
    drawdown = (config.STARTING_CAPITAL - account["portfolio_value"]) / config.STARTING_CAPITAL
    if drawdown > config.MAX_PORTFOLIO_DD:
        return False, f"Portfolio drawdown {drawdown*100:.1f}% exceeds limit"
    return True, ""


def can_execute_trades(alpaca: AlpacaClient) -> tuple[bool, str]:
    if not alpaca.is_market_open():
        return False, "Market closed — analysis complete, no orders submitted"
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
            log_trade_close(trade["id"], trade["entry_price"], "unknown_close")
            continue
        pos           = positions[symbol]
        current_price = pos["current_price"]
        stop_price    = trade["stop_price"]

        if current_price <= stop_price:
            logger.warning("STOP TRIGGERED | %s $%.2f <= $%.2f",
                           symbol, current_price, stop_price)
            order = alpaca.close_position(symbol, reason="stop_loss")
            if order:
                from core.journal import log_trade_close
                log_trade_close(trade["id"], current_price, "stop_loss")

        pnl_pct = pos["unrealized_plpc"]
        if pnl_pct >= config.TRAILING_STOP_TRIGGER:
            peak     = trade["entry_price"] * (1 + pnl_pct)
            new_stop = round(peak * (1 - config.TRAILING_STOP_PCT), 2)
            if new_stop > stop_price:
                trade["stop_price"] = new_stop
                logger.info("Trailing stop → %s $%.2f", symbol, new_stop)


# ── Per-symbol analysis ───────────────────────────────────────────────────────

def analyse_symbol(
    symbol: str,
    fetcher: DataFetcher,
    portfolio_value: float,
    open_positions: list[dict],
    dry_run: bool,
    ok_to_trade: bool,
    alpaca: AlpacaClient,
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

    order = alpaca.submit_market_order(
        symbol=symbol, qty=proposal.shares, side="buy",
        reason=f"conviction={proposal.conviction} | {pm.get('verdict','')}",
    )

    if order:
        log_trade_open(
            symbol=symbol, side="buy", qty=proposal.shares,
            entry_price=current_price, stop_price=proposal.stop_price,
            conviction=proposal.conviction, debate_id=debate_id,
            key_risk=proposal.key_risk,
        )
        logger.info("✅ ORDER FILLED | %s %.4f @ $%.2f stop=$%.2f",
                    symbol, proposal.shares, current_price, proposal.stop_price)
        return True

    return False


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(dry_run: bool = False):
    run_start = datetime.now(ET)
    run_type  = "pre_market" if run_start.hour < 12 else "midday"
    logger.info("══ Pipeline starting | %s | dry_run=%s ══", run_type, dry_run)

    alpaca  = AlpacaClient()
    fetcher = DataFetcher()

    ok, reason = check_hard_stops(alpaca, fetcher)
    if not ok:
        logger.info("Hard stop: %s", reason)
        log_run(run_type, [], 0, skipped_reason=reason)
        return

    check_open_positions(alpaca)

    screener   = Screener(fetcher)
    candidates = screener.run()

    if not candidates:
        logger.info("No screener candidates")
        log_run(run_type, [], 0, skipped_reason="no_candidates")
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
        )
        if executed:
            trades_executed += 1

    spy_bars  = fetcher.get_ohlcv("SPY", days=2)
    spy_price = spy_bars[-1]["close"] if spy_bars else 0.0
    log_snapshot(account["portfolio_value"], account["cash"], spy_price)
    log_run(run_type, [c.symbol for c in candidates], trades_executed)

    logger.info("══ Pipeline complete | portfolio=$%.2f | trades=%d ══",
                account["portfolio_value"], trades_executed)


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
    parser.add_argument("--now",  action="store_true")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    if args.now or args.test:
        run_pipeline(dry_run=args.test)
    else:
        run_scheduled()
