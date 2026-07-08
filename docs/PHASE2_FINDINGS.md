# Phase 2 findings — eval harness, baseline & noise

Data-driven notes from running the full harness end-to-end: golden set (8 tasks) →
runner → judge → **N=5** baseline → regression report. Worker = Haiku 4.5,
judge = Opus 4.8.

> **n-size caveat (read first):** 8 golden items × 5 repeats. That is enough to
> *see* the shape of the noise, not to pin it down. Every stddev below is itself a
> 5-sample estimate (df=4 → the stddev is uncertain by roughly ±30–50%). Treat
> these as directional; they are a Phase 2 finding, not settled numbers.

## Measured run-to-run noise band (aggregate)

| metric | mean | stddev | min–max | CV |
|--------|-----:|-------:|--------:|---:|
| `quality_total` (per-run mean) | 0.906 | 0.013 | 0.888–0.923 | **1.4%** |
| `cost_usd` (per-run worker total) | $0.034079 | $0.001640 | $0.0324–$0.0365 | **4.8%** |

The **aggregate** signal is tight — averaging 8 tasks cancels most per-task noise.

## Per-task tells a different story

| task | quality_total mean ± stddev |
|------|-----------------------------|
| sim-list-public-functions | 1.000 ± 0.000 |
| sim-diagnose-multiply-bug | 1.000 ± 0.000 |
| sim-inception-scope | 1.000 ± 0.000 |
| burn-fix-multiply-go | 1.000 ± 0.000 |
| burn-add-docstring-nogo | 0.800 ± 0.000 |
| burn-inception-docs-go | 0.874 ± 0.013 |
| **sim-count-tests** | **0.950 ± 0.112** |
| **burn-add-subtract-go** | **0.622 ± 0.077** |

Two items carry almost all the variance (CV ~12%); five showed **zero** spread
over 5 runs. Note "zero over 5 runs" ≠ deterministic — it's 5 clean draws, not a
proof. **Per-task bands from n=5 are not yet trustworthy**: the low-variance tasks
look falsely airtight, and the noisy ones have bands too wide to catch a real drop
(add-subtract's k=2 lower bound is 0.47 — a fall to 0.5 wouldn't flag).

## How much signal it took

- **Aggregate quality**: usable at n=5. But it's *barely* usable — a real observed
  run hit 0.888, only 0.008 above the k=2 threshold (0.880). See k discussion.
- **Per-task**: **not** trustworthy at n=5. The noisy tasks would need ~15–20
  repeats before their stddev stabilizes enough to set a per-task band.
- **8 items is small.** One noisy item (add-subtract) moves the aggregate mean
  by itself. The set shakes out the harness; it is not a benchmark.

## What threshold `k` the noise justifies

The observed extremes sit ~1.4σ from the mean over just 5 runs:
`(0.906 − 0.888)/0.013 = 1.38σ` (quality), `(0.0365 − 0.0341)/0.00164 = 1.46σ` (cost).

- **k = 1 is too tight** — it would have false-flagged the real 0.888 run.
- **k = 2 is the practical floor** and is what we default to; the real run cleared
  it by a hair on both axes.
- Given the small-n stddev uncertainty, **k = 3 is the safer choice** for the
  aggregate if false positives are costly. Per-task, don't gate on k until repeats
  are increased.

The check correctly separated real drops from variance: a synthetic −0.30 quality
drop flagged the **quality** axis only; a 3× cost spike flagged the **cost** axis
only; the real observed run passed both.

## Cost baseline by task_type (worker) — and the judge surprise

| | worker cost / task (mean) |
|---|---:|
| `sim` (4 tasks) | **$0.005007** |
| `burn` (4 tasks) | **$0.003513** |

Read-only **sims cost more than side-effectful burns** (verbosity, not mutation,
drives worker cost — consistent with Phase 1). The dearest task is the read-only
`sim-diagnose-multiply-bug` at $0.0085.

**⚠️ Surprise — the judge dominates eval cost.** Per run: worker $0.0341,
**judge $0.1362 → 4.0× the entire worker suite.** Each Opus judgement of a rubric
task ($0.030–$0.037) costs ~6–15× the Haiku worker call it grades. The baseline's
`cost_usd` axis tracks **worker only**; judge cost is captured separately
(`judge_cost_usd`) and is **not** in the regression band — a gap worth closing if
eval spend is monitored.

## Judge reliability (meta-eval)

The live meta-eval (3 hand-scored fixtures) had the Opus judge land **within ±0.25
of the human score on all 3** (good≈0.9, bad≈0.1, partial≈0.5). That tolerance is
a **drift detector, not an accuracy claim** — 3 fixtures is too few to characterize
bias or variance. Corroborating signal from the baseline: judged tasks were stable
run-to-run (diagnose/inception-scope scored 1.000 ± 0.000 across 5 runs), i.e. the
judge was self-consistent here — but self-consistency is not correctness.

## Surprising, in one place

1. **Judge cost is 4× worker cost** and lives outside the regression band.
2. **Read-only sims cost more than burns** ($0.0050 vs $0.0035 worker).
3. **"Deterministic" ≠ noise-free**: `sim-count-tests` (a substring check for "3")
   was the *second*-noisiest task — the *worker's* free-form output varied, and the
   deterministic assert inherited that noise. Determinism of the *check* doesn't
   remove nondeterminism of the *worker*.
4. **Aggregate hides instability**: 0.906 ± 0.013 in aggregate masks a 0.62 ± 0.08
   task. Aggregate regression detection is trustworthy at n=5; per-task is not.

## Recommendation

Use the aggregate band with **k=2 (min) / k=3 (safe)** for now. Before trusting
per-task gates or tightening k, raise repeats to ~15–20 on the noisy items and
grow the golden set past 8. Add judge cost to the tracked cost axis. Re-baseline
on any worker/judge/model change — a stale band is worse than none.
