"""Durable go/no-go demo: PAUSE at the gate, KILL the process, RESTART, decide.

    python -m mission_control.demo_gate

For each of go / no-go, the parent runs two real OS processes sharing one target
repo + thread_id:
  1. pause child  — runs a burn; the gate node calls interrupt() → the graph halts
     durably (persisted to Postgres), then the child is HARD-KILLED (os._exit).
  2. decide child — a FRESH process resumes the same thread_id with the decision:
     `go` continues into apply-burn (change lands in the target); `no-go` scrubs
     (teardown, nothing applied). Neither leaves a worktree leak, and a burn is
     never applied without an approval on record.
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
from .graph import awaiting_gate, build_run_graph, postgres_checkpointer, resume_gate, run_via_graph
from .tasks import Task, TaskType
from .worker import StubWorker
from .worktree import list_worktrees

BURN_FILE = "STUB_BURN.txt"


def _init_repo(path: Path) -> None:
    def g(*a: str) -> None:
        subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)

    g("init", "-b", "main")
    g("config", "user.email", "demo@example.com")
    g("config", "user.name", "Demo")
    (path / "README.md").write_text("# throwaway target repo\n")
    g("add", "-A")
    g("commit", "-m", "init")


def _tracked(repo: Path) -> list[str]:
    return subprocess.run(["git", "-C", str(repo), "ls-files"], check=True,
                          capture_output=True, text=True).stdout.split()


def _child(phase: str, target: str, thread: str, decision: str) -> None:
    checkpointer, pool = postgres_checkpointer(setup=True)
    try:
        graph = build_run_graph(target, worker=StubWorker(), checkpointer=checkpointer)
        if phase == "pause":
            run_via_graph(graph, Task("burn-demo", TaskType.SIDE_EFFECTFUL, "change"),
                          thread_id=thread)  # halts at the durable gate interrupt
            pool.close()
            os._exit(137)  # HARD KILL while paused; interrupt already persisted
        elif phase == "decide":
            final = resume_gate(graph, thread, decision)
            print(f"DECISION={final.get('decision')}")
            print(f"APPLIED={final.get('applied')}")
            print(f"OUTCOME={final.get('outcome')}")
    finally:
        pool.close()


def _scenario(decision: str, base: list[str]) -> None:
    holder = Path(tempfile.mkdtemp(prefix=f"mc-gate-demo-{decision}-"))
    target = holder / "repo"
    target.mkdir()
    _init_repo(target)
    thread = f"gate-demo-{uuid.uuid4().hex[:8]}"
    cmd = base + ["--target", str(target), "--thread", thread]

    print(f"\n===== scenario: resume with '{decision}' =====")
    print(f"{roles.ORCHESTRATOR} dispatching a {roles.WORKER} on a '{roles.BURN}' "
          f"(thread={thread})")

    print("[1] pause child — burn halts at the go/no-go gate, then hard-kill ...")
    r1 = subprocess.run(cmd + ["--child", "pause"], stderr=subprocess.DEVNULL)
    print(f"    child exit code: {r1.returncode} (killed while paused at the gate)")

    # Fresh process inspects the durable pause.
    cp, pool = postgres_checkpointer(setup=False)
    graph = build_run_graph(target, worker=StubWorker(), checkpointer=cp)
    paused = awaiting_gate(graph, thread)
    pool.close()
    assert paused, "interrupt did not survive the restart"
    print(f"    interrupt survived restart? awaiting_gate={paused}")
    print(f"    applied before approval? {BURN_FILE in _tracked(target)}  (must be False)")
    print(f"    worktrees while paused: {len(list_worktrees(target))}")

    print(f"[2] decide child — fresh process resumes with '{decision}' ...")
    r2 = subprocess.run(cmd + ["--child", "decide", "--decision", decision],
                        stdout=subprocess.PIPE, text=True, stderr=subprocess.DEVNULL)
    out = {k: v for k, v in (ln.split("=", 1) for ln in r2.stdout.splitlines() if "=" in ln)}
    applied_now = BURN_FILE in _tracked(target)
    leaks = len(list_worktrees(target))
    print(f"    decision={out.get('DECISION')} applied={out.get('APPLIED')} outcome={out.get('OUTCOME')}")
    print(f"    change in target? {applied_now}   worktrees after: {leaks}")

    if decision == roles.GO:
        assert out.get("APPLIED") == "True" and applied_now, "go should apply the burn"
    else:
        assert out.get("APPLIED") == "False" and not applied_now, "no-go must not apply"
    assert leaks == 1, f"worktree leak: {leaks}"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--child", choices=["pause", "decide"])
    p.add_argument("--target")
    p.add_argument("--thread")
    p.add_argument("--decision", default=roles.GO)
    args = p.parse_args()

    if args.child:
        _child(args.child, args.target, args.thread, args.decision)
        return

    base = [sys.executable, "-m", "mission_control.demo_gate"]
    _scenario(roles.GO, base)
    _scenario(roles.NO_GO, base)
    print(f"\ndurable go/no-go: paused, killed, restarted, decided — go applied once, "
          f"no-go scrubbed, no leaks. {roles.ORCHESTRATOR} out.")


if __name__ == "__main__":
    main()
