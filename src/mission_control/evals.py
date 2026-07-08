"""Deterministic eval runner over the golden set.

Each golden task is dispatched through the **existing** orchestration entry point
(`Orchestrator.run_task`) against a fresh checkout of the golden sandbox. We
capture the worker's output and the Phase 1 telemetry for that run (reusing the
telemetry module — not forked), apply the task's *deterministic* asserts, and
emit a per-task :class:`EvalResult`. Results stream to a JSONL file, one file per
eval run, mirroring the telemetry convention.

No LLM judge yet — the `judge_rubric` half of each spec is ignored here.
"""

from __future__ import annotations

import fnmatch
import json
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from . import roles
from .judge import LlmJudge
from .orchestrator import OUTCOME_COMPLETED, Orchestrator
from .tasks import Task, TaskType
from .telemetry import StepEvent, TelemetrySink
from .worker import StubWorker, Worker

# Map a spec's task_type string (== TaskType.value) to the enum, and a spec's
# approval string to a go/no-go gate — sourced from the vocabulary, never spelled.
_TASK_TYPE_BY_VALUE = {t.value: t for t in TaskType}
_PASS = "PASSED"  # pytest status for a passing node
_PYTEST_LINE = None  # compiled lazily in _run_pytest

# quality_total = weighted blend of the mechanical asserts and the judged rubric.
# Deterministic asserts are high-confidence; the judge is noisier — but we weight
# them EQUALLY here as a deliberate, provisional choice (see golden/README.md →
# LLM judge). Tune once judge reliability is characterized. For deterministic-only
# tasks (no rubric), quality_total == quality_deterministic.
QUALITY_DET_WEIGHT = 0.5
QUALITY_JUDGE_WEIGHT = 0.5


def _mean(values) -> float:
    """Mean of the non-None values, rounded; 0.0 if there are none."""
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else 0.0


