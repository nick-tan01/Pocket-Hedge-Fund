"""
agents/risk_manager.py
Receives the PM verdict and computes position size + ATR stop.
Pure deterministic logic — no LLM call.
"""

import logging
from dataclasses import dataclass
import config

logger = logging.getLogger(__name__)


@dataclass
class TradeProposal:
    symbol:          str
    action:          str       # "buy" | "hold" | "skip" | "watch"
    conviction:      int
    position_usd:    float
    shares:          float
    entry_price:     float
    stop_price:      float
    stop_pct:        float
    reason:          str
    key_risk:        str       # from PM — what to monitor post-entry
    sector:          str = ""


def evaluate(
    symbol:          str,
    pm_verdict:      dict,
    current_price:   float,
    bars:            list[dict],
    portfolio_value: float,
    open_positions:  list[dict],
    regime:          str = "bull",
    vix_regime:      str = "normal",
    fetcher = None,
) -> TradeProposal:

    action    = pm_verdict.get("action", "skip")
    conviction = int(pm_verdict.get("final_conviction", 1))
    key_risk  = pm_verdict.get("key_risk_to_monitor", "")

    # ── No-go conditions ──────────────────────────────────────────────────────
    if action != "buy":
        return TradeProposal(
            symbol=symbol, action=action, conviction=conviction,
            position_usd=0, shares=0, entry_price=current_price,
            stop_price=0, stop_pct=0,
            reason=f"PM decision: {action} — {pm_verdict.get('deciding_factor', '')}",
            key_risk=key_risk,
        )

    if conviction < config.MIN_CONVICTION_SCORE:
        return TradeProposal(
            symbol=symbol, action="skip", conviction=conviction,
            position_usd=0, shares=0, entry_price=current_price,
            stop_price=0, stop_pct=0,
            reason=f"Conviction {conviction} below minimum {config.MIN_CONVICTION_SCORE}",
            key_risk=key_risk,
        )

    required_conviction = config.MIN_CONVICTION_SCORE
    if regime == "bear" or vix_regime == "high_vix":
        required_conviction = max(required_conviction, 8)
    if conviction < required_conviction:
        return TradeProposal(
            symbol=symbol, action="skip", conviction=conviction,
            position_usd=0, shares=0, entry_price=current_price,
            stop_price=0, stop_pct=0,
            reason=(f"Conviction {conviction} below regime-adjusted minimum "
                    f"{required_conviction} ({regime}, {vix_regime})"),
            key_risk=key_risk,
        )

    if len(open_positions) >= config.MAX_POSITIONS:
        return TradeProposal(
            symbol=symbol, action="skip", conviction=conviction,
            position_usd=0, shares=0, entry_price=current_price,
            stop_price=0, stop_pct=0,
            reason=f"Max positions ({config.MAX_POSITIONS}) already open",
            key_risk=key_risk,
        )

    if any(p["symbol"] == symbol for p in open_positions):
        return TradeProposal(
            symbol=symbol, action="hold", conviction=conviction,
            position_usd=0, shares=0, entry_price=current_price,
            stop_price=0, stop_pct=0,
            reason=f"Already holding {symbol}",
            key_risk=key_risk,
        )

    # ── Portfolio exposure cap ─────────────────────────────────────────────────
    deployed_pct = sum(p.get("position_pct", 0) for p in open_positions)
    remaining_exposure = config.MAX_PORTFOLIO_EXPOSURE - deployed_pct
    if remaining_exposure < 0.04:   # less than 4% headroom — too tight to add
        return TradeProposal(
            symbol=symbol, action="skip", conviction=conviction,
            position_usd=0, shares=0, entry_price=current_price,
            stop_price=0, stop_pct=0,
            reason=(f"Portfolio exposure cap reached "
                    f"({deployed_pct*100:.1f}% deployed, "
                    f"cap={config.MAX_PORTFOLIO_EXPOSURE*100:.0f}%)"),
            key_risk=key_risk,
        )

    # ── Position sizing ───────────────────────────────────────────────────────
    size_map  = config.CONVICTION_SIZE_MAP
    capped    = min(conviction, max(size_map.keys()))
    floored   = max(capped, min(size_map.keys()))
    size_pct  = size_map.get(floored, 0.06)

    regime_mult = {"bull": 1.0, "caution": 0.8, "bear": 0.6}.get(regime, 1.0)
    vix_mult    = {"normal": 1.0, "elevated_vix": 0.75, "high_vix": 0.5}.get(vix_regime, 1.0)
    size_pct    = min(size_pct * regime_mult * vix_mult, remaining_exposure)

    sector = _get_sector(symbol)
    sector_deployed = sum(
        p.get("position_pct", 0)
        for p in open_positions
        if p.get("sector", "") == sector
    )
    if sector and sector_deployed + size_pct > config.MAX_SECTOR_PCT:
        return TradeProposal(
            symbol=symbol, action="skip", conviction=conviction,
            position_usd=0, shares=0, entry_price=current_price,
            stop_price=0, stop_pct=0,
            reason=(f"Sector cap: {sector} already at {sector_deployed*100:.1f}% "
                    f"(max {config.MAX_SECTOR_PCT*100:.0f}%)"),
            key_risk=key_risk,
        )

    corr_ok, corr_reason = _check_correlation(symbol, open_positions, fetcher)
    if not corr_ok:
        return TradeProposal(
            symbol=symbol, action="skip", conviction=conviction,
            position_usd=0, shares=0, entry_price=current_price,
            stop_price=0, stop_pct=0,
            reason=corr_reason,
            key_risk=key_risk,
        )

    position_usd = round(portfolio_value * size_pct, 2)
    shares       = round(position_usd / current_price, 4) if current_price > 0 else 0

    # ── ATR stop ─────────────────────────────────────────────────────────────
    atr      = _calculate_atr(bars, config.ATR_PERIOD)
    beta     = _get_beta(symbol)
    atr_mult = config.ATR_MULTIPLIER * 1.2 if beta > 1.5 else config.ATR_MULTIPLIER
    atr_stop = current_price - (atr_mult * atr) if atr else None
    hard_stop  = current_price * (1 - config.HARD_STOP_PCT)
    stop_price = round(max(atr_stop or 0, hard_stop), 2)
    stop_pct   = round((current_price - stop_price) / current_price * 100, 2)

    reason = (
        f"PM conviction={conviction} | size={size_pct*100:.0f}% "
        f"(${position_usd:,.0f}) | regime={regime}/{vix_regime} "
        f"| ATR={round(atr,2) if atr else 'N/A'} x{atr_mult:.1f} "
        f"| stop=${stop_price} (-{stop_pct}%)"
    )

    logger.info("Risk | %s | BUY conviction=%d size=$%.0f stop=$%.2f",
                symbol, conviction, position_usd, stop_price)

    return TradeProposal(
        symbol=symbol, action="buy", conviction=conviction,
        position_usd=position_usd, shares=shares,
        entry_price=current_price, stop_price=stop_price,
        stop_pct=stop_pct, reason=reason, key_risk=key_risk,
        sector=sector,
    )


