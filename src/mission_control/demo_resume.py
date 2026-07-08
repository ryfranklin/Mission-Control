"""Durability demo: KILL a run mid-flight, RESUME from Postgres, no re-pay, no leak.

    python -m mission_control.demo_resume            # StubWorker (deterministic $)
    python -m mission_control.demo_resume --sdk      # real SdkWorker (real $)

Parent orchestrates two real OS processes sharing one target repo + thread_id:
  1. crash child  — runs the graph; the `gate` node hard-exits (os._exit) right
     after `run_worker` has completed AND checkpointed to Postgres. teardown never
     runs → the worktree is left leaked on disk.
  2. resume child — a FRESH process re-invokes the graph with the same thread_id;
     LangGraph reads the checkpoint from Postgres, SKIPS the completed dispatch +
     run_worker nodes (worker never re-called → no re-pay), runs gate→teardown,
     and cleans up the leaked worktree.
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
from .graph import DEFAULT_PG_URL, build_run_graph, postgres_checkpointer, worker_cost_usd
from .tasks import TaskType
from .worker import StubWorker, WorkerResult
from .worktree import list_worktrees

THREAD_ID = "resume-demo-thread"
TASK_ID = "resume-demo-001"


class _RecordingWorker(StubWorker):
    """StubWorker that records whether it was called (to prove no re-execution)."""

    def __init__(self) -> None:
        self.calls = 0

    def investigate(self, task, workdir) -> WorkerResult:
        self.calls += 1
        return super().investigate(task, workdir)


def _init_repo(path: Path) -> None:
    def g(*a: str) -> None:
        subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)

    g("init", "-b", "main")
    g("config", "user.email", "demo@example.com")
    g("config", "user.name", "Demo")
    (path / "README.md").write_text("# throwaway target repo\n")
    g("add", "-A")
    g("commit", "-m", "init")


def _initial() -> dict:
    return {
        "task_id": TASK_ID,
        "task_type": TaskType.READ_ONLY.value,
        "prompt": "survey the target repo",
        "greenfield": False,
        "decision": None,
        "applied": False,
    }


def _worker(use_sdk: bool):
    if use_sdk:
        from .sdk_worker import SdkWorker

        return SdkWorker()
    return StubWorker()


def _child(phase: str, target: str, use_sdk: bool, thread: str) -> None:
    cfg = {"configurable": {"thread_id": thread}}
    checkpointer, pool = postgres_checkpointer(setup=True)
    try:
        if phase == "crash":
            # Run through the (paid) worker node and STOP at the gate boundary,
            # which durably persists the checkpoint (dispatch + run_worker done).
            graph = build_run_graph(
                target, worker=_worker(use_sdk), checkpointer=checkpointer,
                interrupt_before=["gate"],
            )
            graph.invoke(_initial(), config=cfg)  # returns at the interrupt; state is in Postgres
            pool.close()
            os._exit(137)  # HARD KILL — durable state already committed, teardown never runs
        elif phase == "resume":
            rec = _RecordingWorker()
            graph = build_run_graph(target, worker=rec, checkpointer=checkpointer)
            final = graph.invoke(None, config=cfg)  # resume from checkpoint
            print(f"RESUME_WORKER_CALLS={rec.calls}")
            print(f"DOLLARS_SAVED={worker_cost_usd(final):.6f}")
            print(f"OUTCOME={final.get('outcome')}")
    finally:
        pool.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--child", choices=["crash", "resume"])
    p.add_argument("--target")
    p.add_argument("--thread", default=THREAD_ID)
    p.add_argument("--sdk", action="store_true")
    args = p.parse_args()

    if args.child:
        _child(args.child, args.target, args.sdk, args.thread)
        return

    thread = f"resume-demo-{uuid.uuid4().hex[:8]}"
    holder = Path(tempfile.mkdtemp(prefix="mc-resume-demo-"))
    target = holder / "repo"
    target.mkdir()
    _init_repo(target)
    base = [sys.executable, "-m", "mission_control.demo_resume",
            "--target", str(target), "--thread", thread]
    if args.sdk:
        base.append("--sdk")

    print(f"{roles.ORCHESTRATOR} dispatching a {roles.WORKER} (thread={thread}); Postgres = durable state")
    print(f"  worktrees before: {len(list_worktrees(target))} (main only)")

    print("\n[1] crash child — runs worker, then hard-kills before teardown ...")
    r1 = subprocess.run(base + ["--child", "crash"], stderr=subprocess.DEVNULL)
    leaked = list_worktrees(target)
    print(f"    child exit code: {r1.returncode} (killed mid-flight)")
    print(f"    worktrees after crash: {len(leaked)}  ← LEAKED (os._exit skipped teardown)")

    print("\n[2] resume child — fresh process, same thread_id, reads Postgres ...")
    r2 = subprocess.run(base + ["--child", "resume"], stdout=subprocess.PIPE, text=True,
                        stderr=subprocess.DEVNULL)
    out = {k: v for k, v in (ln.split("=", 1) for ln in r2.stdout.splitlines() if "=" in ln)}
    after = list_worktrees(target)
    print(f"    worker re-executed on resume? calls={out.get('RESUME_WORKER_CALLS')}  (0 ⇒ NOT re-paid)")
    print(f"    dollars saved by resume (worker cost not re-paid): ${out.get('DOLLARS_SAVED')}")
    print(f"    outcome={out.get('OUTCOME')}")
    print(f"    worktrees after resume: {len(after)}  ← cleaned up")

    assert out.get("RESUME_WORKER_CALLS") == "0", "worker was re-executed on resume!"
    assert len(after) == 1, f"worktree leak after resume: {after}"
    print(f"\ndurable: resume skipped completed nodes, re-paid $0, left no leak. {roles.ORCHESTRATOR} out.")


if __name__ == "__main__":
    main()
