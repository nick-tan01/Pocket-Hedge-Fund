"""
agents/performance_context.py
Injects rolling performance data into agent prompts to surface recurring
mistakes and prevent the debate from repeating the same errors.

Phases (auto-selected by closed trade count):
  Cold start  (0–4 trades):   principle anchors from documented LLM biases
  Growth      (5–19 trades):  outcome stats + calibration check
  Mature      (20+ trades):   full ATLAS meta-feedback with expectancy analysis

Inject into: portfolio_manager.decide(), bull/bear opening_argument() only.
Do NOT inject into rebuttal functions — those must stay reactive to live debate.
"""

import logging
import config
from core.journal import get_all_trades

logger = logging.getLogger(__name__)


def get_performance_context(lookback: int = 10) -> str:
    """
    Return a formatted performance context block for prompt injection.
    Returns an empty string on any failure so callers need no error handling.
    """
    try:
        trades = get_all_trades()
        regime = _get_regime_tag()
        n = len(trades)

        # C17(3): when v2 is on, unlock outcome-based feedback sooner (n>=3 instead of
        # n>=5) so realized losses (e.g. the NVDA stop) reach the debate instead of two
        # more cold-start-only entries.
        cold_threshold = 3 if getattr(config, "DEBATE_RUBRIC_V2", False) else 5
        if n < cold_threshold:
            return _cold_start_context(regime)
        elif n < 20:
            return _growth_context(trades[-lookback:], regime, n)
        else:
            return _mature_context(trades[-lookback:], regime, n)
    except Exception as e:
        logger.warning("performance_context failed: %s", e)
        return ""


# ── Regime tag ────────────────────────────────────────────────────────────────

def _get_regime_tag() -> str:
    """SPY 1-month return → bullish / sideways / bearish."""
    try:
        import yfinance as yf
        spy = yf.Ticker("SPY").history(period="1mo")
        if spy.empty or len(spy) < 2:
            return "unknown"
        ret = (spy["Close"].iloc[-1] - spy["Close"].iloc[0]) / spy["Close"].iloc[0]
        if ret > 0.04:
            return "bullish"
        elif ret < -0.04:
            return "bearish"
        return "sideways"
    except Exception:
        return "unknown"


# ── Phase builders ─────────────────────────────────────────────────────────────

def _cold_start_context(regime: str) -> str:
    # C17(1): when v2 is on, balance the three pro-momentum priors with symmetric
    # counter-bias guidance so the cold-start block isn't a one-sided bullish thumb
    # on the scale (the suspected driver of the conviction-collapse-to-7).
    symmetric = ""
    if getattr(config, "DEBATE_RUBRIC_V2", False):
        symmetric = """
Counter-biases to apply with EQUAL weight:
• Sycophancy / agreement bias: the bear often parks at 6 and the bull at 7, manufacturing
  a 1-point "tie." A narrow spread is frequently politeness, not evidence — it is NOT a buy signal.
• Catalyst illusion: "continued momentum" is not a catalyst. Require a dated, trade-specific event.
• Conviction-7 default: if you land on 7, ask whether the evidence actually justifies 6 or 8.
  The middle is where unexamined calls hide."""
    return f"""
═══ PERFORMANCE CONTEXT (cold start — fewer than 5 closed trades) ═══
Market regime (SPY 1-month): {regime}

Documented LLM biases to actively counter:
• Debate moderation bias: bull and bear frequently converge to conviction 5-6
  through mutual politeness rather than genuine evidence. A well-supported bull
  case should hold 8-9 even after hearing the bear. Do not reduce conviction
  simply to appear balanced — only reduce it when the evidence changes.
• RSI overbought reflex: RSI above 70 is NOT a sell signal in trending markets
  (ADX > 25). Treat elevated RSI as momentum confirmation, not exhaustion.
  Only flag as bearish when ADX < 20 or volume is declining as price rises.
• Momentum avoidance: LLMs systematically underweight continuation signals in
  bull regimes. Strong technical + intact thesis = high conviction, not "watch".{symmetric}
═══════════════════════════════════════════════════════════════════════════════
"""


