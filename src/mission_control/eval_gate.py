"""Eval gate — the CI contract. Jenkins just calls this.

Runs the eval suite, compares the run to ``baseline.json``, and **exits 0 on pass
/ nonzero on regression** — nonzero if aggregate quality regressed OR total cost
(worker + judge) regressed. Emits both a machine-readable JSON result and a human
report (per-metric baseline mean±stddev, current run, pass/regression per axis).

    python -m mission_control.eval_gate            # or the `eval-gate` console script
    eval-gate --k 2 --n 3 --json gate.json

## The soft-signal problem (why the exit code is stable)

The worker and (especially) the Opus judge are noisy — a single run's aggregate
score wobbles. If we gated on a raw single run against a point target, the exit
code would flap and teams would stop trusting it. Three deliberate choices keep
it stable:

1. **Gate on the k·stddev noise band, not a point.** The baseline stores the
   run-to-run stddev; a result only fails if it's beyond ``mean ∓ k·stddev``.
   A drop *inside* the band is variance, not a regression. Raise ``k`` to widen
   the band (fewer false positives; default 2, ``k=3`` for a conservative gate).
2. **Average N repeats before comparing.** ``--n`` runs the suite N times and
   compares the mean; more repeats shrink the current estimate's variance, so the
   average is even less likely to breach the (single-run) band. Raise ``N`` when a
   run is cheap and flapping matters; keep it low to save spend.
3. **Gate on the aggregate, not per-task.** Per-task bands aren't trustworthy yet
   (high-variance tasks have bands too wide to gate; see docs/PHASE3_FINDINGS.md),
   so the exit code is driven by the whole-suite aggregate, where averaging across
   tasks cancels most per-task noise. Per-task numbers are reported for triage but
   do not set the exit code.

Re-baseline whenever the worker/judge model, the golden set, or the sandbox
changes — a stale band makes the gate meaningless.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .baseline import Baseline, _run_cost, _run_quality, check_regression
from .evals import run_eval
from .judge import LlmJudge
from .sdk_worker import SdkWorker
from .worker import StubWorker


@dataclass
class GateResult:
    passed: bool
    k: float
    n: int
    current_quality: float
    current_cost: float
    report: object  # RegressionReport
    runs: list[dict] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if self.passed else 1

    def to_json(self) -> dict:
        def axis(a) -> dict:
            return {
                "current": round(a.current, 8),
                "baseline_mean": a.baseline_mean,
                "baseline_stddev": a.baseline_stddev,
                "threshold": a.threshold,
                "higher_is_worse": a.higher_is_worse,
                "regressed": a.regressed,
            }

        return {
            "passed": self.passed,
            "exit_code": self.exit_code,
            "k": self.k,
            "n": self.n,
            "current": {
                "quality_total": round(self.current_quality, 8),
                "cost_usd": round(self.current_cost, 8),
            },
            "axes": {
                "quality_total": axis(self.report.quality),
                "cost_usd": axis(self.report.cost),
            },
            "runs": self.runs,
        }

    def human_report(self) -> str:
        body = self.report.report().splitlines()[1:]  # drop the report's own header
        header = f"Eval gate — {'PASS' if self.passed else 'REGRESSION'}  (k={self.k}, N={self.n})"
        note = "  (cost_usd = total worker + judge spend)"
        return "\n".join([header, *body, note, f"exit code: {self.exit_code}"])


def evaluate_gate(
    baseline: Baseline,
    *,
    current_quality: float,
    current_cost: float,
    k: float | None = None,
    n: int = 1,
    runs: list[dict] | None = None,
) -> GateResult:
    """Compare already-measured current metrics to the baseline band → GateResult.

    Pure decision step (no eval run) — the unit that determines the exit code.
    """
    report = check_regression(
        baseline, current_quality=current_quality, current_cost=current_cost, k=k
    )
    return GateResult(
        passed=report.passed,
        k=report.k,
        n=n,
        current_quality=current_quality,
        current_cost=current_cost,
        report=report,
        runs=runs or [],
    )


def run_gate(
    *,
    baseline_path: Path,
    tasks,
    sandbox_src: Path,
    worker=None,
    judge=None,
    k: float | None = None,
    n: int = 1,
    out_dir: Path = Path("evals"),
) -> GateResult:
    """Run the eval suite N times, average the aggregate, and gate on the band."""
    baseline = Baseline.load(baseline_path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    qs, cs, per_run = [], [], []
    for i in range(max(1, n)):
        run = run_eval(
            tasks,
            sandbox_src=Path(sandbox_src),
            worker=worker,
            judge=judge,
            out_dir=Path(out_dir),
            stamp=f"gate-{stamp}-{i}",
        )
        rq, rc = _run_quality(run), _run_cost(run)
        qs.append(rq)
        cs.append(rc)
        per_run.append({"quality_total": round(rq, 8), "cost_usd": round(rc, 8)})
    return evaluate_gate(
        baseline,
        current_quality=statistics.fmean(qs),
        current_cost=statistics.fmean(cs),
        k=k,
        n=len(qs),
        runs=per_run,
    )


def _resolve_k(cli_k: float | None) -> float | None:
    if cli_k is not None:
        return cli_k
    env = os.getenv("MC_GATE_K")
    return float(env) if env else None  # None → run_gate falls back to baseline.k


def _resolve_n(cli_n: int | None) -> int:
    if cli_n is not None:
        return cli_n
    return int(os.getenv("MC_GATE_N", "1"))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="eval-gate",
        description="Run the eval suite, gate against baseline.json (exit 0/nonzero).",
    )
    p.add_argument("--baseline", default=os.getenv("MC_GATE_BASELINE", "golden/baseline.json"))
    p.add_argument("--tasks", default=os.getenv("MC_GATE_TASKS", "golden/tasks"))
    p.add_argument("--sandbox", default=os.getenv("MC_GATE_SANDBOX", "golden/sandbox"))
    p.add_argument("--k", type=float, default=None, help="noise-band multiplier (env MC_GATE_K; default baseline.k)")
    p.add_argument("--n", type=int, default=None, help="repeats of the eval run to average (env MC_GATE_N; default 1)")
    p.add_argument("--json", default=os.getenv("MC_GATE_JSON", "evals/gate-result.json"))
    p.add_argument("--out-dir", default="evals")
    p.add_argument("--worker-model", default=None)
    p.add_argument("--judge-model", default=None)
    p.add_argument(
        "--demo",
        action="store_true",
        default=bool(os.getenv("MC_GATE_DEMO")),
        help="deterministic StubWorker + no judge (offline; for CI/pipeline demos)",
    )
    args = p.parse_args(argv)

    if args.demo:
        worker, judge = StubWorker(), None  # offline, reproducible
    else:
        worker = SdkWorker(model=args.worker_model) if args.worker_model else SdkWorker()
        judge = LlmJudge(model=args.judge_model) if args.judge_model else LlmJudge()
    tasks = sorted(Path(args.tasks).glob("*.yaml"))

    result = run_gate(
        baseline_path=Path(args.baseline),
        tasks=tasks,
        sandbox_src=Path(args.sandbox),
        worker=worker,
        judge=judge,
        k=_resolve_k(args.k),
        n=_resolve_n(args.n),
        out_dir=Path(args.out_dir),
    )

    print(result.human_report())
    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result.to_json(), indent=2, sort_keys=True))
    print(f"[eval-gate] wrote {out}", file=sys.stderr)
    sys.exit(result.exit_code)


if __name__ == "__main__":  # pragma: no cover
    main()
