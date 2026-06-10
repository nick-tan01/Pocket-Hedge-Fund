# Experiment Registry

**Rule (S3, from `docs/FABLE_STRATEGY_AUDIT.md`): no strategy parameter, prompt, or
rule change merges without an entry here.** One change per experiment. Each entry
pre-states its hypothesis, metric, minimum sample, and success/failure thresholds
*before* results exist. Multi-change commits like `0f2b260` (five simultaneous
changes) are what this registry exists to prevent — whatever happens next can never
be attributed to any one of them.

**Parameter freeze:** outside of registered experiments, `config.py` strategy
parameters and agent prompts are frozen until **2026-08-05** (8 weeks from registry
creation) or 30 closed trades, whichever comes first. Bug fixes that don't change
strategy behavior are exempt but must say so in the commit message.

Status values: `proposed` → `running` → `accepted` / `rejected` / `inconclusive`.

---

## EXP-001 — Broker-native protective stops (S1)
- **Status:** running (started 2026-06-10)
- **Change:** `BROKER_NATIVE_STOPS=True` — every fill gets a GTC stop order resting
  at Alpaca; trailing ratchet replaces it; software stop becomes fallback only.
- **Hypothesis:** realized losses will stop exceeding the configured stop distance,
  because stops are no longer sampled only at run times (ARM −12.56% and SMCI
  −11.79% both breached the 8% hard cap under software-only stops).
- **Metric / success:** over the next 20 closed trades, no loss exceeds its stop
  distance by more than 1% slippage.
- **Failure:** broker stops trigger on intraday noise materially more often than the
  sampled system did — compare exit quality via the weekly scorecard (trim/skip
  precision unaffected; watch `initial_stop` exits whose names then beat SPY 10d).
- **Min sample:** 20 closed trades. **Rollback:** `BROKER_NATIVE_STOPS=False`.

## EXP-002 — Deterministic baseline shadow arm (S2)
- **Status:** running (started 2026-06-10)
- **Change:** `BASELINE_SHADOW_ENABLED=True` — log-only twin (screener top-3 passing
  the same caps, fixed 6%, same stop math) recorded each run under
  `baseline_shadow`; scored weekly against live entries by 10/20-day SPY-relative
  forward returns.
- **Hypothesis (null):** the LLM debate layer does NOT select better names than the
  screener + deterministic caps alone.
- **Decision rule:** after ≥40 paired decision points or 3 months: if pipeline's avg
  forward excess ≤ baseline's, demote the LLM layer to veto-only (or remove) and
  re-test; if pipeline beats baseline by a margin exceeding its turnover/cost drag,
  keep and stop re-litigating the debate's existence.
- **Risk:** none (log-only).

## EXP-003 — Trailing stop loosening 0.15/0.10 → 0.20/0.13 *(retroactive)*
- **Status:** running (changed 2026-06-10 in `0f2b260`, registered retroactively)
- **Change:** `TRAILING_STOP_TRIGGER 0.15→0.20`, `TRAILING_STOP_PCT 0.10→0.13`.
- **Origin:** an UNSAVED decision-quality reading ("28 premature trims vs 7 good").
  Flagged by the audit as outcome-driven tuning on n=11 closed trades.
- **Metric / success:** at ≥15 `trailing_stop` exits under the new parameters, avg
  exit-to-20d-later forward return of harvested names is ≤ 0 vs SPY (we are not
  systematically selling winners too early) AND give-back from peak stays < 15%.
- **Failure / rollback:** harvested names keep beating SPY post-exit by >2% avg
  (trail too tight still) or give-back exceeds 15% avg (too loose) → revisit with
  data, one parameter at a time.

## EXP-004 — DEBATE_RUBRIC_V2 conviction calibration *(retroactive)*
- **Status:** running (enabled 2026-06-02, `481c08d`; quality-compounder lane added
  2026-06-10, `0f2b260`)
- **Change:** evidence-anchored conviction rubric + unresolved-bear-points output.
- **Pre-v2 baseline:** conviction mode 7 (103/281), buy-rate at 7 = 100%, PM==bull
  echo 54%. Post-v2 (6/02–6/05): mode 5 (44/66), buys nearly stopped.
- **Metric / success:** conviction stdev among buys > 0.8; buy-rate at conv-7 < 100%;
  monotonic conviction→P&L calibration at ≥20 closed trades.
- **Failure:** funnel stays ~closed (zero-trade rate > 95%) for 3 more weeks, or
  calibration stays flat/inverted → the rubric is relabeling, not discriminating.

## EXP-005 — TYPE-B skip cooldown, 3 days *(retroactive)*
- **Status:** running (enabled 2026-06-10, `0f2b260`)
- **Change:** `TYPEB_SKIP_COOLDOWN_DAYS=3` — suppress re-debating names recently
  skipped on structural grounds (DDOG was debated 53×, AMD 49× in one month).
- **Metric / success:** LLM debates per run drop ≥30% with no fall in skip precision
  on the weekly scorecard; no cooled-down name produces a >5% 5-day SPY-relative
  move the pipeline missed (check via `baseline_shadow` + watchlist records).
- **Failure:** missed-alpha skips rise on the scorecard → shorten cooldown to 1 day.

---

## Template

```
## EXP-NNN — <name>
- **Status:** proposed
- **Change:** <single config/prompt/rule change>
- **Hypothesis:** <what should improve and via what mechanism>
- **Metric / success:** <pre-stated threshold>
- **Failure:** <pre-stated threshold>
- **Min sample:** <N events or duration> **Rollback:** <one-line revert>
```
