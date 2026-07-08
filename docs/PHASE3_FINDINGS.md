# Phase 3 findings — CI gate, total-cost baseline, per-task trust

The gate is wired end-to-end (golden set → runner → judge → baseline → gate CLI →
Jenkins). Re-baselined at **16 tasks × N=15**, worker = Haiku 4.5, judge = Opus 4.8,
with **cost_usd = worker + judge** (the G3 fold-in).

> **n-size honesty:** N=15, so every stddev is a 15-sample estimate — `SE(stddev)
> ≈ ±19%` (`1/√(2(n-1))`). 16 tasks is still smallish. "0 variance over 15 runs"
> is strong evidence, not proof of determinism. Trust the **aggregate** band;
> trust **per-task** only for the tasks flagged gate-ready below.

## Gate flow end-to-end (demonstrated)

| scenario | NONPROD | PROMOTE | exit |
|---|---|---|---|
| clean | **green** | manual go/no-go offered → `go` → deploy | 0 |
| regressed | **red** (cost axis) | **never offered** | 1 |

## Re-baseline — judge cost folded in (before / after)

`cost_usd` used to track worker spend only; the Opus judge sat outside the band.
Per run over the 16-task suite:

| tracked cost axis | per-run cost |
|---|---|
| BEFORE (worker only) | **$0.072** |
| AFTER (worker + judge) | **$0.308** |
| — of which judge | $0.236 = **77% of total, 3.3× the worker** |

Folding the judge in **4.3×'d the gated cost** — the previously-invisible 77% is
now what the gate actually watches. (Consistent with Phase 2's 8-task finding:
judge ≈ 4× worker.) Aggregate baseline: **quality 0.906 ± 0.015** (CV 1.7%),
**cost $0.3079 ± $0.0093** (CV 3.0%).

## Per-task gating trustworthiness (quality axis, k=2)

**12/16 tasks are per-task gate-ready; 4 are aggregate-only.**

| not gate-ready | quality mean ± stddev | CV |
|---|---|---|
| burn-add-subtract-go | 0.629 ± 0.177 | **28%** |
| burn-add-power-go | 0.768 ± 0.082 | 11% |
| burn-inception-docs-go | 0.831 ± 0.088 | 11% |
| burn-add-strings-title-go | 0.805 ± 0.070 | 9% (borderline) |

The other **12** are gate-ready: 11 at **zero** quality variance over 15 runs
(all deterministic-only tasks + `sim-diagnose`/`sim-explain` where the judge
scored 1.000 every run) and `sim-inception-scope` at CV 5%.

**The split is entirely a judge phenomenon.** Every non-gate-ready task is a
rubric/judge-scored burn; every deterministic-only task has 0 quality variance.
The soft signal is the judge, not the asserts.

## Did ~15–20 repeats make per-task bands trustworthy? Partly — refuted as stated.

Phase 2 (n=5) guessed per-task bands would firm up at ~15–20 repeats. At N=15:
- It **reliably separates** gate-ready from not, and pins the stddev to ±19%.
- But it does **not rescue** inherently-noisy tasks. `burn-add-subtract-go` went
  **0.62 ± 0.077 (CV 12%) at n=5 → 0.63 ± 0.177 (CV 28%) at n=15** — more data
  revealed *more* variance; n=5 had **under-estimated** it.

So: N=15 makes low-variance tasks gate-ready and trustworthy-flags the rest — it
doesn't make a genuinely noisy judge-scored task gateable. Those stay aggregate-only.

## How the soft judge signal is kept from flaking the build

- **Gate on the aggregate.** Judge noise concentrates in ~4 tasks; averaged over
  16 it collapses to CV 1.7% (quality). The aggregate is stable even though
  individual tasks aren't.
- **k·stddev band, not a point.** Quality fails only below `0.906 − k·0.015`
  (0.876 at k=2); a wobble inside is variance, not a regression. `k=3` for a
  more conservative gate.
- **N repeats** (`--n`) average the current run before comparing, shrinking its
  variance further.
- Per-task numbers are reported for triage but **do not set the exit code**.

## Exit-code contract → Jenkins (clean mapping)

One process exit code carries the whole decision: `eval-gate` exits `0`/nonzero;
Jenkins `sh` + `set -o pipefail` turns nonzero into a failed **NONPROD** stage,
which aborts the pipeline so the manual **PROMOTE** (go/no-go `input`) is never
reached. No plugin, no parsing — the CLI is the contract, Jenkins just runs it.
The JSON result + human report are archived regardless (`post { always }`).

## Surprising

1. **You're mostly gating the judge, not the work** — 77% of tracked cost is the
   Opus judge (3.3× the worker). The fold-in more than quadrupled the gated cost.
2. **More repeats made the worst task *noisier*** (add-subtract CV 12% → 28%).
   Small-n optimism is real; n=5 lied about that task's stability.
3. **Widening didn't move aggregate quality** — 0.906 at 8 tasks *and* at 16.
   Doubling the set tightened confidence and exposed per-task structure without
   shifting the headline number.
4. **sim vs burn cost barely differs** ($0.0176 vs $0.0209/task) — cost is driven
   by whether a task carries a judge, not by read-only vs side-effectful.

## Recommendation

Gate on the **aggregate** with **k=2 (min) / k=3 (safe)**; the 12 gate-ready tasks
may additionally gate per-task. Leave the 4 judge-noisy burns as aggregate-only
(or reduce their judge dependence). Re-baseline on any worker/judge/golden/sandbox
change. Grow the set past 16 and raise N before tightening k.
