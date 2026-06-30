# Independent Review of the Deployment Recommendations — Analysis + Revised Implementation Plan

**Prepared:** 2026-06-25
**Reviewer:** Independent analysis (second opinion on `pocket-hedge-fund-deployment-recommendations.md`)
**Mandate:** Validate the prior recommendations against the actual repo, the live journal, and external research. Where they hold, say so. Where they don't, give a corrected approach. End product = a step-by-step plan you can paste into Claude Code.
**Constraint:** This document changes no code. It is a plan.

---

## 0. TL;DR — what to actually do

The prior recommendations are **directionally right on the problem and mostly right on the safe levers, but wrong about which lever is "lowest risk," and they skip the single most important thing: you cannot yet prove the engine you're about to deploy more capital into actually works.**

My ranked plan (full detail in §6):

1. **Fix the measurement first (freeze-exempt bugs).** Reconcile MU into the journal **and fix the broken baseline-vs-pipeline scorer** (`n_decided = 0` — your most important experiment is silently producing nothing). Without this you are flying blind on whether to deploy more.
2. **EXP-008 — Raise `MAX_POSITIONS` 8 → 11.** Genuinely the lowest-risk, most-aligned lever. Ship this first among the strategy changes.
3. **EXP-007 — Pyramiding, but only in the de-risked form** (move the original stop to breakeven *before* adding; hard-cap total adds). The prior doc's version is missing the one mitigation the research says is non-negotiable. If you don't want to do it the careful way, **don't do it** — the clean evidence says naive pyramiding *lowers* Sharpe and concentrates you into exactly the names that drive momentum crashes.
4. **EXP-009 — Vol-targeting, but as a DOWN-only de-risking rule, not "size up when calm."** The academic benefit comes from cutting risk when vol rises, not from leaning in when calm. Sizing up in low-VIX loads risk right before vol spikes. (And right now VIX ≈ 18.6 — *not* calm — so the "lean in" premise doesn't even hold today.)
5. **Agree fully: DEFER the conviction→size bump (their lever D).** Your own data shows conviction is **not** calibrated (conv-6 beats conv-7 on win rate). Scaling an uncalibrated signal scales noise.

The honest one-liner: **the deployment problem is real, but the cleanest fix is "a few more slots + size your *qualifying* ideas a bit better," not "pour more into your biggest winners."** And before you lean on any of it, make the scoreboard work.

---

## 1. What the fund does (so the rest makes sense)

An automated, **paper-trading** long/flat US equity momentum fund on a $100k Alpaca account. Pipeline (cron + sentinel, GitHub Actions):

- **Screener** (no LLM): ~112-name curated universe + dynamic discovery, 6 weighted factors (relative strength, technical, volume spike, growth quality, news, valuation), earnings-window filter, cooldowns.
- **LLM debate**: 3 analysts → Bull/Bear two rounds → PM verdict with a 1–10 conviction score.
- **Deterministic risk manager** (`agents/risk_manager.py`): conviction→size map, ATR + 8% hard stop, 60% gross cap, 25% sector cap, 10% per-name cap, correlation tournament.
- **Execution**: market orders via Alpaca; now (EXP-001) with broker-resident stop orders.
- **Instrumentation**: every run, debate, review, risk decision journaled to `dashboard/data.json`; a counterfactual/baseline-shadow engine to measure decision quality.

Stated goals (from `docs/FABLE_STRATEGY_AUDIT.md`): (1) beat SPY risk-adjusted over a long horizon, (2) preserve capital / max drawdown < ~10%, (3) hold winners & avoid low-quality trades, plus consistency, evidence-driven improvement, auditability, and "stay paper until robust evidence."

**The problem you flagged is confirmed in the live data:** the book holds 6 names, **5 of 6 are conviction-6 sized at ~3–4% (~$3–4k each)**, and total deployment is **~26–30%**. Most of your capital is idle.

---

## 2. Current performance — what the journal actually shows (as of 2026-06-24)

I parsed `dashboard/data.json` directly. Numbers below are facts from the journal, not estimates.

| Metric | Value | Note |
|---|---|---|
| Window | 2026-05-08 → 2026-06-24 (~7 weeks) | 136 snapshots |
| Fund return | **+2.22%** ($100,000 → $102,222) | |
| SPY return (same window) | **−0.75%** | Flat-to-down market |
| Excess vs SPY | **+2.97 pp** | …but in a down tape; see caveat |
| Peak equity | $104,934 | Fund has given back ~2.6% from peak |
| Max sampled drawdown | 2.76% | Sampled at run times — true intraday DD is higher |
| Current deployment | **~29.5%** (cash $72,060) | Was 40–44% in mid-May, collapsed to ~11–14% in early June |
| Closed trades | **12** (6W / 6L = 50% win rate) | Still a tiny sample |
| Avg win / avg loss | **+25.1% / −9.5%** | Asymmetry is what's carrying the fund |
| Open positions | 6 (LLY, MS, C, AMD, AMAT, CVS) | 2 Financials / 2 Tech / 2 Healthcare |

**Conviction calibration (the crux for lever D):**

| Conviction | n | Avg P&L | Win rate |
|---|---|---|---|
| 6 | 3 | **+6.14%** | **67%** |
| 7 | 9 | +8.37% | **44%** |

Conv-7's average is propped up entirely by **ARM +84.4%, which the audit established is an accounting artifact** of trim handling (it describes a tiny remnant, not the real trade). Strip ARM and conv-7's median is **0.00%**. So conviction-7 does **not** reliably beat conviction-6 — if anything conv-6 has the better hit rate. **Conviction carries little information beyond "did the PM say ≥6."** Hold that thought for §4 lever D.

**The wins came from letting winners run:** DDOG +24.7%, AMD +16.0%, QCOM +10.4% all exited via **trailing stop**. The losses were mostly **initial stops** (NVDA −8.1%, GOOGL −7.6%, ARM −12.6%, SMCI −11.8%, SMCI −16.7%). Two of those losses (SMCI, ARM) breached the 8% hard cap *because stops weren't yet broker-resident* — which is exactly what EXP-001 was built to fix.

**Caveat that matters for the whole exercise:** +2.97 pp over 7 weeks across 12 trades in one (flat/down) regime is **statistically meaningless**. The audit said this; it's still true. Every parameter recommendation below is being fit to noise until you have ~30+ closed trades. That's not a reason to do nothing — it's a reason to prefer *structural* changes (more slots) over *amplifying* changes (bigger sizes), and to fix your measurement so the next 30 trades actually teach you something.

---

## 3. Verifying the prior doc's factual claims about the code

I checked each load-bearing claim against the repo. The prior doc is, to its credit, **accurate on the code facts**:

| Claim in prior doc | Verdict | Evidence |
|---|---|---|
| Drawdown breaker rewritten to peak-based, halts at −10% | ✅ TRUE | `main.py:85-91` computes `drawdown_from_peak` and returns False if `> MAX_PORTFOLIO_DD`. |
| `BROKER_NATIVE_STOPS=True`, stops rest at Alpaca | ✅ TRUE | `config.py:100`; topup path also replaces the broker stop (`main.py:1017-1020`). |
| Hard caps intact (60% gross, 25% sector, 10% name, correlation) | ✅ TRUE (with one bug, below) | `risk_manager.py` enforces gross + sector + correlation. |
| `MAX_POSITION_PCT` (10%) clamp is **absent** from sizing; a conv-9 ×1.15 would breach 10% | ✅ TRUE — and it's a real latent bug | `MAX_POSITION_PCT` is defined in `config.py:72` but **referenced nowhere** in `risk_manager.py`. The only clamp on `size_pct` is `min(size_pct, remaining_exposure)` (the 60% gross cap), never the per-name 10% cap. |
| C7 remnant delta-sizing exists and can be reused for pyramiding | ✅ TRUE (partially) | `risk_manager.py:178-238` does gap-only sizing for remnants; `main.py:950-1023` already has topup plumbing that updates qty + replaces the broker stop. |
| Cap binds ~⅓ of the time; system tolerated >8 holdings | ✅ Plausible/TRUE | `risk_manager.py:155` counts only `position_pct ≥ MIN_SLOT_PCT` names against `MAX_POSITIONS`. |
| MU (~$4.2k) held at Alpaca but missing from journal | ✅ TRUE | MU is **not** in `open_trades`. Confirmed. |

**So the prior doc is not making things up.** My disagreements are not about the code facts — they're about (a) a measurement gap it under-weights, and (b) the *risk ranking* of the levers, where the external evidence disagrees with its "pyramiding = lowest risk" framing.

---

## 4. Independent take on each lever

I ran external research on each lever (momentum/position-sizing literature, vol-targeting literature, cash-drag). Summary of what I found, lever by lever. Full citations in §7.

### Lever A — Pyramiding into winners *(prior doc: "headline lever, lowest incremental risk")*

**My verdict: do it, but ONLY in the de-risked form, and demote it from "lowest risk" to "medium risk, conditionally worth it." The prior doc's framing is too rosy and omits the critical mitigation.**

What the evidence actually says:
- The cleanest controlled study (Concretum 2024 — same trend signals, *only* sizing differs) found pyramiding **multiplied total return but LOWERED the Sharpe ratio**: annualized vol more than doubled, max drawdown deepened to ~49%, hit rate fell. Pyramiding is a **return-distribution-shaper, not a risk-adjusted-return improver.** It survives on rare fat-tail winners.
- **Byun & Jeon (FAJ 2023), "Momentum Crashes and the 52-Week High":** momentum crashes are driven specifically by stocks **near their 52-week highs.** Pyramiding *mechanically forces you to add to exactly those names* — i.e., it maximizes your exposure to the highest-crash-beta stocks right as a market tops. This is the opposite of "lowest incremental risk."
- **The cost-basis problem the prior doc ignores:** adding ~50% at +8–10% raises your *blended* average entry, which **shrinks the distance to your stop** and lets a normal pullback flip an aggregate winner into an aggregate loss. The universal practitioner mitigation (Turtles, Van Tharp anti-martingale): **only add once the original tranche is de-risked — i.e., move the original stop to breakeven/better first**, so it's the *add* that's at risk, not the whole position. The prior doc's "add ~50% once +8–10% above entry with trend intact" says nothing about this. Your current topup code (`main.py:1017-1020`) replaces the stop at the *existing* `stop_price`, so it does **not** move to breakeven. **Gap confirmed.**
- **Concentration under your caps is the binding issue:** with a 10%/name and 60% gross cap, pyramiding pushes 2–3 winners toward the 10% ceiling and eats your gross budget, crowding out new names and shrinking effective diversification from 6–11 toward a handful of correlated late-stage winners — peaking exactly when crash beta is highest.

**Net:** Pyramiding *can* help deployment and it *does* lean into the one thing your data shows works (winners run). But naive pyramiding is a Sharpe-*reducer* and a crash-concentrator. If you implement it: (1) **move the original stop to ≥breakeven before the add**, (2) cap total adds so one name can't dominate the 60% gross (e.g. adds may not push a name above 8% of NAV, leaving headroom under the 10% hard cap), (3) never add to a name already extended far past its 52-week high. With those three guardrails it's a reasonable EXP. Without them, skip it.

### Lever B — Raise `MAX_POSITIONS` 8 → 10–11 *(prior doc: "trivial, safe")*

**My verdict: AGREE — this is genuinely the best first move. It's the only lever that increases deployment by *diversifying* rather than *concentrating*.**

- More, smaller names = **lower** per-name risk and **lower** crash beta. This is the exact direction the momentum-crash literature says to go (diversify, don't concentrate).
- It's gated by the 60% gross and 25% sector caps, so it can't over-concentrate.
- It directly addresses your stated complaint (too few positions, too little deployed) without touching position *sizes* at all.
- The only caveat: more slots only helps if your funnel actually *produces* more qualifying names. Right now your zero-trade rate is high and `skip_precision` is only ~38–54%. So pair this with watching whether the extra slots actually fill with names that beat SPY (the baseline scorer — once fixed — will tell you).

I'd raise to **10, not 11**, as a first step — smaller change, still a third more slots, easier to attribute. Go to 11 later if 10 clearly fills and helps.

### Lever C — Symmetric vol-targeting (size UP in calm/low-VIX) *(prior doc: "easy, with one guardrail")*

**My verdict: PARTIALLY DISAGREE. Implement the DOWN-leg discipline and the missing 10% clamp, but do NOT size *up* in calm markets. The "lean in when calm" half is the weakest-supported idea in the whole doc.**

- The foundational paper (Moreira & Muir 2017) and the momentum-specific work (Barroso–Santa-Clara; Daniel–Moskowitz) get **almost all their benefit from the de-risking leg** — cutting exposure when volatility rises. That part is real and your engine already does it (`regime_mult`, `vix_mult` scale down). Good — keep it.
- The **up-leg** (adding leverage/size when vol is low) is where the literature is most skeptical. Cederburg et al. (2020), testing 103 strategies out-of-sample, found vol-management beat buy-and-hold in only **53/103 cases — a coin flip** — and robustly *only* for momentum-type factors. Sizing up in calm markets **loads maximum risk right before the regime flips** (volatility clusters, then spikes; you can't de-risk fast when liquidity evaporates).
- **Low VIX is itself a mild caution flag**, not a green light — extreme-low VIX has a complacency signature and poor forward-return compensation.
- **And it doesn't even hold right now:** the prior doc says "87% of runs were normal-VIX bull, lean in." But **VIX is ≈18.6 as of 2026-06-24** — near its long-run average and one tick below your own `VIX_ELEVATED_THRESHOLD = 20`. This is *not* a calm tape. Sizing up today would be sizing up into a near-elevated regime.
- **The one part of lever C you SHOULD ship regardless: the `min(size_pct, MAX_POSITION_PCT)` clamp.** This is a real latent bug (see §3). Even without any vol-up change, the absence of this clamp means a future conv-9 in a bull regime, or any multiplier >1.0, can silently breach your 10% per-name hard cap. **Fix the clamp as a bug, independent of the vol-targeting debate.**

**Net:** Split lever C. Ship the per-position clamp now (bug fix). Keep the existing down-only de-risking. **Do not add an up-multiplier in calm/low-VIX** — the evidence doesn't support it and the current tape doesn't qualify anyway.

### Lever D — Bump conviction→size map (conv-6 4→5%, conv-7 6→7%) *(prior doc: "highest risk, defer")*

**My verdict: AGREE COMPLETELY — defer it, and be even more skeptical than the prior doc.** Your own journal shows conviction is **not calibrated**: conv-6 (67% win, +6.14%) actually *beats* conv-7 (44% win, +8.37% inflated by the ARM artifact). The decision-quality scorecard you've saved says the same. Bumping the size map scales a signal that **carries no demonstrated information** — you'd be amplifying both the edge (if any) and the drawdown, off a 12-trade sample. The prior doc reaches the right conclusion here. Don't touch the size map until conviction→P&L is monotonic at ≥30 closed trades.

### The premise itself — "idle cash earns nothing, so low deployment forces the book to beat SPY ~3×"

**My verdict: directionally right but overstated — and important to understand correctly so you don't over-correct.**

- The arithmetic is **correct given the paper account's zero-cash assumption and ~28% deployment**: to match SPY when only ~28% is invested and cash earns 0%, the invested book must return ~3.6× the market. So the "handicap" is real *in this paper model* and *in a bull market*.
- **But it collapses the moment cash earns yield.** In reality (2026), short-term T-bills yield **~3.7%** and government money-market funds ~3.3–3.6%. A live version of this fund holding 70% cash would earn ~3.5–3.7% on that sleeve, not zero — so the real-world drag is the *equity-risk-premium-over-cash* (~4–6%) × the idle fraction, far less dramatic than "3× the market." Your paper account modeling cash at 0% **overstates** the handicap.
- **And in a flat/down tape (like the last 7 weeks), the cash sleeve HELPS** — it's why you're +2.97 pp vs SPY. Low deployment isn't purely a handicap; it's regime-dependent.
- **The dangerous over-correction the research warns against:** forcing deployment into your 7th–11th-best idea, or loosening the funnel to manufacture buys, **dilutes alpha** for a concentrated book. The right way to raise deployment is to **size and source more *qualifying* ideas**, not to deploy for deployment's sake. (The prior doc actually agrees with this in its anti-recommendations — good — but the framing "70% is dead weight" oversells it.)

**Net:** Raising deployment is a legitimate goal, but "get to 100%/60% as fast as possible" is not. Aim to lift deployment **by adding slots and modestly better-sizing qualifying names**, accept that some dry powder has option value, and stop treating cash as pure dead weight.

---

## 5. The thing the prior doc under-weights: your scoreboard is broken

This is the most important finding in my review, and the prior doc barely mentions it.

**EXP-002 (the deterministic baseline shadow arm) — which the audit itself called "the single most informative experiment" — is silently producing nothing usable.** The decision-quality scorecards you *have* saved show:

```
baseline_vs_pipeline:
  baseline: { n: 35, n_decided: 0,  avg_excess_pct: null }   ← BROKEN
  pipeline: { n: 17, n_decided: 13, avg_excess_pct: +7.74 }
```

`n_decided = 0` means **not one** of the 35 logged baseline picks ever got a forward-return score, so there is **still no answer** to the question that gates this entire deployment decision: *does your LLM debate layer actually pick better names than its own dumb screener?* The scorer in `core/counterfactuals.py::analyze_baseline` resolves `pw` (the 10-day forward excess) for the live pipeline but returns `None` for every baseline pick — almost certainly a date-window/parse mismatch between how `baseline_shadow` records are stamped versus how `trades` are (the baseline picks are keyed off run timestamps that fall inside the still-incomplete forward window, or the symbol/date forward-return lookup is misaligned for that record shape).

**Why this matters for deployment specifically:** every lever in the prior doc deploys *more capital through the LLM funnel*. If the funnel doesn't beat a fixed-size top-3 screener (we can't currently tell), then "deploy more" might just mean "lose more, faster, with more conviction." You should not scale capital into an engine whose edge you have not been able to measure — and the tool to measure it is built, running, and quietly broken. **Fixing it is freeze-exempt (it's a bug) and is the highest-leverage thing on this list.**

Two more measurement notes:
- `skip_precision` ≈ 38–54% and `trim_precision` ≈ 24–35% — your discretionary skip/trim calls are barely better than coin flips. Another reason to favor *structural* (more slots) over *discretionary-amplifying* (bigger conviction sizes) changes.
- MU missing from the journal means your deployment %, exposure, and sector math are all **slightly wrong right now** — fix before measuring any change, exactly as the prior doc says.

---

## 6. The plan — step by step (paste-ready for Claude Code)

This integrates the prior doc's good parts with my corrections. It respects the existing **parameter freeze** (one registered experiment at a time; bug fixes exempt). Order is deliberate: **measurement → diversify → careful pyramiding → de-risk clamp.** Defer amplifiers.

### Phase 0 — Repair the books and the scoreboard (freeze-exempt bug fixes; do these FIRST)

> Claude Code prompt for Phase 0:
>
> "Two freeze-exempt bug fixes, each its own commit, no strategy-parameter changes:
>
> **0a. Reconcile MU into the journal.** MU (~$4.2k) is held at Alpaca but missing from `open_trades` in `dashboard/data.json` (lost to the prior push/merge bug). Use `scripts/reconcile_untracked_positions.py` if it covers this; otherwise add MU to `open_trades` with its real Alpaca `avg_entry`, qty, sector, and a resting broker stop consistent with `HARD_STOP_PCT`/ATR. Also clean up the tiny HOOD remnant (~$85) if present. Verify deployment %, gross exposure, and sector sums recompute correctly afterward. Commit message must say `bugfix (freeze-exempt): reconcile MU/HOOD into journal`.
>
> **0b. Fix the broken baseline-vs-pipeline scorer.** In `core/counterfactuals.py::analyze_baseline`, `baseline.n_decided` is 0 while `pipeline.n_decided` is 13 — the baseline picks never get a forward-return score. Diagnose why `_forward_returns(...)` / `_excess(...)` returns `None` for every `baseline_shadow` pick: check (i) the date used for baseline picks (`rec['ts']`) vs trades (`entry_ts`) and whether baseline picks are being judged inside an incomplete forward window, (ii) the `(symbol, date)` keying and the `price_cache`/`_bars_by_date` lookup for baseline symbols, (iii) any dedup that drops everything. Add a unit test in `tests/` that feeds a synthetic `baseline_shadow` record dated far enough in the past and asserts `n_decided > 0` and a finite `avg_excess_pct`. Re-run `scripts/decision_quality.py --save` and confirm the baseline now reports a real `avg_excess_pct`. Commit message: `bugfix (freeze-exempt): baseline shadow forward-return scoring`.
>
> After 0b, report `baseline.avg_excess_pct` and `hit_rate_pct` vs the pipeline's. We need this number before deciding how aggressively to deploy."

**Decision gate after Phase 0:** look at baseline vs pipeline. If the pipeline's forward excess clearly beats the baseline's, proceed with confidence. If it's within noise or worse, be conservative — prefer Phase 1 (more slots) only, and treat Phases 2–3 as experiments to *measure*, not deployment you trust.

### Phase 1 — EXP-008: more slots (lowest risk; ship first among strategy changes)

> Claude Code prompt for Phase 1:
>
> "Register and implement EXP-008 in `docs/EXPERIMENTS.md` (use the template) and `config.py`:
>
> **Change:** `MAX_POSITIONS` 8 → **10** (single int). Nothing else.
> **Hypothesis:** the 8-slot cap binds ~⅓ of the time and forces idle cash; more slots raise deployment via *diversification* (more, smaller names = lower per-name and lower crash risk), gated by the unchanged 60% gross and 25% sector caps so it cannot over-concentrate.
> **Metric / success (≥6 weeks or until 60% gross is the binding constraint instead of slot count):** average deployment rises meaningfully (target band ~40–50%) AND the names filling the new slots have forward 10/20-day SPY-relative returns ≥ 0 on the (now-fixed) scorer AND no rise in max drawdown beyond the −10% breaker.
> **Failure:** new slots fill with names that underperform SPY on the scorer, or sit empty (funnel, not slots, is the constraint) → revert to 8.
> **Min sample:** 6 weeks / 15 new-slot entries. **Rollback:** `MAX_POSITIONS=8`.
>
> Confirm `risk_manager.py:155` still counts only `position_pct ≥ MIN_SLOT_PCT` names against the cap, so remnants don't consume the new slots. Add/extend a `tests/test_risk_manager.py` case asserting the 9th and 10th meaningful position are allowed while the 11th is skipped, and that gross/sector caps still bind first when relevant."

### Phase 2 — EXP-007: pyramiding, the de-risked way ONLY (medium risk)

> Claude Code prompt for Phase 2:
>
> "Register and implement EXP-007. This is the careful, evidence-based form of adding to winners — **not** naive pyramiding. Reuse the existing C7 delta-sizing (`risk_manager.py:178-238`) and topup plumbing (`main.py:950-1023`), but add the guardrails the momentum literature requires.
>
> **Trigger:** a *meaningful* held position (≥ MIN_SLOT_PCT) that is **≥ +8% above its `avg_entry`** with trend intact (price > EMA10 > EMA30), may receive ONE add.
>
> **Non-negotiable guardrails (these are the whole point):**
> 1. **De-risk first.** Before/at the add, MOVE THE ORIGINAL BROKER STOP UP to **≥ breakeven on the blended `avg_entry`** (currently `main.py:1017-1020` replaces the stop at the *existing* `stop_price` — change it so a topup ratchets the stop to at least breakeven on the new blended basis). The add must never be able to turn the *aggregate* position into a loss.
> 2. **Cap the concentration.** An add may not push the position above **8% of NAV** (leave headroom below the 10% `MAX_POSITION_PCT` hard cap). Size the add as the GAP to 8% max, not a fresh full position.
> 3. **No adding to blow-offs.** Skip the add if the name is extended far past its 52-week high (e.g. within X% of, or > Y% above, the 52-wk high) — momentum crashes concentrate in near-52-wk-high names (Byun & Jeon 2023). Pick a conservative threshold and journal it.
> 4. All existing caps (sector 25%, gross 60%, correlation tournament) still apply to the add.
>
> **Hypothesis:** adding to *confirmed, de-risked* winners raises deployment using proven names without raising aggregate downside (because the stop is at breakeven before the add).
> **Metric / success (≥10 adds):** adds have forward 10/20-day SPY-relative returns ≥ core entries AND no add results in the *aggregate* position closing below blended breakeven AND book-level max drawdown does not worsen.
> **Failure:** adds round-trip through breakeven (guardrail 1 failing), or concentration/crash risk shows up as deeper drawdowns → disable.
> **Min sample:** 10 adds. **Rollback:** config flag `PYRAMID_ADDS=False`.
>
> Add `tests/test_risk_manager.py` cases: (a) a +10% winner gets an add sized only up to 8% NAV; (b) the original stop is ratcheted to ≥ breakeven on blended basis; (c) a near-52-wk-high name is refused; (d) an add that would breach the sector or gross cap is refused.
>
> **If implementing guardrail 1 cleanly is hard, STOP and tell me — I would rather not ship pyramiding at all than ship it without the breakeven-stop mitigation.**"

### Phase 3 — EXP-009: the clamp + down-only vol discipline (NOT size-up)

> Claude Code prompt for Phase 3:
>
> "Two parts. Part A is a freeze-exempt bug fix; Part B is the registered experiment.
>
> **Part A (bug fix, freeze-exempt): add the missing per-position clamp.** In `agents/risk_manager.py`, after all multipliers and shading, clamp the per-name size: `size_pct = min(size_pct, config.MAX_POSITION_PCT)` BEFORE the `min(size_pct, remaining_exposure)` line (currently `:192`). `MAX_POSITION_PCT` (0.10) is defined but referenced nowhere, so today a multiplier >1.0 or a conv-9 could silently breach the 10% per-name hard cap. Add a `tests/test_risk_manager.py` case proving no proposal ever exceeds `MAX_POSITION_PCT` of NAV. Commit: `bugfix (freeze-exempt): enforce MAX_POSITION_PCT per-position clamp`.
>
> **Part B (EXP-009): keep down-only vol de-risking; do NOT add a calm-market up-multiplier.** Register EXP-009 documenting the decision: the engine already scales size DOWN in caution/bear and elevated/high-VIX regimes (`risk_manager.py:106-108`) — keep that. **Do not raise the bull/normal multiplier above 1.0.** Rationale to record in the registry entry: the academic benefit of volatility management comes from the de-risking leg, not from leveraging up in calm markets (Cederburg et al. 2020 found vol-management beats buy-and-hold out-of-sample only ~half the time and robustly only for momentum; sizing up in low-VIX loads risk right before vol spikes). If you ever revisit a vol-up multiplier, it must ship *with* Part A's clamp and be a separate registered experiment with a pre-stated drawdown failure threshold.
>
> Net effect of Phase 3: the per-name hard cap becomes real (safety), and we explicitly decide NOT to size up in calm markets (evidence-based restraint)."

### Phase 4 — DEFERRED: EXP-010 conviction→size bump

Do **not** implement until conviction→P&L is monotonic at **≥30 closed trades** on the fixed scorecard. Current data shows conv-6 beating conv-7 — the signal is uncalibrated, so bumping the size map scales noise. Revisit only with the data.

### What to explicitly NOT do (carried over + reinforced)

- Don't loosen the debate, lower `MIN_CONVICTION`, or shorten cooldowns to force buys — that raises deployment by buying *worse* names (violates Goal 3; the research is explicit that forcing deployment dilutes alpha).
- Don't raise `MAX_PORTFOLIO_EXPOSURE` above 0.60 — not binding at ~28% deployment; revisit only if you're actually pressing 60%.
- Don't size up in calm/low-VIX (lever C up-leg) — see Phase 3.
- Don't bump the conviction size map (lever D) — see Phase 4.

---

## 7. Why this works — in plain English

Your fund's job is to beat the S&P. Right now ~70% of your money sits in cash, and in this paper account that cash earns nothing — so on paper your invested quarter has to massively outrun the market just to keep up. That part of the prior advice is true. (Two honest caveats: in the *real* world cash earns ~3.7% today, so the handicap is smaller than "3× the market" makes it sound; and in a flat or falling market — like the last seven weeks — your cash actually *helped* you beat SPY. So idle cash is a bull-market drag, not pure dead weight.)

The instinct to "just buy more" is dangerous because it means buying your weaker ideas, and that's how funds bleed. So the question is how to put more money to work *without lowering the bar*. The prior advice and I agree on the safest answer: **give yourself a few more slots.** You hit your 8-position ceiling about a third of the time. Going to 10 lets you hold more names, each a bit smaller — which actually *lowers* your risk per name. That's the cleanest win, so it goes first.

Where I disagree with the prior advice is on its "headline" idea — **piling more into your biggest winners.** It sounds great (your winners really do run), but the cleanest study on this finds it *raises* your returns and your *drawdowns* together and actually *lowers* your risk-adjusted quality, because it shovels money into the stocks that have run the furthest — which are precisely the ones that crash hardest when momentum turns. The fix isn't to ban it; it's to do it the way professional trend-followers do: **only add to a winner after you've moved its stop up to break-even, so the new money can't drag the whole trade into a loss, and never let one name balloon.** The prior write-up skipped that safety step entirely, and your current code doesn't do it. So: do it, but do it carefully — or not at all.

I also pushed back on "lean in harder when the market is calm." The research is clear that the *protective* half — getting smaller when things get stormy — is what actually helps; the *aggressive* half — getting bigger when things are calm — mostly just leaves you maximally exposed right before the next storm. And as of today the market isn't even calm (the fear gauge is near its normal level, basically at your own caution line), so there's nothing to lean into. The one genuinely useful piece hiding inside that idea is a **bug fix**: your code has a 10%-per-stock ceiling written down but never actually enforced, so a future high-conviction trade could quietly blow past it. We should just fix that.

And the one thing we both agree to *wait* on: **making every position bigger across the board.** That only pays off if your conviction score reliably predicts which trades win — and right now your own record shows it doesn't (your conviction-6 trades have actually done *better* than your conviction-7s). Until you've got enough trades to prove the score means something, turning up the size dial just turns up the noise.

Finally, the most important thing neither the prior write-up nor your dashboard is telling you: **you built a tool to check whether your fancy AI stock-picker actually beats a dumb checklist, and that tool is quietly broken** (it's scored zero of its 35 test picks). Before you pour more money through the AI funnel, fix that tool and look at the answer. If the AI genuinely beats the simple version, deploy more with confidence. If it doesn't, you'll have just saved yourself from confidently losing money faster. That's why "fix the scoreboard" comes before everything else.

---

## 8. One-page summary of agreements & disagreements

| Item | Prior doc | My review | Net |
|---|---|---|---|
| Problem is real (low deployment, $4k conv-6 buys) | Yes | **Confirmed in live data** (~28% deployed, 5/6 holdings conv-6) | ✅ Agree |
| Pre-step: reconcile MU + clean HOOD | Yes (freeze-exempt) | Agree, confirmed MU missing | ✅ Agree |
| **Fix broken baseline scorer (EXP-002)** | Not mentioned | **Highest-leverage item — scorer is silently dead (`n_decided=0`)** | ⚠️ **My addition** |
| Raise `MAX_POSITIONS` 8→11 | EXP-008, "trivial/safe" | Agree it's the best lever; suggest **8→10 first** | ✅ Agree (minor tweak) |
| Pyramiding into winners | EXP-007, "headline, lowest risk" | **Medium risk, not lowest. Require breakeven-stop + 8% cap + no-blow-off guardrails. Missing in their version.** | 🟡 Conditional |
| Vol-targeting UP in calm | EXP-009, "easy w/ guardrail" | **Don't size up; evidence backs only the down-leg. VIX≈18.6 now — not calm anyway.** | ❌ Disagree on up-leg |
| `min(size_pct, MAX_POSITION_PCT)` clamp | Bundled into EXP-009 | **Ship as standalone bug fix — it's a real latent cap breach** | ✅ Agree (de-bundle) |
| Conviction→size bump | Lever D, defer | **Agree, defer harder — conv-6 beats conv-7 in your data** | ✅ Agree |
| "70% cash is dead weight / beat SPY 3×" | Core framing | **Overstated: cash≈3.7% live, and it helped you in this down tape** | 🟡 Partial |

---

## 9. Sources

External research (full briefs available on request):

- Daniel & Moskowitz, "Momentum Crashes," *JFE* 2016 — https://www.nber.org/system/files/working_papers/w20439/w20439.pdf
- Byun & Jeon, "Momentum Crashes and the 52-Week High," *Financial Analysts Journal* 2023 — https://www.cfainstitute.org/en/research/financial-analysts-journal/2023/2183706
- Concretum Group, "Position Sizing in Trend-Following: Volatility Targeting vs Parity vs Pyramiding" (2024) — https://concretumgroup.com/position-sizing-in-trend-following-comparing-volatility-targeting-volatility-parity-and-pyramiding/
- Swedroe / Alpha Architect, "Reducing the Risk of Momentum Crashes" — https://alphaarchitect.com/risk-of-momentum-crashes/
- Asness, Frazzini, Israel & Moskowitz, "Fact, Fiction and Momentum Investing" (AQR) — https://www.aqr.com/Insights/Research/Journal-Article/Fact-Fiction-and-Momentum-Investing
- Van Tharp, position sizing / anti-martingale — https://vantharpinstitute.com/van-tharp-teaches-position-sizing-strategies-and-risk-management/
- Moreira & Muir, "Volatility-Managed Portfolios," *Journal of Finance* 2017 — https://amoreira2.github.io/alan-moreira.github.io/VolPortfolios_published.pdf
- Cederburg, O'Doherty, Wang & Yan, "On the Performance of Volatility-Managed Portfolios," *JFE* 2020 — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3357038
- Barroso & Santa-Clara / dynamic momentum (Sharpe ~doubles) — https://www.sciencedirect.com/science/article/abs/pii/S1042443118303093
- AQR, "Chasing Your Own Tail (Risk), Revisited" — https://www.aqr.com/-/media/AQR/Documents/Insights/White-Papers/AQR-Chasing-Your-Own-Tail-Risk-Revisited.pdf
- Cliff Asness, "Doing Nothing Is Surprisingly Often the Right Strategy" (cash optionality) — https://acquirersmultiple.com/2025/09/cliff-asness-doing-nothing-is-surprisingly-often-the-right-strategy/
- Bogleheads, "Cash drag" — https://www.bogleheads.org/wiki/Cash_drag
- Federal Reserve H.15 Selected Interest Rates (June 2026) — https://www.federalreserve.gov/releases/h15/
- FRED, 1-Month Treasury Constant Maturity (DGS1MO) — https://fred.stlouisfed.org/series/DGS1MO
- VIX current level (≈18.6, 2026-06-24) — https://fred.stlouisfed.org/series/VIXCLS

Internal evidence (this repo): `dashboard/data.json` (snapshots, trades, decision_quality, baseline_shadow), `agents/risk_manager.py`, `main.py`, `config.py`, `core/counterfactuals.py`, `docs/FABLE_STRATEGY_AUDIT.md`, `docs/EXPERIMENTS.md`.
