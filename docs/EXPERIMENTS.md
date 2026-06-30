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

## EXP-006 — Dynamic universe discovery
- **Status:** running (started 2026-06-10)
- **Change:** `DISCOVERY_ENABLED=True` — each non-sentinel run pulls market-wide
  most-actives + top gainers (Alpaca screener API) and adds up to 25 names that
  pass ALL guards to the screener universe: core floors (price/mcap/volume),
  day gain ≤ 12%, 5-session gain ≤ 25% (anti-blow-off), and EMA10 > EMA30
  (no dead-cat bounces). Discovered candidates carry `signals.discovered=true`;
  every accept/reject is journaled under `universe_discovery`.
- **Hypothesis:** the hand-curated 112-name list misses emerging momentum names
  (audit §4C.1 — universe edits were reactive); a guarded dynamic scan surfaces
  them earlier without feeding parabolic spikes into the pipeline.
- **Metric / success:** after 8 weeks: (a) discovered candidates' forward 10/20-day
  SPY-relative returns ≥ core-list candidates' (from `candidate_details` +
  `universe_discovery`); (b) at least ~1 discovered candidate/week reaches the
  debate stage; (c) skip-precision on the weekly scorecard does not degrade.
- **Failure:** discovered names systematically underperform core names, or the
  blow-off guards admit names that mean-revert hard (avg 10d excess < −2%) →
  tighten guards or disable.
- **Min sample:** 8 weeks or 30 discovered candidates. **Rollback:** `DISCOVERY_ENABLED=False`.

## EXP-007 — Pyramiding into confirmed winners
- **Status:** proposed — **HELD 2026-06-30** (do NOT implement yet)
- **Change (when unheld):** allow ONE de-risked add to a meaningful winner (≥ +8% above
  `avg_entry`, trend intact), gated by — original stop ratcheted to ≥ breakeven on the
  blended basis BEFORE the add; total position ≤ 8% NAV; no adds to names extended past
  their 52-week high; all existing caps apply. Flag `PYRAMID_ADDS`.
- **Why held:** the Phase-0 decision gate (2026-06-30) shows the deterministic baseline
  twin (+4.96% 10d excess vs SPY, n=24) currently OUT-SELECTS the live LLM pipeline
  (+2.23%, n=9). Pyramiding concentrates MORE capital into the LLM funnel's picks —
  the wrong move while the funnel is not beating its own screener. Naive pyramiding also
  lowers Sharpe and maximizes 52-wk-high crash beta (Byun & Jeon 2023).
- **Unhold condition:** pipeline forward excess ≥ baseline at ≥20 paired decisions, OR an
  explicit decision to run it as a measured experiment with the guardrails above.

## EXP-008 — Raise MAX_POSITIONS 8 → 10
- **Status:** running (started 2026-06-30)
- **Change:** `MAX_POSITIONS` 8 → 10 (single int). Nothing else.
- **Hypothesis:** the 8-slot cap binds ~⅓ of the time and forces idle cash; more slots
  raise deployment via DIVERSIFICATION (more, smaller names → lower per-name and lower
  momentum-crash risk), gated by the unchanged 60% gross and 25% sector caps so it cannot
  over-concentrate. The conservative, gate-endorsed deployment lever.
- **Metric / success (≥6 weeks or until 60% gross binds before slot count):** average
  deployment rises toward ~40–50% AND names filling the new slots have forward 10/20-day
  SPY-relative returns ≥ 0 on the decision-quality scorer AND max drawdown stays within
  the −10% breaker.
- **Failure:** new slots fill with names that underperform SPY, or sit empty (funnel, not
  slots, is the binding constraint) → revert to 8.
- **Min sample:** 6 weeks / 15 new-slot entries. **Rollback:** `MAX_POSITIONS=8`.

## EXP-009 — Per-name clamp + DOWN-ONLY volatility discipline
- **Status:** accepted (2026-06-30)
- **Change (two parts):** (A, bug fix) enforce the `MAX_POSITION_PCT` per-name clamp in
  `risk_manager.evaluate` — it was defined in config but applied nowhere (only the 60%
  gross cap was). (B, decision) KEEP the existing down-only regime/VIX size scaling;
  explicitly do NOT add a calm-market up-multiplier.
- **Rationale:** the academic benefit of volatility management comes from the DE-RISKING
  leg, not from leveraging up when calm (Cederburg et al. 2020 — vol-management beats
  buy-and-hold out-of-sample only ~half the time, robustly only for momentum; sizing up in
  low-VIX loads risk right before vol spikes). Part A is a prerequisite for any future
  up-leg to be safe.
- **Revisit:** a vol-up multiplier, if ever proposed, must ship WITH Part A and as its own
  registered experiment with a pre-stated drawdown failure threshold.

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
