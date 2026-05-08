"""
core/data_fetcher.py
Fetches market data, fundamentals, and news.
Sources: Alpaca (price/news), yfinance (fundamentals, VIX).
No Finnhub dependency.
"""

import logging
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

import config

logger = logging.getLogger(__name__)


class DataFetcher:
    def __init__(self):
        # Alpaca news endpoint (uses same keys as trading)
        self._alpaca_data_url = "https://data.alpaca.markets/v1beta1"
        self._headers = {
            "APCA-API-KEY-ID":     config.ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
        }

    # ── Quote (price + market cap) ────────────────────────────────────────────

    def get_quote(self, symbol: str) -> dict | None:
        """Return current price, volume, market cap for one symbol."""
        try:
            t    = yf.Ticker(symbol)
            info = t.fast_info
            return {
                "symbol":     symbol,
                "price":      round(float(info.last_price or 0), 2),
                "volume":     int(info.three_month_average_volume or 0),
                "market_cap": float(info.market_cap or 0),
            }
        except Exception as e:
            logger.debug("Quote failed %s: %s", symbol, e)
            return None

    # ── OHLCV ─────────────────────────────────────────────────────────────────

    def get_ohlcv(self, symbol: str, days: int = 60) -> list[dict]:
        """Return daily OHLCV as list of dicts, oldest first."""
        try:
            df = yf.download(
                symbol, period=f"{days+10}d",
                progress=False, auto_adjust=True
            )
            if df.empty:
                return []
            df = df.reset_index()
            # Flatten MultiIndex columns if present
            df.columns = [
                c[0] if isinstance(c, tuple) else c
                for c in df.columns
            ]
            rows = []
            for _, row in df.iterrows():
                rows.append({
                    "date":   pd.Timestamp(row["Date"]).strftime("%Y-%m-%d"),
                    "open":   round(float(row["Open"]), 2),
                    "high":   round(float(row["High"]), 2),
                    "low":    round(float(row["Low"]), 2),
                    "close":  round(float(row["Close"]), 2),
                    "volume": int(row["Volume"]),
                })
            return rows[-days:]
        except Exception as e:
            logger.warning("OHLCV failed %s: %s", symbol, e)
            return []

    # ── News (Alpaca News API) ────────────────────────────────────────────────

    def get_news(self, symbol: str, days: int = 3) -> list[dict]:
        """
        Return up to 10 recent news headlines via Alpaca News API.
        Falls back to empty list on any error.
        """
        try:
            end   = datetime.utcnow()
            start = end - timedelta(days=days)
            params = {
                "symbols":    symbol,
                "start":      start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end":        end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit":      10,
                "sort":       "desc",
            }
            r = requests.get(
                f"{self._alpaca_data_url}/news",
                headers=self._headers,
                params=params,
                timeout=10,
            )
            r.raise_for_status()
            articles = r.json().get("news", [])
            return [
                {
                    "headline": a.get("headline", ""),
                    "source":   a.get("source", ""),
                    "datetime": a.get("created_at", "")[:16],
                }
                for a in articles
            ]
        except Exception as e:
            logger.debug("News fetch failed %s: %s", symbol, e)
            return []

    # ── Fundamentals (yfinance) ───────────────────────────────────────────────

    def get_fundamentals(self, symbol: str) -> dict | None:
        """Return key fundamental metrics via yfinance."""
        try:
            info = yf.Ticker(symbol).info
            return {
                "symbol":          symbol,
                "pe_ttm":          info.get("trailingPE"),
                "pe_forward":      info.get("forwardPE"),
                "eps_growth":      info.get("earningsGrowth"),
                "revenue_growth":  info.get("revenueGrowth"),
                "debt_equity":     info.get("debtToEquity"),
                "profit_margin":   info.get("profitMargins"),
                "insider_pct":     info.get("heldPercentInsiders"),
            }
        except Exception as e:
            logger.warning("Fundamentals failed %s: %s", symbol, e)
            return None

    # ── VIX ───────────────────────────────────────────────────────────────────

    def get_vix(self) -> float | None:
        """Return current VIX level for circuit breaker check."""
        try:
            return round(float(yf.Ticker("^VIX").fast_info.last_price), 2)
        except Exception as e:
            logger.warning("VIX fetch failed: %s", e)
            return None
