"""Manual run: one converged LIVE FEED over a durable run.

A Flight Director dispatches a Controller through the LangGraph shell and consumes
a SINGLE ordered async iterator that carries BOTH node transitions (updates) and
priced telemetry (custom StepEvents) — while the JSONL bronze spine is written
exactly as before. Clean teardown, no worktree leaks.

    python -m mission_control.demo_live          # sim  (read-only)
    python -m mission_control.demo_live --burn   # burn (pauses at the go/no-go gate)
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

from langgraph.types import Command

from . import roles
from .graph import build_run_graph, initial_state, thread_config
from .live import GateWaiting, NodeTransition, StepMetric, stream_run
from .tasks import Task, TaskType
from .worker import StubWorker
from .worktree import list_worktrees


def _init_repo(path: Path) -> None:
    def g(*a: str) -> None:
        subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)

    g("init", "-b", "main")
    g("config", "user.email", "demo@example.com")
    g("config", "user.name", "Demo")
    (path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    g("add", "-A")
    g("commit", "-m", "init")


def _render(event) -> str:
    if isinstance(event, NodeTransition):
        return f"    → node       {event.node}"
    if isinstance(event, StepMetric):
        e = event.event
        return (
            f"    · telemetry  {e.step_id}  {e.model}  "
            f"in={e.input_tokens} out={e.output_tokens} "
            f"ctx={e.context_size_tokens} cost=${e.cost_usd:.6f} [{e.outcome}]"
        )
    if isinstance(event, GateWaiting):
        return f"    ⏸ gate       waiting on go/no-go for {event.value.get('task_id')}"
    return f"    ? {event!r}"


async def _drain(graph, inp, cfg) -> list:
    seen = []
    async for event in stream_run(graph, inp, cfg):
        seen.append(event)
        print(_render(event))
    return seen


async def _main() -> None:
    burn = "--burn" in sys.argv
    holder = Path(tempfile.mkdtemp(prefix="mc-live-demo-"))
    target = holder / "repo"
    target.mkdir()
    _init_repo(target)
    tele = holder / "telemetry"

    graph = build_run_graph(target, worker=StubWorker(), telemetry_dir=tele)
    if burn:
        task = Task("live-burn-001", TaskType.SIDE_EFFECTFUL, "add a docstring", greenfield=False)
    else:
        task = Task("live-sim-001", TaskType.READ_ONLY, "survey the target repo", greenfield=False)
    cfg = thread_config(task, task.task_id)

    before = list_worktrees(target)
    print(
        f"{roles.ORCHESTRATOR} dispatching a {roles.WORKER} through the LangGraph "
        f"shell on a '{roles.BURN if burn else roles.SIM}' task ({task.task_id})"
    )
    print(f"  worktrees before dispatch: {len(before)} (main only)")
    print("  ONE merged live feed (node transitions + priced telemetry):")

    events = await _drain(graph, initial_state(task), cfg)

    if burn:
        assert isinstance(events[-1], GateWaiting), "burn should pause at the gate"
        print(f"  {roles.ORCHESTRATOR} decides: {roles.GO}. Resuming ...")
        events += await _drain(graph, Command(resume=roles.GO), cfg)

    transitions = [e for e in events if isinstance(e, NodeTransition)]
    metrics = [e for e in events if isinstance(e, StepMetric)]
    jsonl = list(tele.glob("*.jsonl"))
    after = list_worktrees(target)

    print(
        f"  merged feed carried {len(transitions)} node transition(s) + "
        f"{len(metrics)} priced step metric(s) in one ordered iterator"
    )
    print(f"  JSONL bronze spine written: {[p.name for p in jsonl]}")
    print(f"  worktrees after teardown: {len(after)} (main only)")

    assert metrics, "expected priced telemetry in the live feed"
    assert len(jsonl) == 1, f"expected exactly one JSONL file, got {jsonl}"
    assert len(after) == 1 and after == before, f"worktree leak: {after}"
    print(f"clean teardown — no worktree leaks. {roles.ORCHESTRATOR} out.")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
