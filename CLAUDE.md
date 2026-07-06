# Pocket Hedge Fund — Project Context

Paper-trading pipeline: deterministic screener → LLM analysts/debate → PM →
deterministic risk manager → Alpaca paper orders. Runs entirely on GitHub
Actions; the Vercel dashboard is a static page reading the journal.

**Before fixing ANY bug or adding retries/error handling, read
`docs/ENGINEERING_PRINCIPLES.md`.** It encodes the 2026-07-06 audit's rules —
most importantly: fix the failure CLASS (sweep the file for the same pattern),
add the failing test first, and never let an external payload crash a run.

## Layout & state
- Code lives on `main`. The journal (`dashboard/data.json`) is canonical on the
  **`data` branch** — CI workflows are the only writers (serialized by the
  `pipeline-data-json` concurrency group). Local `data.json` is a stale copy.
- `python3 main.py --test` = dry run (safe). A local `--now` run trades against
  the paper account but does NOT publish its journal — use
  `gh workflow run "Pocket Hedge Fund — Trading Pipeline"` instead.
- Workflows: `trade.yml` (scheduled + sentinel-dispatched pipeline),
  `market_tick.yml` (hourly sentinel + snapshot; guard job yields to trade
  runs), `after_close_watchlist.yml`, `decision_quality.yml` (Friday scorecard
  — the one feedback loop that gates deployment decisions), `tests.yml`.

## Verify before declaring done
- `python3 -m pytest tests/ -q` must pass.
- Workflow edits: `python3 -c "import yaml; yaml.safe_load(open(...))"`.
- New journal arrays need a cap in `core/journal.py::_ARRAY_CAPS`.

## Conventions
- Commit prefixes: `reliability (freeze-exempt):` for crash/data-loss fixes,
  `efficiency (process-cost, freeze-exempt):` for cost/frequency changes,
  plain description for strategy changes (which are gated by
  `docs/EXPERIMENTS.md` and the current experiment freeze — until 2026-08-05
  or 30 closed trades).
- Config flags over code deletion for behavior changes (`DEBATE_ROUNDS`,
  `REVIEW_LITE`, `DISCOVERY_ENABLED`, `SENTINEL_ENABLED_TRIGGERS` env) — keep
  changes reversible and measurable.
- Model IDs in `config.py` are pinned dated strings; `_llm_preflight` in
  main.py catches an EOL at run start (red job, `llm_preflight_failed` run
  record) — check the deprecations page before migrating.
