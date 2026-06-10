# Fable Strategy Audit — Pocket Hedge Fund

**Auditor:** Claude (Fable 5), acting as independent principal engineer / quant researcher / adversarial auditor
**Date:** 2026-06-10
**Scope:** Full repository at commit `0f2b260` ("Funnel reopening: 5 regime-adaptive fixes"), journal data in `dashboard/data.json`, git history since 2026-05-08, GitHub Actions workflows. Read-only audit; no code, config, or orders were changed.

**Evidence labels used throughout:** **[FACT]** directly observed in code/data/git, **[INFERENCE]** reasoned from facts, **[HYPOTHESIS]** plausible but untested.

---

## 1. Executive verdict

**The system is a promising, unusually well-instrumented experiment that does not yet have — and currently cannot demonstrate — a measurable edge.** It is not a complicated pipeline producing only convincing explanations: real deterministic risk controls exist, shadow-mode experimentation (C13/C18) is genuinely good practice, and a counterfactual measurement engine exists. But three structural problems mean the stated goals are not currently supported:

1. **Capital preservation is not actually enforced.** Stops are software-sampled a few times a day, not held at the broker. Two of the last three losses realized **-12.56% (ARM) and -11.79% (SMCI)** against a "never lose more than 8%" rule (`HARD_STOP_PCT`). The drawdown circuit breaker measures decline from *starting capital*, not from peak, so it does not implement the ≤10% max-drawdown goal at all.
2. **The decision layer is theater around a threshold.** Across 281 logged debates, PM conviction ≥7 → buy **103/103 + 7/7 = 110/110 times (100%)**; conviction ≤5 → buy 0 times. The two-round LLM debate produces persuasive text, but the trade decision is effectively "did the PM say 7?" — and the PM prompt itself was repeatedly hand-tuned (pro-buy in v1, anti-buy in practice under v2) in response to recent outcomes. Conviction is not calibrated: the only two conviction-6 closed trades outperformed the nine conviction-7 trades.
3. **The improvement loop is anecdote-driven, the opposite of goal 5.** The latest commit changed five things at once (cooldown, trailing-stop parameters, conviction rubric, +19 screener names, watchlist regen) based on 11 closed trades and a decision-quality scorecard whose results were never persisted (`decision_quality: []`). That same commit **deleted 4 days of journal history** (5 runs, 5 snapshots, 22 debates, 22 risk decisions, 10 reviews, 32 tech-shadow records present in `15121c7` but absent at HEAD) — breaking the append-only audit guarantee.

**Three strongest aspects**
- Deterministic risk manager and pre-debate gates ([agents/risk_manager.py](../agents/risk_manager.py)) — sizing, sector/exposure caps, correlation tournament are rule-based, auditable, and mostly correct.
- Shadow-mode change discipline — C13-TECH (149 samples, 84% LLM/rule agreement), C18 gate (16/16 shadow matches before enforcement), options scaffold inert behind a kill switch. This is the right *pattern* for evidence-driven change.
- Instrumentation breadth — runs, debates, risk decisions, reviews, watchlists are all journaled and committed to git, and `core/counterfactuals.py` is a sound SPY-relative decision-scoring design.

**Three largest weaknesses**
- Execution-layer gaps: no broker-resident stops, entry prices recorded from quotes rather than fills, trim proceeds never booked to the P&L ledger (ledger explains only ~$1,877 of the ~$2,724 equity gain).
- A conviction score that carries no information beyond a binary threshold, fed by prompts that have been tuned to produce the outcome the operator wanted that week.
- Measurement that cannot yet support any conclusion: 11 closed trades over ~4 weeks, one of which (ARM, +84.4% "pnl_pct") is an accounting artifact of trim handling.

**Verdict against the primary question:** the current system does **not** yet support goals 1–3 or 5; it partially supports 4, 6, and 7. The highest-impact changes are (a) broker-native bracket stops, (b) a frozen-parameter evaluation window with a deterministic baseline arm running in shadow, and (c) journal/ledger integrity fixes. None of these add strategy complexity; all of them make the existing strategy testable.

---

## 2. Goal-alignment matrix

