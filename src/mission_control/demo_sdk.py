"""Manual end-to-end run: a Flight Director dispatches a Controller backed by the
REAL Claude Agent SDK worker on a read-only (sim) task, against a small sandbox
target repo, then tears the worktree down with no leaks.

Run it:  ``python -m mission_control.demo_sdk``

Requires the Claude Agent SDK to be able to authenticate (an authenticated
`claude` CLI or ``ANTHROPIC_API_KEY``). Uses the cheap default model
(``claude-haiku-4-5``). Pass a model id as the first argument to override.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from . import roles
from .orchestrator import Orchestrator
from .sdk_worker import DEFAULT_MODEL, SdkWorker
from .tasks import Task, TaskType
from .worktree import list_worktrees


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_sandbox(path: Path) -> None:
    """A tiny but non-trivial target repo for the worker to investigate."""
    _run(path, "init", "-b", "main")
    _run(path, "config", "user.email", "demo@example.com")
    _run(path, "config", "user.name", "Demo")
    (path / "README.md").write_text(
        "# widget-service\n\nA small service that formats widgets.\n"
    )
    (path / "widget.py").write_text(
        "def format_widget(name: str, size: int) -> str:\n"
        '    """Return a human-readable widget label."""\n'
        '    return f"{name} (size {size})"\n'
    )
    _run(path, "add", "-A")
    _run(path, "commit", "-m", "init widget-service")


def main() -> None:
    model = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODEL

    holder = Path(tempfile.mkdtemp(prefix="mc-demo-sdk-target-"))
    target = holder / "repo"
    target.mkdir()
    _init_sandbox(target)

    orch = Orchestrator(
        target_repo=target,
        worker=SdkWorker(model=model),
        telemetry_dir=holder / "telemetry",
    )
    task = Task(
        task_id="probe-sdk-001",
        task_type=TaskType.READ_ONLY,
        prompt=(
            "Read widget.py and README.md, then summarize in 2-3 sentences what "
            "this repo does and what the public function is."
        ),
    )

    before = list_worktrees(target)
    print(
        f"{roles.ORCHESTRATOR} dispatching a {roles.WORKER} on a '{roles.SIM}' "
        f"task ({task.task_id}) using model={model}"
    )
    print(f"  worktrees before dispatch: {len(before)} (main only)")
    print("  running the real Claude Agent SDK worker (this makes a live call)...")

    result = orch.run_task(task)

    after = list_worktrees(target)
    print(f"\n  {roles.WORKER} result:\n    {result.worker_result.summary}")
    print(
        f"\n  outcome={result.outcome} applied={result.applied} "
        f"decision={result.decision}"
    )
    print(f"  worktrees after teardown: {len(after)} (main only)")

    assert len(after) == 1, f"worktree leak: {after}"
    assert after == before, "worktree set changed after teardown"
    print(f"\nclean teardown — no worktree leaks. {roles.ORCHESTRATOR} out.")
    print(f"  {result.telemetry.summary_line()}")


if __name__ == "__main__":
    main()
