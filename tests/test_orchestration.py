"""Phase 0 orchestration behavior:

- dispatch works (a worker runs and reports),
- worktree isolation + clean teardown (no leaks),
- the approval gate blocks a side-effectful task until approved,
- terminate (scrub) kills a task and cleans up.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mission_control import Orchestrator, StubWorker, Task, TaskType
from mission_control import roles
from mission_control.orchestrator import (
    OUTCOME_BLOCKED,
    OUTCOME_COMPLETED,
    OUTCOME_TERMINATED,
)
from mission_control.worktree import list_worktrees

_STUB_BURN_FILE = "STUB_BURN.txt"


def _sim(task_id: str = "sim-1") -> Task:
    return Task(task_id=task_id, task_type=TaskType.READ_ONLY, prompt="look around")


def _burn(task_id: str = "burn-1") -> Task:
    return Task(task_id=task_id, task_type=TaskType.SIDE_EFFECTFUL, prompt="make a change")


def _head_files(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout


# -- dispatch --------------------------------------------------------------

def test_dispatch_runs_worker_and_reports(target_repo: Path):
    orch = Orchestrator(target_repo)
    result = orch.run_task(_sim())

    assert result.outcome == OUTCOME_COMPLETED
    assert result.worker_result is not None
    assert result.worker_result.made_changes is False
    assert str(_sim().task_id) in result.worker_result.summary


def test_worker_runs_inside_the_worktree_not_the_target(target_repo: Path):
    # A worker that records where it was run proves isolation from the target repo.
    seen: dict[str, Path] = {}

    class RecordingWorker(StubWorker):
        def investigate(self, task, workdir):
            seen["workdir"] = Path(workdir)
            return super().investigate(task, workdir)

    orch = Orchestrator(target_repo, worker=RecordingWorker())
    orch.run_task(_sim())

    assert seen["workdir"] != target_repo
    assert not seen["workdir"].is_relative_to(target_repo)


# -- isolation + teardown --------------------------------------------------

def test_no_worktree_leak_after_run(target_repo: Path):
    orch = Orchestrator(target_repo)
    before = list_worktrees(target_repo)
    assert len(before) == 1  # main only

    orch.run_task(_sim())

    after = list_worktrees(target_repo)
    assert after == before  # exactly the main worktree remains


def test_teardown_runs_even_when_worker_raises(target_repo: Path):
    class ExplodingWorker(StubWorker):
        def investigate(self, task, workdir):
            raise RuntimeError("boom")

    orch = Orchestrator(target_repo, worker=ExplodingWorker())
    with pytest.raises(RuntimeError):
        orch.run_task(_sim())

    assert len(list_worktrees(target_repo)) == 1  # no leak despite the crash


def test_sim_never_touches_the_target(target_repo: Path):
    orch = Orchestrator(target_repo)
    before = _head_files(target_repo)
    orch.run_task(_sim())
    assert _head_files(target_repo) == before


# -- go / no-go approval gate ----------------------------------------------

def test_burn_blocked_without_approval(target_repo: Path):
    orch = Orchestrator(target_repo)
    result = orch.run_task(_burn())  # no approver wired up

    assert result.applied is False
    assert result.decision == roles.NO_GO
    assert result.outcome == OUTCOME_BLOCKED
    assert _STUB_BURN_FILE not in _head_files(target_repo)


def test_burn_blocked_on_explicit_no_go(target_repo: Path):
    orch = Orchestrator(target_repo)
    result = orch.run_task(_burn(), approval=lambda run: False)

    assert result.applied is False
    assert result.decision == roles.NO_GO
    assert _STUB_BURN_FILE not in _head_files(target_repo)


def test_burn_applied_on_go(target_repo: Path):
    orch = Orchestrator(target_repo)
    result = orch.run_task(_burn(), approval=lambda run: True)

    assert result.applied is True
    assert result.decision == roles.GO
    assert result.outcome == OUTCOME_COMPLETED
    assert _STUB_BURN_FILE in _head_files(target_repo)
    # And still no leaked worktree after applying.
    assert len(list_worktrees(target_repo)) == 1


def test_approver_can_inspect_pending_changes(target_repo: Path):
    # The gate hands the approver a live run so it can inspect the worktree diff.
    inspected: dict[str, bool] = {}

    def approve(run) -> bool:
        inspected["saw_marker"] = (run.worktree.path / _STUB_BURN_FILE).exists()
        return inspected["saw_marker"]

    orch = Orchestrator(target_repo)
    result = orch.run_task(_burn(), approval=approve)

    assert inspected["saw_marker"] is True
    assert result.applied is True


# -- terminate (scrub) -----------------------------------------------------

def test_terminate_kills_task_and_cleans_up(target_repo: Path):
    orch = Orchestrator(target_repo)
    run = orch.dispatch(_burn())

    # A dispatched task holds a live, isolated worktree.
    assert len(list_worktrees(target_repo)) == 2
    assert run.worktree.path.exists()

    result = orch.terminate(run)

    assert result.outcome == OUTCOME_TERMINATED
    assert result.applied is False
    assert run.live is False
    assert not run.worktree.path.exists()
    assert len(list_worktrees(target_repo)) == 1  # no leak
    assert _STUB_BURN_FILE not in _head_files(target_repo)  # work discarded
