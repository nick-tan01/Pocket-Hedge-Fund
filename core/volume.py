"""
core/volume.py — relative volume that is correct intraday (WS-D, 2026-10-05).

THE BUG (review §2.1F): `volumes[-1] / avg(volumes[-21:-1])` was computed straight off
`get_ohlcv`, whose last bar is TODAY's in-progress daily bar. Intraday that ratio is
understated by the share of the session still to come: ~0.3x at 11:20 ET on a perfectly
normal day. It fed the technical analyst's prompt ("Volume today vs 20d avg"), the screener's
volume-spike factor, the conviction rubric's "volume >= average" test, and the position
reviewer — 8 of 13 LLM thesis exits cited a sub-0.5x "volume tripwire" (0.12-0.34x).

THE FIX: normalise today's partial bar by the expected cumulative share of the session's
volume already traded. Chosen over "use the last completed bar" because it keeps the
"today vs average" meaning at every call site (a spike still shows up the same day) and is
byte-identical to the old formula once the bar is complete (after the close, or any bar not
dated today) — so the after-close watchlist and post-close runs are unchanged.

LIMITS (documented, not hidden): the intraday curve below is a typical U-shaped US-equity
profile (heavy open/close), NOT fitted to this fund's data; early-session readings are
noisy so the fraction is floored at MIN_FRACTION. Early-close (13:00 ET) days are treated as
a normal session, so a post-1pm reading on those ~3 days/year runs high. Every caller of
`volumes[-1]` against an average MUST go through relative_volume() — Principle 1.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

_OPEN_MIN = 9 * 60 + 30
_CLOSE_MIN = 16 * 60
MIN_FRACTION = 0.10

# (minutes since 09:30 ET, cumulative fraction of the day's volume traded)
_PROFILE = (
    (0, 0.00), (15, 0.08), (30, 0.14), (60, 0.24), (90, 0.32), (120, 0.39),
    (150, 0.45), (210, 0.55), (270, 0.66), (330, 0.80), (360, 0.89), (390, 1.00),
)


def _now_et() -> datetime:
    return datetime.now(ET)


def session_fraction(now: datetime) -> float:
    """Expected share of today's total volume traded by `now` (ET-aware). 1.0 when the
    session is not in progress (pre-open, after the close)."""
    now = now.astimezone(ET)
    minute = now.hour * 60 + now.minute
    if minute < _OPEN_MIN or minute >= _CLOSE_MIN:
        return 1.0
    t = minute - _OPEN_MIN
    for (t0, f0), (t1, f1) in zip(_PROFILE, _PROFILE[1:]):
        if t0 <= t <= t1:
            return max(MIN_FRACTION, f0 + (f1 - f0) * (t - t0) / (t1 - t0))
    return 1.0


def relative_volume(bars: list[dict], now: datetime | None = None) -> float | None:
    """Last bar's volume vs the prior-20-bar average, with an in-progress last bar
    normalised by session_fraction. None if there is not enough history (callers keep
    their own historical fallback: technical -> 1.0, screener -> 0)."""
    if not bars or len(bars) < 21:
        return None
    now = (now or _now_et()).astimezone(ET)
    try:
        volumes = [float(b["volume"]) for b in bars]
    except (KeyError, TypeError, ValueError):
        return None
    avg = sum(volumes[-21:-1]) / 20
    if avg <= 0:
        return None
    frac = 1.0
    if str(bars[-1].get("date", ""))[:10] == now.date().isoformat():
        frac = session_fraction(now)
    return volumes[-1] / (avg * frac)
