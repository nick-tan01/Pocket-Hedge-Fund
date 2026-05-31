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
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import yfinance as yf

from core.data_fetcher import DataFetcher
import config

ET = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)


class ScreenerDataUnavailable(RuntimeError):
    """
    Raised when the screener's quote health probe fails — all liquid probe symbols
    (SPY, AAPL, MSFT) returned empty data. Distinguishes a data-layer outage from a
    genuine 'no candidates' result so callers can log 'data_unavailable' rather than
    'no_candidates' in the run record.
    """

SECTOR_ETFS = {
    "Technology": "XLK",
    "Health Care": "XLV",
    "Healthcare": "XLV",
    "Financial Services": "XLF",
    "Financials": "XLF",
    "Consumer Cyclical": "XLY",
    "Consumer Discretionary": "XLY",
    "Consumer Defensive": "XLP",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Communication Services": "XLC",
    "Real Estate": "XLRE",
    "Basic Materials": "XLB",
    "Materials": "XLB",
    "Utilities": "XLU",
}

BULLISH_KEYWORDS = [
    "beat", "beats", "raised", "raise", "guidance", "upgrade", "upgraded",
    "acquisition", "buyback", "dividend", "contract", "approval", "fda",
    "deal", "record", "exceeded", "upside", "outperform", "strong",
    "accelerating", "top", "breakout", "partnership", "wins",
]

