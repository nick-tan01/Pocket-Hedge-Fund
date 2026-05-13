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
    rotate_from:     str = ""
    rotation_reason: str = ""
    correlation:     float = 0.0


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

    if any(p["symbol"] == symbol for p in open_positions):
        return TradeProposal(
            symbol=symbol, action="hold", conviction=conviction,
            position_usd=0, shares=0, entry_price=current_price,
            stop_price=0, stop_pct=0,
            reason=f"Already holding {symbol}",
            key_risk=key_risk,
        )

    # ── Current exposure baseline ───────────────────────────────────────────────
    deployed_pct = sum(p.get("position_pct", 0) for p in open_positions)

    # ── Position sizing ───────────────────────────────────────────────────────
    size_map  = config.CONVICTION_SIZE_MAP
    capped    = min(conviction, max(size_map.keys()))
    floored   = max(capped, min(size_map.keys()))
    size_pct  = size_map.get(floored, 0.06)

    regime_mult = {"bull": 1.0, "caution": 0.8, "bear": 0.6}.get(regime, 1.0)
    vix_mult    = {"normal": 1.0, "elevated_vix": 0.75, "high_vix": 0.5}.get(vix_regime, 1.0)
    size_pct    = size_pct * regime_mult * vix_mult

    corr_decision = _correlation_tournament(
        symbol=symbol,
        pm_verdict=pm_verdict,
        bars=bars,
        open_positions=open_positions,
        fetcher=fetcher,
    )
    if corr_decision["action"] == "skip":
        return TradeProposal(
            symbol=symbol, action="skip", conviction=conviction,
            position_usd=0, shares=0, entry_price=current_price,
            stop_price=0, stop_pct=0,
            reason=corr_decision["reason"],
            key_risk=key_risk,
            correlation=corr_decision.get("correlation", 0.0),
        )
    if len(open_positions) >= config.MAX_POSITIONS and corr_decision["action"] != "rotate":
        return TradeProposal(
            symbol=symbol, action="skip", conviction=conviction,
            position_usd=0, shares=0, entry_price=current_price,
            stop_price=0, stop_pct=0,
            reason=f"Max positions ({config.MAX_POSITIONS}) already open",
            key_risk=key_risk,
        )

    rotation_credit = 0.0
    if corr_decision["action"] == "rotate":
        rotate_from = corr_decision.get("rotate_from")
        rotation_credit = sum(
            p.get("position_pct", 0)
            for p in open_positions
            if p.get("symbol") == rotate_from
        )

    effective_deployed = max(0, deployed_pct - rotation_credit)
    remaining_exposure = config.MAX_PORTFOLIO_EXPOSURE - effective_deployed
    if remaining_exposure < 0.04:   # less than 4% headroom — too tight to add
        return TradeProposal(
            symbol=symbol, action="skip", conviction=conviction,
            position_usd=0, shares=0, entry_price=current_price,
            stop_price=0, stop_pct=0,
            reason=(f"Portfolio exposure cap reached "
                    f"({effective_deployed*100:.1f}% deployed after rotation credit, "
                    f"cap={config.MAX_PORTFOLIO_EXPOSURE*100:.0f}%)"),
            key_risk=key_risk,
        )
    size_pct = min(size_pct, remaining_exposure)

    sector = _get_sector(symbol)
    sector_deployed = sum(
        p.get("position_pct", 0)
        for p in open_positions
        if p.get("sector", "") == sector
    )
    if corr_decision["action"] == "rotate":
        rotate_from = corr_decision.get("rotate_from")
        sector_deployed -= sum(
            p.get("position_pct", 0)
            for p in open_positions
            if p.get("symbol") == rotate_from and p.get("sector", "") == sector
        )
        sector_deployed = max(0, sector_deployed)

    if sector and sector_deployed + size_pct > config.MAX_SECTOR_PCT:
        return TradeProposal(
            symbol=symbol, action="skip", conviction=conviction,
            position_usd=0, shares=0, entry_price=current_price,
            stop_price=0, stop_pct=0,
            reason=(f"Sector cap: {sector} already at {sector_deployed*100:.1f}% "
                    f"(max {config.MAX_SECTOR_PCT*100:.0f}%)"),
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
    if corr_decision["action"] == "rotate":
        reason += f" | rotate_from={corr_decision['rotate_from']}"

    logger.info("Risk | %s | BUY conviction=%d size=$%.0f stop=$%.2f",
                symbol, conviction, position_usd, stop_price)

    return TradeProposal(
        symbol=symbol, action="buy", conviction=conviction,
        position_usd=position_usd, shares=shares,
        entry_price=current_price, stop_price=stop_price,
        stop_pct=stop_pct, reason=reason, key_risk=key_risk,
        sector=sector,
        rotate_from=corr_decision.get("rotate_from", ""),
        rotation_reason=corr_decision.get("reason", ""),
        correlation=corr_decision.get("correlation", 0.0),
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


def _correlation_tournament(
    symbol: str,
    pm_verdict: dict,
    bars: list[dict],
    open_positions: list[dict],
    fetcher,
) -> dict:
    """
    Treat high return-correlation as a slot competition, not an automatic block.
    If the new candidate clearly beats the overlapping holding, allow a rotation.
    """
    if not open_positions or fetcher is None:
        return {"action": "allow", "reason": ""}
    try:
        import pandas as pd

        candidate_returns = _daily_returns([b["close"] for b in bars])
        if len(candidate_returns) < 10:
            return {"action": "allow", "reason": ""}

        candidate_score = _rotation_score(
            conviction=int(pm_verdict.get("final_conviction", 1)),
            bars=bars,
            thesis_status="intact",
        )
        conflicts = []
        for existing in open_positions:
            existing_sym = existing["symbol"]
            existing_bars = fetcher.get_ohlcv(existing_sym, days=60)
            existing_returns = _daily_returns([b["close"] for b in existing_bars])
            min_len = min(len(candidate_returns), len(existing_returns))
            if min_len < 10:
                continue

            corr = pd.Series(candidate_returns[-min_len:]).corr(
                pd.Series(existing_returns[-min_len:])
            )
            if corr is None or pd.isna(corr) or corr < config.CORRELATION_THRESHOLD:
                continue

            existing_score = _rotation_score(
                conviction=int(
                    existing.get("last_review_conviction")
                    or existing.get("conviction")
                    or 1
                ),
                bars=existing_bars,
                thesis_status=existing.get("thesis_status", "intact"),
            )
            conflicts.append({
                "symbol": existing_sym,
                "correlation": float(corr),
                "existing_score": existing_score,
            })

        if not conflicts:
            return {"action": "allow", "reason": ""}

        hardest = max(conflicts, key=lambda c: c["correlation"])
        weakest = min(conflicts, key=lambda c: c["existing_score"])
        if candidate_score >= hardest["existing_score"] + config.ROTATION_SCORE_MARGIN:
            return {
                "action": "rotate",
                "rotate_from": weakest["symbol"],
                "correlation": round(hardest["correlation"], 2),
                "reason": (
                    f"Correlation tournament: {symbol} score {candidate_score:.1f} "
                    f"beats {hardest['symbol']} score {hardest['existing_score']:.1f} "
                    f"(r={hardest['correlation']:.2f}); rotate out of {weakest['symbol']}"
                ),
            }

        return {
            "action": "skip",
            "correlation": round(hardest["correlation"], 2),
            "reason": (
                f"Correlation tournament: {symbol} score {candidate_score:.1f} "
                f"does not beat {hardest['symbol']} score {hardest['existing_score']:.1f} "
                f"by {config.ROTATION_SCORE_MARGIN:.1f} (r={hardest['correlation']:.2f})"
            ),
        }
    except Exception as e:
        logger.debug("Correlation tournament failed for %s: %s", symbol, e)
    return {"action": "allow", "reason": ""}


def _daily_returns(closes: list[float]) -> list[float]:
    returns = []
    for prev, current in zip(closes, closes[1:]):
        if prev:
            returns.append((current - prev) / prev)
    return returns


def _rotation_score(conviction: int, bars: list[dict], thesis_status: str) -> float:
    score = float(conviction)
    closes = [float(b["close"]) for b in bars if b.get("close")]
    if len(closes) >= 21 and closes[-21]:
        score += max(-1.0, min(1.0, ((closes[-1] / closes[-21]) - 1) * 5))
    if len(closes) >= 6 and closes[-6]:
        score += max(-0.5, min(0.5, ((closes[-1] / closes[-6]) - 1) * 3))

    status = str(thesis_status or "").lower()
    if status in {"weakened", "deteriorating"}:
        score -= 0.75
    elif status in {"broken", "invalidated"}:
        score -= 2.0
    return round(score, 2)
