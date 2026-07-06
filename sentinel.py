"""
sentinel.py — Lightweight event monitor (no LLM calls).
Checks for market events that warrant an unscheduled pipeline run.
Writes trigger=true/false to $GITHUB_OUTPUT for GitHub Actions.
"""

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

WATCHLIST = [
    # Tech & AI
    "NVDA", "AAPL", "AMD", "GOOGL", "META", "AMZN", "QCOM", "AMAT",
    "MSFT", "TSLA", "CRWD", "NET", "SNOW", "DDOG", "PLTR", "RKLB",
    "SMCI", "MSTR", "IONQ", "GTLB", "TTD", "HIMS", "DUOL", "ARM",
    "ACLS", "COHR", "ONTO", "CELH", "DOCS", "BILL",
    # Financials & defensives — added for risk-off regime coverage
    "JPM", "GS", "MS", "V", "MA", "WFC", "ICE", "CME", "PGR", "TRV",
    "LLY", "UNH", "ABBV", "ELV", "MDT", "SYK", "ISRG",
    "PG", "KO", "WMT", "COST",
]
MARKET_SENTINELS = ["SPY", "QQQ", "IWM"]

INTRADAY_MOVE_THRESHOLD = 0.035
POSITION_MOVE_THRESHOLD = 0.025
VOLUME_SPIKE_THRESHOLD = 4.0
STOP_PROXIMITY_PCT = 0.85
COOLDOWN_MINUTES = 180
SEVERE_INTRADAY_MOVE = 0.06
SEVERE_POSITION_MOVE = 0.05
SEVERE_VOLUME_SPIKE = 8.0
SEVERE_STOP_PROXIMITY = 0.95

# Audit 2026-07-06 (efficiency): sentinel-triggered runs were 65% of ALL pipeline
# runs with a ~8% trade rate — overwhelmingly ordinary intraday noise
# (intraday_move fired 1,169x) re-running full LLM pipelines and re-debating the
# same held names (AMD 80x, DDOG 76x, LLY reviewed 98x). near_stop is redundant
# now that broker-native GTC stops rest at Alpaca between runs. Default trigger
# whitelist: earnings only. Re-expand deliberately via
#   SENTINEL_ENABLED_TRIGGERS="earnings_today,intraday_move,position_move,..."
ENABLED_TRIGGERS = {
    t.strip()
    for t in os.getenv("SENTINEL_ENABLED_TRIGGERS", "earnings_today").split(",")
    if t.strip()
}


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


def load_recent_runs() -> list[dict]:
    path = Path("dashboard/data.json")
    if not path.exists():
        return []
    try:
        with open(path) as f:
            return json.load(f).get("runs", [])
    except Exception as e:
        logger.warning("Could not read recent runs: %s", e)
        return []


def event(symbol: str, trigger_type: str, value: float, threshold: float, **extra) -> dict:
    severity = "high" if (
        trigger_type in {"earnings_today", "near_stop"} or
        (trigger_type in {"intraday_move", "intraday_rebound"} and value >= SEVERE_INTRADAY_MOVE) or
        (trigger_type == "position_move" and value >= SEVERE_POSITION_MOVE) or
        (trigger_type == "volume_spike" and value >= SEVERE_VOLUME_SPIKE)
    ) else "normal"
    out = {
        "symbol":       symbol,
        "trigger_type": trigger_type,
        "value":        round(value, 4),
        "threshold":    threshold,
        "severity":     severity,
    }
    out.update(extra)
    return out


def check_intraday_moves(symbols: list[str]) -> list[dict]:
    triggers = []
    if not symbols:
        return triggers
    try:
        data = yf.download(
            symbols, period="5d", interval="15m",
            progress=False, group_by="ticker", auto_adjust=False,
        )
        for sym in symbols:
            try:
                if len(symbols) == 1:
                    prices = data["Close"].dropna()
                else:
                    prices = data[sym]["Close"].dropna()
                if len(prices) < 2:
                    continue
                latest_day = prices.index[-1].date()
                today_prices = prices[prices.index.date == latest_day]
                previous_prices = prices[prices.index.date < latest_day]
                if today_prices.empty or previous_prices.empty:
                    continue
                previous_close = float(previous_prices.iloc[-1])
                latest = float(today_prices.iloc[-1])
                intraday_low = float(today_prices.min())
                raw_move = (latest - previous_close) / previous_close
                from_prev_close = abs(raw_move)
                rebound_from_low = (latest - intraday_low) / intraday_low if intraday_low else 0
                if from_prev_close >= INTRADAY_MOVE_THRESHOLD:
                    logger.info(
                        "TRIGGER: %s intraday move %.1f%%",
                        sym, raw_move * 100,
                    )
                    triggers.append(event(
                        sym, "intraday_move", from_prev_close, INTRADAY_MOVE_THRESHOLD,
                        signed_move=round(raw_move, 4),
                        latest=round(latest, 2),
                        previous_close=round(previous_close, 2),
                    ))
                if rebound_from_low >= INTRADAY_MOVE_THRESHOLD:
                    logger.info("TRIGGER: %s rebound %.1f%% from intraday low",
                                sym, rebound_from_low * 100)
                    triggers.append(event(
                        sym, "intraday_rebound", rebound_from_low, INTRADAY_MOVE_THRESHOLD,
                        latest=round(latest, 2),
                        intraday_low=round(intraday_low, 2),
                    ))
            except Exception:
                pass
    except Exception as e:
        logger.warning("Price check failed: %s", e)
    return triggers


def check_position_moves(open_trades: list[dict]) -> list[dict]:
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
                triggers.append(event(
                    symbol, "position_move", move, POSITION_MOVE_THRESHOLD,
                    price=round(price, 2), entry=round(entry, 2),
                ))
        except Exception:
            pass
    return triggers


