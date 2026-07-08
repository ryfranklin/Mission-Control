"""Phase 4 end-to-end: durable go/no-go → kill → restart → resume (no re-pay) →
apply once → clean teardown; then DuckDB analytics; then an eval-gate over MCP.

    python -m mission_control.demo_phase4          # StubWorker (deterministic $)
    python -m mission_control.demo_phase4 --sdk    # real SdkWorker (real $ saved)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from . import roles
from .analytics import analyze
from .eval_gate import call_eval_gate_over_mcp
from .graph import (
    awaiting_gate,
    build_run_graph,
    postgres_checkpointer,
    resume_gate,
    run_via_graph,
    worker_cost_usd,
)
from .tasks import Task, TaskType
from .worker import StubWorker, WorkerResult
from .worktree import list_worktrees


class _RecordingWorker(StubWorker):
    def __init__(self) -> None:
        self.calls = 0

    def investigate(self, task, workdir) -> WorkerResult:
        self.calls += 1
        return super().investigate(task, workdir)


def _git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *a], check=True,
                          capture_output=True, text=True).stdout.strip()


def _init_repo(path: Path) -> None:
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "demo@example.com")
    _git(path, "config", "user.name", "Demo")
    (path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")


def _worker(use_sdk: bool):
    if use_sdk:
        from .sdk_worker import SdkWorker

        return SdkWorker()
    return StubWorker()


def _child(phase: str, target: str, thread: str, use_sdk: bool) -> None:
    cp, pool = postgres_checkpointer(setup=True)
    try:
        if phase == "pause":
            graph = build_run_graph(target, worker=_worker(use_sdk), checkpointer=cp)
            run_via_graph(graph, Task("p4-burn", TaskType.SIDE_EFFECTFUL,
                                      "Add a one-line module docstring to the top of calc.py."),
                          thread_id=thread)  # runs the paid worker, halts at the durable gate
            pool.close()
            os._exit(137)  # HARD KILL while paused; checkpoint already durable
        elif phase == "resume":
            rec = _RecordingWorker()
            graph = build_run_graph(target, worker=rec, checkpointer=cp)
            final = resume_gate(graph, thread, roles.GO)
            print(f"WORKER_CALLS={rec.calls}")
            print(f"DOLLARS_SAVED={worker_cost_usd(final):.6f}")
            print(f"APPLIED={final.get('applied')}")
            print(f"OUTCOME={final.get('outcome')}")
    finally:
        pool.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--child", choices=["pause", "resume"])
    p.add_argument("--target")
    p.add_argument("--thread")
    p.add_argument("--sdk", action="store_true")
    args = p.parse_args()
    if args.child:
        _child(args.child, args.target, args.thread, args.sdk)
        return

    holder = Path(tempfile.mkdtemp(prefix="mc-p4-"))
    target = holder / "repo"
    target.mkdir()
    _init_repo(target)
    baseline_head = _git(target, "rev-parse", "HEAD")
    thread = f"p4-{uuid.uuid4().hex[:8]}"
    base = [sys.executable, "-m", "mission_control.demo_phase4", "--target", str(target),
            "--thread", thread] + (["--sdk"] if args.sdk else [])

    print(f"=== Phase 4 end-to-end ({roles.ORCHESTRATOR} → {roles.WORKER}, thread={thread}) ===")

    print("\n[1] run a burn → pause at the DURABLE go/no-go gate, then HARD-KILL ...")
    r1 = subprocess.run(base + ["--child", "pause"], stderr=subprocess.DEVNULL)
    cp, pool = postgres_checkpointer(setup=False)
    graph = build_run_graph(target, worker=StubWorker(), checkpointer=cp)
    paused = awaiting_gate(graph, thread)
    pool.close()
    print(f"    kill exit={r1.returncode}; interrupt survived restart? {paused}")
    print(f"    applied before approval? {_git(target, 'rev-parse', 'HEAD') != baseline_head}  (must be False)")
    print(f"    worktrees while paused: {len(list_worktrees(target))}  (leaked by the kill)")

    print("\n[2] RESTART: fresh process resumes with 'go' ...")
    r2 = subprocess.run(base + ["--child", "resume"], stdout=subprocess.PIPE, text=True,
                        stderr=subprocess.DEVNULL)
    out = {k: v for k, v in (ln.split("=", 1) for ln in r2.stdout.splitlines() if "=" in ln)}
    applied = _git(target, "rev-parse", "HEAD") != baseline_head
    print(f"    worker re-executed on resume? calls={out.get('WORKER_CALLS')}  (0 ⇒ not re-paid)")
    print(f"    dollars saved (completed worker step not re-paid): ${out.get('DOLLARS_SAVED')}")
    print(f"    applied={out.get('APPLIED')} outcome={out.get('OUTCOME')}  change in target? {applied}")
    print(f"    worktrees after resume: {len(list_worktrees(target))}  (clean)")
    assert out.get("WORKER_CALLS") == "0" and applied and len(list_worktrees(target)) == 1

    print("\n[3] DuckDB analytics over the accumulated JSONL spine ...")
    analyze().print_report()

    print("\n[4] eval-gate routed over MCP (demo baseline) ...")
    subprocess.run([sys.executable, "ci/demo/setup.py"], check=True, capture_output=True)
    res = call_eval_gate_over_mcp(baseline="ci/demo/baseline.pass.json", tasks="ci/demo/tasks",
                                  sandbox="ci/demo/sandbox", demo=True, out_dir="ci/demo/out/mcp")
    print(f"    eval-gate over MCP → passed={res['passed']} exit_code={res['exit_code']}")

    print(f"\nPhase 4 end-to-end complete. {roles.ORCHESTRATOR} out.")


if __name__ == "__main__":
    main()