| # | Goal | Supporting components | Conflicting / missing | Evidence | Confidence in current support |
|---|------|----------------------|------------------------|----------|-------------------------------|
| 1 | Beat SPY risk-adjusted, long horizon | Screener momentum factors; SPY-relative counterfactual engine; SPY tracked in snapshots | `MAX_PORTFOLIO_EXPOSURE=0.60` means the stock book must beat SPY by ~1.67× in up-markets just to match it (idle 40% cash earns nothing in paper); no Sharpe/alpha computation anywhere (`core/benchmark.py` is **empty**, 0 lines); dashboard shows raw total return only | [FACT] fund +2.64% vs SPY −1.39% (2026-05-08 → 06-09, recovered snapshots); n≈13 entries, ~4 weeks — statistically meaningless | **Unknown — unmeasurable today.** The +4pp spread over 4 weeks is noise. |
| 2 | Preserve capital, max DD < ~10% | ATR + hard stop sizing; VIX/regime multipliers; exposure/sector caps; trailing stop persistence (C2) | **Stops not held at broker** (sampled at run times only); DD breaker computes `(STARTING_CAPITAL − value)/STARTING_CAPITAL` ([main.py:76-77](../main.py)) — decline from $100k, not from peak; after a run-up to $120k a fall to $95k (−21% from peak) would read as −5% | [FACT] ARM_20260601 stopped at −12.56%, SMCI −11.79% vs 8% hard cap; observed max DD 2.18% (sampled at run times only — true intraday DD unknown) | **Partially.** Caps genuinely limit exposure, but the two explicit DD mechanisms are defective. |
| 3 | Hold winners, avoid low-quality trades | TRIM-DISC, C13-EXIT price confirmation, hysteresis counters, trailing stops | Reviewer re-trims winners on static TYPE-B concerns (52 trims in 4 weeks; DOCS trimmed 15×, ARM 8×); ARM case: position cut ~75% during an +84% run, remnant exited at $409.46, **same symbol re-bought at full size at $410.12 six minutes later**, stopped out at $358.61 four days after | [FACT] trade records `ARM_20260514152848` / `ARM_20260601185259`, trim_history; reviews: 262 total, 203 hold / 52 trim / 7 exit | **No (historically), improving.** The recent fixes target exactly this, but they were applied after the damage and all at once. |
| 4 | Consistent, automatic decisions | GitHub Actions cron + sentinel; serialized pipeline concurrency group; queued-action audit; candidate isolation (C3) | Manual interventions recurring: `derisk_positions.py` manual trims (6/01), manual journal reconciliation, hand-edited universe; 9 PM JSON-parse failures silently became "skip — error" decisions | [FACT] 99 runs logged, 61 sentinel-triggered, 90% zero-trade; commit history shows ~weekly manual interventions | **Mostly yes mechanically; no in practice** — the operator is in the loop weekly changing rules. |
| 5 | Improve from measured outcomes, not anecdotes | `core/counterfactuals.py` + `scripts/decision_quality.py`; shadow modes; conviction-calibration audit hooks | `decision_quality` history is **empty** (never `--save`d); commit `0f2b260` tuned 3 parameter families simultaneously off one unsaved scorecard reading and named individual trades (DDOG +24%, AMD +16%) as justification; `performance_context.py` injects bias-correction prose into prompts (an unmeasured feedback loop, itself re-tuned twice) | [FACT] `data["decision_quality"] == []`; git log of config.py shows 5+ parameter changes in 4 weeks, each citing recent trades | **No.** The measurement *tooling* exists; the *discipline* does not. |
| 6 | Understandable, auditable, reliable, safe | Extensive config comments; every run committed to git; risk decisions logged with reasons | Append-only violated (0f2b260 deleted 4 days of records); `journal._load()` silently resets the whole journal on parse failure; `-X theirs` merge can drop local records; ledger P&L incomplete (trims, fills); zero automated tests | [FACT] record-count diff `15121c7` vs `0f2b260`; [core/journal.py:18-39](../core/journal.py); [trade.yml:87](../.github/workflows/trade.yml) | **Partially.** Very legible code; fragile data layer; no tests. |
| 7 | Stay paper until robust evidence | `PAPER_TRADING=True` with warning comment; options live mode requires explicit sign-off | Nothing enforces it (one-line flip); no "graduation criteria" defined anywhere | [FACT] [config.py:17](../config.py) | **Yes today**, but the *criteria* for graduating are undefined — define them now, in writing. |

