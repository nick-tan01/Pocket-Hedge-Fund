"""
core/pyramid.py — EXP-017 pyramiding decision (pure logic, no I/O besides the lazy 52w lookup).

ONE de-risked add to a confirmed winner. `evaluate_add` returns either a plan or a block
with the reason; the caller (main._maybe_pyramid_add) journals it, moves the stop, then
buys. ALL four EXP-007 guardrails live here so each is unit-testable:

  1. breakeven stop on the BLENDED basis — the plan carries `new_stop` >= blended basis;
     the caller must have the broker stop moved BEFORE the order is sent.
  2. position after the add <= PYRAMID_MAX_POSITION_PCT of NAV (and <= MAX_POSITION_PCT).
  3. no add within PYRAMID_52W_BLOCK_PCT of (or past) the 52-week high; unknown high blocks.
  4. every existing cap: MAX_PORTFOLIO_EXPOSURE, MAX_SECTOR_PCT, regime/VIX size scaling and
     the regime conviction floor.

Caps are shaved by SLIPPAGE_BUFFER so a fill a hair above the quote cannot breach one.
"""

import math

import config

SLIPPAGE_BUFFER = 0.005


def _block(reason: str, journal: bool = True, **facts) -> dict:
    return {"ok": False, "reason": reason, "journal": journal, **facts}


def evaluate_add(
    trade: dict,
    *,
    price: float,
    avg_entry: float,
    qty: float,
    market_value: float,
    portfolio_value: float,
    deployed_pct: float,
    sector_pct: float,
    conviction: int,
    required_conviction: int,
    thesis_status: str,
    trend: str,
    size_mult: float,
    high_52w_fn,
) -> dict:
    """`sector_pct` includes this position; `high_52w_fn` is only called once the cheaper
    gates have passed (it is a network read). journal=False blocks are the steady state
    (nothing to add to) and are not written to the journal."""
    if price <= 0 or avg_entry <= 0 or qty <= 0 or portfolio_value <= 0:
        return _block("bad_inputs", journal=False)
    if int(trade.get("pyramid_adds", 0) or 0) >= config.PYRAMID_MAX_ADDS:
        return _block("already_added", journal=False)
    gain = price / avg_entry - 1
    if gain < config.PYRAMID_TRIGGER_PCT:
        return _block("below_trigger", journal=False, gain_pct=round(gain * 100, 2))

    facts = {"gain_pct": round(gain * 100, 2), "price": price, "avg_entry": avg_entry}
    if thesis_status != "intact":
        return _block("thesis_not_intact", thesis_status=thesis_status, **facts)
    if trend != "up":
        return _block("trend_not_up", trend=trend, **facts)
    if conviction < required_conviction:
        return _block("conviction_below_required", conviction=conviction,
                      required=required_conviction, **facts)

    # Guardrail 3 — conservative 52-week-high extension test, fail closed.
    high = high_52w_fn()
    if not high or high <= 0:
        return _block("52w_high_unavailable", **facts)
    dist = price / high - 1                      # >= 0 means at/past the high
    facts.update(high_52w=high, pct_from_52w_high=round(dist * 100, 2),
                 block_within_pct=config.PYRAMID_52W_BLOCK_PCT * 100)
    if price >= high * (1 - config.PYRAMID_52W_BLOCK_PCT):
        return _block("extended_52w_high", **facts)

    # Guardrails 2 + 4 — the add is the smallest of the base size and every cap's headroom.
    cur_pct = market_value / portfolio_value
    rooms = {
        "position_8pct": min(config.PYRAMID_MAX_POSITION_PCT, config.MAX_POSITION_PCT) - cur_pct,
        "gross":         config.MAX_PORTFOLIO_EXPOSURE - deployed_pct,
        "sector":        config.MAX_SECTOR_PCT - sector_pct,
    }
    rooms = {k: v * (1 - SLIPPAGE_BUFFER) for k, v in rooms.items()}
    base = config.PYRAMID_ADD_PCT * size_mult
    binding = min(rooms, key=rooms.get)
    add_pct = min(base, rooms[binding])
    facts.update(position_pct_before=round(cur_pct, 4), base_add_pct=round(base, 4),
                 rooms={k: round(v, 4) for k, v in rooms.items()})
    if add_pct < config.PYRAMID_MIN_ADD_PCT:
        return _block(f"cap_binds:{binding}", **facts)

    add_qty = round(portfolio_value * add_pct / price, 4)
    if add_qty < 0.01:
        return _block("add_qty_too_small", **facts)

    # Guardrail 1 — breakeven on the blended basis (rounded UP to the cent).
    blended = (qty * avg_entry + add_qty * price) / (qty + add_qty)
    required_stop = math.ceil(blended * 100) / 100
    if required_stop > price * (1 - config.PYRAMID_MIN_STOP_GAP_PCT):
        return _block("no_room_for_breakeven_stop", blended_basis=round(blended, 4),
                      required_stop=required_stop, **facts)
    new_stop = max(float(trade.get("stop_price", 0) or 0), required_stop)

    return {
        "ok": True, "reason": "add", "journal": True,
        "add_pct": round(add_pct, 4), "add_usd": round(portfolio_value * add_pct, 2),
        "add_qty": add_qty, "binding_cap": binding if add_pct < base else "base_size",
        "blended_basis": round(blended, 4), "required_stop": required_stop,
        "new_stop": round(new_stop, 2),
        "position_pct_after": round(cur_pct + add_pct, 4),
        **facts,
    }
