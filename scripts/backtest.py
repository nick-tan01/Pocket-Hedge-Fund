"""
scripts/backtest.py
Offline replay harness for the DETERMINISTIC core — the missing calibration loop.

Why this exists: every strategy parameter in this project has been tuned LIVE, over weeks,
in a single regime, on ~30 closed trades. That is why the buy rate could swing 42% -> 1.6%
on one knob (EXP-010) without anyone being able to check it first. This harness replays the
real screener and the real stop math over years of history in seconds, so calibration
questions get answered BEFORE they touch the paper account.

What it answers (the standing gate, at n=hundreds instead of n=92):
    "Does the deterministic screener core, always deployed, beat SPY on its own?"
If yes, the core/satellite split is validated and the core should be always-on.
If no, the whole active approach — LLM or not — needs re-examining.

Faithfulness: it injects a point-in-time fetcher into the REAL `Screener` and uses the REAL
`risk_manager._calculate_atr`, so it tests production code, not a reimplementation that
would drift. The LLM layer is deliberately absent — this measures the core.

KNOWN DEVIATIONS (printed with every run; read them before trusting a number):
  - No LLM debate. This is the deterministic core only, by design.
  - get_news() returns [] -> the news_quality factor (weight 0.10) is neutral for all
    names, so composite scores differ slightly from live.
  - market_cap is treated as passing the floor. The curated 112-name WATCHLIST is all
    mega/large-cap, so MIN_MARKET_CAP is not a binding filter for this universe.
  - Universe is today's WATCHLIST applied to history => survivorship bias. Results are
    optimistic in absolute terms; use them for RELATIVE parameter comparison, not as a
    forecast of live returns.
  - Fills at the daily close, no slippage/commission (paper account has none either).

Usage:
  python scripts/backtest.py --start 2024-01-01 --end 2026-08-31
  python scripts/backtest.py --start 2024-01-01 --top-n 3 --size-pct 0.06 --max-positions 8
"""

import argparse
import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from agents import screener as screener_mod
from agents.screener import SECTOR_ETFS, Screener
from agents.risk_manager import _calculate_atr

INFO_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ".backtest_info_cache.json")

# Fields the screener reads off yfinance `info`. Cached once instead of re-fetched per
# symbol per rebalance (the live code calls yf.Ticker().info inside _get_info and
# _get_company_key — ~9,500 network calls in a 20-month replay, which is what made the
# first run exceed 10 minutes).
_INFO_KEYS = (
    "sector", "shortName", "longName", "marketCap", "trailingPE", "forwardPE",
    "earningsGrowth", "revenueGrowth", "returnOnEquity", "profitMargins",
    "earningsTimestamp", "earningsDate",
)


def load_info_map(symbols: list[str]) -> dict[str, dict]:
    """Snapshot fundamentals once, cached to disk. First run is slow; later runs instant.

    LOOK-AHEAD CAVEAT: these are TODAY's values applied to history, so the
    growth_quality factor (weight 0.20) and the earnings-window filter are not
    point-in-time. The momentum / technical / volume factors (0.65 combined) ARE clean.
    """
    cache: dict[str, dict] = {}
    if os.path.exists(INFO_CACHE):
        try:
            cache = json.load(open(INFO_CACHE))
        except (OSError, ValueError):
            cache = {}
    # Treat an EMPTY entry as missing so a rate-limited fetch self-heals on a later run
    # instead of poisoning the cache with permanently-blank fundamentals.
    missing = [s for s in symbols if not cache.get(s)]
    if missing:
        import yfinance as yf
        print(f"Snapshotting fundamentals for {len(missing)} symbols "
              f"(one-time, cached to {os.path.basename(INFO_CACHE)}) ...", flush=True)
        for n, s in enumerate(missing, 1):
            try:
                info = yf.Ticker(s).info or {}
                cache[s] = {k: info.get(k) for k in _INFO_KEYS
                            if isinstance(info.get(k), (str, int, float, type(None)))}
            except Exception:
                cache[s] = {}
            if n % 25 == 0:
                print(f"  {n}/{len(missing)}", flush=True)
        try:
            json.dump(cache, open(INFO_CACHE, "w"))
        except OSError:
            pass
    return cache


def patch_screener_for_replay(info_map: dict[str, dict]) -> None:
    """Route the screener's two direct-yfinance calls through the cached snapshot."""
    Screener._get_info = lambda self, symbol: info_map.get(symbol, {})
    # Dedupe by ticker in replay — the curated universe has no duplicate companies, and
    # the live version exists only to catch two tickers of the same issuer.
    screener_mod._get_company_key = lambda symbol: symbol


# ── Point-in-time data shim ───────────────────────────────────────────────────

