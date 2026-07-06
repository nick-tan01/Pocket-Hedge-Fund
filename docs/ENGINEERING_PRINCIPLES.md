# Engineering Principles — Pocket Hedge Fund

Written after the 2026-07-06 full audit. Between 2026-05-29 and 2026-07-06 the
pipeline failed 19 times in CI. Every single failure was the same story: **an
external dependency misbehaved and the code crashed instead of retrying or
degrading** — then got a point-fix at the crash site. The push-race class alone
took five fix attempts over six weeks before the structural fix landed. These
rules exist so that never happens again. Read this before fixing anything.

## 1. Fix the class, not the crash site

When a fix patches a crash, **sweep the whole file/pattern for the same bug
before committing**. The canonical failure: commit `726a7fc` (2026-07-06) added
None-tolerance to `get_account()` — with tests — while `get_positions()`, forty
lines below, kept the identical eager-cast pattern with a wider blast radius.
The audit found it the same day. A fix that isn't generalized is a scheduled
recurrence.

Checklist for any crash fix:
- [ ] `grep` for the same pattern across the file and its siblings
- [ ] Fix all instances, not the one in the traceback
- [ ] Name the incident in a comment where future readers will look

## 2. Every external boundary gets the full treatment

The system has five external boundaries. Each must have: **(a)** timeouts,
**(b)** retries with backoff on READS, **(c)** None/schema tolerance,
**(d)** graceful degradation instead of a hard crash, **(e)** a failure-path
test. The utilities already exist — use them, don't reinvent them:

| Boundary | Use | Never |
|---|---|---|
| Alpaca REST | `_retry_read`, `_f`/`_i`, `_account_dict`/`_position_dict` in `core/alpaca_client.py` | retry order submission/cancel/replace (double-submit risk) |
| Yahoo/yfinance | `_with_retry` in `core/data_fetcher.py`; return `None`/`[]` on failure | return a "valid-looking" zero (price=0.0 passed guards and reached division) |
| Anthropic API | `get_client()` + `complete_json()` in `core/llm_json.py` (call inside the retry try) | construct clients at module import time |
| git publication | CI workflow push steps only (single writer, `pipeline-data-json` group, `data` branch) | push from library code or local runs |
| journal file | `journal._save` (atomic tmp+replace), `JournalCorrupt` halt, `_ARRAY_CAPS` | uncapped `append`; auto-resetting a corrupt journal |

Every numeric field read off a broker/API payload goes through `_f`/`_i`.
`float(x)` directly on an API field is a bug even if it has never crashed.

## 3. No fix without a failing test first

None of the four historical failure classes had a test before it fired in
production. Reproduce the failure in `tests/` (a `SimpleNamespace` payload with
None fields, a function that raises `ConnectTimeout` twice, a corrupt journal
file), watch it fail, then fix. The test names the incident date.

## 4. Loud failure beats silent degradation

The 2026-06-15 model EOL produced three days of conviction-1 "skip — error"
decisions before a human noticed. Silence is the enemy:
- Degraded LLM → neutral fallback is fine, but it must be **counted**
  (`llm_failures` in the run record) and **preflighted** (`_llm_preflight`
  fails the job red before 30 debates burn silently).
- A crashed run must still commit its journal (trade.yml salvage push runs on
  failure) — losing decision history silently biased the scorecard.
- Never `|| true`, never `-X theirs`, never swallow a push rejection as a
  warning. Red jobs are a feature.

## 5. One writer for shared state

`dashboard/data.json` is the database. Its rules:
- Canonical copy lives on the **`data` branch**; main is pure code.
- Only CI workflow push steps write it upstream, serialized by the
  `pipeline-data-json` concurrency group. `journal.push_to_github()` is a
  deliberate no-op everywhere.
- GitHub keeps at most ONE pending run per concurrency group — a queued run can
  be evicted. That's why market_tick's guard job yields to trade runs. Don't
  add a new writer workflow without the guard + group.
- Local `python3 main.py` runs do not publish. If a run must be recorded,
  trigger the workflow (`gh workflow run`).

## 6. Growth must be bounded

Any new journal array gets an entry in `journal._ARRAY_CAPS` (or uses
`_append_capped`) the day it's born. The closed-trades ledger is the only
uncapped array, deliberately. Unbounded observability data took data.json to
5.9 MB rewritten wholesale 10-14×/day.

## 7. Know which kind of change you're making

Three categories, three bars:
- **Reliability fix** (`reliability (freeze-exempt):`) — crash/retry/data-loss.
  Ship promptly, with tests, sweep the class (Rule 1).
- **Process cost** (`efficiency (process-cost, freeze-exempt):`) — run
  frequency, call counts, storage. Must not change entry/exit/sizing rules.
  Config-flagged and reversible.
- **Strategy change** — anything touching what gets bought/sold/sized. Gated by
  the experiment registry (`docs/EXPERIMENTS.md`) and the freeze. Needs its
  measurement plan written BEFORE deployment.

## 8. Three strikes → question the architecture

If the same failure class needs a third fix, stop patching. The push-race class
(5 attempts: pull-rebase → revert → two workflow patches → concurrency group +
single writer) proved the pattern: repeated fixes at a boundary mean the
boundary itself is wrong. Write down what invariant keeps breaking and change
the design so violating it is impossible, not discouraged.

## 9. The scorecard gates everything

The weekly decision-quality scorecard is the only feedback loop that has ever
changed a deployment decision (EXP-007 held on the 6/30 baseline-vs-pipeline
reading). Protect its inputs: journal records must survive crashes (Rule 4),
reconciled positions carry `conviction=6, debate_id=""` and are visibly
second-class. If the baseline still beats the LLM funnel at n≥30 closed trades,
believe the data.