**Goal conflicts/ambiguities to resolve:**
- Goal 1 vs Goal 2: 60% max exposure + 10% per-name cap + 25% sector cap is a structurally SPY-lagging configuration in bull markets. Either accept "absolute-return, lower-beta" as the real goal 1, or measure against a 60/40 SPY/cash blend rather than raw SPY.
- "Sufficiently long evaluation period" is undefined. Nothing in the repo defines a minimum sample (trades or months) before declaring success/failure. This vacuum is what permits weekly re-tuning.
- Goal 3 ("avoid premature exits") and Goal 2 ("preserve capital") collide precisely in trim policy; the system has oscillated between the two rather than defining which wins under which regime (TRIM-DISC is a reasonable first written answer).

---

## 3. Current pipeline map

```
                          ┌─ GitHub Actions cron 13:30/17:00 UTC (trade.yml)
 TRIGGERS ────────────────┼─ Sentinel cron */15 13-20 UTC (sentinel.yml → workflow_dispatch)
                          └─ After-close watchlist cron 21:30 UTC (after_close_watchlist.yml)

 main.run_pipeline()
 ├─ check_hard_stops()            [risk authority #1: VIX regime, SPY 200dma regime,
 │                                 drawdown-vs-starting-capital breaker]
 ├─ check_open_positions()        [execution authority #1: software stop-loss sells,
 │                                 trailing-stop ratchet, journal refresh from Alpaca,
 │                                 orphan close, time-stop flag]
 ├─ review_open_positions()       [judgment: full re-debate per position (≈8 LLM calls each)
 │     Mondays / sentinel / overnight alerts → reviewer LLM → hold/trim/exit
 │     → deterministic overrides: consec-weakened exit (price-confirmed),
 │       remnant cleanup, TRIM-DISC suppression]
 ├─ Screener (no LLM)             [information: 112-name hand-curated universe, 6 weighted
 │     factors, earnings-window filter, TYPE-B cooldown, held-name exclusion,
 │     after-close watchlist memory bonus +0.06]
 ├─ C18 pre-debate gate (enforce) [risk authority #2: sector_saturated / exposure_maxed]
 ├─ per candidate: analyse_symbol()
 │     ├─ technical (LLM, shadow-compared to rule) / fundamental (LLM) / sentiment (LLM)
 │     ├─ Bull R1 → Bear R1 → Bull R2 → Bear R2          [judgment, sequential not independent]
 │     ├─ PM verdict (LLM + rubric v2 + performance-context injection)   [judgment]
 │     ├─ risk_manager.evaluate() [risk authority #3: conviction floor, regime/VIX sizing,
 │     │     bear-spread shading, correlation tournament/rotation, exposure & sector caps,
 │     │     ATR+hard stop computation — deterministic]
 │     └─ execution: dup-guard → market order (quote price logged as entry) → journal
 ├─ log_snapshot / log_run → data.json
 └─ push (CI: workflow step with merge -X theirs; local: push_to_github)
```

Information enters at the screener, data fetcher (yfinance + Alpaca news), and analyst LLMs. Judgment enters at the 6 LLM roles (3 analysts, bull, bear, PM) plus the reviewer. Risk control enters at three deterministic layers (hard stops/regime, pre-debate gate, risk manager). Execution authority is exercised in `check_open_positions` (stops), `review_open_positions` (trims/exits), and `analyse_symbol` (buys/rotations) — all via market orders through [core/alpaca_client.py](../core/alpaca_client.py). **No protective orders ever rest at the broker.**

Dead/inert components: `core/benchmark.py` (empty), `core/executor.py` (empty), options scaffold (gated off), `core/risk_book.py` (not imported by the live path), `scripts/prompt_optimizer.py` (requires 20 closed trades; never runnable yet).

---

## 4. Ranked findings

### A. Confirmed software defects (highest impact first)

