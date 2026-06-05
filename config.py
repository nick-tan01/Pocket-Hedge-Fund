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
# C16-Phase1: the old "earnings_catalyst" factor (top-weighted 0.25) actually read
# TRAILING TTM growth, not event proximity — a growth-quality tilt mislabeled as a
# catalyst, near-constant for most names. Renamed to growth_quality and demoted 0.25→0.20;
# the freed weight goes to relative_strength (the core momentum edge) + technical.
# (Phase 2 will add a real PEAD earnings-proximity factor.) Weights sum to 1.00.
SCREENER_WEIGHTS = {
    "growth_quality":    0.20,
    "relative_strength": 0.23,
    "technical":         0.22,
    "volume_spike":      0.20,
    "news_quality":      0.10,
    "valuation":         0.05,
}

# ── After-close market memory ────────────────────────────────────────────────
AFTER_CLOSE_WATCHLIST_MAX = 20     # Logged evidence cards; not all become candidates
WATCHLIST_HISTORY_LIMIT   = 30     # Keep recent lists for audit without bloating data.json
WATCHLIST_MEMORY_BONUS    = 0.06   # Small tie-breaker after next-run revalidation
WATCHLIST_EXPIRE_HOUR_ET  = 14     # C6: expire 14:00 ET so the 13:00 ET midday run still
WATCHLIST_EXPIRE_MINUTE_ET= 0      #     sees overnight memory (was 10:15, which locked it out);
                                   #     still expires before the next open, never carried overnight.

# ── Portfolio rules ───────────────────────────────────────────────────────────
MAX_POSITIONS          = 8         # Max concurrent open positions
MAX_POSITION_PCT       = 0.10      # 10% of portfolio hard ceiling per position
MAX_PORTFOLIO_EXPOSURE = 0.60      # Total equity exposure cap (60% deployed, 40% reserve)
MIN_CONVICTION_SCORE   = 6         # Out of 10 — below this, hold cash
MAX_SECTOR_PCT         = 0.25      # C11: No single sector > 25% of NAV (raised from 0.20).
                                   # NOTE: measured as a fraction of NAV (sum of position_pct),
                                   # NOT of deployed capital. At 0.60 gross, 0.25 of NAV ≈ 42% of
                                   # the deployed book — admits a 4th conviction-7 tech name while
                                   # still forcing ≥2 other sectors. The watchlist is ~9/14 tech,
                                   # so 0.20 was structurally capping the book at ~3 tech positions.
CORRELATION_THRESHOLD  = 0.75      # Return-correlation threshold for overlap checks
ROTATION_SCORE_MARGIN  = 1.0       # Candidate must beat current holding by this much
MIN_SLOT_PCT           = 0.03      # Positions below 3% don't count against MAX_POSITIONS —
                                   # trimmed remnants can't block new full-size entries

