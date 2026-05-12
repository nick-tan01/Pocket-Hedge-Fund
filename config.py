"""
config.py — single source of truth for all system parameters.
Change values here; nothing else needs to be edited.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── API Keys ──────────────────────────────────────────────────────────────────
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# ── Alpaca ────────────────────────────────────────────────────────────────────
PAPER_TRADING     = True          # Never flip to False without careful review
STARTING_CAPITAL  = 100_000.0     # Paper account starting value

# ── Universe filters (applied before any LLM call) ───────────────────────────
MIN_PRICE         = 5.0           # No penny stocks
MIN_MARKET_CAP    = 2_000_000_000 # $2B minimum
MIN_VOLUME        = 500_000       # 500k avg daily volume minimum
VOLUME_SPIKE_MULT = 2.0           # Flag if today's vol > 2× 20-day avg

# ── Screener ──────────────────────────────────────────────────────────────────
SCREENER_MAX_CANDIDATES = 6       # Hard ceiling for candidates passed to LLM agents
SCREENER_MIN_CANDIDATES = 3       # Floor so the system keeps scanning for replacements
SCREENER_WEIGHTS = {
    "earnings_catalyst": 0.25,
    "relative_strength": 0.20,
    "technical":         0.20,
    "volume_spike":      0.20,
    "news_quality":      0.10,
    "valuation":         0.05,
}

# ── Portfolio rules ───────────────────────────────────────────────────────────
MAX_POSITIONS          = 8         # Max concurrent open positions
MAX_POSITION_PCT       = 0.10      # 10% of portfolio hard ceiling per position
MAX_PORTFOLIO_EXPOSURE = 0.60      # Total equity exposure cap (60% deployed, 40% reserve)
MIN_CONVICTION_SCORE   = 7         # Out of 10 — below this, hold cash
MAX_SECTOR_PCT         = 0.20      # No single sector > 20% of deployed capital

# Conviction → position size mapping
CONVICTION_SIZE_MAP = {
    7:  0.06,   # 6%  of portfolio ($6,000 on $100k)
    8:  0.08,   # 8%  of portfolio ($8,000)
    9:  0.10,   # 10% of portfolio ($10,000)
    10: 0.10,   # capped at 10%
}

# ── Risk / Stop loss ──────────────────────────────────────────────────────────
ATR_PERIOD           = 14         # 14-day ATR
ATR_MULTIPLIER       = 2.5        # Stop = entry - (2.5 × ATR), widened for momentum names
HARD_STOP_PCT        = 0.08       # Never lose more than 8% on any single trade
TRAILING_STOP_TRIGGER= 0.15       # Activate trailing stop once +15% profit
TRAILING_STOP_PCT    = 0.10       # Trail 10% below peak

# ── Circuit breakers ──────────────────────────────────────────────────────────
VIX_ELEVATED_THRESHOLD = 20.0     # Scale new entries down in elevated-volatility regimes
VIX_HIGH_THRESHOLD     = 30.0     # Require stronger conviction and smaller sizing
MAX_PORTFOLIO_DD     = 0.10       # Pause if portfolio drawdown > 10%
MARKET_OPEN_BUFFER   = 30         # Minutes after open before trading
MARKET_CLOSE_BUFFER  = 30         # Minutes before close to stop trading

# ── Schedule (ET times) ──────────────────────────────────────────────────────
RUN_TIMES_ET = ["08:30", "13:00"]

# ── AI Model ─────────────────────────────────────────────────────────────────
ANALYST_MODEL   = "claude-sonnet-4-20250514"   # Fast, cheap — analysts
DEBATE_MODEL    = "claude-sonnet-4-20250514"   # Debate agents
MAX_TOKENS         = 500    # Screener / analyst agents (structured JSON, short output)
DEBATE_MAX_TOKENS  = 1000   # Bull, Bear, PM, position reviewer (need nuanced reasoning)

# ── Benchmark ────────────────────────────────────────────────────────────────
BENCHMARK_TICKER = "SPY"

# ── Paths ────────────────────────────────────────────────────────────────────
JOURNAL_PATH      = "dashboard/data.json"
LOG_DIR           = "logs/"