| # | Finding | Evidence | Impact / Confidence |
|---|---------|----------|---------------------|
| A1 | **Stops are advisory, not real.** `check_open_positions()` compares price to stop only when a pipeline run happens during market hours; no stop order exists at Alpaca. Gap-downs and between-run moves blow through the 8% hard cap. | ARM_20260601 exit −12.56%, SMCI −11.79% (both 2026-06-05) vs `HARD_STOP_PCT=0.08`; [main.py:157-162](../main.py) | Directly violates goal 2. **High confidence.** |
| A2 | **Max-drawdown breaker mis-specified.** `(STARTING_CAPITAL − portfolio_value)/STARTING_CAPITAL` ([main.py:76](../main.py)) is loss-from-inception, not drawdown from peak. The ≤10% DD goal is unimplemented. | Code reading; no peak tracking anywhere | Goal 2. **High confidence.** |
| A3 | **Journal history deleted by commit `0f2b260`.** Versus `15121c7`: −5 snapshots, −5 runs, −22 debate_logs, −22 risk_decisions, −10 position_reviews, −32 tech_shadow (all of 6/06–6/09). Likely a stale local `data.json` committed over the remote auto-commits. | `git show 15121c7:dashboard/data.json` vs HEAD record counts | Breaks append-only auditability (goal 6); also deleted the very runs cited as motivation for the funnel changes. **High confidence (observed).** Recoverable from git. |
| A4 | **`journal._load()` resets the journal on parse failure.** A truncated/corrupt `data.json` (e.g., killed mid-`_save`) silently returns a fresh empty structure; the next log call overwrites the file — total history loss without error. | [core/journal.py:18-39](../core/journal.py) (`except Exception: pass` → fresh dict) | Latent catastrophic data loss. **High confidence (code path), not yet triggered.** |
| A5 | **Realized P&L ledger is materially incomplete.** Trim proceeds are never booked (`log_trade_trim` records qty/price but no realized P&L); closes compute P&L on remaining qty only; orphan closes fabricate P&L=0 (AMZN). Ledger total (realized $1,081 + open unrealized $796 = $1,877) vs equity gain $2,724 — ~$847 untracked. ARM's "+84.44% pnl_pct" describes only the 5.9-share remnant of an originally 23.4-share position. | [core/journal.py:359-387](../core/journal.py), trades array, snapshot cross-check | Every downstream "outcome-based" mechanism (performance context, calibration, prompt optimizer) consumes distorted per-trade outcomes. Goal 5. **High confidence.** |
| A6 | **Entry prices are quotes, not fills.** `analyse_symbol` logs `current_price` from yfinance as `entry_price` immediately after submitting a market order; the actual fill (and slippage) is never reconciled (later partially patched by overwriting from Alpaca `avg_entry`, which then disagrees with `entry_price` used in stop/trailing math). | [main.py:826-852](../main.py); DDOG record: `entry_price 198.09` vs `avg_entry 218.44` after top-up | Stop distances and P&L computed off mixed bases. **High confidence.** |
| A7 | **Trailing-stop peak math mixes bases.** Peak = journal `entry_price × (1+unrealized_plpc)`, but `unrealized_plpc` is computed by Alpaca off `avg_entry` (which includes top-ups). For topped-up positions the ratchet level is wrong. | [main.py:164-173](../main.py) | Wrong trail levels on exactly the positions the system adds to. **High confidence.** |
| A8 | **`exit_reason="stop_loss"` conflates initial stops with trailing-stop harvests.** 7/11 closes labeled stop_loss include +24.7% (DDOG), +16.0% (AMD), +10.4% (QCOM) wins. `_check_stop_clustering()` would warn "entries too early" off a list dominated by profitable trailing exits. | trades array; [agents/performance_context.py:207-219](../agents/performance_context.py) | Pollutes the only feedback loop. **High confidence.** |
| A9 | **CI push uses `git pull --no-rebase -X theirs`** — on any `data.json` conflict, the *local just-written run records lose* to the remote, silently. The after-close workflow also lacks the `pipeline-data-json` concurrency group, so it can interleave with a trade run. | [trade.yml:87](../.github/workflows/trade.yml); [after_close_watchlist.yml](../.github/workflows/after_close_watchlist.yml) | Plausible mechanism for past desyncs (MS/SMCI double-buys per commit `5c4a676`). **Medium-high.** |
| A10 | Minor: cron comments wrong in EDT (13:30 UTC = 9:30am ET, not 8:30 — the "pre-market" run actually happens at the open in summer); `datetime.utcnow()` deprecation; 9 PM JSON-parse failures logged as ordinary "skip — error" decisions with no alerting. | [trade.yml:5](../.github/workflows/trade.yml); risk_decisions | Low each; cheap to fix. **High confidence.** |

### B. Operational risks

