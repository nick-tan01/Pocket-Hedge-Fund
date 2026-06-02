"""
scripts/derisk_positions.py
One-off MANUAL de-risk: trim positions by a fraction of their LIVE Alpaca share count.

Sized off the REAL broker quantity (not the journal), so it correctly halves the actual
holding even when the journal qty is stale. Dry-run by default; pass --confirm to submit.

Usage:
  python scripts/derisk_positions.py                          # DRY-RUN: MS+SMCI by 50%
  python scripts/derisk_positions.py --confirm                # EXECUTE (market hours only)
  python scripts/derisk_positions.py --symbols MS,SMCI --fraction 0.5 --confirm
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.alpaca_client import AlpacaClient
from core.journal import get_open_trades, log_trade_trim


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="MS,SMCI")
    ap.add_argument("--fraction", type=float, default=0.5,
                    help="fraction of current LIVE shares to SELL (0.5 = sell half)")
    ap.add_argument("--confirm", action="store_true",
                    help="actually submit the sell orders (default: dry-run preview only)")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    alpaca = AlpacaClient()
    journal = {t["symbol"]: t for t in get_open_trades()}

    if args.confirm and not alpaca.is_market_open():
        print("Market is CLOSED — sells would queue or fail. Re-run during market hours.")
        return

    try:
        pv = float(alpaca.get_account().get("portfolio_value", 0))
    except Exception:
        pv = 0.0

    mode = "EXECUTE" if args.confirm else "DRY-RUN (no orders submitted)"
    print(f"=== {mode} | trim {args.fraction*100:.0f}% of {symbols} | portfolio=${pv:,.0f} ===\n")

    for sym in symbols:
        pos = alpaca.get_position(sym)
        if not pos:
            print(f"  {sym}: not held at broker — skipping")
            continue
        qty   = float(pos["qty"])
        price = float(pos["current_price"])
        mv    = float(pos["market_value"])
        sell_qty = round(qty * args.fraction, 4)
        new_qty  = round(qty - sell_qty, 4)
        new_mv   = new_qty * price
        cur_pct  = (mv / pv * 100) if pv else 0
        new_pct  = (new_mv / pv * 100) if pv else 0
        print(f"  {sym}: hold {qty:.4f} sh  ${mv:,.0f} ({cur_pct:.1f}%) @ ${price:.2f}")
        print(f"       -> SELL {sell_qty:.4f}  |  keep {new_qty:.4f} sh  ~${new_mv:,.0f} ({new_pct:.1f}%)")
        if args.confirm:
            order = alpaca.submit_market_order(symbol=sym, qty=sell_qty, side="sell",
                                               reason=f"manual_derisk_{int(args.fraction*100)}pct")
            if order:
                t = journal.get(sym)
                if t:
                    log_trade_trim(t["id"], sell_qty, new_qty, price,
                                   f"manual_derisk_{int(args.fraction*100)}pct")
                    print(f"       ✅ SOLD {sell_qty:.4f} {sym}; journal trim recorded.")
                else:
                    print(f"       ✅ SOLD {sell_qty:.4f} {sym}; (no journal record found to update)")
            else:
                print(f"       ❌ ORDER FAILED for {sym}")
        print()

    if not args.confirm:
        print("Dry-run only. To execute, run during market hours:")
        print(f"  python scripts/derisk_positions.py --symbols {','.join(symbols)} "
              f"--fraction {args.fraction} --confirm")


if __name__ == "__main__":
    main()
