"""
agents/screener.py
Stage 1 of the pipeline. Narrows the watchlist down to 5-8 candidates
using pure data filters (no LLM) before passing to analyst agents.

Deduplication is automatic — uses yfinance company name matching,
no hardcoded ticker pairs needed.
"""

import logging
import pandas as pd
from dataclasses import dataclass, field
import yfinance as yf

from core.data_fetcher import DataFetcher
import config

logger = logging.getLogger(__name__)


@dataclass
class ScreenerCandidate:
    symbol:          str
    price:           float
    market_cap:      float
    composite_score: float
    signals:         dict = field(default_factory=dict)


# ── Pure-pandas TA ────────────────────────────────────────────────────────────

def _rsi(closes: pd.Series, period: int = 14) -> float | None:
    try:
        delta    = closes.diff()
        gain     = delta.clip(lower=0)
        loss     = (-delta).clip(lower=0)
        avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
        rs  = avg_gain / avg_loss.replace(0, float("inf"))
        rsi = 100 - (100 / (1 + rs))
        val = rsi.iloc[-1]
        return round(float(val), 1) if not pd.isna(val) else None
    except Exception:
        return None


def _macd_crossover(closes: pd.Series) -> bool:
    try:
        ema12  = closes.ewm(span=12, adjust=False).mean()
        ema26  = closes.ewm(span=26, adjust=False).mean()
        macd   = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        return (
            macd.iloc[-1] > signal.iloc[-1] and
            macd.iloc[-2] <= signal.iloc[-2]
        )
    except Exception:
        return False


def _bollinger_pct(closes: pd.Series, period: int = 20) -> float | None:
    try:
        sma   = closes.rolling(period).mean()
        std   = closes.rolling(period).std()
        upper = sma + (2 * std)
        lower = sma - (2 * std)
        width = float(upper.iloc[-1] - lower.iloc[-1])
        if width == 0:
            return None
        return round((float(closes.iloc[-1]) - float(lower.iloc[-1])) / width, 2)
    except Exception:
        return None


# ── Company identity helpers ──────────────────────────────────────────────────

def _normalize_name(name: str) -> str:
    """
    Strip legal suffixes and punctuation so 'Alphabet Inc.' and
    'Alphabet Inc' both reduce to 'alphabet'.
    """
    if not name:
        return ""
    suffixes = [
        " inc", " inc.", " corp", " corp.", " co.", " co",
        " ltd", " ltd.", " llc", " plc", " class a", " class b",
        " class c", " holdings", " group",
    ]
    n = name.lower().strip()
    for s in suffixes:
        if n.endswith(s):
            n = n[: -len(s)].strip()
    return n


def _get_company_key(symbol: str) -> str | None:
    """
    Return a normalized company name string for deduplication.
    Uses yfinance shortName — already cached from earlier calls so
    no extra network cost for symbols we've already scored.
    """
    try:
        info = yf.Ticker(symbol).info
        name = info.get("shortName") or info.get("longName") or ""
        return _normalize_name(name) if name else None
    except Exception:
        return None


def _deduplicate(candidates: list[ScreenerCandidate]) -> list[ScreenerCandidate]:
    """
    Remove duplicate companies from the sorted candidate list.
    For each company, keeps the highest-scoring ticker (already sorted desc).
    Matching is done on normalized company name — no hardcoding needed.
    """
    seen_companies: dict[str, str] = {}   # normalized_name → symbol kept
    deduped = []

    for c in candidates:
        key = _get_company_key(c.symbol)

        if key is None:
            # Can't determine company — keep it to be safe
            deduped.append(c)
            continue

        if key in seen_companies:
            logger.info(
                "Dedup: dropping %s (same company '%s' as %s)",
                c.symbol, key, seen_companies[key],
            )
            continue

        seen_companies[key] = c.symbol
        deduped.append(c)

    return deduped


# ── Screener ──────────────────────────────────────────────────────────────────

