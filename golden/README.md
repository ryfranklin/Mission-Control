# Phase 2 golden set

Curated worker tasks for the eval harness. Each task runs through the **existing**
Flight Director → Controller path (`Orchestrator.run_task`, `sim` and `burn`
behind go/no-go) against the sandbox target in `sandbox/`. The golden set does
**not** change the dispatch path or the worker — it only describes tasks and what
"good" looks like.

```
golden/
  README.md          ← this file
  sandbox/           ← the target repo the tasks run against (a fixture, not a git repo)
  tasks/             ← one YAML spec per task
```

## How a harness is expected to run a task (not built here)

For each spec: materialize `sandbox/` into a temp dir, `git init` + commit it
(the baseline), then dispatch the task via the existing runtime —
`Orchestrator(target, worker=SdkWorker()).run_task(Task(...), approval=<gate>)`
— mapping `task_type` → `TaskType` and `approval` → the go/no-go callback. After
the run, score the `known_good` block against `RunResult` + the target repo's git
state + `worker_result.summary`. Nothing in the dispatch path needs to change.

## The sandbox target (`sandbox/`)

A tiny arithmetic library. **Baseline test state: 2 passing, 1 failing** — the
failing test is the intentional `multiply` bug. That red baseline is deliberate:
it lets tasks assert both `goes_green` (fix it) and `stays_green` (don't regress
the passing tests). It also carries an AI-DLC install (`AGENTS.md` +
`.aidlc-rule-details/`), so the probe detects `flavor=generic` and INCEPTION/
CONSTRUCTION tasks exercise the steering path.

## Spec format

```yaml
id: <slug>                 # unique; matches the filename
task_type: sim | burn      # sim = read-only, burn = side-effectful (TaskType values)
greenfield: true | false   # true adds the "Using AI-DLC, …" opener when steering is detected
approval: go | no-go       # BURNS ONLY: the gate decision the eval applies

prompt: |
  <the task prompt handed to the worker>

known_good:
  # 1) Deterministic — checkable WITHOUT a model, from RunResult + git + output.
  deterministic:
    outcome: completed | blocked      # RunResult.outcome
    applied: true | false             # RunResult.applied (were changes merged)  [optional]
    decision: go | no-go              # RunResult.decision                        [optional]
    no_changes: true | false          # true => target byte-identical to baseline
    files_touched: [ <glob>, ... ]    # each glob MUST match ≥1 changed path
    files_unchanged: [ <glob>, ... ]  # none of these paths may change
    tests: ignore | stays_green | goes_green
    output_contains: [ <str>, ... ]   # substrings required in worker_result.summary
    output_absent:   [ <str>, ... ]   # substrings that must NOT appear

  # 2) Judge rubric — ONLY the parts that aren't deterministically checkable
  #    (quality of analysis, correctness of reasoning). Empty ⇒ deterministic-only.
  judge_rubric:
    - criterion: <short statement of what a good answer does>
      weight: <int>
```

### Field semantics

- **`no_changes: true`** overrides the glob lists — the whole target tree must be
  unchanged (read-only sims, and blocked burns whose diff was discarded).
- **`files_touched`** — every glob must match at least one changed file (relative
  to the baseline commit). **`files_unchanged`** — matching files must be
  byte-identical to baseline.
- **`tests`** (delta semantics against the 2-pass/1-fail baseline):
  - `stays_green` — no test that **passed at baseline** may fail (regression guard).
    Pre-existing baseline failures are ignored.
  - `goes_green` — the **full** suite passes (baseline failure fixed, no new breaks).
  - `ignore` — test state is not a signal for this task.
- **`output_contains` / `output_absent`** — plain substring checks on the worker's
  reported summary.

### Deterministic vs. judge — the split

Put anything mechanically checkable in `deterministic`; put **only** what needs
human/model judgement (was the analysis correct? is the reasoning sound?) in
`judge_rubric`. If a task is fully pinned by asserts, its `judge_rubric` is `[]`.

**Judge scoring (provisional):** an LLM judge scores each criterion 0.0–1.0; the
task's judge score is the weight-weighted mean. A suggested pass threshold is
**≥ 0.7** — provisional and Phase-2-tunable, not settled.

## LLM judge (implemented)

The runner (`mission_control.evals`) invokes an LLM judge (`mission_control.judge.LlmJudge`)
**only for tasks with a non-empty `judge_rubric`** — deterministic-only tasks never
call the judge, so we don't pay for it.

