"""
core/options_greeks.py
Local Black-Scholes options Greeks calculator.

Source of truth for IV and Greeks — used instead of the Alpaca indicative feed
because the Basic plan only provides a 15-min-delayed indicative feed, and real-time
OPRA greeks require the paid Algo Trader Plus tier. Alpaca's snapshot is used only
as a cross-check when available.

Reference: Hull, J.C. "Options, Futures, and Other Derivatives" (10th ed.), Ch. 19.
           Black, F. & Scholes, M. (1973) "The Pricing of Options and Corporate Liabilities."

All time inputs in YEARS. Prices in dollars. Rates as decimals (e.g. 0.05 = 5%).
"""

import math
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Risk-free rate — use the 3-month T-bill proxy.
# Can be overridden at call sites; default is a conservative estimate.
DEFAULT_RISK_FREE_RATE = 0.045  # 4.5% as of mid-2026 approximation


@dataclass
class Greeks:
    delta:    float
    gamma:    float
    theta:    float   # per calendar day (not per year)
    vega:     float   # per 1 percentage-point move in IV
    rho:      float
    iv:       float   # implied volatility (decimal, e.g. 0.30 = 30%)
    price:    float   # theoretical BSM price
    intrinsic: float  # max(S-K, 0) for calls, max(K-S, 0) for puts


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erfc for numerical stability."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float):
    """Compute d1 and d2. Returns (None, None) on invalid inputs."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None, None
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return d1, d2


def bsm_price(S: float, K: float, T: float, r: float, sigma: float,
              option_type: str = "call") -> float:
    """Black-Scholes-Merton option price. option_type: 'call' | 'put'."""
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if d1 is None:
        return max(S - K, 0) if option_type == "call" else max(K - S, 0)
    disc = math.exp(-r * T)
    if option_type == "call":
        return S * _norm_cdf(d1) - K * disc * _norm_cdf(d2)
    else:
        return K * disc * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def compute_greeks(S: float, K: float, T: float, r: float, sigma: float,
                   option_type: str = "call") -> Greeks:
    """
    Compute full Greeks for a European option.

    Args:
        S:           Underlying spot price
        K:           Strike price
        T:           Time to expiry in years (e.g. 30 days = 30/365)
        r:           Risk-free rate (decimal)
        sigma:       Implied volatility (decimal)
        option_type: 'call' | 'put'
    """
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if d1 is None:
        intrinsic = max(S - K, 0) if option_type == "call" else max(K - S, 0)
        return Greeks(delta=1.0 if intrinsic > 0 else 0.0,
                      gamma=0.0, theta=0.0, vega=0.0, rho=0.0,
                      iv=sigma, price=intrinsic, intrinsic=intrinsic)

    sqrt_T  = math.sqrt(T)
    disc    = math.exp(-r * T)
    pdf_d1  = _norm_pdf(d1)
    price   = bsm_price(S, K, T, r, sigma, option_type)
    intrinsic = max(S - K, 0) if option_type == "call" else max(K - S, 0)

    # Delta
    if option_type == "call":
        delta = _norm_cdf(d1)
    else:
        delta = _norm_cdf(d1) - 1.0

    # Gamma (same for call and put)
    gamma = pdf_d1 / (S * sigma * sqrt_T)

    # Theta — per calendar day (Hull formula, divided by 365)
    if option_type == "call":
        theta_yr = (-S * pdf_d1 * sigma / (2 * sqrt_T)
                    - r * K * disc * _norm_cdf(d2))
    else:
        theta_yr = (-S * pdf_d1 * sigma / (2 * sqrt_T)
                    + r * K * disc * _norm_cdf(-d2))
    theta = theta_yr / 365.0

    # Vega — per 1pp (0.01) move in IV; hull gives per unit, we divide by 100
    vega = S * sqrt_T * pdf_d1 / 100.0

    # Rho — per 1pp move in r
    if option_type == "call":
        rho = K * T * disc * _norm_cdf(d2) / 100.0
    else:
        rho = -K * T * disc * _norm_cdf(-d2) / 100.0

    return Greeks(delta=round(delta, 4), gamma=round(gamma, 6),
                  theta=round(theta, 4), vega=round(vega, 4),
                  rho=round(rho, 4), iv=round(sigma, 4),
                  price=round(price, 4), intrinsic=round(intrinsic, 4))


def implied_vol(market_price: float, S: float, K: float, T: float, r: float,
                option_type: str = "call",
                tol: float = 1e-5, max_iter: int = 100) -> float | None:
    """
    Newton-Raphson implied volatility solver.
    Returns IV (decimal) or None if it fails to converge.
    """
    if T <= 0 or market_price <= 0 or S <= 0 or K <= 0:
        return None

    intrinsic = max(S - K, 0) if option_type == "call" else max(K - S, 0)
    if market_price < intrinsic:
        return None  # below intrinsic — arbitrage; can't solve

    sigma = 0.30  # starting guess: 30% IV
    for _ in range(max_iter):
        try:
            price = bsm_price(S, K, T, r, sigma, option_type)
            d1, _ = _d1_d2(S, K, T, r, sigma)
            if d1 is None:
                break
            vega_raw = S * math.sqrt(T) * _norm_pdf(d1)  # raw vega (per unit sigma)
            if abs(vega_raw) < 1e-8:
                break
            diff = price - market_price
            if abs(diff) < tol:
                return round(sigma, 6)
            sigma -= diff / vega_raw
            if sigma <= 0:
                sigma = 1e-4
        except (ValueError, OverflowError, ZeroDivisionError):
            break
    return None


def spread_greeks(long_greeks: Greeks, short_greeks: Greeks,
                  qty: int = 1) -> dict:
    """
    Net Greeks for a vertical debit spread: long_leg - short_leg.
    qty = number of spreads (each controls 100 shares).
    """
    multiplier = qty * 100
    return {
        "net_delta":  round((long_greeks.delta  - short_greeks.delta)  * multiplier, 4),
        "net_gamma":  round((long_greeks.gamma  - short_greeks.gamma)  * multiplier, 6),
        "net_theta":  round((long_greeks.theta  - short_greeks.theta)  * multiplier, 4),
        "net_vega":   round((long_greeks.vega   - short_greeks.vega)   * multiplier, 4),
        "net_delta_pct": round(long_greeks.delta - short_greeks.delta, 4),
        "max_loss_per_spread": round(
            (long_greeks.price - short_greeks.price) * 100, 2),
        "max_profit_per_spread": round(
            (abs(long_greeks.intrinsic - short_greeks.intrinsic)
             - (long_greeks.price - short_greeks.price)) * 100, 2),
    }
