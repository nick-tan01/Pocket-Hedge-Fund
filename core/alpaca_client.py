"""
core/alpaca_client.py
Thin wrapper around alpaca-py. Single import point for the rest of the system.
All order submission goes through here so risk checks are centrally enforced.
"""

import logging
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    TrailingStopOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timedelta
import pytz

import config

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")


class AlpacaClient:
    def __init__(self):
        self.trading = TradingClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
            paper=config.PAPER_TRADING,
        )
        self.data = StockHistoricalDataClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
        )
        logger.info("Alpaca client initialised (paper=%s)", config.PAPER_TRADING)

    # ── Account ───────────────────────────────────────────────────────────────

    def get_account(self) -> dict:
        """Return key account fields as a plain dict."""
        acct = self.trading.get_account()
        return {
            "portfolio_value": float(acct.portfolio_value),
            "cash":            float(acct.cash),
            "equity":          float(acct.equity),
            "buying_power":    float(acct.buying_power),
            "daytrade_count":  int(acct.daytrade_count),
        }

    def get_portfolio_value(self) -> float:
        return float(self.trading.get_account().portfolio_value)

    # ── Positions ─────────────────────────────────────────────────────────────

    def get_positions(self) -> list[dict]:
        """Return all open positions as plain dicts."""
        raw = self.trading.get_all_positions()
        return [
            {
                "symbol":      p.symbol,
                "qty":         float(p.qty),
                "avg_entry":   float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value":  float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
                "side":        p.side.value,
            }
            for p in raw
        ]

    def get_position(self, symbol: str) -> dict | None:
        """Return a single position or None if not held."""
        try:
            p = self.trading.get_open_position(symbol)
            return {
                "symbol":      p.symbol,
                "qty":         float(p.qty),
                "avg_entry":   float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value":  float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
            }
        except Exception:
            return None

    # ── Orders ────────────────────────────────────────────────────────────────

    def submit_market_order(
        self, symbol: str, qty: float, side: str, reason: str = ""
    ) -> dict:
        """
        Submit a market order. side = 'buy' | 'sell'.
        Returns order info dict. Raises on failure.
        """
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        req = MarketOrderRequest(
            symbol=symbol,
            qty=round(qty, 4),
            side=order_side,
            time_in_force=TimeInForce.DAY,
        )
        order = self.trading.submit_order(req)
        logger.info(
            "ORDER SUBMITTED | %s %s %.4f | reason: %s | id: %s",
            side.upper(), symbol, qty, reason, order.id,
        )
        return {
            "id":     str(order.id),
            "symbol": symbol,
            "side":   side,
            "qty":    float(qty),
            "status": str(order.status),
            "reason": reason,
        }

    def submit_limit_order(
        self, symbol: str, qty: float, side: str, limit_price: float, reason: str = ""
    ) -> dict:
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        req = LimitOrderRequest(
            symbol=symbol,
            qty=round(qty, 4),
            side=order_side,
            time_in_force=TimeInForce.DAY,
            limit_price=round(limit_price, 2),
        )
        order = self.trading.submit_order(req)
        logger.info(
            "LIMIT ORDER | %s %s %.4f @ $%.2f | reason: %s",
            side.upper(), symbol, qty, limit_price, reason,
        )
        return {
            "id":          str(order.id),
            "symbol":      symbol,
            "side":        side,
            "qty":         float(qty),
            "limit_price": float(limit_price),
            "status":      str(order.status),
        }

    def close_position(self, symbol: str, reason: str = "") -> dict | None:
        """Close entire position in a symbol."""
        try:
            order = self.trading.close_position(symbol)
            logger.info("CLOSE POSITION | %s | reason: %s", symbol, reason)
            return {"symbol": symbol, "status": str(order.status), "reason": reason}
        except Exception as e:
            logger.warning("Could not close %s: %s", symbol, e)
            return None

    def cancel_all_orders(self):
        self.trading.cancel_orders()
        logger.info("All open orders cancelled")

    # ── Market data ───────────────────────────────────────────────────────────

    def get_bars(self, symbol: str, days: int = 60) -> list[dict]:
        """
        Return daily OHLCV bars for the last `days` calendar days.
        Returns list of dicts sorted oldest → newest.
        """
        end   = datetime.now(ET)
        start = end - timedelta(days=days + 10)  # buffer for weekends/holidays
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        bars = self.data.get_stock_bars(req)
        result = []
        if symbol in bars.data:
            for b in bars.data[symbol]:
                result.append({
                    "date":   b.timestamp.strftime("%Y-%m-%d"),
                    "open":   float(b.open),
                    "high":   float(b.high),
                    "low":    float(b.low),
                    "close":  float(b.close),
                    "volume": int(b.volume),
                })
        return sorted(result, key=lambda x: x["date"])[-days:]

    def get_latest_price(self, symbol: str) -> float | None:
        """Return latest trade price for a symbol."""
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            quote = self.data.get_stock_latest_quote(req)
            return float(quote[symbol].ask_price)
        except Exception as e:
            logger.warning("Could not get price for %s: %s", symbol, e)
            return None

    # ── Market hours check ────────────────────────────────────────────────────

    def is_market_open(self) -> bool:
        clock = self.trading.get_clock()
        return clock.is_open

    def minutes_to_open(self) -> int:
        clock = self.trading.get_clock()
        if clock.is_open:
            return 0
        delta = clock.next_open - datetime.now(ET)
        return max(0, int(delta.total_seconds() / 60))

    def minutes_to_close(self) -> int:
        clock = self.trading.get_clock()
        if not clock.is_open:
            return 0
        delta = clock.next_close - datetime.now(ET)
        return max(0, int(delta.total_seconds() / 60))