def _growth_context(recent_trades: list[dict], regime: str, total_n: int) -> str:
    if not recent_trades:
        return _cold_start_context(regime)

    wins   = [t for t in recent_trades if t.get("pnl", 0) > 0]
    losses = [t for t in recent_trades if t.get("pnl", 0) <= 0]
    win_rate = len(wins) / len(recent_trades) * 100 if recent_trades else 0
    avg_win  = (sum(t.get("pnl_pct", 0) for t in wins)   / len(wins))   if wins   else 0
    avg_loss = (sum(t.get("pnl_pct", 0) for t in losses) / len(losses)) if losses else 0

    exit_reasons: dict[str, int] = {}
    for t in recent_trades:
        r = t.get("exit_reason", "unknown")
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    calibration = _check_conviction_calibration(recent_trades)

    return f"""
═══ PERFORMANCE CONTEXT ({total_n} closed trades, last {len(recent_trades)} shown) ═══
Market regime (SPY 1-month): {regime}

Recent outcomes:
• Win rate: {win_rate:.0f}%  ({len(wins)}W / {len(losses)}L)
• Avg winner: +{avg_win:.1f}%  |  Avg loser: {avg_loss:.1f}%
• Exit breakdown: {exit_reasons}
{calibration}
Documented biases to actively counter:
• Debate moderation bias: hold conviction when evidence supports it — do not
  drift to 5-6 just because the other side pushed back politely.
• RSI overbought reflex: RSI > 70 + ADX > 25 = momentum continuation, not exit.
• Momentum avoidance: underweighting continuation signals in bull regimes.
═══════════════════════════════════════════════════════════════════════════════
"""


def _mature_context(recent_trades: list[dict], regime: str, total_n: int) -> str:
    if not recent_trades:
        return _cold_start_context(regime)

    wins   = [t for t in recent_trades if t.get("pnl", 0) > 0]
    losses = [t for t in recent_trades if t.get("pnl", 0) <= 0]
    win_rate = len(wins) / len(recent_trades) * 100 if recent_trades else 0
    avg_win  = (sum(t.get("pnl_pct", 0) for t in wins)   / len(wins))   if wins   else 0
    avg_loss = (sum(t.get("pnl_pct", 0) for t in losses) / len(losses)) if losses else 0
    expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)

    exit_reasons: dict[str, int] = {}
    for t in recent_trades:
        r = t.get("exit_reason", "unknown")
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    calibration    = _check_conviction_calibration(recent_trades)
    overconfidence = _check_stop_clustering(recent_trades)

    return f"""
═══ PERFORMANCE CONTEXT ({total_n} closed trades, last {len(recent_trades)} shown) ═══
Market regime (SPY 1-month): {regime}

Performance summary:
• Win rate: {win_rate:.0f}%  |  Avg W: +{avg_win:.1f}%  |  Avg L: {avg_loss:.1f}%
• Expectancy per trade: {expectancy:+.2f}%
• Exit breakdown: {exit_reasons}
{calibration}{overconfidence}
CALIBRATION NOTE: Stats above come from a {regime} regime. Apply proportional
skepticism if the current regime differs from when these trades were taken.
═══════════════════════════════════════════════════════════════════════════════
"""


# ── Pattern detectors ─────────────────────────────────────────────────────────

def _check_conviction_calibration(trades: list[dict]) -> str:
    """Warn if high-conviction calls (8-10) are underperforming."""
    # C17(2): when v2 is on, also audit the conviction-7 bucket — where ~93% of buys
    # actually live. The legacy >=8 gate is structurally unreachable given the
    # conviction collapse, so the default bucket is never checked.
    focal_note = ""
    if getattr(config, "DEBATE_RUBRIC_V2", False):
        focal = [t for t in trades if t.get("conviction", 0) == 7]
        if len(focal) >= 3:
            focal_wins = sum(1 for t in focal if t.get("pnl", 0) > 0)
            focal_wr = focal_wins / len(focal) * 100
            if focal_wr < 50:
                focal_note = (
                    f"\n⚠ CALIBRATION ALERT: conviction-7 trades (your default bucket) "
                    f"winning only {focal_wr:.0f}% ({focal_wins}/{len(focal)}). "
                    f"A 7 should mean something — differentiate 6 vs 8 instead of defaulting.\n"
                )

    high_conv = [t for t in trades if t.get("conviction", 0) >= 8]
    if len(high_conv) < 3:
        return focal_note
    high_wins = sum(1 for t in high_conv if t.get("pnl", 0) > 0)
    high_wr   = high_wins / len(high_conv) * 100
    if high_wr < 40:
        return focal_note + (
            f"\n⚠ CALIBRATION ALERT: High-conviction trades (8-10) winning only "
            f"{high_wr:.0f}% ({high_wins}/{len(high_conv)}). "
            f"Apply extra scrutiny before assigning conviction ≥ 8.\n"
        )
    if high_wr > 75:
        return focal_note + (
            f"\n✓ High-conviction trades (8-10) performing well: "
            f"{high_wr:.0f}% win rate ({high_wins}/{len(high_conv)}).\n"
        )
    return focal_note


def _check_stop_clustering(trades: list[dict]) -> str:
    """Warn if too many recent trades are exiting via stop loss."""
    stops = [t for t in trades if t.get("exit_reason") == "stop_loss"]
    if not trades:
        return ""
    rate = len(stops) / len(trades) * 100
    if rate > 40:
        return (
            f"\n⚠ RISK ALERT: {rate:.0f}% of recent trades hit stop loss "
            f"({len(stops)}/{len(trades)}). Entries may be too early or in "
            f"low-quality setups. Require stronger technical confirmation.\n"
        )
    return ""