1. **yfinance as the primary market-data source on shared CI runners** — rate limiting already caused outage modes (the probe/`ScreenerDataUnavailable` machinery exists because of it). Quotes, fundamentals, VIX, sentinel checks all depend on an unauthenticated scraper. [INFERENCE: this will be the most common silent degradation as run frequency grows.]
2. **data.json triple duty** (journal of record + dashboard feed + sentinel input) with full-file rewrite on every log call (~40k lines re-serialized dozens of times per run) and no locking. One writer at a time is only guaranteed by the Actions concurrency group — local runs bypass it.
3. **Cost/latency churn:** 61 of 99 runs were sentinel-triggered; each position review re-runs an ~8-call LLM debate; DDOG was debated 53× and AMD 49× in a month, mostly to be re-skipped on the same TYPE-B reasoning (now mitigated by the 3-day cooldown — a good fix).
4. **No automated tests of any kind** (`find . -name "*test*"` → none). Most dangerous untested logic: risk_manager cap/sizing arithmetic (incl. top-up double-count credits), trailing-stop persistence, journal round-trip, `_cooled_down_symbols` keyword matching, watchlist expiry timezone math.
5. **Failure vs no-trade ambiguity** is partially solved (`data_unavailable` vs `no_candidates` — good), but LLM-layer failures (PM parse errors, analyst fallbacks to neutral-5) flow into decisions indistinguishably from real analysis.

### C. Strategy-design weaknesses

