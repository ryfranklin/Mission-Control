"""Manual run: a Flight Director dispatches a Controller on a read-only task in
an isolated worktree, then tears it down with no leaks.

Run it:  ``python -m mission_control.demo``

It builds a throwaway git repo, runs one read-only (sim) task through the
orchestrator, and asserts no worktree leaks. Metaphor terms in the output come
straight from :mod:`.roles`.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from . import roles
from .orchestrator import Orchestrator
from .tasks import Task, TaskType
from .worktree import list_worktrees


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "-C", str(path), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "demo@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Demo"], check=True, capture_output=True)
    (path / "README.md").write_text("# throwaway target repo\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)


def main() -> None:
    holder = Path(tempfile.mkdtemp(prefix="mc-demo-target-"))
    target = holder / "repo"
    target.mkdir()
    _init_repo(target)

    orch = Orchestrator(target_repo=target, telemetry_dir=holder / "telemetry")
    task = Task(
        task_id="probe-001",
        task_type=TaskType.READ_ONLY,
        prompt="survey the target repo layout",
    )

    before = list_worktrees(target)
    print(f"{roles.ORCHESTRATOR} dispatching a {roles.WORKER} on a '{roles.SIM}' task ({task.task_id})")
    print(f"  worktrees before dispatch: {len(before)} (main only)")

    result = orch.run_task(task)

    after = list_worktrees(target)
    print(f"  {roles.WORKER} reported: {result.worker_result.summary}")
    print(f"  outcome={result.outcome} applied={result.applied} decision={result.decision}")
    print(f"  worktrees after teardown: {len(after)} (main only)")

    assert len(after) == 1, f"worktree leak: {after}"
    assert after == before, "worktree set changed after teardown"
    print(f"clean teardown — no worktree leaks. {roles.ORCHESTRATOR} out.")
    print(f"  {result.telemetry.summary_line()}")


if __name__ == "__main__":
    main()