- **Model:** configurable; **defaults to a stronger tier than the worker**
  (`claude-opus-4-8` judge vs. the `claude-haiku-4-5` worker). It runs through the
  same Claude Agent SDK path (reusing the worker's auth) with `setting_sources=[]`
  and no tools — a pure scoring call.
- **Output:** the judge scores each criterion 0..1; `quality_judge` is the
  weight-weighted mean, returned with a rationale.
- **`quality_total` weighting:** `0.5 · quality_deterministic + 0.5 · quality_judge`.
  Equal weight is a **deliberate, provisional** choice — deterministic asserts are
  higher-confidence than the judge, so this may shift toward the deterministic side
  once judge reliability is characterized. For deterministic-only tasks (no rubric),
  `quality_total == quality_deterministic`.
- **Cost visibility:** the judge is not free. Its own token usage is priced through
  the telemetry module and reported **separately** on each `EvalResult`
  (`judge_model`, `judge_cost_usd`, `judge_tokens`) and in the run summary
  (`judge_cost_usd`, `judge_tokens`) — never folded into the worker's `cost_usd`.

### ⚠️ Judge reliability is a finding, not a solved problem

An LLM judge is itself a noisy instrument, and it does not come with a correctness
guarantee. The meta-eval (`tests/test_judge.py`, live, gated by `MC_LIVE_JUDGE=1`)
hand-scores a few outputs and asserts the judge lands within a **loose tolerance
(±0.25)** of the human score — that tolerance is a *drift detector*, not a claim of
accuracy. Treat `quality_judge` as directional; re-run the meta-eval when the judge
model or rubric changes, and tighten the tolerance only once you've measured
run-to-run variance. Do not gate anything important on a single judge score.

## Baseline & regression check (`mission_control.baseline`)

Because the worker (and judge) are noisy, a single run can't tell a regression
from variance. So we **measure the variance first**:

- **`build_baseline(...)`** runs the full golden set **N times** (default 5) and
  records, per task and for the whole suite, the `mean / stddev / min / max` (and
  raw samples) of `quality_total` and `cost_usd`. It persists **`baseline.json`**
  (committed, diffable) — the aggregate axes are per-run *mean quality* and per-run
  *total cost*.
- **`check_regression(...)` / `check_run(...)`** compare a new run to the baseline
  and flag a regression **only outside the k·stddev noise band** (k default 2):
  quality below `mean − k·stddev`, or cost above `mean + k·stddev`. A drop inside
  the band is treated as variance, not a regression — that's the whole point.
- **`RegressionReport.report()`** prints, per axis, `baseline mean ± stddev`, the
  current value, the threshold, and pass/REGRESSION; plus any per-task regressions.

Not wired into CI (Phase 3) — it's a callable check + report. Rebuild the baseline
(`python -m mission_control.baseline [N]`) whenever the worker/judge model, the
golden set, or the sandbox changes; a stale baseline makes the band meaningless.
`stddev` from N=5 is itself a coarse estimate — see the noisy-signal caveat below.

## Task inventory (8 tasks: 4 sim / 4 burn; 4 deterministic-only)

| task | type | gate | key deterministic signal | judge? |
|------|------|------|--------------------------|:------:|
| `sim-list-public-functions` | sim | — | no changes; output lists `add`, `multiply` | — |
| `sim-count-tests` | sim | — | no changes; output == `3` | — |
| `sim-diagnose-multiply-bug` | sim | — | no changes; mentions `multiply` | ✓ root-cause |
| `sim-inception-scope` | sim | — | no changes (AI-DLC INCEPTION, greenfield) | ✓ scope quality |
| `burn-fix-multiply-go` | burn | go | `calc.py` touched; tests **goes_green** | — |
| `burn-add-subtract-go` | burn | go | `calc.py` touched; tests **stays_green** | ✓ correctness |
| `burn-add-docstring-nogo` | burn | no-go | **blocked**; target unchanged | — |
| `burn-inception-docs-go` | burn | go | `aidlc-docs/**` created; code untouched | ✓ doc quality |

## ⚠️ CAVEAT — this signal is NOISY

**A ~6–10 item golden set is a NOISY signal.** Treat pass/fail rates from this set
as directional, not authoritative:

- **Small n.** One flaky run swings the rate by ~10–15 percentage points. Do not
  read a single run as a regression or an improvement.
- **Non-determinism upstream.** The worker is a live LLM; identical specs vary run
  to run (wording, tool choices, verbosity). Deterministic asserts absorb some of
  this; the judge rubric does not.
- **Substring/glob asserts are coarse.** `output_contains` can pass on a wrong
  answer that happens to contain the string, and fail on a right answer phrased
  differently. Expect both false passes and false fails.
- **Judge variance.** The rubric threshold (0.7) is a guess; the judge model adds
  its own noise on top of the worker's.

**The right sample size is itself a Phase 2 finding, not a settled number.** Use
this set to shake out the harness and the deterministic/judge split; grow it (and
add repeated trials per task) before trusting any aggregate number.