1. **The edge is not stated in falsifiable form.** Closest articulation: momentum/relative-strength continuation in liquid mid/large caps, catalyst-gated, with LLM debate as a quality filter. But screener weights ([config.py:34-41](../config.py)) are hand-set, never backtested; the "catalyst" factor was discovered to be mislabeled trailing growth (C16) after a month of trading on it; and the universe is 112 hand-picked names whose composition is edited reactively (19 defensives added 6/09 *because they outperformed on 6/09* — momentum chasing at the meta level, and a form of selection bias).
2. **Conviction is binary in practice.** Buy-rate by PM conviction: 6 → 10/45, 7 → 103/103, 8 → 7/7, ≤5 → 0/104. Sizing map therefore collapses to ~6% positions almost always. The 1–10 scale, the two-round debate, and the bear-spread shading (spread ≥2 occurred in ~10% of debates) carry little decision information. PM echoes bull R2 exactly 54% of the time.
3. **Prompt-regime whiplash.** Legacy PM prompt is heavily pro-buy ("tied debates in uptrends are a BUY", "do NOT lower conviction for systemic risks"); DEBATE_RUBRIC_V2 (6/02) swung the distribution from mode-7 (103/281) to mode-5 (44/66 post-6/02) and buys nearly stopped — which then motivated the 6/10 "funnel reopening" loosening. Two opposing prompt philosophies tuned within 8 days of each other, both justified by the same handful of trades. [FACT: conviction distributions pre/post 6/02; INFERENCE: this is outcome-chasing, not calibration.]
4. **Exit machinery fights itself.** Five overlapping exit/trim authorities (software hard stop, trailing ratchet, reviewer trims, consec-weakened exits, remnant cleanup) produced the ARM sequence: trim 75% of the fund's biggest winner on re-discovered TYPE-B leverage concerns at $207 and $353, exit the remnant at $409 as "dead weight", re-buy full size at $410 six minutes later, stop out at $358. Each rule fired "correctly" in isolation. [FACT: trade records + trim_history.]
5. **Recent fixes are plausible but unattributable.** Commit `0f2b260` changed cooldown, trailing trigger/trail, rubric lane, universe, and watchlist simultaneously. Whatever happens next cannot be attributed to any one change. This pattern (multi-change commits citing individual trades: RKLB/DOCS in C13-EXIT, AMD in TRIM-DISC, DDOG/AMD in cooldown) repeats across the last 3 weeks. With 11 closed trades, every one of these is fit to noise until proven otherwise. [INFERENCE, high confidence.]
6. **Performance-context prompt injection is an unmeasured feedback loop.** Win-rate/exit stats plus bias-correction prose are injected into bull, bear, and PM prompts each run ([agents/performance_context.py](../agents/performance_context.py)). The cold-start block actively pushes against conviction-lowering; C17 then added symmetric counter-bias. Two layers of prompt-psychology tuning with no A/B evidence either layer helps. [HYPOTHESIS that it's net harmful; test by ablation.]

### D. Measurement weaknesses

1. **n = 11 closed trades / ~4 weeks / one regime.** No conclusion about edge, win rate, calibration, or any parameter is supportable. The +2.64% vs SPY −1.39% spread is well within noise (one position, ARM, dominates the realized ledger and its headline number is an artifact — see A5).
2. **The decision-quality engine has never persisted a scorecard** (`decision_quality: []`), so claims driving parameter changes ("28 premature trims vs 7 good") are unreproducible from saved data; the underlying skip/trim verdicts use a single 10-day SPY-relative window — itself an untested choice — and judge partial trims as if they were full exits.
3. **Equity curve is sampled only at run times** — true max drawdown is understated by construction; no Sharpe/Sortino/beta-adjusted comparison exists (benchmark module empty); the dashboard compares raw cumulative return of a 60%-max-deployed book against 100% SPY.
4. **Look-ahead/survivorship hygiene is mostly OK** (decisions journaled before outcomes; no backtest claims), but the hand-edited universe and reactive name additions introduce selection bias that no current metric captures, and `counterfactuals.py` only scores names the screener surfaced — missed opportunities outside the 112-name list are invisible.
5. **Reconstructability is good for trades** (debate_id links, risk reasons) but **broken by A3/A5** and by `candidate_details` only being logged since C3-OBS — most historical runs can't answer "why was X ranked above Y".

### E. Hypotheses requiring experiments (not yet findings)

- H1: The LLM debate layer adds no selection value over "buy the screener's top composite score with the same deterministic risk controls." (Testable in shadow — see §6.)
- H2: The deterministic technical rule can replace the technical LLM at zero quality cost (84% agreement already logged; disagreements skew LLM-bearish on GOOGL/AMZN/QCOM — resolve which side was right by forward returns before flipping).
- H3: Looser trailing stops (0.20/0.13) improve net expectancy vs the old 0.15/0.10. (Was changed on anecdote; needs ≥30 trailing-stop events to evaluate.)
- H4: Performance-context injection changes decisions at all (ablation A/B across identical candidate sets), let alone for the better.
- H5: Sector/correlation caps are binding on returns (count of gated buys that subsequently beat SPY) vs protective.

---

## 5. Performance and evidence assessment

**What the data says (all [FACT], none significant):**
- Equity: $100,000 → $102,644 (+2.64%) per recovered snapshots through 6/09 (HEAD's own snapshots end 6/05 due to A3). SPY over the same window: −1.39%. Sampled max DD: 2.18%.
- 11 closed trades: 6 wins / 5 losses; realized +$1,081. Biggest realized winner is the ARM remnant artifact; excluding it, realized P&L is −$27 — i.e., **closed-trade trading has been a wash; the equity gain lives in unbooked trim proceeds and the two open positions** (LLY +12.4%, MS +4.7%).
- Conviction calibration is inverted at current n: conviction-6 trades avg +17.6% (n=2) vs conviction-7 avg +8.4% (n=9, median ≈ 0, σ huge).
- Process volume vastly outstrips outcome volume: 281 debates, 262 reviews, 213 risk decisions → 13 entries, 11 closes. The system *generates* enough process data to do counterfactual measurement — it just hasn't saved it.
- Zero-trade rate: 89/99 runs (90%). Post-rubric-v2 (6/02–6/05): 49 skips / 16 PM-buys, conviction mode 5. The funnel did close; whether that was wrong depends on forward returns of the skipped names — computable with the existing tooling, never computed/saved.

**Where data is insufficient (everything else):** edge existence, factor weights, stop parameters, debate value, trim policy, regime multipliers. Minimum bar before *any* of these can be judged: see §6 sample sizes.

---

## 6. Strategy improvement roadmap

Ordered. Each is deliberately a measurement or control fix, not added intelligence. **Freeze all strategy parameters during the evaluation window except where stated.**

### S1. Broker-native bracket stops (do first — see §8)
- **Mechanism:** Convert the software stop into a real stop order (or stop + take-profit OCO bracket) resting at Alpaca, updated when the trailing ratchet moves. Eliminates gap/sampling risk; makes the 8% hard cap real.
- **Implementation:** On fill, submit a `stop` (or bracket) child order; `check_open_positions` updates the stop order instead of market-selling; cancel/replace on trail ratchet.
- **Success metric:** No realized loss exceeds stop distance + 1% slippage over the next 20 closed trades. **Failure:** stop orders triggered by intraday noise materially more often than the sampled system (compare exit quality via the counterfactual engine).
- **Eval period:** immediate safety win; 20 trades for the noise-trigger check. **Rollback:** revert to software stops (one flag).
- **Risk:** day-gap fills below stop price still possible (stop ≠ stop-limit); wash-trade interactions with same-day re-entries (the dup-guard helps).

### S2. Deterministic baseline shadow arm (the single most informative experiment)
- **Mechanism:** Every run, log (never execute) the decisions of a "null-intelligence" twin: buy the screener's top-N composite names that pass the same deterministic risk caps, fixed 6% size, same ATR/hard stops, exit only via stops. If the full LLM pipeline can't beat its own screener + risk manager on forward SPY-relative returns, the debate layer is cost without edge.
- **Implementation:** ~100 lines; a `shadow_baseline` array in the journal reusing existing screener output and risk_manager sizing in dry mode (the C13/C18 shadow pattern already proves the approach).
- **Success metric:** after ≥40 paired decision points or 3 months, pipeline forward 10/20-day SPY-relative returns beat baseline's by a margin > the LLM's cost in turnover/slippage. **Failure:** baseline matches/beats pipeline → demote LLM layer to veto-only or remove.
- **Risks:** none to live trading (log-only). **Rollback:** delete the logger.

### S3. Experiment registry + parameter freeze (process change, zero code)
- **Mechanism:** Stops outcome-chasing. A `docs/EXPERIMENTS.md` ledger: one change per experiment, hypothesis, metric, minimum n (pre-stated: e.g., 30 relevant events or 60 trading days), success/failure thresholds, decision date. No config/prompt change merges without an entry. Trailing-stop loosening (H3) and rubric v2 (already running) become the first two retroactive entries.
- **Success metric:** the process is the metric — zero out-of-registry parameter changes for 8 weeks.

### S4. Persist decision-quality scorecards automatically
- **Mechanism:** Weekly Actions job runs `scripts/decision_quality.py --save` (and extends it to also score the S2 baseline). Turns the existing engine into an accumulating record instead of a one-off CLI.
- **Success metric:** skip-precision and trim-precision trendlines with CIs exist by week 4; parameter discussions cite them instead of single trades.

### S5. Simplify the exit stack (only after S1–S4 produce data)
- **Hypothesis to test:** in calm-bull regimes, "stops only" (TRIM-DISC fully suppressing reviewer trims) outperforms reviewer-driven trimming. TRIM-DISC already half-implements this; instrument both arms (reviewer recommendation vs executed action are both journaled) and judge with the trim-precision metric at n ≥ 30 suppressed trims.
- **Explicitly not recommended now:** options Phase 2, more agents, more prompt psychology, universe expansion, new factors (the PEAD factor idea is reasonable but belongs behind the registry after S2 reports).

---

## 7. Engineering improvement roadmap

Priority order:

1. **Journal integrity (pairs with A3/A4/A9):**
   - `_load()` must never silently reset: on parse failure, halt and alert (exit nonzero in CI) and keep a `.bak` written before every `_save`.
   - Replace `-X theirs` with a conflict-fail + retry-from-fresh-checkout, or better: move the journal to append-only JSONL files per record type (no merge conflicts by construction) with `data.json` generated as a build artifact for the dashboard.
   - Add the `pipeline-data-json` concurrency group to `after_close_watchlist.yml`.
   - Restore the 6/06–6/09 records from `15121c7` (mechanical git archaeology; all data still in history).
2. **Trade ledger correctness (A5/A6):** book realized P&L on every trim using actual fill data (poll order status by id); reconcile `entry_price` to fill price; split `exit_reason` into `initial_stop` / `trailing_stop` / distinct reasons; backfill labels for the 11 closed trades (recoverable from stop vs entry relationship).
3. **Tests** (first suite, highest danger first): risk_manager sizing/caps/top-up credits with table-driven cases; trailing-stop ratchet + persistence; journal round-trip incl. corrupt-file behavior; `_cooled_down_symbols`; watchlist expiry around DST/holidays; `_safe_review_decision` normalization. All pure-logic, no network — a day of work.
4. **Failure observability:** count and surface LLM fallbacks per run (analyst neutral-5 fallbacks, PM parse failures) in the run record; a run where 3 analysts fell back is a data outage, not an analysis.
5. **Data layer hardening:** cache/contain yfinance (retry with jitter, persist last-good fundamentals); consider Alpaca bars (already entitled) as primary OHLCV with yfinance fallback — `alpaca_client.get_bars` exists and is barely used.
6. **Cleanup:** delete empty `core/benchmark.py` / `core/executor.py` or implement benchmark properly (Sharpe, beta, peak-DD vs SPY and vs 60/40 blend — ~50 lines on existing snapshots); remove the stale `.claude/worktrees/upbeat-johnson-9e1751` copy; fix cron comments; correct the DD breaker to peak-relative (one line, but log both during transition).

---

## 8. Recommended next action

**Implement S1: broker-native bracket stops (plus the one-line peak-relative drawdown fix, A2).**

**Why before everything else:**
- It is the only finding where the *current live behavior* actively violates a stated goal with realized money (−12.56% and −11.79% against an 8% promise) — every other improvement is about learning faster; this one is about the system doing what it already claims to do.
- It is small, deterministic, fully testable in paper, and independent of every strategy debate. S2–S4 (the measurement program) should start in the same week, but S1 is the prerequisite for trusting any future loss statistics: stop parameters can't be evaluated while stops aren't real.
- It reduces, rather than adds, system complexity: sentinel "near_stop" triggers and part of `check_open_positions` exist *because* stops aren't at the broker.

**Bounded implementation plan (est. ~150 LOC + tests; do not implement until approved):**
1. `alpaca_client.submit_stop_order(symbol, qty, stop_price)` + `replace_stop_order(order_id, new_stop)` + `get_open_stop_order(symbol)` (alpaca-py supports stop orders and order replacement).
2. In `analyse_symbol` post-fill: submit the stop child for the filled qty; store `stop_order_id` on the trade record.
3. In `check_open_positions`: trailing ratchet → `replace_stop_order`; trims/exits → cancel/replace stop for the new qty; orphan/exit paths cancel the resting stop.
4. Migration: one-time script places stops for currently open positions (LLY @ 934.42, MS @ 191.60) — *gated behind `--confirm` and your approval*.
5. Config flag `BROKER_NATIVE_STOPS = True` with the legacy path retained for rollback.
6. Verification: unit tests for ratchet/replace logic; one paper-market session confirming a stop rests at Alpaca after entry and moves after a ratchet; then watch the next 20 closed trades for max-loss compliance (S1 success metric).
7. Out of scope for this change: take-profit legs, stop-limit conversion, options — keep the diff reviewable.

---

## Appendix: key numbers referenced

| Metric | Value | Source |
|---|---|---|
| Period audited | 2026-05-08 → 2026-06-09 | journal meta / snapshots |
| Equity | +2.64% (vs SPY −1.39%) | snapshots (incl. recovered 6/08–6/09) |
| Sampled max drawdown | 2.18% | 85 snapshots (pre-deletion) |
| Closed trades | 11 (6W/5L), realized +$1,081 (−$27 ex-ARM-remnant) | trades array |
| Open | LLY 4.1% (+12.4%), MS 7.3% (+4.7%) | open_trades |
| Debates / reviews / risk decisions | 281 / 262 / 213 | journal arrays |
| Buy-rate at conviction 7 / 8 / ≤5 | 100% / 100% / 0% | debate_logs |
| Zero-trade runs | 89/99 (90%) | runs |
| Sentinel-triggered runs | 61/99 | runs |
| Tech LLM-vs-rule agreement | 125/149 (84%) | tech_shadow |
| Pre-debate gate shadow accuracy | 16/16 | pre_debate_gates + commit fc6f464 |
| Saved decision-quality scorecards | 0 | decision_quality |
| Records deleted by `0f2b260` | 5 runs, 5 snapshots, 22 debates, 22 risk decisions, 10 reviews, 32 tech-shadow | git diff 15121c7→HEAD |
| Stop-cap breaches | ARM −12.56%, SMCI −11.79% vs 8% cap | trades array |
| Automated tests | 0 | repo search |

*Audit complete. No changes were made outside this document. Awaiting approval before any implementation.*