def _calculate_atr(bars: list[dict], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    try:
        trs = []
        for i in range(1, len(bars)):
            high, low, prev = bars[i]["high"], bars[i]["low"], bars[i-1]["close"]
            trs.append(max(high - low, abs(high - prev), abs(low - prev)))
        return round(sum(trs[-period:]) / period, 4)
    except Exception as e:
        logger.warning("ATR failed: %s", e)
        return None


def _get_beta(symbol: str) -> float:
    try:
        import yfinance as yf
        return float(yf.Ticker(symbol).info.get("beta") or 1.0)
    except Exception:
        return 1.0


def _get_sector(symbol: str) -> str:
    try:
        import yfinance as yf
        return yf.Ticker(symbol).info.get("sector", "Unknown") or "Unknown"
    except Exception:
        return "Unknown"


def _check_correlation(symbol: str, open_positions: list[dict], fetcher) -> tuple[bool, str]:
    """Return (ok_to_trade, reason). Blocks if corr > 0.75 with any open position."""
    if not open_positions or fetcher is None:
        return True, ""
    try:
        import pandas as pd
        symbols = [p["symbol"] for p in open_positions] + [symbol]
        closes = {}
        for sym in symbols:
            bars = fetcher.get_ohlcv(sym, days=30)
            if bars:
                closes[sym] = [b["close"] for b in bars]
        if len(closes) < 2 or symbol not in closes:
            return True, ""
        min_len = min(len(v) for v in closes.values())
        df = pd.DataFrame({k: v[-min_len:] for k, v in closes.items()})
        corr = df.corr()
        for existing in open_positions:
            existing_sym = existing["symbol"]
            if existing_sym in corr.columns and symbol in corr.index:
                r = corr.loc[symbol, existing_sym]
                if r > 0.75:
                    return False, f"High correlation with existing {existing_sym} (r={r:.2f})"
    except Exception as e:
        logger.debug("Correlation check failed for %s: %s", symbol, e)
    return True, ""
