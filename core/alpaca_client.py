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
        # LIVE trading must never happen silently. Make it loud so a misconfigured
        # PAPER_TRADING=false is impossible to miss in the logs.
        if not config.PAPER_TRADING:
            logger.warning(
                "=" * 60 + "\n"
                "!!! LIVE TRADING ENABLED (PAPER_TRADING=false) — REAL MONEY !!!\n"
                + "=" * 60
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
        self, symbol: str, qty: float, side: str, reason: str = "",
        ref_price: float | None = None
    ) -> dict | None:
        """
        Submit a market order. side = 'buy' | 'sell'.
        Returns order info dict, or None on failure.

        C3: previously this raised on any Alpaca rejection (insufficient buying power,
        halted symbol, wash-trade block, rate limit). The caller guards with `if order:`,
        so a raised exception propagated out of analyse_symbol and aborted the whole
        run before log_run/push — a genuine buy vanished with no journal trace. We now
        mirror close_position() and return None on failure so the guard works.

        Safety backstop: when a BUY passes ref_price, reject it here if its notional
        (qty × ref_price) exceeds config.MAX_ORDER_NOTIONAL_USD — an independent guard
        against an upstream sizing bug. Sells are never capped so an exit can't be
        blocked. Disabled if ref_price is omitted or the cap is 0.
        """
        cap = getattr(config, "MAX_ORDER_NOTIONAL_USD", 0)
        if side.lower() == "buy" and ref_price and cap and qty * ref_price > cap:
            logger.error(
                "Order BLOCKED by notional cap | BUY %s %.4f × $%.2f = $%.0f > $%.0f | reason: %s",
                symbol, qty, ref_price, qty * ref_price, cap, reason,
            )
            return None

        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        req = MarketOrderRequest(
            symbol=symbol,
            qty=round(qty, 4),
            side=order_side,
            time_in_force=TimeInForce.DAY,
        )
        try:
            order = self.trading.submit_order(req)
        except Exception as e:
            logger.warning(
                "Order submit FAILED | %s %s %.4f | reason: %s | error: %s",
                side.upper(), symbol, qty, reason, e,
            )
            return None
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
        """
        Close entire position in a symbol.
        S1: any resting stop order is cancelled first — Alpaca holds the shares
        against the open sell stop, so close_position would otherwise be rejected
        for insufficient available qty.
        """
        try:
            self.cancel_stop_orders(symbol)
            order = self.trading.close_position(symbol)
            logger.info("CLOSE POSITION | %s | reason: %s", symbol, reason)
            return {"symbol": symbol, "status": str(order.status), "reason": reason}
        except Exception as e:
            logger.warning("Could not close %s: %s", symbol, e)
            return None

    # ── Stop orders (S1 — broker-native stops) ────────────────────────────────

    def submit_stop_order(
        self, symbol: str, qty: float, stop_price: float, reason: str = ""
    ) -> dict | None:
        """
        Submit a GTC sell STOP order so downside protection rests at the broker
        between pipeline runs (the software stop only samples a few times a day).

        Alpaca constraint: fractional orders must be DAY orders, so a GTC stop can
        only cover WHOLE shares. We floor the qty — the fractional remainder stays
        protected by the software stop fallback (a few dollars of exposure, vs the
        whole position unprotected overnight). qty < 1 share → no broker stop.
        Returns order dict or None on failure — callers fall back to software stops.
        """
        whole_qty = int(qty)
        if whole_qty < 1:
            logger.info("Stop order skipped | %s qty %.4f < 1 whole share — "
                        "software stop fallback covers", symbol, qty)
            return None
        try:
            from alpaca.trading.requests import StopOrderRequest
            req = StopOrderRequest(
                symbol=symbol,
                qty=whole_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=round(stop_price, 2),
            )
            order = self.trading.submit_order(req)
            logger.info("STOP ORDER SUBMITTED | %s %d @ $%.2f | %s | id: %s",
                        symbol, whole_qty, stop_price, reason, order.id)
            return {"id": str(order.id), "symbol": symbol, "qty": float(whole_qty),
                    "stop_price": round(stop_price, 2), "status": str(order.status)}
        except Exception as e:
            logger.warning("Stop order submit FAILED | %s %d @ $%.2f: %s",
                           symbol, whole_qty, stop_price, e)
            return None

    def replace_stop_order(
        self, order_id: str, new_stop_price: float, new_qty: float | None = None
    ) -> dict | None:
        """
        Ratchet/resize a resting stop order in place. Returns None on failure.
        qty is floored to whole shares (GTC stops can't be fractional); a resize
        below 1 share cancels instead — software fallback covers the dust.
        """
        try:
            from alpaca.trading.requests import ReplaceOrderRequest
            kwargs = {"stop_price": round(new_stop_price, 2)}
            if new_qty is not None:
                whole = int(new_qty)
                if whole < 1:
                    self.trading.cancel_order_by_id(order_id)
                    logger.info("STOP ORDER CANCELLED (resize < 1 share) | id=%s", order_id)
                    return None
                kwargs["qty"] = whole
            order = self.trading.replace_order_by_id(order_id, ReplaceOrderRequest(**kwargs))
            logger.info("STOP ORDER REPLACED | id=%s → stop=$%.2f qty=%s",
                        order_id, new_stop_price, new_qty if new_qty is not None else "unchanged")
            return {"id": str(order.id), "stop_price": round(new_stop_price, 2)}
        except Exception as e:
            logger.warning("Stop order replace FAILED | id=%s: %s", order_id, e)
            return None

    def get_order(self, order_id: str) -> dict | None:
        """Return key fields of an order by id (status, filled qty/price)."""
        try:
            o = self.trading.get_order_by_id(order_id)
            return {
                "id":               str(o.id),
                "symbol":           o.symbol,
                "status":           str(o.status.value if hasattr(o.status, "value") else o.status),
                "qty":              float(o.qty or 0),
                "filled_qty":       float(o.filled_qty or 0),
                "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else None,
            }
        except Exception as e:
            logger.warning("get_order failed | id=%s: %s", order_id, e)
            return None

    def get_open_stop_orders(self, symbol: str) -> list[dict]:
        """Return open sell STOP orders for a symbol."""
        try:
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
            orders = self.trading.get_orders(req)
            out = []
            for o in orders:
                otype = str(getattr(o, "type", "") or getattr(o, "order_type", ""))
                if "stop" in otype.lower() and str(o.side).lower().endswith("sell"):
                    out.append({
                        "id":         str(o.id),
                        "symbol":     o.symbol,
                        "qty":        float(o.qty or 0),
                        "stop_price": float(o.stop_price) if getattr(o, "stop_price", None) else None,
                    })
            return out
        except Exception as e:
            logger.warning("get_open_stop_orders failed | %s: %s", symbol, e)
            return []

    def get_last_filled_buy(self, symbol: str, lookback_days: int = 90) -> dict | None:
        """Most recent FILLED buy order for a symbol — used to recover a lost entry
        timestamp / fill price when reconciling an untracked broker position. Returns
        {filled_at (iso), filled_avg_price, qty} or None. Read-only.

        Uses status=ALL + an explicit `after` window because get_orders defaults to a
        short recent window and to non-closed orders, which silently misses older fills.
        """
        try:
            after = datetime.now(pytz.UTC) - timedelta(days=lookback_days)
            req = GetOrdersRequest(status=QueryOrderStatus.ALL, after=after,
                                   symbols=[symbol], limit=500)
            orders = self.trading.get_orders(req)
        except Exception as e:
            logger.warning("get_last_filled_buy failed | %s: %s", symbol, e)
            return None
        fills = [o for o in orders
                 if str(o.side).lower().endswith("buy") and getattr(o, "filled_at", None)]
        if not fills:
            return None
        o = max(fills, key=lambda x: x.filled_at)
        return {
            "filled_at":        o.filled_at.isoformat(),
            "filled_avg_price": float(o.filled_avg_price) if getattr(o, "filled_avg_price", None) else None,
            "qty":              float(o.filled_qty) if getattr(o, "filled_qty", None) else None,
        }

    def cancel_stop_orders(self, symbol: str) -> int:
        """Cancel all resting sell stop orders for a symbol. Returns count cancelled."""
        cancelled = 0
        for o in self.get_open_stop_orders(symbol):
            try:
                self.trading.cancel_order_by_id(o["id"])
                cancelled += 1
                logger.info("STOP ORDER CANCELLED | %s id=%s", symbol, o["id"])
            except Exception as e:
                logger.warning("Stop cancel failed | %s id=%s: %s", symbol, o["id"], e)
        return cancelled

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

    def minutes_since_open(self) -> int:
        """Return minutes elapsed since today's market open, or 0 if closed."""
        clock = self.trading.get_clock()
        if not clock.is_open:
            return 0
        delta = datetime.now(ET) - clock.next_open
        if delta.total_seconds() < 0:
            # Some Alpaca clock implementations expose the next session's open.
            market_open = datetime.now(ET).replace(hour=9, minute=30, second=0, microsecond=0)
            delta = datetime.now(ET) - market_open
        return max(0, int(delta.total_seconds() / 60))

    # ── Options (Phase 0 scaffolding) ─────────────────────────────────────────
    # All methods below are additive and never called from the existing pipeline
    # (gated by OPTIONS_ENABLED=False in config). They become active in Phase 1+.

    def get_option_contracts(
        self, symbol: str, option_type: str = "call",
        expiry_gte: str = "", expiry_lte: str = "",
        limit: int = 200,
    ) -> list[dict]:
        """
        Query the option chain for an underlying. Returns a list of contract dicts
        {occ_symbol, strike, expiry, type}. Uses the trading client (contract metadata
        is entitlement-free on all Alpaca plans — no feed subscription needed).
        """
        try:
            from alpaca.trading.requests import GetOptionContractsRequest
            from alpaca.trading.enums import ContractType
            req = GetOptionContractsRequest(
                underlying_symbols=[symbol],
                type=ContractType.CALL if option_type.lower() == "call" else ContractType.PUT,
                expiration_date_gte=expiry_gte or None,
                expiration_date_lte=expiry_lte or None,
                status="active",
                limit=limit,
            )
            result = self.trading.get_option_contracts(req)
            contracts = getattr(result, "option_contracts", []) or []
            return [
                {
                    "occ_symbol": c.symbol,
                    "strike":     float(c.strike_price),
                    "expiry":     str(c.expiration_date)[:10],
                    "type":       option_type.lower(),
                    "open_interest": getattr(c, "open_interest", None),
                }
                for c in contracts
            ]
        except Exception as e:
            logger.warning("get_option_contracts failed for %s: %s", symbol, e)
            return []

    def get_option_snapshot(self, occ_symbols: list[str]) -> dict:
        """
        Fetch the latest indicative-feed snapshot (quote + greeks, 15-min delayed
        on Basic plan) for a list of OCC symbols. Used ONLY as a cross-check against
        local BSM greeks — do NOT use as the primary pricing source. Returns {} on
        any entitlement/feed failure.
        """
        if not occ_symbols:
            return {}
        try:
            from alpaca.data.historical.option import OptionHistoricalDataClient
            from alpaca.data.requests import OptionLatestQuoteRequest
            data_client = OptionHistoricalDataClient(
                api_key=config.ALPACA_API_KEY,
                secret_key=config.ALPACA_SECRET_KEY,
            )
            req    = OptionLatestQuoteRequest(symbol_or_symbols=occ_symbols, feed="indicative")
            quotes = data_client.get_option_latest_quote(req)
            out = {}
            for sym, q in (quotes or {}).items():
                mid = None
                if hasattr(q, "ask_price") and hasattr(q, "bid_price"):
                    ask = getattr(q, "ask_price", 0) or 0
                    bid = getattr(q, "bid_price", 0) or 0
                    if ask > 0 and bid > 0:
                        mid = round((ask + bid) / 2, 4)
                out[sym] = {"mid": mid, "ask": getattr(q, "ask_price", None),
                            "bid": getattr(q, "bid_price", None)}
            return out
        except Exception as e:
            logger.debug("get_option_snapshot failed (%s) — indicative feed unavailable", e)
            return {}

    def submit_vertical_spread(
        self,
        long_occ: str,
        short_occ: str,
        qty: int,
        net_limit: float,
        reason: str = "",
    ) -> dict | None:
        """
        Submit a vertical debit spread as a single MLEG order.
        net_limit = net debit per spread (positive = debit, e.g. 1.50 = $1.50/spread).
        Phase 1 (shadow mode) never calls this. Phase 2+ calls this for live paper trades.
        Returns order dict or None on failure.
        """
        try:
            from alpaca.trading.requests import OptionLegRequest
            from alpaca.trading.enums import OrderClass, PositionIntent, OrderType
            order = self.trading.submit_order(
                order_data={
                    "order_class": "mleg",
                    "qty":         str(qty),
                    "type":        "limit",
                    "limit_price": str(round(net_limit, 2)),
                    "time_in_force": "day",
                    "legs": [
                        {"symbol": long_occ,  "ratio_qty": "1",
                         "side": "buy",  "position_intent": "buy_to_open"},
                        {"symbol": short_occ, "ratio_qty": "1",
                         "side": "sell", "position_intent": "sell_to_open"},
                    ],
                }
            )
            logger.info(
                "SPREAD SUBMITTED | %s/%s qty=%d net_debit=%.2f | %s",
                long_occ, short_occ, qty, net_limit, reason,
            )
            return {"id": str(order.id), "long_occ": long_occ, "short_occ": short_occ,
                    "qty": qty, "net_limit": net_limit, "reason": reason}
        except Exception as e:
            logger.warning("submit_vertical_spread failed: %s", e)
            return None

    def close_option_position(
        self, occ_symbol: str, qty: int | None = None, reason: str = ""
    ) -> dict | None:
        """
        Close (or partially close) an option position by OCC symbol.
        qty=None closes the full position. Returns order dict or None.
        """
        try:
            if qty is None:
                order = self.trading.close_position(occ_symbol)
            else:
                order = self.trading.submit_order(
                    order_data={
                        "symbol": occ_symbol,
                        "qty": str(qty),
                        "side": "sell",
                        "type": "market",
                        "time_in_force": "day",
                        "position_intent": "sell_to_close",
                    }
                )
            logger.info("CLOSE OPTION | %s qty=%s | %s", occ_symbol, qty or "all", reason)
            return {"symbol": occ_symbol, "qty": qty, "reason": reason}
        except Exception as e:
            logger.warning("close_option_position failed for %s: %s", occ_symbol, e)
            return None

    def get_account_extended(self) -> dict:
        """
        Extended account fields needed for options/margin accounting:
        long_market_value, short_market_value, regt_buying_power,
        maintenance_margin, initial_margin, multiplier.
        Supplements get_account(); all fields default to None if unavailable
        so existing code paths are unaffected.
        """
        try:
            acct = self.trading.get_account()
            base = self.get_account()
            base.update({
                "long_market_value":    float(getattr(acct, "long_market_value",  0) or 0),
                "short_market_value":   float(getattr(acct, "short_market_value", 0) or 0),
                "regt_buying_power":    float(getattr(acct, "regt_buying_power",  0) or 0),
                "maintenance_margin":   float(getattr(acct, "maintenance_margin", 0) or 0),
                "initial_margin":       float(getattr(acct, "initial_margin",     0) or 0),
                "multiplier":           float(getattr(acct, "multiplier",         1) or 1),
            })
            return base
        except Exception as e:
            logger.warning("get_account_extended failed: %s", e)
            return self.get_account()
