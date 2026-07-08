"""Baseline + threshold (regression) analysis — the statistics deliverable.

Run the full golden set N times to measure run-to-run spread, persist the
per-task and aggregate mean/stddev/min/max for `quality_total` and `cost_usd`
(``baseline.json``, diffable), then compare a new run against that baseline and
flag a regression ONLY when it falls outside a k·stddev noise band. The whole
point is to tell a real drop apart from variance.

Not wired into CI (that's Phase 3) — this is a callable check + printed report.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from .evals import EvalRun, run_eval
from .worker import StubWorker, Worker

# Phase 3 raised this from 5 → 15: per-task stddev estimates need ~15-20 repeats
# before the band is trustworthy (see docs/PHASE3_FINDINGS.md).
DEFAULT_N = 15
DEFAULT_K = 2.0


# -- statistics ------------------------------------------------------------

@dataclass
class Stat:
    """Distribution of one metric across N runs."""

    mean: float
    stddev: float
    min: float
    max: float
    n: int
    samples: list[float] = field(default_factory=list)

    @classmethod
    def from_samples(cls, samples) -> "Stat":
        s = [float(x) for x in samples]
        if not s:
            return cls(0.0, 0.0, 0.0, 0.0, 0, [])
        return cls(
            mean=round(statistics.fmean(s), 8),
            stddev=round(statistics.stdev(s), 8) if len(s) >= 2 else 0.0,
            min=round(min(s), 8),
            max=round(max(s), 8),
            n=len(s),
            samples=[round(x, 8) for x in s],
        )

    def to_dict(self) -> dict:
        return {
            "mean": self.mean,
            "stddev": self.stddev,
            "min": self.min,
            "max": self.max,
            "n": self.n,
            "samples": self.samples,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Stat":
        return cls(
            mean=d["mean"],
            stddev=d["stddev"],
            min=d["min"],
            max=d["max"],
            n=d["n"],
            samples=list(d.get("samples", [])),
        )


@dataclass
class MetricStats:
    """The two axes we track for an entity (a task, or the whole suite)."""

    quality_total: Stat
    cost_usd: Stat

    def to_dict(self) -> dict:
        return {"quality_total": self.quality_total.to_dict(), "cost_usd": self.cost_usd.to_dict()}

    @classmethod
    def from_dict(cls, d: dict) -> "MetricStats":
        return cls(Stat.from_dict(d["quality_total"]), Stat.from_dict(d["cost_usd"]))


@dataclass
class Baseline:
    """Persisted run-to-run spread for the golden set under a given config."""

    n: int
    k: float
    tasks: list[str]
    aggregate: MetricStats  # per-run suite metrics: mean(quality) & sum(cost)
    per_task: dict[str, MetricStats]
    worker_model: str | None = None
    judge_model: str | None = None
    created: str | None = None

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "n": self.n,
            "k": self.k,
            "created": self.created,
            "worker_model": self.worker_model,
            "judge_model": self.judge_model,
            "tasks": self.tasks,
            "aggregate": self.aggregate.to_dict(),
            "per_task": {t: m.to_dict() for t, m in self.per_task.items()},
        }

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")
        return path

    @classmethod
    def from_dict(cls, d: dict) -> "Baseline":
        return cls(
            n=d["n"],
            k=d["k"],
            tasks=list(d["tasks"]),
            aggregate=MetricStats.from_dict(d["aggregate"]),
            per_task={t: MetricStats.from_dict(m) for t, m in d["per_task"].items()},
            worker_model=d.get("worker_model"),
            judge_model=d.get("judge_model"),
            created=d.get("created"),
        )

    @classmethod
    def load(cls, path: Path) -> "Baseline":
        return cls.from_dict(json.loads(Path(path).read_text()))


# -- building the baseline -------------------------------------------------

def _run_quality(run: EvalRun) -> float:
    return statistics.fmean([r.quality_total for r in run.results]) if run.results else 0.0


def _run_cost(run: EvalRun) -> float:
    return sum(r.cost_usd for r in run.results)


def build_baseline(
    task_paths,
    *,
    sandbox_src: Path,
    worker: Worker | None = None,
    judge=None,
    n: int = DEFAULT_N,
    k: float = DEFAULT_K,
    out_dir: Path = Path("evals"),
    baseline_path: Path | None = None,
    created: str | None = None,
) -> Baseline:
    """Run the full golden set ``n`` times and summarize the spread."""
    if n < 1:
        raise ValueError("n must be >= 1")
    worker = worker if worker is not None else StubWorker()
    task_paths = [Path(p) for p in task_paths]
    stamp = created or time.strftime("%Y%m%d-%H%M%S")

    runs: list[EvalRun] = []
    for i in range(n):
        runs.append(
            run_eval(
                task_paths,
                sandbox_src=Path(sandbox_src),
                worker=worker,
                judge=judge,
                out_dir=Path(out_dir),
                stamp=f"{stamp}-b{i}",
            )
        )

    # Union of task ids across runs (first-seen order), so a run that dropped a
    # task to a transient failure doesn't remove it from the baseline.
    task_ids = list(dict.fromkeys(r.task_id for run in runs for r in run.results))
    per_task: dict[str, MetricStats] = {}
    for tid in task_ids:
        q, c = [], []
        for run in runs:
            res = next((r for r in run.results if r.task_id == tid), None)
            if res is None:
                continue  # this run didn't produce a sample for this task
            q.append(res.quality_total)
            c.append(res.cost_usd)
        per_task[tid] = MetricStats(Stat.from_samples(q), Stat.from_samples(c))

    aggregate = MetricStats(
        Stat.from_samples([_run_quality(r) for r in runs]),
        Stat.from_samples([_run_cost(r) for r in runs]),
    )

    baseline = Baseline(
        n=n,
        k=k,
        tasks=task_ids,
        aggregate=aggregate,
        per_task=per_task,
        worker_model=getattr(worker, "model", "stub"),
        judge_model=getattr(judge, "model", None),
        created=stamp,
    )
    if baseline_path is not None:
        baseline.save(baseline_path)
    return baseline


# -- regression check ------------------------------------------------------

@dataclass
class AxisCheck:
    """One metric on one entity, checked against the baseline's noise band."""

    metric: str  # "quality_total" | "cost_usd"
    scope: str  # "aggregate" | a task_id
    current: float
    baseline_mean: float
    baseline_stddev: float
    threshold: float
    higher_is_worse: bool  # cost True; quality False
    regressed: bool