class PointInTimeFetcher:
    """Serves only data at-or-before `as_of` so the replayed screener cannot look ahead.

    Implements exactly the three methods Screener touches: get_quote / get_ohlcv / get_news.
    """

    def __init__(self, bars: dict[str, list[dict]]):
        self.bars = bars
        # Pre-index dates per symbol so an as-of slice is a bisect, not a full scan.
        # (Scanning every bar on every call made a 20-month replay exceed 10 minutes.)
        self._dates = {s: [b["date"] for b in rows] for s, rows in bars.items()}
        self.as_of: date | None = None

    def _cut(self, symbol: str) -> int:
        """Index one past the last bar at-or-before as_of. O(log n)."""
        import bisect
        return bisect.bisect_right(self._dates.get(symbol, []), self.as_of)

    def get_ohlcv(self, symbol: str, days: int = 60) -> list[dict]:
        i = self._cut(symbol)
        return self.bars.get(symbol, [])[max(0, i - days):i]

    def get_quote(self, symbol: str) -> dict | None:
        i = self._cut(symbol)
        if i == 0:
            return None
        rows = self.bars[symbol]
        px = rows[i - 1]["close"]
        if px <= 0:
            return None
        window = rows[max(0, i - 63):i]          # ~3 months, matching live's avg volume
        vol = int(sum(b["volume"] for b in window) / max(1, len(window)))
        return {
            "symbol": symbol,
            "price": round(px, 2),
            "volume": vol,
            # Deviation (documented above): the curated universe is all large-cap, so the
            # $2B floor never binds. Pass a value above it rather than making 112 slow calls.
            "market_cap": float(config.MIN_MARKET_CAP * 10),
        }

    def get_news(self, symbol: str, days: int = 3) -> list[dict]:
        return []


# ── Data loading ──────────────────────────────────────────────────────────────

