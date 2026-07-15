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
- **Status:** rejected (2026-07-06 audit) — failed its own gate (b): in 29 runs it
  accepted 128 candidate-slots covering only 15 unique symbols (AAL accepted 29×,
  never once debated), fed 9 debates (8 of them MU), and produced exactly 1 trade
  (MU, now a dust remnant). ~0.3 discovered candidates/week reached debate vs the
  ≥1/week gate. `DISCOVERY_ENABLED=False`. Re-propose only with a mechanism that
  surfaces NEW names (e.g. exclude already-accepted symbols for N days).
- **Original status:** running (started 2026-06-10)
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

## EXP-008 — Raise MAX_POSITIONS 8 → 11
- **Status:** running (started 2026-06-30)
- **Change:** `MAX_POSITIONS` 8 → 11 (single int). Nothing else. (Operator chose 11 over the
  plan's suggested 10-first step — marginal difference, same diversification rationale.)
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

## EXP-010 — Single-round debate (R2 → measured prior)
- **Status:** **REJECTED (2026-07-15)** — failed its own pre-stated failure line; rolled
  back to `DEBATE_ROUNDS=2` (ran 2026-07-07 → 2026-07-15).
- **Result (n=55 post-change debates, min sample met):**
  - **Buy rate 24.3% → 41.8% (+17.5pp)** — and +26.8pp vs the ~15% baseline this entry
    named. The failure line was **">10pp"**. Breached decisively.
  - **The buy threshold slid from conviction-7 to conviction-6:** conv-7 fell to 7.0% of
    debates while conv-6 rose to 55.4%, and **97 of 131 buys came from conv ≤6** — the
    entry required "conviction-7 still the buy threshold". Not met.
  - Avg conviction drifted 5.18 → 5.64.
  - The one criterion it PASSED: the pipeline-vs-baseline 10d gap did not widen (it
    narrowed — baseline −2.00% vs pipeline −1.89% at review).
- **Why the hypothesis was wrong:** R2's *modal* effect being "bull −1" is not the same as
  R2 carrying no decision information. The bear's live rebuttal was acting as the **brake
  on the PM** — replacing it with a static −1 prior removed the adversarial pressure that
  was suppressing marginal conv-6 buys, and the funnel nearly doubled its buy rate. A
  measured average is not a substitute for an argument that has to be answered.
- **If re-tested:** hold the buy rate fixed as the control (e.g. raise `MIN_CONVICTION_SCORE`
  to compensate) so the call-volume saving can be isolated from the behaviour change.
- **Change:** `DEBATE_ROUNDS=1` — the two R2 rebuttal calls are replaced by the
  measured prior (`DEBATE_R2_BULL_PRIOR=-1` applied to bull R1; bear R1 carried
  forward). Rationale: across 670 logged debates R2's modal effect was exactly
  "bull −1" (441/670 in the (−1,0)/(−1,±1) cells), only 24% moved any score ≥2,
  and the PM echoed bull R2 54% of the time.
- **Hypothesis:** R2 carries ~no decision information; removing it cuts 2 of 8
  debate calls with no change in what gets bought.
- **Metric / success (next review + weekly scorecard):** over ≥40 post-change
  debates, buy rate within ±5pp of the pre-change rate (~15%); conviction
  distribution shape comparable (conviction-7 still the buy threshold); scorecard
  pipeline-vs-baseline forward-return gap does not widen.
- **Failure:** buy rate shifts >10pp, or the pipeline-vs-baseline 10d excess gap
  widens by >2pp vs the 2026-07-03 reading → restore live R2.
- **Min sample:** 40 debates / 4 weeks. **Rollback:** `DEBATE_ROUNDS=2`.

## EXP-011 — Lite position reviews (3 calls, stored entry debate)
- **Status:** running (started 2026-07-07)
- **Change:** `REVIEW_LITE=True` — reviews use fresh technical + sentiment + the
  reviewer call, judged against the STORED entry debate, instead of re-running
  fundamental + a full 4-call re-debate per position per run. Reviews were 49%
  of all LLM calls for 89% "hold" and ≤35% trim precision.
- **Hypothesis:** the reviewer's hold/trim/exit decision quality does not depend
  on re-litigating fundamentals and a fresh debate every run; broker stops carry
  the real downside protection.
- **Metric / success (weekly scorecard, ≥25 post-change reviews):** trim precision
  ≥ the pre-change baseline (~24–35%); hold rate stays in the 80–95% band; exits
  still fire on genuinely broken theses (spot-check any stop-outs for a review
  that said "hold" within 3 days prior).
- **Failure:** trim precision < 20%, or two incidents where a position rode
  through its stop after a lite review said "hold" on visibly broken thesis
  evidence the full path would have surfaced (fundamental deterioration).
- **Min sample:** 25 reviews / 4 weeks. **Rollback:** `REVIEW_LITE=False`.

## EXP-012 — Sentinel earnings-only trigger whitelist
- **Status:** running (started 2026-07-07)
- **Change:** `SENTINEL_ENABLED_TRIGGERS` defaults to `earnings_today`;
  intraday_move / intraday_rebound / position_move / volume_spike / near_stop
  disabled. Sentinel-triggered runs were 65% of all runs (129/199) with a ~8%
  trade rate; near_stop is redundant with broker-native GTC stops. Tick cadence
  also dropped to hourly.
- **Hypothesis:** the noise triggers produced re-reviews and re-debates (LLY 98×,
  AMD 80×), not trades; removing them cuts ~60% of run volume with no P&L cost.
- **Metric / success (next review, then 4 weeks):** runs/day drops to ~3-4;
  trades/week and scorecard forward returns unchanged; no adverse-move incident
  where an intraday trigger would plausibly have exited materially better than
  the resting broker stop did.
- **Failure:** two incidents where a position gapped through its stop and the
  disabled intraday trigger had ≥1 tick of lead time to act → re-enable
  `position_move` (not the full set) via the env var.
- **Min sample:** 4 weeks. **Rollback:** `SENTINEL_ENABLED_TRIGGERS` env var.

## EXP-013 — Watchlist buy-side memory bonus zeroed
- **Status:** running (started 2026-07-07)
- **Change:** `WATCHLIST_MEMORY_BONUS` 0.06 → 0.0. The bonus produced exactly one
  attributable entry in 8 weeks: SMCI 6/9, the fund's worst closed trade (−16.7%).
  Overnight position ALERTS (targeted reviews) are unchanged.
- **Hypothesis:** a +0.06 composite tie-breaker on overnight-momentum names
  selects for gap-chasing entries; removing it loses nothing.
- **Metric / success:** nothing to gain — success is the absence of evidence that
  watchlist-tagged names the screener now ranks just below the cutoff would have
  outperformed (check `candidate_details` scores near the cutoff at review).
- **Failure:** ≥3 watchlist-revalidated names miss the candidate cut by < 0.06
  and go on to +10% 10d excess → restore a smaller bonus (0.03) as a new entry.
- **Min sample:** 4 weeks. **Rollback:** `WATCHLIST_MEMORY_BONUS=0.06`.

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
