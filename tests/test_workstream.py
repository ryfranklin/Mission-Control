"""Workstreams: many teams build the same project on separate mc/ws/<name> branches
and reconcile into trunk through an explicit, gated promote. Independent changes promote
cleanly; overlapping changes surface a blocked: merge conflict (files listed) with the
remote left un-force-pushed. Conflicts becoming visible here is the intended behavior.

A LOCAL BARE REPO stands in for the remote; two clones stand in for two teams/hosts —
no network."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mission_control import repo_source, roles, worktree
from mission_control.graph import PUSH_PUSHED, resume_gate, run_via_graph
from mission_control.repo_source import MergeConflict
from mission_control.runs_store import STATUS_APPLIED
from mission_control.tasks import Task, TaskType
from mission_control.worker import StubWorker


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def remote(tmp_path):
    """A pristine bare-repo remote with a ``main`` trunk (no origin of its own)."""
    work = tmp_path / "seed"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    (work / "README.md").write_text("# seed\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "trunk")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(work), str(bare)], check=True, capture_output=True)
    _git(bare, "remote", "remove", "origin")
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    return bare


def _clone(remote: Path, dest: Path) -> Path:
    subprocess.run(["git", "clone", str(remote), str(dest)], check=True, capture_output=True)
    _git(dest, "config", "user.email", "t@example.com")
    _git(dest, "config", "user.name", "T")
    return dest


def _remote_files(remote: Path, branch: str) -> list[str]:
    return _git(remote, "ls-tree", "-r", "--name-only", f"refs/heads/{branch}").split()


def _remote_tip(remote: Path, branch: str) -> str:
    return _git(remote, "rev-parse", f"refs/heads/{branch}").strip()


def _commit_file(clone: Path, name: str, content: str) -> None:
    (clone / name).write_text(content)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", f"add {name}")


# -- ensure_workstream_branch ----------------------------------------------

def test_ensure_workstream_branch_creates_from_trunk_then_is_idempotent(remote, tmp_path):
    clone = _clone(remote, tmp_path / "c")
    branch = repo_source.ensure_workstream_branch(clone, "team1")
    assert branch == "mc/ws/team1"
    assert branch in _git(remote, "branch", "--list", branch)
    # Created off trunk → same tip as main.
    assert _remote_tip(remote, "mc/ws/team1") == _remote_tip(remote, "main")
    # Idempotent: a second call just re-confirms (no error, still one branch).
    assert repo_source.ensure_workstream_branch(clone, "team1") == "mc/ws/team1"


# -- two teams, non-overlapping changes, both promote cleanly ---------------

def _work_on_branch(clone: Path, branch: str, filename: str, content: str) -> None:
    """Carve a task worktree off origin/<branch>, make a change, and push it to that
    branch — the per-task workstream write path (repo_source.push_to_remote)."""
    wt = worktree.create_worktree(clone, f"task-{filename}", base=f"origin/{branch}")
    try:
        _commit_file(wt.path, filename, content)
        repo_source.push_to_remote(wt.path, branch, lock_repo=clone)
    finally:
        worktree.remove_worktree(wt)


def test_two_workstreams_nonoverlapping_promote_to_trunk_cleanly(remote, tmp_path):
    clone_a = _clone(remote, tmp_path / "team1")
    clone_b = _clone(remote, tmp_path / "team2")

    # Two teams, two workstream branches, non-overlapping files.
    repo_source.ensure_workstream_branch(clone_a, "team1")
    repo_source.ensure_workstream_branch(clone_b, "team2")
    _work_on_branch(clone_a, "mc/ws/team1", "alpha.txt", "alpha\n")
    _work_on_branch(clone_b, "mc/ws/team2", "beta.txt", "beta\n")

    # Each change is on its OWN branch; trunk is untouched so far.
    assert "alpha.txt" not in _remote_files(remote, "main")
    assert "beta.txt" not in _remote_files(remote, "main")

    # Promote both into trunk — independent changes reconcile cleanly.
    repo_source.promote(clone_a, "team1")
    repo_source.promote(clone_b, "team2")   # clone_b integrates team1's trunk advance
    trunk = _remote_files(remote, "main")
    assert "alpha.txt" in trunk and "beta.txt" in trunk   # both landed on trunk


# -- overlapping changes → blocked: merge conflict, files listed, no force ---

def test_overlapping_workstreams_conflict_at_promote(remote, tmp_path):
    clone_a = _clone(remote, tmp_path / "team1")
    clone_b = _clone(remote, tmp_path / "team2")
    repo_source.ensure_workstream_branch(clone_a, "team1")
    repo_source.ensure_workstream_branch(clone_b, "team2")

    # Both teams edit the SAME file differently on their own branches.
    _work_on_branch(clone_a, "mc/ws/team1", "shared.txt", "team1 version\n")
    _work_on_branch(clone_b, "mc/ws/team2", "shared.txt", "team2 version\n")

    # First promote wins cleanly.
    repo_source.promote(clone_a, "team1")
    assert "team1 version" in _git(remote, "show", "refs/heads/main:shared.txt")
    trunk_before = _remote_tip(remote, "main")

    # Second promote CONFLICTS: surfaced with the file, remote NOT force-pushed.
    with pytest.raises(MergeConflict) as exc:
        repo_source.promote(clone_b, "team2")
    assert "shared.txt" in exc.value.files
    assert _remote_tip(remote, "main") == trunk_before        # trunk untouched, not overwritten
    assert "team1 version" in _git(remote, "show", "refs/heads/main:shared.txt")
    assert len(worktree.list_worktrees(clone_b)) == 1         # promote worktree cleaned up


# -- overlapping changes conflict at the WORKSTREAM push (same branch, two hosts) --

def test_conflicting_push_to_same_workstream_surfaces_conflict(remote, tmp_path):
    clone_a = _clone(remote, tmp_path / "hostA")
    clone_b = _clone(remote, tmp_path / "hostB")
    repo_source.ensure_workstream_branch(clone_a, "shared")
    _git(clone_b, "fetch", "origin")     # host B now tracks mc/ws/shared at its BASE (f.txt absent)

    _work_on_branch(clone_a, "mc/ws/shared", "f.txt", "A\n")   # host A pushes first
    tip = _remote_tip(remote, "mc/ws/shared")

    # Host B works from its STALE base (pre-A) and pushes a divergent same-file change →
    # push_to_remote fetches, integrates origin/mc/ws/shared, and hits an add/add conflict.
    wt = worktree.create_worktree(clone_b, "task-b", base="origin/mc/ws/shared")
    try:
        _commit_file(wt.path, "f.txt", "B\n")
        with pytest.raises(MergeConflict) as exc:
            repo_source.push_to_remote(wt.path, "mc/ws/shared", lock_repo=clone_b)
        assert "f.txt" in exc.value.files
    finally:
        worktree.remove_worktree(wt)
    assert _remote_tip(remote, "mc/ws/shared") == tip          # not force-overwritten


# -- the graph runs a workstream burn onto mc/ws/<name>, not trunk ----------

def test_graph_workstream_burn_pushes_to_ws_branch_not_trunk(remote, tmp_path, mem_store):
    from mission_control.graph import build_run_graph

    cache = tmp_path / "cache"
    g = build_run_graph(target_ref=str(remote), cache_root=cache,
                        worker=StubWorker(), runs_store=mem_store)
    task = Task("burn-ws", TaskType.SIDE_EFFECTFUL, "change", workstream="team1")
    run_via_graph(g, task, thread_id="burn-ws")
    final = resume_gate(g, "burn-ws", roles.GO)

    assert final["push_status"] == PUSH_PUSHED
    assert mem_store.get_run("burn-ws").status == STATUS_APPLIED
    # The burn landed on the workstream branch, NOT trunk.
    assert "STUB_BURN.txt" in _remote_files(remote, "mc/ws/team1")
    assert "STUB_BURN.txt" not in _remote_files(remote, "main")
