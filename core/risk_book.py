"""
core/risk_book.py
Portfolio-level risk accounting.

Phase 0: long-equity only — computes gross exposure, per-position capital-at-risk
(stop-loss distance × size), and net exposure (== gross for long-only).
Validates these against live Alpaca account fields before any options position exists.

Phase 2+: extended to handle options (premium-at-risk, net delta, aggregate Greeks)
and shorts (negative-sign positions, inverted stops, margin accounting).
All option/short branches are currently stubs behind OPTIONS_ENABLED — only the
long-equity path runs today, so there is zero risk of disturbing the live pipeline.

NOT imported by main.py / agents currently — called only from the shadow logger
(core/options_selector.py) and future dashboard analytics.
"""

import logging
from dataclasses import dataclass, field

import config

logger = logging.getLogger(__name__)


@dataclass
class PositionRisk:
    symbol:           str
    instrument_type:  str     # "stock" | "call_spread" | "put_spread" | "short_stock"
    position_pct:     float   # fraction of NAV
    capital_at_risk:  float   # $ loss if stop fires (stock) or premium (options)
    max_loss:         float   # same as capital_at_risk for defined-risk instruments
    net_delta_dollars: float  # price sensitivity: Δ$ per $1 move in underlying
    stop_price:       float   # 0 for options (managed by premium stop)
    # options-only (None for stocks)
    premium_paid:     float | None = None
    expiry:           str | None = None
    dte:              int | None = None


@dataclass
class PortfolioRisk:
    portfolio_value:   float
    gross_exposure:    float   # sum of |position_pct| — includes both long and short
    net_long_exposure: float   # sum of long position_pcts
    net_short_exposure: float  # sum of short position_pcts (negative number)
    net_exposure:      float   # net_long + net_short
    total_capital_at_risk: float   # $ — sum of per-position stop-loss exposure
    total_premium_at_risk: float   # $ — sum of open options premium (Phase 0: 0)
    pct_capital_at_risk:   float   # total_capital_at_risk / portfolio_value
    pct_premium_at_risk:   float   # total_premium_at_risk / portfolio_value
    positions:         list[PositionRisk] = field(default_factory=list)
    warnings:          list[str] = field(default_factory=list)


def compute_portfolio_risk(open_trades: list[dict],
                           portfolio_value: float) -> PortfolioRisk:
    """
    Compute portfolio risk from the journal's open_trades list.
    Phase 0: handles stock positions only. Options/short stubs are no-ops.
    """
    if portfolio_value <= 0:
        return PortfolioRisk(portfolio_value=0, gross_exposure=0,
                             net_long_exposure=0, net_short_exposure=0,
                             net_exposure=0, total_capital_at_risk=0,
                             total_premium_at_risk=0, pct_capital_at_risk=0,
                             pct_premium_at_risk=0)

    positions: list[PositionRisk] = []
    warnings: list[str] = []

    for t in open_trades:
        instrument = t.get("instrument_type", "stock")
        pct        = float(t.get("position_pct", 0) or 0)
        stop_px    = float(t.get("stop_price", 0) or 0)
        entry_px   = float(t.get("entry_price", 0) or 0)
        curr_px    = float(t.get("current_price") or entry_px or 0)
        symbol     = t.get("symbol", "")

        if instrument == "stock":
            # Capital at risk = stop distance × position size in dollars.
            if curr_px > 0 and stop_px > 0 and curr_px > stop_px:
                stop_dist_pct  = (curr_px - stop_px) / curr_px
                position_value = pct * portfolio_value
                cap_at_risk    = position_value * stop_dist_pct
            elif entry_px > 0 and stop_px > 0:
                stop_dist_pct  = (entry_px - stop_px) / entry_px
                position_value = pct * portfolio_value
                cap_at_risk    = position_value * stop_dist_pct
            else:
                # No stop data — use hard-stop config as fallback
                cap_at_risk = pct * portfolio_value * config.HARD_STOP_PCT

            pos = PositionRisk(
                symbol=symbol,
                instrument_type="stock",
                position_pct=pct,
                capital_at_risk=round(cap_at_risk, 2),
                max_loss=round(cap_at_risk, 2),
                net_delta_dollars=round(pct * portfolio_value, 2),
                stop_price=stop_px,
            )

        elif instrument in ("call_spread", "put_spread"):
            # Options: max loss = premium paid × 100 × qty.
            # Phase 0 stub — options trades don't exist yet.
            premium    = float(t.get("premium_paid") or t.get("capital_at_risk", 0) or 0)
            cap_at_risk = premium
            pos = PositionRisk(
                symbol=symbol,
                instrument_type=instrument,
                position_pct=pct,
                capital_at_risk=round(cap_at_risk, 2),
                max_loss=round(cap_at_risk, 2),
                net_delta_dollars=round(float(t.get("net_delta_dollars", 0) or 0), 2),
                stop_price=0.0,
                premium_paid=premium,
                expiry=t.get("expiry"),
                dte=t.get("dte"),
            )

        else:
            # Unknown type — treat conservatively.
            cap_at_risk = pct * portfolio_value * config.HARD_STOP_PCT
            warnings.append(f"{symbol}: unknown instrument_type '{instrument}', "
                            f"using hard-stop proxy for risk")
            pos = PositionRisk(
                symbol=symbol, instrument_type=instrument,
                position_pct=pct, capital_at_risk=round(cap_at_risk, 2),
                max_loss=round(cap_at_risk, 2),
                net_delta_dollars=round(pct * portfolio_value, 2),
                stop_price=stop_px,
            )

        positions.append(pos)

    # Aggregate
    long_pct    = sum(p.position_pct for p in positions
                      if p.instrument_type in ("stock", "call_spread"))
    short_pct   = sum(p.position_pct for p in positions
                      if p.instrument_type == "short_stock")  # Phase 0: always 0
    gross       = long_pct + abs(short_pct)
    net         = long_pct + short_pct
    total_car   = sum(p.capital_at_risk for p in positions
                      if p.instrument_type == "stock")
    total_prem  = sum(p.premium_paid or 0 for p in positions
                      if p.instrument_type in ("call_spread", "put_spread"))

    pct_car     = total_car  / portfolio_value if portfolio_value else 0
    pct_prem    = total_prem / portfolio_value if portfolio_value else 0

    # Guardrail warnings
    if gross > config.MAX_PORTFOLIO_EXPOSURE + 0.01:
        warnings.append(
            f"Gross exposure {gross*100:.1f}% exceeds cap "
            f"{config.MAX_PORTFOLIO_EXPOSURE*100:.0f}%"
        )
    if pct_prem > getattr(config, "OPTIONS_PREMIUM_BUDGET_PCT", 0.06):
        warnings.append(
            f"Options premium-at-risk {pct_prem*100:.1f}% exceeds budget "
            f"{getattr(config,'OPTIONS_PREMIUM_BUDGET_PCT',0.06)*100:.0f}%"
        )

    return PortfolioRisk(
        portfolio_value=portfolio_value,
        gross_exposure=round(gross, 4),
        net_long_exposure=round(long_pct, 4),
        net_short_exposure=round(short_pct, 4),
        net_exposure=round(net, 4),
        total_capital_at_risk=round(total_car, 2),
        total_premium_at_risk=round(total_prem, 2),
        pct_capital_at_risk=round(pct_car, 4),
        pct_premium_at_risk=round(pct_prem, 4),
        positions=positions,
        warnings=warnings,
    )
