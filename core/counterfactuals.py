"""
core/counterfactuals.py
Decision-quality measurement engine — the "did our decisions actually pan out?" layer.

This is MEASUREMENT ONLY. It is deterministic (no LLM), read-only (never writes to the
journal or changes any trading behavior), and is NOT wired into the live pipeline. It is
called by scripts/decision_quality.py on demand to give the human reviewer real signal
about decision quality — derived from the fast-accumulating process data (skips, trims,
debates) rather than only from the scarce closed-trade P&L.

Core idea: for each decision the system made, look at what subsequently happened to that
stock (forward return vs SPY) to judge whether the decision was good — WITHOUT needing
the trade to have closed. This manufactures learnable signal at low closed-trade counts.

Benchmark logic (the fund's goal is to BEAT SPY, so everything is SPY-relative):
  - A SKIP is "good" if the skipped name subsequently UNDERPERFORMED SPY (we avoided
    relative drag); "missed" if it beat SPY (we left alpha on the table).
  - A TRIM is "premature/bad" if the name subsequently BEAT SPY (we cut a winner);
    "good" if it underperformed (we de-risked correctly).
  - Conviction calibration: bucket realized closed-trade P&L by conviction — higher
    conviction should earn higher returns; if not, the scale isn't discriminating.
"""

import logging
from datetime import datetime, date

logger = logging.getLogger(__name__)

# Forward windows (in trading days) over which to measure what happened next.
FORWARD_WINDOWS = (5, 10, 20)


def _parse_date(ts: str) -> date | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
    except Exception:
        try:
            return date.fromisoformat(str(ts)[:10])
        except Exception:
            return None


def _bars_by_date(bars: list[dict]) -> list[tuple[date, float]]:
    """Convert OHLCV bars to a sorted list of (date, close), oldest first."""
    out = []
    for b in bars:
        d = _parse_date(b.get("date", ""))
        if d is not None:
            try:
                out.append((d, float(b["close"])))
            except (KeyError, TypeError, ValueError):
                continue
    out.sort(key=lambda x: x[0])
    return out


def _forward_returns(series: list[tuple[date, float]], decision_dt: date) -> dict:
    """
    Given a (date, close) series and a decision date, compute forward returns over
    each FORWARD_WINDOWS horizon. Returns {window: pct or None if not enough data}.
    The 'entry' close is the first bar on or after the decision date.
    """
    if not series:
        return {w: None for w in FORWARD_WINDOWS}

    # find the index of the first bar on/after the decision date
    start_idx = None
    for i, (d, _) in enumerate(series):
        if d >= decision_dt:
            start_idx = i
            break
    if start_idx is None:
        return {w: None for w in FORWARD_WINDOWS}

    base_close = series[start_idx][1]
    if base_close <= 0:
        return {w: None for w in FORWARD_WINDOWS}

    out = {}
    for w in FORWARD_WINDOWS:
        fwd_idx = start_idx + w
        if fwd_idx < len(series):
            out[w] = round((series[fwd_idx][1] - base_close) / base_close * 100, 2)
        else:
            out[w] = None  # not enough forward data yet (decision too recent)
    return out


def _excess(stock_fwd: dict, spy_fwd: dict) -> dict:
    """Stock forward return minus SPY forward return, per window."""
    out = {}
    for w in FORWARD_WINDOWS:
        s, m = stock_fwd.get(w), spy_fwd.get(w)
        out[w] = round(s - m, 2) if (s is not None and m is not None) else None
    return out


