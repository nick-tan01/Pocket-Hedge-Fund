"""
sentinel.py — Lightweight event monitor (no LLM calls).
Checks for market events that warrant an unscheduled pipeline run.
Writes trigger=true/false to $GITHUB_OUTPUT for GitHub Actions.
"""

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

import yfinance as yf
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

WATCHLIST = [
    "NVDA", "AAPL", "AMD", "GOOGL", "META", "AMZN", "QCOM", "AMAT",
    "MSFT", "TSLA", "CRWD", "NET", "SNOW", "DDOG", "PLTR", "RKLB",
    "SMCI", "MSTR", "IONQ", "GTLB", "TTD", "HIMS", "DUOL", "ARM",
    "ACLS", "COHR", "ONTO", "CELH", "DOCS", "BILL",
]

INTRADAY_MOVE_THRESHOLD = 0.035
POSITION_MOVE_THRESHOLD = 0.025
VOLUME_SPIKE_THRESHOLD = 4.0
STOP_PROXIMITY_PCT = 0.85


def load_open_trades() -> list[dict]:
    path = Path("dashboard/data.json")
    if not path.exists():
        return []
    try:
        with open(path) as f:
            return json.load(f).get("open_trades", [])
    except Exception as e:
        logger.warning("Could not read open trades: %s", e)
        return []


def check_intraday_moves(symbols: list[str]) -> list[str]:
    triggers = []
    if not symbols:
        return triggers
    try:
        data = yf.download(symbols, period="2d", interval="1d", progress=False, group_by="ticker")
        for sym in symbols:
            try:
                if len(symbols) == 1:
                    prices = data["Close"].dropna()
                else:
                    prices = data[sym]["Close"].dropna()
                if len(prices) >= 2:
                    move = abs((prices.iloc[-1] - prices.iloc[-2]) / prices.iloc[-2])
                    if move >= INTRADAY_MOVE_THRESHOLD:
                        logger.info("TRIGGER: %s moved %.1f%%", sym, move * 100)
                        triggers.append(sym)
            except Exception:
                pass
    except Exception as e:
        logger.warning("Price check failed: %s", e)
    return triggers


def check_position_moves(open_trades: list[dict]) -> list[str]:
    triggers = []
    for trade in open_trades:
        try:
            symbol = trade["symbol"]
            entry = float(trade.get("entry_price", 0))
            if entry <= 0:
                continue
            price = float(yf.Ticker(symbol).fast_info.last_price)
            move = abs((price - entry) / entry)
            if move >= POSITION_MOVE_THRESHOLD:
                logger.info("TRIGGER: %s position moved %.1f%% from entry", symbol, move * 100)
                triggers.append(symbol)
        except Exception:
            pass
    return triggers


def check_volume_spikes(open_trades: list[dict]) -> list[str]:
    triggers = []
    for trade in open_trades:
        try:
            symbol = trade["symbol"]
            hist = yf.Ticker(symbol).history(period="30d")
            if len(hist) < 21:
                continue
            avg_vol = hist["Volume"].iloc[-21:-1].mean()
            today_vol = hist["Volume"].iloc[-1]
            if avg_vol and today_vol / avg_vol >= VOLUME_SPIKE_THRESHOLD:
                logger.info("TRIGGER: %s volume spike %.1fx", symbol, today_vol / avg_vol)
                triggers.append(symbol)
        except Exception:
            pass
    return triggers


def check_position_proximity_to_stop(open_trades: list[dict]) -> list[str]:
    triggers = []
    if not open_trades or not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return triggers
    symbols = [t["symbol"] for t in open_trades]
    try:
        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        quotes = client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbols))
        for trade in open_trades:
            sym = trade["symbol"]
            quote = quotes.get(sym)
            if not quote:
                continue
            current = float(quote.ask_price or quote.bid_price or 0)
            entry = float(trade.get("entry_price", 0))
            stop = float(trade.get("stop_price", 0))
            if entry and stop and current and entry != stop:
                pct_to_stop = (entry - current) / (entry - stop)
                if pct_to_stop >= STOP_PROXIMITY_PCT:
                    logger.info("TRIGGER: %s at %.1f%% of stop distance", sym, pct_to_stop * 100)
                    triggers.append(sym)
    except Exception as e:
        logger.warning("Stop proximity check failed: %s", e)
    return triggers


def check_earnings_today(symbols: list[str]) -> list[str]:
    triggers = []
    today = date.today()
    for sym in symbols:
        try:
            info = yf.Ticker(sym).info
            raw = info.get("earningsDate") or info.get("earningsTimestamp")
            ed = parse_earnings_date(raw)
            if ed == today:
                logger.info("TRIGGER: %s has earnings today", sym)
                triggers.append(sym)
        except Exception:
            pass
    return triggers


def parse_earnings_date(raw) -> date | None:
    try:
        if raw is None:
            return None
        if isinstance(raw, (list, tuple)) and raw:
            raw = raw[0]
        if hasattr(raw, "iloc"):
            raw = raw.iloc[0]
        if isinstance(raw, (int, float)):
            return datetime.utcfromtimestamp(raw).date()
        if isinstance(raw, datetime):
            return raw.date()
        if isinstance(raw, date):
            return raw
        return date.fromisoformat(str(raw)[:10])
    except Exception:
        return None


def set_github_output(key: str, value: str):
    output_file = os.getenv("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"{key}={value}\n")


def main():
    open_trades = load_open_trades()
    open_symbols = [t["symbol"] for t in open_trades]
    all_symbols = sorted(set(WATCHLIST + open_symbols))

    triggers = []
    triggers.extend(check_intraday_moves(all_symbols))
    triggers.extend(check_position_moves(open_trades))
    triggers.extend(check_volume_spikes(open_trades))
    triggers.extend(check_position_proximity_to_stop(open_trades))
    triggers.extend(check_earnings_today(all_symbols))

    unique = sorted(set(triggers))
    if unique:
        logger.info("EVENT DETECTED — triggering pipeline. Symbols: %s", unique)
        set_github_output("trigger", "true")
        set_github_output("trigger_symbols", ",".join(unique))
    else:
        logger.info("No events detected — no pipeline trigger")
        set_github_output("trigger", "false")
        set_github_output("trigger_symbols", "")


if __name__ == "__main__":
    main()