@dataclass
class AssertResult:
    """One deterministic check and whether it held."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class EvalResult:
    """Per-task scores + captured telemetry (worker AND judge)."""

    task_id: str
    task_type: str
    asserts_passed: int
    asserts_failed: int
    quality_deterministic: float  # fraction of deterministic asserts passed
    # Judged rubric score (0..1), or None when the task has no rubric (judge skipped).
    quality_judge: float | None
    # Documented blend of the two (see QUALITY_*_WEIGHT); == quality_deterministic
    # when there is no rubric.
    quality_total: float
    # Worker (Phase 1) telemetry:
    cost_usd: float
    total_tokens: int
    latency_ms: int
    # Judge telemetry — kept SEPARATE so the judge's cost is always visible:
    judge_model: str | None = None
    judge_cost_usd: float = 0.0
    judge_tokens: int = 0


@dataclass
class EvalRun:
    """A whole eval run: the JSONL path + every per-task result, with a rollup."""

    path: Path
    results: list[EvalResult] = field(default_factory=list)

    def summary(self) -> dict:
        n = len(self.results)
        passed = [r for r in self.results if r.asserts_failed == 0]
        judged = [r for r in self.results if r.quality_judge is not None]
        return {
            "tasks": n,
            "tasks_fully_passed": len(passed),
            "tasks_judged": len(judged),
            "asserts_passed": sum(r.asserts_passed for r in self.results),
            "asserts_failed": sum(r.asserts_failed for r in self.results),
            "mean_quality_deterministic": _mean(r.quality_deterministic for r in self.results),
            "mean_quality_judge": _mean(r.quality_judge for r in judged),
            "mean_quality_total": _mean(r.quality_total for r in self.results),
            # Worker vs judge cost, kept separate so the judge's spend is visible.
            "cost_usd": round(sum(r.cost_usd for r in self.results), 6),
            "judge_cost_usd": round(sum(r.judge_cost_usd for r in self.results), 6),
            "total_tokens": sum(r.total_tokens for r in self.results),
            "judge_tokens": sum(r.judge_tokens for r in self.results),
            "latency_ms": sum(r.latency_ms for r in self.results),
        }

    def summary_line(self) -> str:
        s = self.summary()
        return (
            f"eval: {s['tasks_fully_passed']}/{s['tasks']} tasks fully passed, "
            f"asserts {s['asserts_passed']}✓/{s['asserts_failed']}✗, "
            f"quality det={s['mean_quality_deterministic']:.2f} "
            f"judge={s['mean_quality_judge']:.2f} total={s['mean_quality_total']:.2f}, "
            f"cost worker=${s['cost_usd']:.6f} judge=${s['judge_cost_usd']:.6f} "
            f"→ {self.path.name}"
        )


# -- spec loading ----------------------------------------------------------

def load_spec(path: Path) -> dict:
    """Load and lightly validate a golden task spec."""
    spec = yaml.safe_load(Path(path).read_text())
    if spec.get("task_type") not in _TASK_TYPE_BY_VALUE:
        raise ValueError(f"{path}: task_type must be one of {sorted(_TASK_TYPE_BY_VALUE)}")
    spec.setdefault("known_good", {}).setdefault("deterministic", {})
    return spec


def _approval_for(spec: dict):
    """Spec approval string → go/no-go gate callback (or None for a sim)."""
    decision = spec.get("approval")
    if decision is None:
        return None
    if decision == roles.GO:
        return lambda run: True
    if decision == roles.NO_GO:
        return lambda run: False
    raise ValueError(f"unknown approval: {decision!r}")


# -- sandbox / git helpers -------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def _materialize_sandbox(sandbox_src: Path, dest: Path) -> str:
    """Copy the sandbox fixture into a fresh git repo; return the baseline SHA."""
    shutil.copytree(
        sandbox_src,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
    )
    _git(dest, "init", "-b", "main")
    _git(dest, "config", "user.email", "eval@example.com")
    _git(dest, "config", "user.name", "Eval")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-m", "baseline")
    return _git(dest, "rev-parse", "HEAD").strip()


def _changed_files(repo: Path, baseline_sha: str) -> set[str]:
    """Paths that differ from baseline (committed diff + any working-tree noise)."""
    committed = _git(repo, "diff", "--name-only", baseline_sha, "HEAD").split()
    status = _git(repo, "status", "--porcelain").splitlines()
    working = [line[3:].strip() for line in status if line.strip()]
    return {p for p in [*committed, *working] if p}


def _run_pytest(repo: Path) -> tuple[int, dict[str, str]]:
    """Run the target's suite; return (returncode, {node_id: status})."""
    import os
    import re

    global _PYTEST_LINE
    if _PYTEST_LINE is None:
        _PYTEST_LINE = re.compile(
            r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )
    # Inherit the environment (so the interpreter finds everything it needs) and
    # prepend the target repo to PYTHONPATH so its top-level modules import.
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        [str(repo), os.environ.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", "--tb=no", "-p", "no:cacheprovider"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
    )
    states: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        m = _PYTEST_LINE.match(line)
        if m:
            states[m.group(1)] = m.group(2)
    return proc.returncode, states


# -- deterministic assert engine -------------------------------------------