def analyze_decisions(data: dict, fetcher, lookback_days: int = 45,
                      primary_window: int = 10) -> dict:
    """
    Build the decision-quality scorecard from the journal + forward price action.

    Args:
        data:          parsed dashboard/data.json
        fetcher:       DataFetcher instance (for forward OHLCV)
        lookback_days: only analyze decisions newer than this many days
        primary_window: which FORWARD_WINDOW to headline in the precision stats
    """
    today = date.today()
    cutoff = today.toordinal() - lookback_days

    # Gather the unique symbols we'll need price history for (+ SPY benchmark).
    skips = [r for r in data.get("risk_decisions", [])
             if r.get("action") == "skip"
             and (_parse_date(r.get("ts", "")) or date.min).toordinal() >= cutoff]
    trims = [r for r in data.get("position_reviews", [])
             if r.get("action") == "trim"
             and (_parse_date(r.get("ts", "")) or date.min).toordinal() >= cutoff]
    closed = data.get("trades", [])

    symbols = {r.get("symbol") for r in (skips + trims) if r.get("symbol")}
    symbols.discard(None)

    # Fetch price series once per symbol (+ SPY). lookback + max forward window margin.
    fetch_days = lookback_days + max(FORWARD_WINDOWS) + 15
    price_cache: dict[str, list] = {}
    for sym in list(symbols) + ["SPY"]:
        try:
            price_cache[sym] = _bars_by_date(fetcher.get_ohlcv(sym, days=fetch_days))
        except Exception as e:
            logger.debug("counterfactuals: price fetch failed %s: %s", sym, e)
            price_cache[sym] = []
    spy_series = price_cache.get("SPY", [])

    # ── SKIP quality ──────────────────────────────────────────────────────────
    skip_rows, skip_good, skip_missed, skip_pending = [], 0, 0, 0
    for r in skips:
        sym = r.get("symbol")
        dt  = _parse_date(r.get("ts", ""))
        if not sym or not dt:
            continue
        fwd     = _forward_returns(price_cache.get(sym, []), dt)
        spy_fwd = _forward_returns(spy_series, dt)
        exc     = _excess(fwd, spy_fwd)
        pw      = exc.get(primary_window)
        verdict = "pending"
        if pw is not None:
            # good skip = avoided a name that underperformed SPY
            verdict = "good" if pw < 0 else "missed"
            if verdict == "good": skip_good += 1
            else: skip_missed += 1
        else:
            skip_pending += 1
        skip_rows.append({"symbol": sym, "date": dt.isoformat(),
                          "conviction": r.get("conviction"),
                          "reason": (r.get("reason", "") or "")[:80],
                          "fwd_excess_vs_spy": exc, "verdict": verdict})

    skip_decided = skip_good + skip_missed
    skip_precision = round(skip_good / skip_decided * 100, 1) if skip_decided else None

    # ── TRIM quality ────────────────────────────────────────────────────────────
    trim_rows, trim_good, trim_premature, trim_pending = [], 0, 0, 0
    for r in trims:
        sym = r.get("symbol")
        dt  = _parse_date(r.get("ts", ""))
        if not sym or not dt:
            continue
        fwd     = _forward_returns(price_cache.get(sym, []), dt)
        spy_fwd = _forward_returns(spy_series, dt)
        exc     = _excess(fwd, spy_fwd)
        pw      = exc.get(primary_window)
        verdict = "pending"
        if pw is not None:
            # premature trim = we cut a name that then BEAT SPY
            verdict = "premature" if pw > 0 else "good"
            if verdict == "good": trim_good += 1
            else: trim_premature += 1
        else:
            trim_pending += 1
        trim_rows.append({"symbol": sym, "date": dt.isoformat(),
                          "conviction": r.get("conviction"),
                          "thesis": r.get("thesis_status"),
                          "fwd_excess_vs_spy": exc, "verdict": verdict})

    trim_decided = trim_good + trim_premature
    trim_precision = round(trim_good / trim_decided * 100, 1) if trim_decided else None

    # ── Conviction calibration (closed trades) ──────────────────────────────────
    conv_buckets: dict[int, list] = {}
    for t in closed:
        c = t.get("conviction")
        pnl = t.get("pnl_pct")
        if c is not None and pnl is not None:
            conv_buckets.setdefault(int(c), []).append(float(pnl))
    calibration = {
        c: {"n": len(v), "avg_pnl_pct": round(sum(v) / len(v), 2),
            "win_rate": round(sum(1 for x in v if x > 0) / len(v) * 100, 0)}
        for c, v in sorted(conv_buckets.items())
    }

    return {
        "as_of": today.isoformat(),
        "lookback_days": lookback_days,
        "primary_window_days": primary_window,
        "skip_quality": {
            "n_decided": skip_decided, "n_pending": skip_pending,
            "good": skip_good, "missed": skip_missed,
            "precision_pct": skip_precision, "rows": skip_rows,
        },
        "trim_quality": {
            "n_decided": trim_decided, "n_pending": trim_pending,
            "good": trim_good, "premature": trim_premature,
            "precision_pct": trim_precision, "rows": trim_rows,
        },
        "conviction_calibration": calibration,
        "closed_trades": len(closed),
    }