class Screener:
    WATCHLIST = [
        # Mega-cap tech & AI
        "NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "TSLA",
        "AVGO", "ORCL", "CRM", "AMD", "INTC", "QCOM", "TXN", "AMAT", "LRCX",
        # Healthcare & biotech
        "LLY", "UNH", "JNJ", "ABBV", "MRK", "PFE", "AMGN", "GILD",
        "REGN", "VRTX", "BMY", "CVS",
        # Financials
        "JPM", "BAC", "GS", "MS", "BLK", "SCHW", "AXP", "V", "MA", "PYPL",
        # Consumer & retail
        "WMT", "COST", "TGT", "HD", "LOW", "NKE", "SBUX", "MCD", "CMG",
        # Energy & industrials
        "XOM", "CVX", "COP", "GE", "CAT", "DE", "BA", "HON", "LMT",
        # Communication & media
        "NFLX", "DIS", "CMCSA", "T", "VZ", "TMUS",
        # High-growth / emerging
        "RKLB", "PLTR", "SOFI", "COIN", "SNOW", "DDOG", "CRWD",
        "ZS", "NET", "MDB", "AFRM", "HOOD",
    ]

    def __init__(self, data_fetcher: DataFetcher):
        self.fetcher = data_fetcher

    def run(self) -> list[ScreenerCandidate]:
        logger.info("Screener starting — universe: %d symbols", len(self.WATCHLIST))
        candidates = []

        for symbol in self.WATCHLIST:
            try:
                candidate = self._score_symbol(symbol)
                if candidate:
                    candidates.append(candidate)
            except Exception as e:
                logger.debug("Screener skipped %s: %s", symbol, e)

        # Sort by score descending, then deduplicate same-company tickers
        candidates.sort(key=lambda c: c.composite_score, reverse=True)
        candidates = _deduplicate(candidates)
        top = candidates[:config.SCREENER_MAX_CANDIDATES]

        logger.info(
            "Screener complete — %d passed filters, returning top %d: %s",
            len(candidates), len(top), [c.symbol for c in top],
        )
        return top

    def _score_symbol(self, symbol: str) -> ScreenerCandidate | None:
        quote = self.fetcher.get_quote(symbol)
        if not quote:
            return None
        if quote["price"] < config.MIN_PRICE:
            return None
        if quote["market_cap"] < config.MIN_MARKET_CAP:
            return None

        bars = self.fetcher.get_ohlcv(symbol, days=60)
        if len(bars) < 30:
            return None

        signals = {}
        vol_score,  vol_sig  = self._score_volume(bars)
        ta_score,   ta_sig   = self._score_technical(bars)
        news_score, news_sig = self._score_news(symbol)
        val_score,  val_sig  = self._score_valuation(symbol)

        signals.update(vol_sig)
        signals.update(ta_sig)
        signals.update(news_sig)
        signals.update(val_sig)

        if vol_score == 0 and ta_score == 0 and news_score == 0 and val_score == 0:
            return None

        w = config.SCREENER_WEIGHTS
        composite = (
            news_score * w["news_catalyst"] +
            vol_score  * w["volume_spike"]  +
            ta_score   * w["technical"]     +
            val_score  * w["valuation"]
        )

        return ScreenerCandidate(
            symbol=symbol,
            price=quote["price"],
            market_cap=quote["market_cap"],
            composite_score=round(composite, 4),
            signals=signals,
        )

    def _score_volume(self, bars):
        volumes    = [b["volume"] for b in bars]
        avg_vol    = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else 0
        today_vol  = volumes[-1]
        spike_ratio = today_vol / avg_vol if avg_vol > 0 else 0
        score = 0.0
        if spike_ratio >= config.VOLUME_SPIKE_MULT:
            score = min(1.0, (spike_ratio - config.VOLUME_SPIKE_MULT) / 2.0 + 0.5)
        return score, {
            "volume_spike_ratio": round(spike_ratio, 2),
            "volume_spike":       spike_ratio >= config.VOLUME_SPIKE_MULT,
        }

    def _score_technical(self, bars):
        closes = pd.Series([b["close"] for b in bars], dtype=float)
        signals = {}
        score_components = []

        rsi = _rsi(closes)
        if rsi is not None:
            signals["rsi"] = rsi
            if rsi < 35:
                score_components.append(0.8)
            elif rsi > 65:
                score_components.append(0.6)
            else:
                score_components.append(0.2)

        if len(closes) >= 35:
            crossover = _macd_crossover(closes)
            signals["macd_crossover"] = crossover
            score_components.append(0.9 if crossover else 0.1)

        if len(closes) >= 20:
            bb_pct = _bollinger_pct(closes)
            if bb_pct is not None:
                signals["bb_pct"] = bb_pct
                score_components.append(0.7 if (bb_pct < 0.15 or bb_pct > 0.85) else 0.3)

        ta_score = sum(score_components) / len(score_components) if score_components else 0.0
        return round(ta_score, 4), signals

    def _score_news(self, symbol):
        news  = self.fetcher.get_news(symbol, days=2)
        count = len(news)
        return min(1.0, count / 5.0) if count > 0 else 0.0, {
            "recent_news_count": count,
            "top_headline": news[0]["headline"] if news else "",
        }

    def _score_valuation(self, symbol):
        try:
            info  = yf.Ticker(symbol).info
            pe    = info.get("trailingPE")
            fwd   = info.get("forwardPE")
            pgr   = info.get("earningsGrowth")
            signals = {
                "pe_trailing":     round(pe, 1) if pe else None,
                "pe_forward":      round(fwd, 1) if fwd else None,
                "earnings_growth": round(pgr * 100, 1) if pgr else None,
            }
            score = 0.0
            if pe and 0 < pe < 15:
                score += 0.5
            elif pe and pe < 22:
                score += 0.3
            if pgr and pgr > 0.15:
                score += 0.4
            elif pgr and pgr > 0.05:
                score += 0.2
            return min(1.0, round(score, 4)), signals
        except Exception:
            return 0.0, {}

    def format_for_log(self, candidates: list[ScreenerCandidate]) -> str:
        lines = ["── Screener results ──"]
        for i, c in enumerate(candidates, 1):
            lines.append(
                f"  {i}. {c.symbol:<6} score={c.composite_score:.3f} "
                f"price=${c.price:.2f} "
                f"vol_spike={c.signals.get('volume_spike', False)} "
                f"rsi={c.signals.get('rsi', '—')} "
                f"news={c.signals.get('recent_news_count', 0)}"
            )
        return "\n".join(lines)