BEARISH_KEYWORDS = [
    "miss", "missed", "cut", "lowered", "downgrade", "downgraded", "recall",
    "investigation", "lawsuit", "fine", "disappoints", "below", "warns",
    "guidance cut", "layoffs", "delayed", "cancelled",
]


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
        # Mid-cap momentum / slower information diffusion
        "SMCI", "MSTR", "IONQ", "GTLB", "TTD", "HIMS", "DUOL",
        "SOUN", "APLD", "ARM", "ACLS", "COHR", "ONTO", "RXRX",
        "CELH", "DOCS", "BILL", "TMDX", "WING",
    ]

    def __init__(self, data_fetcher: DataFetcher):
        self.fetcher = data_fetcher

    # Liquid probe symbols used for the data health check.
    # These are always in the universe and should always return a valid quote.
    _PROBE_SYMBOLS = ["SPY", "AAPL", "MSFT"]

    def run(
        self,
        max_candidates: int | None = None,
        symbols: list[str] | None = None,
    ) -> list[ScreenerCandidate]:
        universe = symbols or self.WATCHLIST
        logger.info("Screener starting — universe: %d symbols", len(universe))

        # ── Quote data health check ───────────────────────────────────────────
        # yfinance fast_info can silently return None/0 for all symbols when
        # rate-limited or the network is down on GitHub Actions. When that
        # happens every _score_symbol() call returns None, producing an empty
        # candidate list that the pipeline logs as 'no_candidates'. This is a
        # false negative — the screener didn't find 'nothing good', it found
        # 'no data'. We distinguish the two cases with three liquid probes:
        # if all probes fail, raise ScreenerDataUnavailable so the caller can
        # log 'data_unavailable' instead of 'no_candidates'.
        probes_ok = sum(1 for s in self._PROBE_SYMBOLS if self.fetcher.get_quote(s))
        if probes_ok == 0:
            raise ScreenerDataUnavailable(
                f"All probe symbols {self._PROBE_SYMBOLS} returned empty quotes — "
                "yfinance likely rate-limited or network unavailable"
            )
        if probes_ok < len(self._PROBE_SYMBOLS):
            logger.warning(
                "Screener partial data — only %d/%d probes returned quotes (%s). "
                "Some symbols may be skipped.",
                probes_ok, len(self._PROBE_SYMBOLS), self._PROBE_SYMBOLS,
            )

        candidates = []
        for symbol in universe:
            try:
                candidate = self._score_symbol(symbol)
                if candidate:
                    candidates.append(candidate)
            except Exception as e:
                logger.warning("Screener skipped %s: %s", symbol, e)  # was debug → warning

        # Sort by score descending, then deduplicate same-company tickers
        candidates.sort(key=lambda c: c.composite_score, reverse=True)
        candidates = _deduplicate(candidates)
        limit = max_candidates or config.SCREENER_MAX_CANDIDATES
        top = candidates[:limit]

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

        info = self._get_info(symbol)
        if self._earnings_within_days(info, days=3):
            logger.info("Screener: %s skipped — earnings within 3 days", symbol)
            return None

        signals = {}
        earnings_score, earnings_sig = self._score_earnings_catalyst(symbol, info)
        rs_score,       rs_sig       = self._score_relative_strength(symbol, bars, info)
        high52_score,   high52_sig   = self._score_52wk_high(symbol, info)
        vol_score,      vol_sig      = self._score_volume(bars)
        ta_score,       ta_sig       = self._score_technical(bars)
        news_score,     news_sig     = self._score_news(symbol)
        val_score,      val_sig      = self._score_valuation(symbol, info)

        signals.update(earnings_sig)
        signals.update(rs_sig)
        signals.update(high52_sig)
        signals.update(vol_sig)
        signals.update(ta_sig)
        signals.update(news_sig)
        signals.update(val_sig)

        if (
            earnings_score == 0 and rs_score == 0 and vol_score == 0 and
            ta_score == 0 and high52_score == 0 and news_score == 0 and
            val_score == 0
        ):
            return None

        w = config.SCREENER_WEIGHTS
        technical_score = (ta_score + high52_score) / 2
        composite = (
            earnings_score * w["earnings_catalyst"] +
            rs_score       * w["relative_strength"] +
            technical_score * w["technical"]        +
            vol_score      * w["volume_spike"]      +
            news_score     * w["news_quality"]      +
            val_score      * w["valuation"]
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
            if rsi > 65:
                score_components.append(0.8)   # strong momentum — confirmation signal
            elif rsi < 35:
                score_components.append(0.6)   # oversold bounce opportunity
            else:
                score_components.append(0.2)   # neutral — lowest priority

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
        if not news:
            return 0.0, {"news_count": 0, "top_headline": "", "news_quality": 0.0}
        quality = 0.0
        for article in news[:5]:
            headline = article.get("headline", "").lower()
            if any(k in headline for k in BULLISH_KEYWORDS):
                quality += 0.25
            if any(k in headline for k in BEARISH_KEYWORDS):
                quality -= 0.20
        quality = max(0.0, min(1.0, quality))
        return quality, {
            "news_count": len(news),
            "top_headline": news[0].get("headline", ""),
            "news_quality": round(quality, 2),
        }

    def _score_valuation(self, symbol, info=None):
        try:
            info  = info or self._get_info(symbol)
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

    def _score_relative_strength(self, symbol: str, bars: list[dict], info=None) -> tuple[float, dict]:
        try:
            info = info or self._get_info(symbol)
            sector = info.get("sector", "")
            etf = SECTOR_ETFS.get(sector)
            if not etf or len(bars) < 21:
                return 0.4, {"rs_sector": None, "sector": sector}
            stock_ret = (bars[-1]["close"] - bars[-21]["close"]) / bars[-21]["close"]
            etf_bars = self.fetcher.get_ohlcv(etf, days=25)
            if not etf_bars or len(etf_bars) < 21:
                return 0.4, {"rs_sector": None, "sector": sector, "sector_etf": etf}
            etf_ret = (etf_bars[-1]["close"] - etf_bars[-21]["close"]) / etf_bars[-21]["close"]
            spread = stock_ret - etf_ret
            if spread > 0.05:
                score = 0.9
            elif spread > 0.02:
                score = 0.7
            elif spread > 0:
                score = 0.5
            elif spread > -0.02:
                score = 0.3
            else:
                score = 0.1
            return score, {
                "rs_vs_sector_pct": round(spread * 100, 1),
                "sector": sector,
                "sector_etf": etf,
            }
        except Exception:
            return 0.4, {"rs_sector": None}

    def _score_52wk_high(self, symbol: str, info=None) -> tuple[float, dict]:
        try:
            info = info or self._get_info(symbol)
            hi52 = info.get("fiftyTwoWeekHigh")
            lo52 = info.get("fiftyTwoWeekLow")
            price = info.get("currentPrice") or info.get("regularMarketPrice")
            if not all([hi52, lo52, price]) or hi52 == lo52:
                return 0.3, {}
            pct_of_range = (price - lo52) / (hi52 - lo52)
            score = (
                0.9 if pct_of_range >= 0.90 else
                0.75 if pct_of_range >= 0.80 else
                0.5 if pct_of_range >= 0.60 else
                0.3
            )
            return score, {
                "pct_of_52wk_range": round(pct_of_range, 2),
                "52wk_high": round(float(hi52), 2),
            }
        except Exception:
            return 0.3, {}

    def _score_earnings_catalyst(self, symbol: str, info=None) -> tuple[float, dict]:
        try:
            info = info or self._get_info(symbol)
            eps_growth = info.get("earningsGrowth")
            rev_growth = info.get("revenueGrowth")
            if eps_growth is not None and rev_growth is not None:
                signals = {
                    "eps_growth": round(eps_growth * 100, 1),
                    "rev_growth": round(rev_growth * 100, 1),
                }
                if eps_growth > 0.5 and rev_growth > 0.2:
                    signals["catalyst"] = "strong_beat"
                    return 0.9, signals
                if eps_growth > 0.2:
                    signals["catalyst"] = "moderate_beat"
                    return 0.6, signals
                if eps_growth < 0:
                    signals["catalyst"] = "miss"
                    return 0.1, signals
            return 0.3, {"catalyst": "unknown"}
        except Exception:
            return 0.3, {"catalyst": "unknown"}

    def _get_info(self, symbol: str) -> dict:
        try:
            return yf.Ticker(symbol).info
        except Exception:
            return {}

    def _earnings_within_days(self, info: dict, days: int = 3) -> bool:
        earnings_date = self._parse_earnings_date(
            info.get("earningsDate") or info.get("earningsTimestamp")
        )
        if not earnings_date:
            return False
        # C20a: use the ET market date, consistent with the ET-converted earnings_date.
        days_to_earnings = (earnings_date - datetime.now(ET).date()).days
        return 0 < days_to_earnings <= days

    def _parse_earnings_date(self, raw) -> date | None:
        try:
            if raw is None:
                return None
            if isinstance(raw, (list, tuple)) and raw:
                raw = raw[0]
            if hasattr(raw, "iloc"):
                raw = raw.iloc[0]
            if isinstance(raw, (int, float)):
                # C20a: convert the UTC epoch to the ET market date before taking
                # .date(), so "earnings within 3 days" compares ET-to-ET. Using a raw
                # UTC date could be off by one near the date line on late-night runs.
                return datetime.fromtimestamp(raw, tz=timezone.utc).astimezone(ET).date()
            if isinstance(raw, datetime):
                return raw.date()
            if isinstance(raw, date):
                return raw
            return date.fromisoformat(str(raw)[:10])
        except Exception:
            return None

    def format_for_log(self, candidates: list[ScreenerCandidate]) -> str:
        lines = ["── Screener results ──"]
        for i, c in enumerate(candidates, 1):
            lines.append(
                f"  {i}. {c.symbol:<6} score={c.composite_score:.3f} "
                f"price=${c.price:.2f} "
                f"vol_spike={c.signals.get('volume_spike', False)} "
                f"rsi={c.signals.get('rsi', '—')} "
                f"rs={c.signals.get('rs_vs_sector_pct', '—')} "
                f"52w={c.signals.get('pct_of_52wk_range', '—')} "
                f"news_q={c.signals.get('news_quality', 0)}"
            )
        return "\n".join(lines)