def _axis(metric, scope, current, stat: Stat, k: float, higher_is_worse: bool) -> AxisCheck:
    if higher_is_worse:
        threshold = stat.mean + k * stat.stddev
        regressed = current > threshold
    else:
        threshold = stat.mean - k * stat.stddev
        regressed = current < threshold
    return AxisCheck(
        metric=metric,
        scope=scope,
        current=round(current, 8),
        baseline_mean=stat.mean,
        baseline_stddev=stat.stddev,
        threshold=round(threshold, 8),
        higher_is_worse=higher_is_worse,
        regressed=regressed,
    )


@dataclass
class RegressionReport:
    """Result of comparing a run against a baseline."""

    k: float
    quality: AxisCheck  # aggregate quality_total
    cost: AxisCheck  # aggregate cost_usd
    per_task: list[AxisCheck] = field(default_factory=list)

    @property
    def regressions(self) -> list[AxisCheck]:
        out = [a for a in (self.quality, self.cost) if a.regressed]
        out += [a for a in self.per_task if a.regressed]
        return out

    @property
    def passed(self) -> bool:
        return not self.regressions

    def report(self) -> str:
        def fmt(a: AxisCheck) -> str:
            money = a.metric == "cost_usd"
            cur = f"${a.current:.6f}" if money else f"{a.current:.3f}"
            mean = f"${a.baseline_mean:.6f}" if money else f"{a.baseline_mean:.3f}"
            std = f"${a.baseline_stddev:.6f}" if money else f"{a.baseline_stddev:.3f}"
            thr = f"${a.threshold:.6f}" if money else f"{a.threshold:.3f}"
            bound = "≤" if a.higher_is_worse else "≥"
            verdict = "REGRESSION" if a.regressed else "pass"
            return (
                f"  {a.metric:13} [{a.scope}] baseline {mean} ± {std}  "
                f"current {cur}  threshold {bound}{thr}  → {verdict}"
            )

        lines = [f"Regression check (k={self.k}) — {'PASS' if self.passed else 'REGRESSION'}"]
        lines.append(fmt(self.quality))
        lines.append(fmt(self.cost))
        per_task_regs = [a for a in self.per_task if a.regressed]
        if per_task_regs:
            lines.append("  per-task regressions:")
            lines.extend(fmt(a) for a in per_task_regs)
        elif self.per_task:
            lines.append("  per-task: all within band")
        return "\n".join(lines)