def load_bars(symbols: list[str], start: str, end: str) -> dict[str, list[dict]]:
    """Batch-load daily bars from ALPACA — the same source the fund trades on.

    Deliberately not yfinance: yf.download rate-limits hard (it is what produced the
    2026-05-25 "20 false no-candidate runs" incident), and backtesting on a different
    feed than production trades on invites silent divergence. Alpaca's StockBarsRequest
    accepts a symbol LIST, so this is one request for the whole universe.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from core.alpaca_client import AlpacaClient

    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    # The free plan rejects "recent SIP data"; keep the window a few days in the past.
    cutoff = datetime.combine(date.today(), datetime.min.time()) - __import__("datetime").timedelta(days=2)
    if end_dt > cutoff:
        end_dt = cutoff
        print(f"  (end clamped to {end_dt.date()} — free plan blocks recent SIP data)", flush=True)

    print(f"Loading {len(symbols)} symbols from Alpaca {start} -> {end_dt.date()} ...", flush=True)
    ac = AlpacaClient()
    req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
                           start=start_dt, end=end_dt)
    resp = ac.data.get_stock_bars(req)

    out: dict[str, list[dict]] = {}
    for sym, rows in (resp.data or {}).items():
        parsed = []
        for b in rows:
            try:
                parsed.append({
                    "date": b.timestamp.date(),
                    "open": float(b.open), "high": float(b.high),
                    "low": float(b.low), "close": float(b.close),
                    "volume": float(b.volume or 0),
                })
            except (TypeError, ValueError):
                continue
        if parsed:
            out[sym] = parsed
    print(f"  loaded {len(out)}/{len(symbols)} symbols", flush=True)
    return out


# ── Simulation ────────────────────────────────────────────────────────────────

def run_backtest(bars, start_d, end_d, top_n, size_pct, max_positions, rebalance_days):
    fetcher = PointInTimeFetcher(bars)
    screener = Screener(fetcher)

    all_dates = sorted({b["date"] for s in bars for b in bars[s]})
    dates = [d for d in all_dates if start_d <= d <= end_d]
    if not dates:
        raise SystemExit("No trading days in range — check --start/--end.")

    cash = config.STARTING_CAPITAL
    positions: dict[str, dict] = {}          # sym -> {qty, entry, stop, peak}
    equity_curve: list[tuple[date, float]] = []
    trades: list[dict] = []
    deployments: list[float] = []
    last_rebalance: date | None = None

    # O(1) same-day bar lookup — the linear scans here dominated runtime.
    by_day = {s: {b["date"]: b for b in rows} for s, rows in bars.items()}

    def price_on(sym, d):
        b = by_day.get(sym, {}).get(d)
        if b:
            return b["close"]
        i = fetcher._cut(sym)                    # as_of == d inside the loop
        rows = bars.get(sym, [])
        return rows[i - 1]["close"] if i else None

    for d in dates:
        fetcher.as_of = d

        # 1) EXITS first — stops are checked against the day's low, like a resting order.
        for sym in list(positions):
            todays = by_day.get(sym, {}).get(d)
            if not todays:
                continue
            p = positions[sym]
            p["peak"] = max(p["peak"], todays["high"])
            # Trailing stop arms after the configured gain, then trails below the peak.
            if p["peak"] >= p["entry"] * (1 + config.TRAILING_STOP_TRIGGER):
                p["stop"] = max(p["stop"], p["peak"] * (1 - config.TRAILING_STOP_PCT))
            if todays["low"] <= p["stop"]:
                exit_px = p["stop"]                       # assume the stop fills at its price
                pnl = (exit_px - p["entry"]) * p["qty"]
                cash += exit_px * p["qty"]
                trades.append({"symbol": sym, "entry": p["entry"], "exit": exit_px,
                               "pnl": pnl,
                               "pnl_pct": (exit_px - p["entry"]) / p["entry"] * 100,
                               "exit_date": d})
                del positions[sym]

        # 2) ENTRIES on the rebalance cadence.
        due = last_rebalance is None or (d - last_rebalance).days >= rebalance_days
        if due:
            last_rebalance = d
            equity_now = cash + sum(
                (price_on(s, d) or p["entry"]) * p["qty"] for s, p in positions.items()
            )
            try:
                candidates = screener.run(max_candidates=top_n * 4)
            except Exception:
                candidates = []
            for c in candidates:
                if len(positions) >= max_positions:
                    break
                if c.symbol in positions:
                    continue
                px = price_on(c.symbol, d)
                if not px or px <= 0:
                    continue
                hist = fetcher.get_ohlcv(c.symbol, days=60)
                atr = _calculate_atr(hist, config.ATR_PERIOD)
                atr_stop = px - (config.ATR_MULTIPLIER * atr) if atr else 0.0
                stop = max(atr_stop, px * (1 - config.HARD_STOP_PCT))
                alloc = equity_now * size_pct
                if alloc > cash:
                    continue
                qty = alloc / px
                cash -= alloc
                positions[c.symbol] = {"qty": qty, "entry": px, "stop": stop, "peak": px}

        # 3) Mark to market.
        equity = cash + sum((price_on(s, d) or p["entry"]) * p["qty"]
                            for s, p in positions.items())
        equity_curve.append((d, equity))
        deployments.append((equity - cash) / equity if equity > 0 else 0.0)

    return equity_curve, trades, deployments


def report(equity_curve, trades, deployments, bars, start_d, end_d):
    start_eq, end_eq = config.STARTING_CAPITAL, equity_curve[-1][1]
    total_ret = (end_eq - start_eq) / start_eq * 100

    spy = [b for b in bars.get("SPY", []) if start_d <= b["date"] <= end_d]
    spy_ret = ((spy[-1]["close"] - spy[0]["close"]) / spy[0]["close"] * 100) if len(spy) > 1 else float("nan")

    peak, max_dd = -1e18, 0.0
    for _, v in equity_curve:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak * 100)

    wins = [t for t in trades if t["pnl"] > 0]
    avg_dep = sum(deployments) / len(deployments) * 100 if deployments else 0.0

    print("\n" + "=" * 64)
    print(f"  DETERMINISTIC CORE BACKTEST — {start_d} -> {end_d}")
    print("=" * 64)
    print(f"  Final equity      : ${end_eq:,.2f}")
    print(f"  Total return      : {total_ret:+.2f}%")
    print(f"  SPY (same window) : {spy_ret:+.2f}%")
    print(f"  EXCESS vs SPY     : {total_ret - spy_ret:+.2f}pp   <-- the standing gate")
    print(f"  Max drawdown      : {max_dd:.2f}%")
    print(f"  Avg deployment    : {avg_dep:.1f}%")
    print(f"  Closed trades     : {len(trades)}  |  win rate: "
          f"{(len(wins)/len(trades)*100 if trades else 0):.0f}%")
    if trades:
        losses = [t for t in trades if t["pnl"] <= 0]
        aw = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
        al = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
        print(f"  Avg win / loss    : {aw:+.1f}% / {al:+.1f}%")
    print("-" * 64)
    print("  DEVIATIONS: no LLM; news factor neutral; survivorship-biased universe;")
    print("  close fills, no slippage. Use for RELATIVE comparison, not as a forecast.")
    print("=" * 64 + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--top-n", type=int, default=3,
                    help="candidates considered per rebalance (baseline arm uses 3)")
    ap.add_argument("--size-pct", type=float, default=0.06,
                    help="fixed fraction of equity per position (baseline arm uses 0.06)")
    ap.add_argument("--max-positions", type=int, default=config.MAX_POSITIONS)
    ap.add_argument("--rebalance-days", type=int, default=7)
    args = ap.parse_args()

    start_d = datetime.fromisoformat(args.start).date()
    end_d = datetime.fromisoformat(args.end).date()

    symbols = sorted(set(Screener.WATCHLIST) | set(SECTOR_ETFS.values()) | {"SPY"})
    bars = load_bars(symbols, args.start, args.end)
    if "SPY" not in bars:
        print("WARNING: no SPY bars — the benchmark comparison will be NaN.", flush=True)

    # Remove the screener's per-symbol network calls before replaying it.
    patch_screener_for_replay(load_info_map(list(Screener.WATCHLIST)))
    print(f"Replaying {args.start} -> {args.end} "
          f"(rebalance every {args.rebalance_days}d) ...", flush=True)

    curve, trades, deps = run_backtest(
        bars, start_d, end_d, args.top_n, args.size_pct,
        args.max_positions, args.rebalance_days,
    )
    report(curve, trades, deps, bars, start_d, end_d)


if __name__ == "__main__":
    main()
