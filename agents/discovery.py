"""
agents/discovery.py
EXP-006: dynamic universe discovery — replaces "wait for a human to type the
ticker in" with a daily market-wide scan, WITHOUT turning the fund into a
day-gainer chaser.

Sources (Alpaca screener API, no new credentials):
  - most-actives by volume  (liquidity leaders — where institutional flow is)
  - top gainers             (momentum candidates — but heavily filtered)
Losers are never ingested (long-only book).

Every candidate must pass ALL guards before joining the screener universe:
  1. Core floors: price >= MIN_PRICE, mcap >= MIN_MARKET_CAP, avg vol >= MIN_VOLUME.
  2. Blow-off filter: day gain <= DISCOVERY_MAX_DAY_GAIN AND
     5-session gain <= DISCOVERY_MAX_5D_GAIN. A +15% day or +30% week is exactly
     the parabolic profile the PM rubric's exhaustion guardrail exists to avoid —
     discovery must not feed it.
  3. Trend filter: EMA10 > EMA30 (established uptrend). Kills dead-cat bounces
     and falling knives that dominate raw mover lists.
  4. Hard cap (DISCOVERY_MAX_SYMBOLS) keeps screener runtime bounded.

Names already in the core WATCHLIST are skipped (they're scanned anyway).
Every accept/reject is journaled under "universe_discovery" so EXP-006 can later
compare forward returns of discovered vs core candidates.
"""

import logging
import math

import pandas as pd

import config
from core.journal import log_universe_discovery

logger = logging.getLogger(__name__)


def _trend_ok(closes: list[float]) -> bool:
    """EMA10 > EMA30 — same trend definition the technical agent uses."""
    try:
        s = pd.Series(closes, dtype=float)
        ema10 = s.ewm(span=10, adjust=False).mean().iloc[-1]
        ema30 = s.ewm(span=30, adjust=False).mean().iloc[-1]
        return bool(ema10 > ema30)
    except Exception:
        return False


def discover_universe(fetcher, core_watchlist: list[str]) -> list[str]:
    """
    Return discovered symbols that passed all guards (possibly empty).
    Journals the full accept/reject audit trail. Never raises.
    """
    try:
        if not getattr(config, "DISCOVERY_ENABLED", False):
            return []
        top       = getattr(config, "DISCOVERY_SOURCE_TOP", 50)
        max_syms  = getattr(config, "DISCOVERY_MAX_SYMBOLS", 25)
        max_day   = getattr(config, "DISCOVERY_MAX_DAY_GAIN", 0.12)
        max_5d    = getattr(config, "DISCOVERY_MAX_5D_GAIN", 0.25)
        core      = set(core_watchlist)

        # Gather sources. most-actives first (volume leaders, least spike-biased),
        # then gainers ordered SMALLEST move first — steadier momentum gets the
        # scarce slots before the big poppers.
        ordered: list[tuple[str, str, float | None]] = []   # (symbol, source, day_pct)
        for a in fetcher.get_most_actives(top=top):
            sym = str(a.get("symbol", "")).upper()
            if sym:
                ordered.append((sym, "most_actives", None))
        movers = fetcher.get_market_movers(top=top)
        gainers = sorted(movers.get("gainers", []) or [],
                         key=lambda g: g.get("percent_change", 0))
        for g in gainers:
            sym = str(g.get("symbol", "")).upper()
            if sym:
                ordered.append((sym, "movers_gainers",
                                float(g.get("percent_change", 0)) / 100.0))

        accepted, rejected, seen = [], [], set()
        for sym, source, day_pct_hint in ordered:
            if len(accepted) >= max_syms:
                break
            if sym in seen:
                continue
            seen.add(sym)
            if sym in core:
                continue   # already scanned every run; not logged as a rejection
            if "." in sym or "/" in sym:
                continue   # units/preferreds/odd classes

            # Cheap pre-filter from the movers payload before any extra fetches.
            if day_pct_hint is not None and day_pct_hint > max_day:
                rejected.append({"symbol": sym, "source": source,
                                 "reason": f"blowoff_day_gain {day_pct_hint*100:.1f}%"})
                continue

            quote = fetcher.get_quote(sym)
            if not quote or quote["price"] < config.MIN_PRICE:
                rejected.append({"symbol": sym, "source": source, "reason": "price_floor"})
                continue
            if quote["market_cap"] < config.MIN_MARKET_CAP:
                rejected.append({"symbol": sym, "source": source, "reason": "mcap_floor"})
                continue
            if quote["volume"] < config.MIN_VOLUME:
                rejected.append({"symbol": sym, "source": source, "reason": "volume_floor"})
                continue

            bars = fetcher.get_ohlcv(sym, days=45)
            if len(bars) < 31:
                rejected.append({"symbol": sym, "source": source, "reason": "insufficient_bars"})
                continue
            closes = [b["close"] for b in bars]
            # NaN/zero closes make every threshold comparison vacuously False —
            # a name with corrupt bars must be rejected, not waved through (and a
            # NaN reaching data.json breaks strict JSON parsers like the dashboard).
            if any((not isinstance(c, (int, float))) or (not math.isfinite(c)) or c <= 0
                   for c in closes[-31:]):
                rejected.append({"symbol": sym, "source": source, "reason": "bad_bars"})
                continue
            day_gain = closes[-1] / closes[-2] - 1
            gain_5d  = closes[-1] / closes[-6] - 1
            if day_gain > max_day:
                rejected.append({"symbol": sym, "source": source,
                                 "reason": f"blowoff_day_gain {day_gain*100:.1f}%"})
                continue
            if gain_5d > max_5d:
                rejected.append({"symbol": sym, "source": source,
                                 "reason": f"blowoff_5d_gain {gain_5d*100:.1f}%"})
                continue
            if not _trend_ok(closes):
                rejected.append({"symbol": sym, "source": source, "reason": "no_uptrend"})
                continue

            accepted.append({
                "symbol": sym, "source": source,
                "day_gain_pct": round(day_gain * 100, 2),
                "gain_5d_pct":  round(gain_5d * 100, 2),
                "price": quote["price"],
            })

        log_universe_discovery({
            "accepted": accepted,
            "rejected": rejected,
            "core_size": len(core),
        })
        if accepted:
            logger.info("Discovery (EXP-006): +%d symbols %s | rejected %d",
                        len(accepted), [a["symbol"] for a in accepted], len(rejected))
        else:
            logger.info("Discovery (EXP-006): no symbols passed guards (%d rejected)",
                        len(rejected))
        return [a["symbol"] for a in accepted]
    except Exception as exc:
        logger.warning("Discovery failed (ignored — core watchlist unaffected): %s", exc)
        return []