def check_volume_spikes(open_trades: list[dict]) -> list[dict]:
    triggers = []
    for trade in open_trades:
        try:
            symbol = trade["symbol"]
            hist = yf.Ticker(symbol).history(period="30d")
            if len(hist) < 21:
                continue
            avg_vol = hist["Volume"].iloc[-21:-1].mean()
            today_vol = hist["Volume"].iloc[-1]
            ratio = today_vol / avg_vol if avg_vol else 0
            if ratio >= VOLUME_SPIKE_THRESHOLD:
                logger.info("TRIGGER: %s volume spike %.1fx", symbol, ratio)
                triggers.append(event(
                    symbol, "volume_spike", ratio, VOLUME_SPIKE_THRESHOLD,
                    today_volume=int(today_vol), avg_volume=int(avg_vol),
                ))
        except Exception:
            pass
    return triggers


def check_position_proximity_to_stop(open_trades: list[dict]) -> list[dict]:
    triggers = []
    if not open_trades or not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return triggers
    symbols = [t["symbol"] for t in open_trades]
    try:
        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        quotes = client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbols))
        for trade in open_trades:
            sym = trade["symbol"]
            quote = quotes.get(sym) if hasattr(quotes, "get") else quotes[sym]
            if not quote:
                continue
            current = float(quote.ask_price or quote.bid_price or 0)
            entry = float(trade.get("entry_price", 0))
            stop = float(trade.get("stop_price", 0))
            if entry and stop and current and entry != stop:
                pct_to_stop = (entry - current) / (entry - stop)
                if pct_to_stop >= STOP_PROXIMITY_PCT:
                    logger.info("TRIGGER: %s at %.1f%% of stop distance", sym, pct_to_stop * 100)
                    triggers.append(event(
                        sym, "near_stop", pct_to_stop, STOP_PROXIMITY_PCT,
                        current=round(current, 2),
                        entry=round(entry, 2),
                        stop=round(stop, 2),
                    ))
    except Exception as e:
        logger.warning("Stop proximity check failed: %s", e)
    return triggers


def check_earnings_today(symbols: list[str]) -> list[dict]:
    triggers = []
    today = date.today()
    for sym in symbols:
        try:
            info = yf.Ticker(sym).info
            raw = info.get("earningsDate") or info.get("earningsTimestamp")
            ed = parse_earnings_date(raw)
            if ed == today:
                logger.info("TRIGGER: %s has earnings today", sym)
                triggers.append(event(sym, "earnings_today", 1.0, 1.0))
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
            return datetime.fromtimestamp(raw, tz=timezone.utc).date()
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


def is_recent_duplicate(evt: dict, runs: list[dict]) -> bool:
    if evt.get("severity") == "high":
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=COOLDOWN_MINUTES)
    symbol = evt.get("symbol")
    trigger_type = evt.get("trigger_type")

    for run in reversed(runs):
        try:
            ts = datetime.fromisoformat(str(run.get("ts", "")).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts < cutoff:
            break

        details = run.get("event_details") or []
        if any(
            d.get("symbol") == symbol and d.get("trigger_type") == trigger_type
            for d in details if isinstance(d, dict)
        ):
            return True

        # Backward compatibility for runs before event_details existed.
        if not details and symbol in run.get("event_symbols", []):
            return True
    return False


def apply_cooldown(events: list[dict], runs: list[dict]) -> list[dict]:
    kept = []
    for evt in events:
        if is_recent_duplicate(evt, runs):
            logger.info(
                "COOLDOWN: suppressing %s %s for %d minutes",
                evt.get("symbol"), evt.get("trigger_type"), COOLDOWN_MINUTES,
            )
            continue
        kept.append(evt)
    return kept


def main():
    open_trades = load_open_trades()
    recent_runs = load_recent_runs()
    open_symbols = [t["symbol"] for t in open_trades]
    all_symbols = sorted(set(WATCHLIST + MARKET_SENTINELS + open_symbols))

    logger.info("Enabled triggers: %s", sorted(ENABLED_TRIGGERS))
    events = []
    if {"intraday_move", "intraday_rebound"} & ENABLED_TRIGGERS:
        events.extend(check_intraday_moves(all_symbols))
    if "position_move" in ENABLED_TRIGGERS:
        events.extend(check_position_moves(open_trades))
    if "volume_spike" in ENABLED_TRIGGERS:
        events.extend(check_volume_spikes(open_trades))
    if "near_stop" in ENABLED_TRIGGERS:
        events.extend(check_position_proximity_to_stop(open_trades))
    if "earnings_today" in ENABLED_TRIGGERS:
        events.extend(check_earnings_today(all_symbols))
    events = [e for e in events if e.get("trigger_type") in ENABLED_TRIGGERS]
    events = apply_cooldown(events, recent_runs)

    unique = sorted({evt["symbol"] for evt in events})
    if unique:
        logger.info("EVENT DETECTED — triggering pipeline. Events: %s", events)
        set_github_output("trigger", "true")
        set_github_output("trigger_symbols", ",".join(unique))
        set_github_output(
            "trigger_details",
            json.dumps(events, separators=(",", ":")),
        )
    else:
        logger.info("No events detected — no pipeline trigger")
        set_github_output("trigger", "false")
        set_github_output("trigger_symbols", "")
        set_github_output("trigger_details", "[]")


if __name__ == "__main__":
    # Fail fast with a clear message (sentinel stays config-free: minimal deps).
    _missing = [k for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY") if not os.getenv(k)]
    if _missing:
        raise SystemExit(f"Missing required environment variable(s): {', '.join(_missing)}")
    main()
