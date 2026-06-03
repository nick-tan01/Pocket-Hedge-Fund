"""
core/options_selector.py
Given a conviction signal, resolve the concrete call-debit-spread contracts
and sizing for the Phase 1 shadow log.

Phase 0 (OPTIONS_ENABLED=False): never called.
Phase 1 (OPTIONS_MODE="shadow"): called by _options_shadow_log in main.py;
    returns a SpreadProposal for logging only — no orders submitted.
Phase 2 (OPTIONS_MODE="live"): same return value routes to the Alpaca client.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone

import config
from core.options_greeks import (
    compute_greeks,
    implied_vol,
    spread_greeks,
    DEFAULT_RISK_FREE_RATE,
    Greeks,
)

logger = logging.getLogger(__name__)


@dataclass
class SpreadLeg:
    occ_symbol:  str    # e.g. "AAPL260117C00200000"
    strike:      float
    expiry:      str    # "YYYY-MM-DD"
    option_type: str    # "call" | "put"
    greeks:      Greeks
    market_mid:  float | None  # None if not available on indicative feed


@dataclass
class SpreadProposal:
    underlying:       str
    structure:        str     # "call_debit_spread"
    long_leg:         SpreadLeg
    short_leg:        SpreadLeg
    net_debit:        float   # per-spread premium (long - short), in dollars × 100
    max_loss:         float   # = net_debit (defined-risk)
    max_profit:       float   # (spread width - net_debit) × 100
    breakeven:        float   # long_strike + net_debit/100
    qty:              int     # number of spreads (≥ 1)
    total_premium:    float   # net_debit × qty (total dollars at risk)
    pct_of_portfolio: float   # total_premium / portfolio_value
    net_greeks:       dict    # from spread_greeks()
    dte:              int
    expiry_date:      str
    conviction:       int
    catalyst:         str
    veto_reason:      str | None  # set if a veto fired; caller logs and discards


def _parse_expiry(expiry_str: str) -> date | None:
    """Parse YYYY-MM-DD expiry string."""
    try:
        return datetime.strptime(expiry_str[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _dte(expiry_str: str) -> int:
    """Calendar days to expiry from today."""
    exp = _parse_expiry(expiry_str)
    if exp is None:
        return 0
    return max(0, (exp - date.today()).days)


def _earnings_in_window(fetcher, symbol: str, expiry_str: str) -> bool:
    """
    Return True if known earnings date falls between today and expiry.
    Uses yfinance earningsDate via the DataFetcher if available.
    Conservative: if we can't determine, returns False (don't veto on uncertainty).
    """
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        raw  = info.get("earningsDate") or info.get("earningsTimestamp")
        if raw is None:
            return False
        if isinstance(raw, (list, tuple)) and raw:
            raw = raw[0]
        if hasattr(raw, "iloc"):
            raw = raw.iloc[0]
        if isinstance(raw, (int, float)):
            from datetime import datetime
            ed = datetime.utcfromtimestamp(raw).date()
        elif isinstance(raw, datetime):
            ed = raw.date()
        elif isinstance(raw, date):
            ed = raw
        else:
            ed = date.fromisoformat(str(raw)[:10])
        exp = _parse_expiry(expiry_str)
        today = date.today()
        if exp is None:
            return False
        return today < ed <= exp
    except Exception:
        return False


def _select_contracts(alpaca_client, symbol: str, option_type: str,
                      expiry_gte: str, expiry_lte: str,
                      long_delta_target: float,
                      short_delta_target: float,
                      spot: float, dte: float) -> tuple | None:
    """
    Query GetOptionContractsRequest for symbol, find the long + short leg closest
    to the target deltas using local BSM with a 30% IV seed.
    Returns (long_contract_dict, short_contract_dict) or None.
    """
    try:
        from alpaca.trading.requests import GetOptionContractsRequest
        from alpaca.trading.enums import ContractType
        req = GetOptionContractsRequest(
            underlying_symbols=[symbol],
            type=ContractType.CALL if option_type == "call" else ContractType.PUT,
            expiration_date_gte=expiry_gte,
            expiration_date_lte=expiry_lte,
            status="active",
            limit=200,
        )
        contracts = alpaca_client.trading.get_option_contracts(req)
    except Exception as e:
        logger.warning("options_selector: contract query failed for %s: %s", symbol, e)
        return None

    if not contracts or not hasattr(contracts, "option_contracts"):
        logger.debug("options_selector: no contracts returned for %s", symbol)
        return None

    T = dte / 365.0
    r = DEFAULT_RISK_FREE_RATE
    sigma_seed = 0.30  # BSM IV seed — we solve for delta given spot + strike

    long_best:  tuple[float, dict] | None = None
    short_best: tuple[float, dict] | None = None

    for c in contracts.option_contracts:
        try:
            strike = float(c.strike_price)
            g = compute_greeks(spot, strike, T, r, sigma_seed, option_type)
            delta_abs = abs(g.delta)

            # long leg: closest to OPTION_LONG_DELTA
            long_dist = abs(delta_abs - long_delta_target)
            if long_best is None or long_dist < long_best[0]:
                long_best = (long_dist, {
                    "occ_symbol":  c.symbol,
                    "strike":      strike,
                    "expiry":      str(c.expiration_date)[:10],
                    "option_type": option_type,
                    "greeks":      g,
                    "market_mid":  None,
                })

            # short leg: closest to OPTION_SHORT_DELTA (must be OTM vs long)
            short_dist = abs(delta_abs - short_delta_target)
            if option_type == "call" and strike > spot:
                if short_best is None or short_dist < short_best[0]:
                    short_best = (short_dist, {
                        "occ_symbol":  c.symbol,
                        "strike":      strike,
                        "expiry":      str(c.expiration_date)[:10],
                        "option_type": option_type,
                        "greeks":      g,
                        "market_mid":  None,
                    })
            elif option_type == "put" and strike < spot:
                if short_best is None or short_dist < short_best[0]:
                    short_best = (short_dist, {
                        "occ_symbol":  c.symbol,
                        "strike":      strike,
                        "expiry":      str(c.expiration_date)[:10],
                        "option_type": option_type,
                        "greeks":      g,
                        "market_mid":  None,
                    })
        except Exception:
            continue

    if long_best is None or short_best is None:
        return None

    long_c  = long_best[1]
    short_c = short_best[1]

    # Ensure the short leg is actually more OTM than the long for a debit spread
    if option_type == "call" and short_c["strike"] <= long_c["strike"]:
        return None
    if option_type == "put" and short_c["strike"] >= long_c["strike"]:
        return None

    return long_c, short_c


def select_call_debit_spread(
    symbol: str,
    spot: float,
    portfolio_value: float,
    conviction: int,
    catalyst: str,
    fetcher,
    alpaca_client,
) -> SpreadProposal | None:
    """
    Select the best call debit spread for a given symbol + conviction.
    Returns a SpreadProposal (for shadow logging or live execution) or None.

    Vetoes: earnings inside expiry, premium would exceed per-trade cap.
    """
    long_delta  = getattr(config, "OPTION_LONG_DELTA",  0.575)
    short_delta = getattr(config, "OPTION_SHORT_DELTA", 0.30)
    target_dte  = getattr(config, "OPTION_TARGET_DTE",  38)
    min_dte     = getattr(config, "OPTION_MIN_DTE",     21)
    max_prem_pct = getattr(config, "OPTIONS_MAX_PREMIUM_PER_TRADE", 0.02)

    today      = date.today()
    expiry_gte = (today + timedelta(days=min_dte)).isoformat()
    expiry_lte = (today + timedelta(days=target_dte + 14)).isoformat()  # ±2wk band
    best_expiry = (today + timedelta(days=target_dte)).isoformat()

    # Earnings veto
    if _earnings_in_window(fetcher, symbol, expiry_lte):
        return SpreadProposal(
            underlying=symbol, structure="call_debit_spread",
            long_leg=None, short_leg=None,  # type: ignore
            net_debit=0, max_loss=0, max_profit=0, breakeven=0,
            qty=0, total_premium=0, pct_of_portfolio=0,
            net_greeks={}, dte=0, expiry_date=expiry_lte,
            conviction=conviction, catalyst=catalyst,
            veto_reason=f"earnings_in_window: earnings detected before {expiry_lte}",
        )

    # Contract selection
    result = _select_contracts(
        alpaca_client, symbol, "call",
        expiry_gte, expiry_lte,
        long_delta, short_delta,
        spot, target_dte,
    )
    if result is None:
        logger.info("options_selector: no valid spread found for %s", symbol)
        return None

    long_c, short_c = result
    long_leg  = SpreadLeg(**long_c)
    short_leg = SpreadLeg(**short_c)

    # Net debit = (long BSM price - short BSM price) × 100
    net_debit_per_share = long_leg.greeks.price - short_leg.greeks.price
    if net_debit_per_share <= 0:
        return None  # credit spread — not a debit; skip
    net_debit_dollars   = round(net_debit_per_share * 100, 2)
    spread_width        = abs(long_leg.strike - short_leg.strike)
    max_profit_dollars  = round((spread_width - net_debit_per_share) * 100, 2)
    breakeven           = round(long_leg.strike + net_debit_per_share, 2)

    # Sizing: how many spreads fit within the per-trade premium cap?
    max_prem_dollars = portfolio_value * max_prem_pct
    qty = max(1, math.floor(max_prem_dollars / net_debit_dollars))
    total_premium    = round(qty * net_debit_dollars, 2)
    pct_of_portfolio = round(total_premium / portfolio_value, 4)

    # Premium cap veto
    budget_pct = getattr(config, "OPTIONS_PREMIUM_BUDGET_PCT", 0.06)
    if pct_of_portfolio > budget_pct:
        return SpreadProposal(
            underlying=symbol, structure="call_debit_spread",
            long_leg=long_leg, short_leg=short_leg,
            net_debit=net_debit_dollars, max_loss=net_debit_dollars,
            max_profit=max_profit_dollars, breakeven=breakeven,
            qty=qty, total_premium=total_premium,
            pct_of_portfolio=pct_of_portfolio,
            net_greeks=spread_greeks(long_leg.greeks, short_leg.greeks, qty),
            dte=_dte(long_leg.expiry), expiry_date=long_leg.expiry,
            conviction=conviction, catalyst=catalyst,
            veto_reason=f"premium_exceeds_budget: {pct_of_portfolio*100:.1f}% > {budget_pct*100:.0f}%",
        )

    actual_dte = _dte(long_leg.expiry)
    return SpreadProposal(
        underlying=symbol,
        structure="call_debit_spread",
        long_leg=long_leg,
        short_leg=short_leg,
        net_debit=net_debit_dollars,
        max_loss=net_debit_dollars,
        max_profit=max_profit_dollars,
        breakeven=breakeven,
        qty=qty,
        total_premium=total_premium,
        pct_of_portfolio=pct_of_portfolio,
        net_greeks=spread_greeks(long_leg.greeks, short_leg.greeks, qty),
        dte=actual_dte,
        expiry_date=long_leg.expiry,
        conviction=conviction,
        catalyst=catalyst,
        veto_reason=None,
    )