# Conviction → position size mapping
CONVICTION_SIZE_MAP = {
    6:  0.04,   # 4%  of portfolio ($4,000) — near-miss captures
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

# C13-EXIT: when True (default), a thesis-"weakened" review streak only force-exits a
# position if price ALSO confirms weakness (below entry OR EMA10/30 trend no longer up).
# Prevents dumping winners on thesis-nerves (the rule cut RKLB/DOCS pre-rally in
# backtest); a "broken" thesis still exits unconditionally. Set False for legacy behavior.
WEAKENED_EXIT_REQUIRE_PRICE = True

# C12: force-exit a weakened/broken REMNANT (position < MIN_SLOT_PCT) after 2 consecutive
# weakened reviews — clears dead-weight crumbs (e.g. ARM). Intact remnants are NOT exited;
# they are left to be rebought to full by C7. Set False to disable remnant cleanup.
REMNANT_FORCE_EXIT = True

# C7: allow a held REMNANT (< MIN_SLOT_PCT) to be rebought back toward full size. The
# buy is sized as the GAP only (target − current), never a fresh full position, so shares
# are never double-counted, and it grows the existing position record instead of creating
# a duplicate. Meaningful holdings (≥ MIN_SLOT_PCT) still hard-skip as before.
# ACTIVE — delta-math reviewed (2% remnant -> conv-8 buys the 6% gap = $6,000, not $8,000).
# Set False to restore the legacy "skip any held symbol" behavior.
REMNANT_REBUY = True

# C13-TECH: "llm" (legacy, LLM narrates indicators), "shadow" (compute BOTH the
# deterministic rule and the LLM, log agreement, but USE the LLM — data collection,
# no behavior change), or "deterministic" (use the rule, skip the LLM call). Default shadow.
TECHNICAL_MODE = "shadow"

# C18: "off", "watch" (compute the pre-debate skip decision + log it, but STILL run the
# debate — data collection, no behavior change), or "enforce" (actually skip the debate
# when a buy is structurally impossible). Flipped to enforce 2026-06-04 after 16/16
# shadow samples matched the risk manager's post-debate skips (100% accuracy).
# Gates active: sector_saturated + exposure_maxed. Score-floor gate deferred pending
# more C3-OBS composite data.
PRE_DEBATE_GATE_MODE = "enforce"

# ── Circuit breakers ──────────────────────────────────────────────────────────
VIX_ELEVATED_THRESHOLD = 20.0     # Scale new entries down in elevated-volatility regimes
VIX_HIGH_THRESHOLD     = 30.0     # Require stronger conviction and smaller sizing
MAX_PORTFOLIO_DD     = 0.10       # Pause if portfolio drawdown > 10%
MARKET_OPEN_BUFFER   = 30         # Minutes after open before trading
MARKET_CLOSE_BUFFER  = 30         # Minutes before close to stop trading

# ── Schedule (ET times) ──────────────────────────────────────────────────────
RUN_TIMES_ET = ["08:30", "13:00"]
SENTINEL_MARKET_SYMBOLS = ["SPY", "QQQ", "IWM"]  # Broad event symbols
SENTINEL_EVENT_MAX_CANDIDATES = 4                 # Narrow reruns after symbol events

# ── AI Model ─────────────────────────────────────────────────────────────────
ANALYST_MODEL   = "claude-sonnet-4-20250514"   # Fast, cheap — analysts
DEBATE_MODEL    = "claude-sonnet-4-20250514"   # Debate agents
MAX_TOKENS         = 500    # Screener / analyst agents (structured JSON, short output)
DEBATE_MAX_TOKENS  = 1000   # Bull, Bear, PM, position reviewer (need nuanced reasoning)

# ── Debate calibration (C14/C17) ─────────────────────────────────────────────
# Experimental fix for the conviction-collapse-to-7 / PM-echoes-bull pathology.
# ON (trial started 2026-06-02) — evidence-anchored conviction rubric + unresolved-
# bear-points requirement + exhaustion guardrail + symmetric cold-start priors + conv-7
# calibration audit. Forward-paper experiment; watching for: conviction stdev among
# buys widening (from ~0.3 toward >0.8), buy-rate at conv-7 dropping below 100%, and
# PM==bull echo-rate dropping below ~60%. Flip back to False to revert instantly.
DEBATE_RUBRIC_V2 = True

# ── Options scaffold (Phase 0 / Phase 1) ─────────────────────────────────────
# Master kill-switch: False = byte-identical to pre-options pipeline.
# Nothing in options code executes unless this is True.
OPTIONS_ENABLED               = False

# "shadow" = log would-have-traded but submit NO orders (Phase 1 — data collection).
# "live"   = submit real paper orders (Phase 2 — needs explicit sign-off to flip).
OPTIONS_MODE                  = "shadow"

# Safety: only defined-risk structures allowed (debit spreads, long options).
# Never flip False without a full short/naked-options risk-model review.
DEFINED_RISK_ONLY             = True

# Portfolio-wide premium-at-risk cap (% of NAV). Total open options premium ≤ this.
OPTIONS_PREMIUM_BUDGET_PCT    = 0.06
# Per-trade premium cap (% of NAV). No single spread costs more than this.
OPTIONS_MAX_PREMIUM_PER_TRADE = 0.02
# Max concurrent open option positions (Phase 1: 1 — proof-of-concept only).
OPTIONS_MAX_CONCURRENT        = 1

# Minimum conviction to trigger an options shadow/trade (adjusted from 9/10 to 8
# given historical distribution: 0 trades ever scored 9+; 8 is the highest realistic
# threshold that DEBATE_RUBRIC_V2 can reach. Revisit after 20+ debates with v2 on).
OPTION_MIN_CONVICTION         = 8

# Contract selection parameters.
OPTION_TARGET_DTE             = 38    # Target DTE at entry (30-45 band).
OPTION_MIN_DTE                = 21    # Exit/roll trigger DTE.
OPTION_LONG_DELTA             = 0.575 # Long-leg target delta (0.55-0.60; slightly ITM,
                                      # lower premium than 0.65 given conv-8 threshold).
OPTION_SHORT_DELTA            = 0.30  # Short-leg target delta (0.30-0.35).
OPTION_PREMIUM_STOP_PCT       = 0.50  # Exit if premium falls to 50% of debit paid.

# Greeks source: "local_bsm" = our own Black-Scholes calc (core/options_greeks.py).
# Alpaca indicative feed used only as a cross-check (15-min delayed on Basic plan).
GREEKS_SOURCE                 = "local_bsm"

# ── Benchmark ────────────────────────────────────────────────────────────────
BENCHMARK_TICKER = "SPY"

# ── Paths ────────────────────────────────────────────────────────────────────
JOURNAL_PATH      = "dashboard/data.json"
LOG_DIR           = "logs/"
