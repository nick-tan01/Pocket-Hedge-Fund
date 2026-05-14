#!/usr/bin/env python3
"""Build and store the after-close watchlist."""

import argparse
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agents.after_close_watchlist import build_after_close_watchlist
from core.data_fetcher import DataFetcher
from core.journal import log_watchlist, push_to_github


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print without writing dashboard/data.json")
    parser.add_argument("--push", action="store_true", help="Commit and push dashboard/data.json after writing")
    parser.add_argument("--symbols", default="", help="Optional comma-separated universe override")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or None
    record = build_after_close_watchlist(DataFetcher(), symbols=symbols)

    logger.info(
        "After-close watchlist: entries=%d long=%d weakness=%d alerts=%d expires=%s",
        record["entry_count"],
        record["long_candidate_count"],
        record["weakness_count"],
        record["position_alert_count"],
        record["expires_at"],
    )
    for entry in record["entries"][:10]:
        logger.info(
            "%-5s %-24s tier=%s score=%.3f %s",
            entry["symbol"],
            entry["setup_type"],
            entry["tier"],
            entry["score"],
            entry["reason"],
        )

    if args.dry_run:
        return

    watchlist_id = log_watchlist(record)
    logger.info("Stored %s", watchlist_id)

    if args.push:
        push_to_github("Auto: after-close watchlist")


if __name__ == "__main__":
    main()
