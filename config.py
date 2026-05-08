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
SCREENER_MAX_CANDIDATES = 8       # Max tickers passed to analyst agents per run
SCREENER_WEIGHTS = {
    "news_catalyst": 0.30,
    "volume_spike":  0.25,
    "technical":     0.25,
    "valuation":     0.20,
}

# ── Portfolio rules ───────────────────────────────────────────────────────────
MAX_POSITIONS         = 5         # Max concurrent open positions
MAX_POSITION_PCT      = 0.05      # 5% of portfolio per position
MIN_CONVICTION_SCORE  = 7         # Out of 10 — below this, hold cash
MAX_SECTOR_PCT        = 0.20      # No single sector > 20% of deployed capital

# Conviction → position size mapping
CONVICTION_SIZE_MAP = {
    7: 0.03,   # 3% of portfolio
    8: 0.04,   # 4%
    9: 0.05,   # 5%
    10: 0.05,  # capped at 5%
}

# ── Risk / Stop loss ──────────────────────────────────────────────────────────
ATR_PERIOD           = 14         # 14-day ATR
ATR_MULTIPLIER       = 2.0        # Stop = entry - (2 × ATR)
HARD_STOP_PCT        = 0.08       # Never lose more than 8% on any single trade
TRAILING_STOP_TRIGGER= 0.05       # Activate trailing stop once +5% profit
TRAILING_STOP_PCT    = 0.03       # Trail 3% below peak

# ── Circuit breakers ──────────────────────────────────────────────────────────
VIX_PAUSE_THRESHOLD  = 30.0       # Pause all new trades above this VIX level
MAX_PORTFOLIO_DD     = 0.10       # Pause if portfolio drawdown > 10%
MARKET_OPEN_BUFFER   = 30         # Minutes after open before trading
MARKET_CLOSE_BUFFER  = 30         # Minutes before close to stop trading

# ── Schedule (ET times) ──────────────────────────────────────────────────────
RUN_TIMES_ET = ["08:30", "13:00"]

# ── AI Model ─────────────────────────────────────────────────────────────────
ANALYST_MODEL   = "claude-sonnet-4-20250514"   # Fast, cheap — analysts
DEBATE_MODEL    = "claude-sonnet-4-20250514"   # Debate agents
MAX_TOKENS      = 500                           # Hard cap per agent call

# ── Benchmark ────────────────────────────────────────────────────────────────
BENCHMARK_TICKER = "SPY"

# ── Paths ────────────────────────────────────────────────────────────────────
JOURNAL_PATH      = "dashboard/data.json"
LOG_DIR           = "logs/"
