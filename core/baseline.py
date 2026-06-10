"""
core/baseline.py
S2 (audit): the "null-intelligence" baseline twin — measurement only.

Every pipeline run this logs what a deterministic strategy WOULD buy using ONLY
components the live pipeline already trusts: the screener's composite ranking and
the risk manager's deterministic caps/stop math. No LLM calls, no orders, no effect
on any live decision. If the full debate pipeline cannot beat this twin's forward
SPY-relative returns, the LLM layer is cost without selection value.

Design constraints:
  - Mirrors the live caps (exposure, sector, max positions, held-name skip) so the
    comparison isolates SELECTION, not risk appetite.
  - Fixed size (config.BASELINE_SHADOW_SIZE_PCT) mirrors the de-facto conviction-7
    size, so sizing skill is not being measured either.
  - Append-only journal records under "baseline_shadow"; scored later by
    core/counterfactuals.analyze_baseline().
"""

import logging

import config
from agents.risk_manager import _calculate_atr
from core.journal import log_baseline_shadow

logger = logging.getLogger(__name__)


def log_baseline_decisions(
    candidates: list,
    open_positions: list[dict],
    portfolio_value: float,
    fetcher,
    regime: str,
    vix_regime: str,
    run_reason: str = "scheduled",
) -> None:
    """Log the baseline twin's would-buy decisions for this run. Never raises."""
    try:
        if not getattr(config, "BASELINE_SHADOW_ENABLED", False):
            return
        top_n    = getattr(config, "BASELINE_SHADOW_TOP_N", 3)
        size_pct = getattr(config, "BASELINE_SHADOW_SIZE_PCT", 0.06)
        min_slot = getattr(config, "MIN_SLOT_PCT", 0.03)

        held = {p.get("symbol") for p in open_positions}
        deployed = sum(p.get("position_pct", 0) for p in open_positions)
        meaningful = sum(1 for p in open_positions
                         if p.get("position_pct", 0) >= min_slot)
        sector_deployed: dict[str, float] = {}
        for p in open_positions:
            sec = p.get("sector", "") or ""
            sector_deployed[sec] = sector_deployed.get(sec, 0) + p.get("position_pct", 0)

        would_buy, skipped = [], []
        slots = config.MAX_POSITIONS - meaningful
        exposure_left = config.MAX_PORTFOLIO_EXPOSURE - deployed

        for c in sorted(candidates, key=lambda x: x.composite_score, reverse=True):
            if len(would_buy) >= top_n:
                break
            sym = c.symbol
            if sym in held:
                skipped.append({"symbol": sym, "reason": "held"})
                continue
            if slots <= 0:
                skipped.append({"symbol": sym, "reason": "max_positions"})
                continue
            if exposure_left < size_pct:
                skipped.append({"symbol": sym, "reason": "exposure_cap"})
                continue
            sector = (c.signals or {}).get("sector", "") or ""
            if sector and sector_deployed.get(sector, 0) + size_pct > config.MAX_SECTOR_PCT:
                skipped.append({"symbol": sym, "reason": f"sector_cap:{sector}"})
                continue

            bars = fetcher.get_ohlcv(sym, days=60)
            price = c.price
            atr = _calculate_atr(bars, config.ATR_PERIOD) if bars else None
            atr_stop  = price - config.ATR_MULTIPLIER * atr if atr else None
            hard_stop = price * (1 - config.HARD_STOP_PCT)
            stop = round(max(atr_stop or 0, hard_stop), 2)

            would_buy.append({
                "symbol":          sym,
                "composite_score": c.composite_score,
                "price":           round(price, 2),
                "size_pct":        size_pct,
                "position_usd":    round(portfolio_value * size_pct, 2),
                "stop_price":      stop,
                "sector":          sector,
            })
            slots -= 1
            exposure_left -= size_pct
            if sector:
                sector_deployed[sector] = sector_deployed.get(sector, 0) + size_pct

        log_baseline_shadow({
            "regime":      regime,
            "vix_regime":  vix_regime,
            "run_reason":  run_reason,
            "n_candidates": len(candidates),
            "would_buy":   would_buy,
            "skipped":     skipped,
        })
        logger.info("Baseline shadow | would buy %s | skipped %d",
                    [b["symbol"] for b in would_buy], len(skipped))
    except Exception as exc:
        # Measurement must never affect the live pipeline.
        logger.warning("baseline shadow failed (ignored): %s", exc)