def check_regression(
    baseline: Baseline,
    *,
    current_quality: float,
    current_cost: float,
    per_task_current: dict[str, tuple[float, float]] | None = None,
    k: float | None = None,
) -> RegressionReport:
    """Compare current metrics against the baseline's k·stddev noise band.

    A regression is flagged only OUTSIDE the band: quality below
    ``mean − k·stddev`` or cost above ``mean + k·stddev``.
    """
    k = baseline.k if k is None else k
    quality = _axis("quality_total", "aggregate", current_quality, baseline.aggregate.quality_total, k, higher_is_worse=False)
    cost = _axis("cost_usd", "aggregate", current_cost, baseline.aggregate.cost_usd, k, higher_is_worse=True)

    per_task: list[AxisCheck] = []
    for tid, (cq, cc) in (per_task_current or {}).items():
        stats = baseline.per_task.get(tid)
        if stats is None:
            continue
        per_task.append(_axis("quality_total", tid, cq, stats.quality_total, k, higher_is_worse=False))
        per_task.append(_axis("cost_usd", tid, cc, stats.cost_usd, k, higher_is_worse=True))

    return RegressionReport(k=k, quality=quality, cost=cost, per_task=per_task)


def check_run(baseline: Baseline, run: EvalRun, *, k: float | None = None) -> RegressionReport:
    """Convenience: extract aggregate + per-task metrics from an EvalRun and check."""
    per_task = {r.task_id: (r.quality_total, r.cost_usd) for r in run.results}
    return check_regression(
        baseline,
        current_quality=_run_quality(run),
        current_cost=_run_cost(run),
        per_task_current=per_task,
        k=k,
    )


# -- live convenience entry point ------------------------------------------

def _golden_root() -> Path:
    return Path(__file__).resolve().parents[2] / "golden"


def main() -> None:  # pragma: no cover - live convenience entry point
    """Build a baseline over the real golden set and print per-metric spread."""
    import sys

    from .judge import LlmJudge
    from .sdk_worker import SdkWorker

    n = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_N
    golden = _golden_root()
    tasks = sorted((golden / "tasks").glob("*.yaml"))
    baseline = build_baseline(
        tasks,
        sandbox_src=golden / "sandbox",
        worker=SdkWorker(),
        judge=LlmJudge(),
        n=n,
        baseline_path=golden / "baseline.json",
    )
    agg = baseline.aggregate
    print(f"baseline over {baseline.n} runs (worker={baseline.worker_model}, judge={baseline.judge_model})")
    print(f"  aggregate quality_total:      {agg.quality_total.mean:.3f} ± {agg.quality_total.stddev:.3f} "
          f"[{agg.quality_total.min:.3f}, {agg.quality_total.max:.3f}]")
    print(f"  aggregate cost_usd (TOTAL):   ${agg.cost_usd.mean:.6f} ± ${agg.cost_usd.stddev:.6f} "
          f"[${agg.cost_usd.min:.6f}, ${agg.cost_usd.max:.6f}]  (worker + judge)")
    for tid, m in baseline.per_task.items():
        print(f"    {tid:28} q={m.quality_total.mean:.3f}±{m.quality_total.stddev:.3f} "
              f"cost=${m.cost_usd.mean:.6f}±${m.cost_usd.stddev:.6f}")
    print(f"saved → {golden / 'baseline.json'}")


if __name__ == "__main__":  # pragma: no cover
    main()
