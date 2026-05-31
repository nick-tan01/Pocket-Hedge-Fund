"""
After-close watchlist builder.

Creates deterministic evidence cards from completed regular-session bars. These
cards are market memory for the next run, not trading instructions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yfinance as yf

import config
from agents.screener import (
    BEARISH_KEYWORDS,
    BULLISH_KEYWORDS,
    Screener,
)
from core.data_fetcher import DataFetcher
from core.journal import get_open_trades

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# C20b: NYSE full-day closures. The watchlist-expiry calendar must skip these (not
# just weekends) so memory never expires on, or carries into, a closed session.
# Covers the 6-month strategy horizon (May–Dec 2026) plus early 2027 for rollover.
US_MARKET_HOLIDAYS = frozenset({
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),   # Independence Day (observed)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
    date(2027, 1, 1),   # New Year's Day
    date(2027, 1, 18),  # MLK Jr. Day
    date(2027, 2, 15),  # Washington's Birthday
})

LONG_SETUP_TYPES = {
    "long_momentum_continuation",
    "post_earnings_drift",
    "relative_strength_leader",
}


@dataclass
class WatchlistCard:
    symbol: str
    score: float
    tier: str
    setup_type: str
    side: str
    reason: str
    stable_signals: dict = field(default_factory=dict)
    risk_flags: dict = field(default_factory=dict)
    secondary_setups: list[str] = field(default_factory=list)

    def to_dict(self, generated_at: str, expires_at: str) -> dict:
        return {
            "symbol": self.symbol,
            "score": round(self.score, 4),
            "tier": self.tier,
            "setup_type": self.setup_type,
            "side": self.side,
            "reason": self.reason,
            "stable_signals": self.stable_signals,
            "risk_flags": self.risk_flags,
            "secondary_setups": self.secondary_setups,
            "status": "pending_revalidation",
            "generated_at": generated_at,
            "expires_at": expires_at,
            "source": "after_close_regular_session",
            "data_source": "yfinance_daily_ohlcv",
        }


def _utc_iso(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _next_trading_morning(now_et: datetime) -> datetime:
    day = now_et.date() + timedelta(days=1)
    # C20b: skip weekends AND full-day market holidays.
    while day.weekday() >= 5 or day in US_MARKET_HOLIDAYS:
        day += timedelta(days=1)
    return datetime(
        day.year,
        day.month,
        day.day,
        config.WATCHLIST_EXPIRE_HOUR_ET,
        config.WATCHLIST_EXPIRE_MINUTE_ET,
        tzinfo=ET,
    )


def _safe_pct(new: float, old: float) -> float:
    return (new - old) / old if old else 0.0


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _true_ranges(bars: list[dict]) -> list[float]:
    ranges = []
    prev_close = None
    for bar in bars:
        high = float(bar["high"])
        low = float(bar["low"])
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        ranges.append(tr)
        prev_close = float(bar["close"])
    return ranges


def _ret(bars: list[dict], lookback: int) -> float:
    if len(bars) <= lookback:
        return 0.0
    return _safe_pct(float(bars[-1]["close"]), float(bars[-lookback - 1]["close"]))


def _download_ohlcv_map(symbols: list[str], days: int = 70) -> dict[str, list[dict]]:
    tickers = sorted(set(symbols))
    if not tickers:
        return {}
    try:
        df = yf.download(
            tickers=tickers,
            period=f"{days + 10}d",
            progress=False,
            auto_adjust=True,
            group_by="ticker",
            threads=True,
        )
    except Exception as exc:
        logger.warning("After-close batch OHLCV download failed: %s", exc)
        return {}

    out: dict[str, list[dict]] = {}
    if df.empty:
        return out

    for symbol in tickers:
        try:
            if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
                if symbol not in df.columns.get_level_values(0):
                    continue
                sub = df[symbol].copy()
            else:
                if len(tickers) != 1:
                    continue
                sub = df.copy()
            sub = sub.dropna(subset=["Open", "High", "Low", "Close", "Volume"]).reset_index()
            rows = []
            for _, row in sub.iterrows():
                rows.append({
                    "date":   row["Date"].strftime("%Y-%m-%d"),
                    "open":   round(float(row["Open"]), 2),
                    "high":   round(float(row["High"]), 2),
                    "low":    round(float(row["Low"]), 2),
                    "close":  round(float(row["Close"]), 2),
                    "volume": int(row["Volume"]),
                })
            out[symbol] = rows[-days:]
        except Exception as exc:
            logger.debug("After-close OHLCV parse failed %s: %s", symbol, exc)
    return out


def _news_signal(fetcher: DataFetcher, symbol: str) -> tuple[float, dict]:
    articles = fetcher.get_news(symbol, days=3)
    if not articles:
        return 0.0, {"news_count": 0, "top_headline": ""}

    score = 0.0
    earnings_like = False
    bearish_like = False
    for article in articles[:5]:
        headline = article.get("headline", "").lower()
        if any(k in headline for k in BULLISH_KEYWORDS):
            score += 0.20
        if any(k in headline for k in BEARISH_KEYWORDS):
            score -= 0.18
            bearish_like = True
        if any(k in headline for k in ("earnings", "revenue", "eps", "guidance", "beat", "beats")):
            earnings_like = True

    return _clamp(score), {
        "news_count": len(articles),
        "top_headline": articles[0].get("headline", ""),
        "news_quality": round(_clamp(score), 2),
        "earnings_like_news": earnings_like,
        "bearish_like_news": bearish_like,
    }


def _score_symbol(
    fetcher: DataFetcher,
    symbol: str,
    open_symbols: set[str],
    bar_map: dict[str, list[dict]],
) -> WatchlistCard | None:
    bars = bar_map.get(symbol, [])
    if len(bars) < 35:
        return None

    latest = bars[-1]
    prev = bars[-2]
    close = float(latest["close"])
    if close < config.MIN_PRICE:
        return None
    high = float(latest["high"])
    low = float(latest["low"])
    volume = float(latest["volume"])
    avg_volume = sum(float(b["volume"]) for b in bars[-21:-1]) / 20
    volume_ratio = volume / avg_volume if avg_volume else 0.0
    day_return = _safe_pct(close, float(prev["close"]))
    ret_5d = _ret(bars, 5)
    ret_20d = _ret(bars, 20)
    close_location = (close - low) / (high - low) if high > low else 0.5

    trs = _true_ranges(bars)
    atr14 = sum(trs[-14:]) / 14 if len(trs) >= 14 else 0.0
    avg_tr20 = sum(trs[-21:-1]) / 20 if len(trs) >= 21 else 0.0
    atr_pct = atr14 / close if close else 0.0
    range_expansion = trs[-1] / avg_tr20 if avg_tr20 else 0.0

    prior_20 = bars[-21:-1]
    prior_high_20 = max(float(b["high"]) for b in prior_20)
    prior_low_20 = min(float(b["low"]) for b in prior_20)
    breakout_20d = close >= prior_high_20
    breakdown_20d = close <= prior_low_20
    failed_breakout = high >= prior_high_20 and close_location <= 0.35 and volume_ratio >= 1.4

    spy_bars = bar_map.get("SPY", [])
    spy_ret_20d = _ret(spy_bars, 20) if len(spy_bars) >= 21 else 0.0
    rs_vs_spy = ret_20d - spy_ret_20d

    rs_vs_sector = None

    near_high = close_location >= 0.75
    near_low = close_location <= 0.25
    strong_volume = volume_ratio >= 1.5
    strong_rs = rs_vs_spy >= 0.03

    needs_news = (
        (day_return >= 0.01 and volume_ratio >= 1.2 and close_location >= 0.60) or
        day_return <= -0.025 or
        symbol in open_symbols
    )
    if needs_news:
        news_score, news = _news_signal(fetcher, symbol)
    else:
        news_score, news = 0.0, {"news_count": 0, "top_headline": "", "news_quality": 0.0}
    earnings_soon = None
    earnings_tailwind = news.get("earnings_like_news")

    setup_scores: dict[str, float] = {}
    if day_return >= 0.02 and strong_volume and near_high:
        setup_scores["long_momentum_continuation"] = (
            0.25 * _clamp(day_return / 0.06) +
            0.25 * _clamp((volume_ratio - 1.0) / 2.0) +
            0.20 * close_location +
            0.20 * _clamp((rs_vs_spy + 0.02) / 0.10) +
            0.10 * (1.0 if breakout_20d else 0.4)
        )
    if earnings_tailwind and day_return >= 0.01 and volume_ratio >= 1.2 and close_location >= 0.60:
        setup_scores["post_earnings_drift"] = (
            0.25 * _clamp(day_return / 0.05) +
            0.25 * _clamp((volume_ratio - 1.0) / 2.0) +
            0.20 * close_location +
            0.15 * news_score +
            0.15 * _clamp((ret_5d + 0.02) / 0.08)
        )
    if strong_rs and close_location >= 0.60:
        setup_scores["relative_strength_leader"] = (
            0.35 * _clamp((rs_vs_spy + 0.01) / 0.10) +
            0.25 * _clamp(((rs_vs_sector or 0.0) + 0.01) / 0.08) +
            0.20 * close_location +
            0.10 * _clamp((volume_ratio - 0.8) / 1.7) +
            0.10 * (1.0 if breakout_20d else 0.3)
        )
    if failed_breakout:
        setup_scores["failed_breakout_avoid"] = (
            0.35 * (1.0 - close_location) +
            0.30 * _clamp((volume_ratio - 1.0) / 2.5) +
            0.20 * _clamp(range_expansion / 2.5) +
            0.15 * _clamp(day_return * -1 / 0.04)
        )
    if (near_low and strong_volume) or day_return <= -0.025 or breakdown_20d:
        setup_scores["hedge_or_weakness_watch"] = (
            0.30 * (1.0 - close_location) +
            0.25 * _clamp((volume_ratio - 1.0) / 2.5) +
            0.25 * _clamp(day_return * -1 / 0.06) +
            0.20 * (1.0 if breakdown_20d else 0.3)
        )
    if symbol in open_symbols and (near_low or rs_vs_spy <= -0.03 or failed_breakout):
        setup_scores["existing_position_alert"] = max(
            setup_scores.get("existing_position_alert", 0.0),
            0.65 + (0.15 if failed_breakout else 0.0),
        )

    if not setup_scores:
        return None

    setup_type, score = max(setup_scores.items(), key=lambda item: item[1])
    secondary = [k for k, v in sorted(setup_scores.items(), key=lambda item: item[1], reverse=True)[1:] if v >= 0.50]
    side = "long_watch" if setup_type in LONG_SETUP_TYPES else "weakness_or_hedge_watch"
    if setup_type == "existing_position_alert":
        side = "position_alert"

    tier = "A" if score >= 0.75 else "B" if score >= 0.60 else "C"
    signal_text = [
        f"close location {close_location:.0%}",
        f"day {day_return * 100:+.1f}%",
        f"volume {volume_ratio:.1f}x",
        f"RS vs SPY {rs_vs_spy * 100:+.1f}%",
    ]
    reason = f"{setup_type.replace('_', ' ')}: " + ", ".join(signal_text)

    return WatchlistCard(
        symbol=symbol,
        score=score,
        tier=tier,
        setup_type=setup_type,
        side=side,
        reason=reason,
        stable_signals={
            "asof_date": latest.get("date"),
            "close": round(close, 2),
            "day_return_pct": round(day_return * 100, 2),
            "return_5d_pct": round(ret_5d * 100, 2),
            "return_20d_pct": round(ret_20d * 100, 2),
            "rs_vs_spy_20d_pct": round(rs_vs_spy * 100, 2),
            "rs_vs_sector_20d_pct": round(rs_vs_sector * 100, 2) if rs_vs_sector is not None else None,
            "sector": None,
            "sector_etf": None,
            "volume_ratio_20d": round(volume_ratio, 2),
            "close_location": round(close_location, 3),
            "atr14_pct": round(atr_pct * 100, 2),
            "range_expansion": round(range_expansion, 2),
            "breakout_20d": breakout_20d,
            "breakdown_20d": breakdown_20d,
            "news": news,
        },
        risk_flags={
            "earnings_within_3d": earnings_soon,
            "close_near_day_low": near_low,
            "failed_breakout": failed_breakout,
            "bearish_news": bool(news.get("bearish_like_news")),
        },
        secondary_setups=secondary,
    )


def build_after_close_watchlist(
    fetcher: DataFetcher | None = None,
    symbols: list[str] | None = None,
    max_entries: int | None = None,
) -> dict:
    """Return a watchlist record ready to store in the journal."""
    fetcher = fetcher or DataFetcher()
    universe = symbols or Screener.WATCHLIST
    max_entries = max_entries or config.AFTER_CLOSE_WATCHLIST_MAX
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(ET)
    generated_at = _utc_iso(now_utc)
    expires_at = _utc_iso(_next_trading_morning(now_et))
    open_symbols = {t.get("symbol") for t in get_open_trades() if t.get("symbol")}
    bar_map = _download_ohlcv_map(list(universe) + ["SPY"], days=70)

    cards = []
    for symbol in universe:
        try:
            card = _score_symbol(fetcher, symbol, open_symbols, bar_map)
            if card:
                cards.append(card)
        except Exception as exc:
            logger.debug("After-close watchlist skipped %s: %s", symbol, exc)

    cards.sort(key=lambda c: c.score, reverse=True)
    entries = [card.to_dict(generated_at, expires_at) for card in cards[:max_entries]]
    return {
        "source": "after_close",
        "generated_at": generated_at,
        "expires_at": expires_at,
        "universe_count": len(universe),
        "entry_count": len(entries),
        "long_candidate_count": sum(1 for e in entries if e.get("side") == "long_watch"),
        "weakness_count": sum(1 for e in entries if e.get("side") == "weakness_or_hedge_watch"),
        "position_alert_count": sum(1 for e in entries if e.get("side") == "position_alert"),
        "entries": entries,
    }
