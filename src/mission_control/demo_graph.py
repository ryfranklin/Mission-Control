"""Manual run: a Flight Director dispatches a Controller through the LangGraph
durable shell on a read-only (sim) task, streaming the node trace, with clean
teardown and no worktree leaks.

    python -m mission_control.demo_graph            # StubWorker (offline)
    python -m mission_control.demo_graph --sdk      # real SdkWorker (live call)
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from . import roles
from .graph import build_run_graph, run_via_graph
from .tasks import Task, TaskType
from .worker import StubWorker
from .worktree import list_worktrees


def _init_repo(path: Path) -> None:
    def g(*a: str) -> None:
        subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)

    g("init", "-b", "main")
    g("config", "user.email", "demo@example.com")
    g("config", "user.name", "Demo")
    (path / "README.md").write_text("# throwaway target repo\n")
    (path / "widget.py").write_text("def add(a, b):\n    return a + b\n")
    g("add", "-A")
    g("commit", "-m", "init")


def main() -> None:
    use_sdk = "--sdk" in sys.argv
    holder = Path(tempfile.mkdtemp(prefix="mc-graph-demo-"))
    target = holder / "repo"
    target.mkdir()
    _init_repo(target)

    if use_sdk:
        from .sdk_worker import SdkWorker

        worker = SdkWorker()
    else:
        worker = StubWorker()

    graph = build_run_graph(target, worker=worker)
    task = Task("probe-graph-001", TaskType.READ_ONLY, "survey the target repo", greenfield=False)

    before = list_worktrees(target)
    print(
        f"{roles.ORCHESTRATOR} dispatching a {roles.WORKER} through the LangGraph "
        f"shell on a '{roles.SIM}' task ({task.task_id})"
    )
    print(f"  worktrees before dispatch: {len(before)} (main only)")
    print("  node trace:")

    thread_id = f"run-{task.task_id}"
    initial = {
        "task_id": task.task_id,
        "task_type": task.task_type.value,
        "prompt": task.prompt,
        "greenfield": task.greenfield,
        "decision": None,
        "applied": False,
    }
    live = len(list_worktrees(target))
    for update in graph.stream(initial, config={"configurable": {"thread_id": thread_id}},
                               stream_mode="updates"):
        for node in update:
            live = len(list_worktrees(target))
            print(f"    → {node:11} (worktrees now: {live})")

    after = list_worktrees(target)
    final = graph.get_state({"configurable": {"thread_id": thread_id}}).values
    print(f"  {roles.WORKER} reported: {final.get('worker_summary')}")
    print(f"  outcome={final.get('outcome')} applied={final.get('applied')} decision={final.get('decision')}")
    print(f"  worktrees after teardown: {len(after)} (main only)")

    assert len(after) == 1, f"worktree leak: {after}"
    assert after == before, "worktree set changed after teardown"
    print(f"clean teardown — no worktree leaks. {roles.ORCHESTRATOR} out.")


if __name__ == "__main__":
    main()