def _check(
    det: dict,
    *,
    run_result,
    worker_summary: str,
    changed: set[str],
    baseline_tests: dict[str, str] | None,
    after_tests: tuple[int, dict[str, str]] | None,
) -> list[AssertResult]:
    checks: list[AssertResult] = []

    def add(name: str, passed: bool, detail: str = "") -> None:
        checks.append(AssertResult(name, passed, detail))

    if "outcome" in det:
        add(
            f"outcome=={det['outcome']}",
            run_result.outcome == det["outcome"],
            f"got {run_result.outcome!r}",
        )
    if "applied" in det:
        add(
            f"applied=={det['applied']}",
            run_result.applied is det["applied"],
            f"got {run_result.applied!r}",
        )
    if det.get("decision") is not None:
        add(
            f"decision=={det['decision']}",
            run_result.decision == det["decision"],
            f"got {run_result.decision!r}",
        )
    if det.get("no_changes") is True:
        add("no_changes", not changed, f"changed={sorted(changed)}")

    for glob in det.get("files_touched") or []:
        hit = any(fnmatch.fnmatch(p, glob) for p in changed)
        add(f"files_touched:{glob}", hit, f"changed={sorted(changed)}")

    for glob in det.get("files_unchanged") or []:
        touched = [p for p in changed if fnmatch.fnmatch(p, glob)]
        add(f"files_unchanged:{glob}", not touched, f"touched={touched}")

    tests_mode = det.get("tests", "ignore")
    if tests_mode != "ignore" and after_tests is not None:
        rc, after = after_tests
        if tests_mode == "goes_green":
            add("tests:goes_green", rc == 0, f"pytest rc={rc}")
        elif tests_mode == "stays_green":
            baseline_passed = {
                n for n, s in (baseline_tests or {}).items() if s == _PASS
            }
            regressed = [
                n for n in baseline_passed if after.get(n) != _PASS
            ]
            add("tests:stays_green", not regressed, f"regressed={regressed}")

    for needle in det.get("output_contains") or []:
        add(f"output_contains:{needle!r}", needle in worker_summary)
    for needle in det.get("output_absent") or []:
        add(f"output_absent:{needle!r}", needle not in worker_summary)

    return checks


# -- per-task evaluation ---------------------------------------------------

def evaluate_task(
    spec: dict,
    *,
    worker: Worker,
    sandbox_src: Path,
    work_dir: Path,
    judge: LlmJudge | None = None,
) -> tuple[EvalResult, list[AssertResult], dict | None]:
    """Run one golden task end-to-end; score deterministic asserts, and — only if
    the task has a rubric — the judged portion. Returns (result, checks, judge_info)."""
    det = spec["known_good"]["deterministic"]
    tests_mode = det.get("tests", "ignore")

    target = work_dir / "target"
    baseline_sha = _materialize_sandbox(sandbox_src, target)

    baseline_tests: dict[str, str] | None = None
    if tests_mode == "stays_green":
        _, baseline_tests = _run_pytest(target)

    task = Task(
        task_id=spec["id"],
        task_type=_TASK_TYPE_BY_VALUE[spec["task_type"]],
        prompt=spec["prompt"],
        greenfield=bool(spec.get("greenfield", False)),
    )
    orch = Orchestrator(
        target_repo=target,
        worker=worker,
        telemetry_dir=work_dir / "telemetry",
    )
    run_result = orch.run_task(task, approval=_approval_for(spec))

    changed = _changed_files(target, baseline_sha)
    after_tests = _run_pytest(target) if tests_mode != "ignore" else None

    worker_summary = run_result.worker_result.summary if run_result.worker_result else ""
    checks = _check(
        det,
        run_result=run_result,
        worker_summary=worker_summary,
        changed=changed,
        baseline_tests=baseline_tests,
        after_tests=after_tests,
    )

    passed = sum(1 for c in checks if c.passed)
    failed = len(checks) - passed
    quality_det = round(passed / len(checks), 4) if checks else 1.0
    summary = run_result.telemetry.summary()
    total_tokens = (
        summary["input_tokens"]
        + summary["output_tokens"]
        + summary["cache_read_tokens"]
        + summary["cache_creation_tokens"]
    )

    # --- judged rubric (only if there is a rubric — otherwise never pay) -----
    rubric = spec["known_good"].get("judge_rubric") or []
    quality_judge: float | None = None
    quality_total = quality_det
    judge_model = None
    judge_cost = 0.0
    judge_tokens = 0
    judge_info: dict | None = None
    if rubric:
        j = judge if judge is not None else LlmJudge()
        verdict = j.score(
            task_prompt=spec["prompt"], worker_output=worker_summary, rubric=rubric
        )
        quality_judge = verdict.score
        quality_total = round(
            QUALITY_DET_WEIGHT * quality_det + QUALITY_JUDGE_WEIGHT * quality_judge, 4
        )
        # Capture the judge's OWN cost via the telemetry module — one JSONL line.
        event = StepEvent.from_usage(
            verdict.usage,
            step_id=f"{spec['id']}-judge",
            parent_step_id=None,
            task_id=spec["id"],
            task_type=spec["task_type"],
            outcome=OUTCOME_COMPLETED,
        )
        with TelemetrySink(work_dir / "telemetry" / f"judge-{spec['id']}.jsonl") as sink:
            sink.record(event)
        judge_model = verdict.usage.model
        judge_cost = event.cost_usd
        judge_tokens = (
            event.input_tokens
            + event.output_tokens
            + event.cache_read_tokens
            + event.cache_creation_tokens
        )
        judge_info = {
            "score": quality_judge,
            "rationale": verdict.rationale,
            "per_criterion": verdict.per_criterion,
        }

    result = EvalResult(
        task_id=spec["id"],
        task_type=spec["task_type"],
        asserts_passed=passed,
        asserts_failed=failed,
        quality_deterministic=quality_det,
        quality_judge=quality_judge,
        quality_total=quality_total,
        cost_usd=summary["cost_usd"],
        total_tokens=total_tokens,
        latency_ms=summary["latency_ms"],
        judge_model=judge_model,
        judge_cost_usd=judge_cost,
        judge_tokens=judge_tokens,
    )
    return result, checks, judge_info


