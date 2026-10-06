#!/usr/bin/env bash
# Bounded pull-rebase-retry push of the data branch (WS-B, 2026-10-05).
#
# Run from inside the data-branch checkout with the journal commit already made.
# Used by BOTH trade.yml and market_tick.yml: the tick no longer shares the
# `pipeline-data-json` concurrency group, so a tick and a trade run can race here.
#
# Rules (docs/ENGINEERING_PRINCIPLES.md §4/§5):
#   - on rejection: `git pull --rebase`, retry — at most PUSH_MAX_ATTEMPTS (default 3)
#   - NO `-X theirs`, NO swallowing a rejected push: a rebase conflict aborts cleanly
#     (never leaves a half-applied rebase) and, once attempts are exhausted, the job
#     goes RED. The trade run's journal is never clobbered by the tick's.
set -u

BRANCH="${DATA_BRANCH:-data}"
MAX="${PUSH_MAX_ATTEMPTS:-3}"
SLEEP="${PUSH_RETRY_SLEEP:-5}"

attempt=1
while [ "$attempt" -le "$MAX" ]; do
  if git push origin "HEAD:${BRANCH}"; then
    exit 0
  fi
  echo "::warning::data-branch push rejected (attempt ${attempt}/${MAX})"
  if [ "$attempt" -ge "$MAX" ]; then
    break
  fi
  if ! git pull --rebase origin "${BRANCH}"; then
    echo "::warning::rebase onto origin/${BRANCH} conflicted (attempt ${attempt}/${MAX}) — aborting rebase"
    git rebase --abort >/dev/null 2>&1 || echo "(no rebase in progress)"
  fi
  attempt=$((attempt + 1))
  sleep "$((SLEEP * (attempt - 1)))"
done

echo "::error::data-branch push failed after ${MAX} attempts — journal NOT published"
exit 1
