"""
scripts/check_llm_health.py
Audit recommendation #1: alert on LLM outages.

The 2026-06-15 model-EOL outage took the whole debate stack offline for 3 days; the
llm_failures run field (audit §7.4) RECORDED it perfectly, but nothing alerted, so it
was only caught by a manual review on 6/17. This script reads the most recent pipeline
run and exits non-zero when it shows an outage pattern — wired as the LAST step of the
trade workflow (after the journal push), so a degraded run turns the GitHub Actions job
RED and emails the owner while the data still commits.

Outage signal (either):
  - any pm_failures in the run (the PM erroring is never normal), OR
  - analyst_fallbacks >= ANALYST_FALLBACK_ALERT (a data/model outage, not one bad name)

Exit 0 = healthy or no recent run; exit 2 = outage detected (distinct from a crash=1).

Usage:
  python scripts/check_llm_health.py            # check latest run
  python scripts/check_llm_health.py --quiet    # only print on problems
"""

import argparse
import json
import sys

DATA_PATH = "dashboard/data.json"
ANALYST_FALLBACK_ALERT = 3   # >=3 analyst fallbacks in one run = systemic, not one name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        with open(DATA_PATH) as f:
            data = json.load(f)
    except Exception as e:
        print(f"check_llm_health: could not read {DATA_PATH}: {e}")
        return 0  # don't fail CI on a read hiccup

    runs = data.get("runs", [])
    if not runs:
        if not args.quiet:
            print("check_llm_health: no runs yet — OK")
        return 0

    last = runs[-1]
    lf = last.get("llm_failures") or {}
    pm = int(lf.get("pm_failures", 0) or 0)
    af = int(lf.get("analyst_fallbacks", 0) or 0)
    ts = last.get("ts", "")[:19]

    if pm > 0 or af >= ANALYST_FALLBACK_ALERT:
        print("🔴 LLM OUTAGE DETECTED in latest run "
              f"({ts}): pm_failures={pm}, analyst_fallbacks={af}.")
        print("   Likely causes: model end-of-life (check config.ANALYST_MODEL/DEBATE_MODEL "
              "against https://docs.anthropic.com/en/docs/resources/model-deprecations), "
              "API key/billing, or a data outage. The fund cannot make real decisions "
              "until this is resolved.")
        return 2

    if not args.quiet:
        print(f"check_llm_health: latest run {ts} healthy "
              f"(pm_failures={pm}, analyst_fallbacks={af}) — OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
