"""
core/data_fetcher.py
Fetches market data, fundamentals, and news.
Sources: Alpaca (price/news), yfinance (fundamentals, VIX).
No Finnhub dependency.
"""

import logging
import random
import time

import requests
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger(__name__)


def _sanitize_news_text(text: str, limit: int = 240) -> str:
    """Defense-in-depth against prompt injection via third-party news text:
    drop control characters/newlines and cap length so a crafted headline can't
    smuggle a large instruction block into an LLM prompt. Real headlines are
    short, single-line, plain text, so this leaves normal inputs unchanged."""
    if not text:
        return ""
    cleaned = "".join(ch if (ch >= " " and ch != "\x7f") else " " for ch in text)
    return " ".join(cleaned.split())[:limit]

# Audit §7.5: yfinance on shared CI runners is the most common silent-degradation
# point (rate limits). One cheap retry with jittered backoff recovers transient
# failures without masking a real outage (the screener's probe check still catches
# a full outage and logs 'data_unavailable').
_RETRIES = 2
_BACKOFF_S = 1.5


def _with_retry(fn, what: str):
    last_exc = None
    for attempt in range(_RETRIES + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < _RETRIES:
                time.sleep(_BACKOFF_S * (attempt + 1) + random.uniform(0, 0.5))
    raise last_exc


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
        def _fetch():
            info = yf.Ticker(symbol).fast_info
            # A missing last_price must yield None (treated as "no quote"), not a
            # truthy dict with price=0.0 — a zero price passes `if not quote:` guards
            # and reaches sizing math / percent-change division downstream.
            if not info.last_price:
                raise ValueError(f"no last_price for {symbol}")
            return {
                "symbol":     symbol,
                "price":      round(float(info.last_price), 2),
                "volume":     int(info.three_month_average_volume or 0),
                "market_cap": float(info.market_cap or 0),
            }
        try:
            return _with_retry(_fetch, f"quote {symbol}")
        except Exception as e:
            logger.debug("Quote failed %s: %s", symbol, e)
            return None

    # ── OHLCV ─────────────────────────────────────────────────────────────────

    def get_ohlcv(self, symbol: str, days: int = 60) -> list[dict]:
        """Return daily OHLCV as list of dicts, oldest first."""
        try:
            def _dl():
                out = yf.download(
                    symbol, period=f"{days+10}d",
                    progress=False, auto_adjust=True
                )
                if out.empty:
                    raise RuntimeError("empty dataframe")
                return out
            try:
                df = _with_retry(_dl, f"ohlcv {symbol}")
            except Exception:
                return []
            df = df.reset_index()
            # Flatten MultiIndex columns if present
            df.columns = [
                c[0] if isinstance(c, tuple) else c
                for c in df.columns
            ]
            # Drop rows with NaN prices — yfinance occasionally returns them for
            # cross-listed names; NaN poisons every downstream comparison (RSI,
            # ATR, discovery guards) and is invalid in strict JSON.
            df = df.dropna(subset=[c for c in ("Open", "High", "Low", "Close", "Volume")
                                   if c in df.columns])
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
            end   = datetime.now(timezone.utc).replace(tzinfo=None)
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
                    "headline": _sanitize_news_text(a.get("headline", "")),
                    "source":   _sanitize_news_text(a.get("source", ""), limit=64),
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

    # ── Market screeners (Alpaca, EXP-006 dynamic universe) ──────────────────

    def get_most_actives(self, top: int = 50) -> list[dict]:
        """
        Market-wide most-active stocks by volume (Alpaca screener endpoint,
        entitled on all plans with the existing keys). Returns
        [{symbol, volume, trade_count}], [] on any failure.
        """
        try:
            r = requests.get(
                f"{self._alpaca_data_url}/screener/stocks/most-actives",
                headers=self._headers,
                params={"by": "volume", "top": top},
                timeout=10,
            )
            r.raise_for_status()
            return r.json().get("most_actives", []) or []
        except Exception as e:
            logger.warning("most-actives fetch failed: %s", e)
            return []

    def get_market_movers(self, top: int = 50) -> dict:
        """
        Market-wide top movers (Alpaca screener endpoint). Returns
        {"gainers": [{symbol, percent_change, change, price}], "losers": [...]},
        {} on any failure.
        """
        try:
            r = requests.get(
                f"{self._alpaca_data_url}/screener/stocks/movers",
                headers=self._headers,
                params={"top": top},
                timeout=10,
            )
            r.raise_for_status()
            return r.json() or {}
        except Exception as e:
            logger.warning("movers fetch failed: %s", e)
            return {}

    # ── VIX ───────────────────────────────────────────────────────────────────

    def get_vix(self) -> float | None:
        """Return current VIX level for circuit breaker check."""
        try:
            return round(float(yf.Ticker("^VIX").fast_info.last_price), 2)
        except Exception as e:
            logger.warning("VIX fetch failed: %s", e)
            return None