# -- run over the golden set ----------------------------------------------

DEFAULT_EVAL_DIR = Path("evals")


def run_eval(
    task_paths,
    *,
    sandbox_src: Path,
    worker: Worker | None = None,
    judge: LlmJudge | None = None,
    out_dir: Path = DEFAULT_EVAL_DIR,
    stamp: str | None = None,
) -> EvalRun:
    """Evaluate each spec in ``task_paths`` and stream results to one JSONL file.

    A judge is only invoked for tasks with a non-empty ``judge_rubric``; a default
    :class:`~mission_control.judge.LlmJudge` is created lazily on first need, so an
    all-deterministic set never constructs one and never makes a judge call.
    """
    worker = worker if worker is not None else StubWorker()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"eval-{stamp}-{uuid.uuid4().hex[:6]}.jsonl"

    run = EvalRun(path=path)
    with path.open("w", encoding="utf-8") as fh:
        for i, task_path in enumerate(task_paths):
            spec = load_spec(Path(task_path))
            work_dir = out_dir / "work" / f"{stamp}-{i}-{spec['id']}"
            result, checks, judge_info = evaluate_task(
                spec,
                worker=worker,
                sandbox_src=Path(sandbox_src),
                work_dir=work_dir,
                judge=judge,
            )
            line = asdict(result)
            line["checks"] = [asdict(c) for c in checks]
            line["judge"] = judge_info  # None when the task had no rubric
            fh.write(json.dumps(line, sort_keys=True) + "\n")
            fh.flush()
            run.results.append(result)
    return run


def _golden_root() -> Path:
    # repo_root/golden — this file lives at repo_root/src/mission_control/evals.py
    return Path(__file__).resolve().parents[2] / "golden"


def main() -> None:  # pragma: no cover - live convenience entry point
    """Run the real golden set with the live SDK worker."""
    from .sdk_worker import SdkWorker

    golden = _golden_root()
    tasks = sorted((golden / "tasks").glob("*.yaml"))
    run = run_eval(
        tasks,
        sandbox_src=golden / "sandbox",
        worker=SdkWorker(),
        out_dir=DEFAULT_EVAL_DIR,
    )
    for r in run.results:
        judge = (
            f"judge={r.quality_judge:.2f} (${r.judge_cost_usd:.6f})"
            if r.quality_judge is not None
            else "judge=—"
        )
        print(
            f"  {r.task_id:30} det={r.quality_deterministic:.2f} {judge} "
            f"total={r.quality_total:.2f} "
            f"({r.asserts_passed}✓/{r.asserts_failed}✗) worker=${r.cost_usd:.6f}"
        )
    print(run.summary_line())


if __name__ == "__main__":  # pragma: no cover
    main()
