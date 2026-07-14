"""Tests for the acquisition layer: clone/fetch a target into the per-host cache and
carve worktrees off the fetched remote trunk.

A LOCAL BARE REPO stands in for the remote, so nothing here touches the network."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mission_control import project_ref, repo_source, worktree
from mission_control.graph import build_run_graph, run_via_graph
from mission_control.repo_source import RepoAcquireError
from mission_control.tasks import Task, TaskType


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def remote(tmp_path):
    """A bare repo (the stand-in 'remote') with a ``main`` trunk holding one commit.
    Its HEAD points at ``main`` so a clone sets ``origin/HEAD`` → ``origin/main``."""
    work = tmp_path / "seed"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    (work / "README.md").write_text("# seed\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "trunk commit")

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(work), str(bare)],
                   check=True, capture_output=True)
    # Make the bare repo's default branch explicit (so origin/HEAD resolves on clone).
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    return bare


def _remote_head(bare: Path) -> str:
    return _git(bare, "rev-parse", "refs/heads/main").strip()


# -- ensure_local: clone when absent, fetch when present -------------------

def test_ensure_local_clones_when_cache_absent(remote, tmp_path):
    cache_root = tmp_path / "cache"
    ref = str(remote)  # a local bare repo path is a perfectly cloneable "ref"

    local = repo_source.ensure_local(ref, root=cache_root)

    assert local == project_ref.cache_dir_for(ref, root=cache_root).resolve()
    assert local.is_dir() and (local / ".git").exists()
    # It's a clone of the remote: same trunk commit, origin wired up.
    assert _git(local, "rev-parse", "refs/remotes/origin/main").strip() == _remote_head(remote)


def test_ensure_local_fetches_when_cache_present(remote, tmp_path):
    cache_root = tmp_path / "cache"
    ref = str(remote)
    local = repo_source.ensure_local(ref, root=cache_root)  # first: clone
    first_head = _git(local, "rev-parse", "refs/remotes/origin/main").strip()

    # Advance the remote by one commit.
    clone2 = tmp_path / "pusher"
    subprocess.run(["git", "clone", str(remote), str(clone2)], check=True, capture_output=True)
    _git(clone2, "config", "user.email", "t@example.com")
    _git(clone2, "config", "user.name", "T")
    (clone2 / "NEW.md").write_text("new\n")
    _git(clone2, "add", "-A")
    _git(clone2, "commit", "-m", "second commit")
    _git(clone2, "push", "origin", "main")

    # Second ensure_local reuses the SAME cache dir and fetches the new commit.
    local2 = repo_source.ensure_local(ref, root=cache_root)
    assert local2 == local  # same cache dir, not re-cloned
    second_head = _git(local2, "rev-parse", "refs/remotes/origin/main").strip()
    assert second_head != first_head
    assert second_head == _remote_head(remote)


def test_ensure_local_in_place_for_existing_working_repo(remote, tmp_path):
    """A ref that already names a non-bare working repo is used in place (no clone) —
    the remote-less fallback / operator's-own-checkout path stays unchanged."""
    work = tmp_path / "checkout"
    subprocess.run(["git", "clone", str(remote), str(work)], check=True, capture_output=True)
    local = repo_source.ensure_local(str(work), root=tmp_path / "cache")
    assert local == work.resolve()
    assert not (tmp_path / "cache").exists()  # nothing acquired


def test_ensure_local_fails_loudly_on_bad_ref(tmp_path):
    with pytest.raises(RepoAcquireError):
        repo_source.ensure_local(str(tmp_path / "does-not-exist.git"), root=tmp_path / "cache")
    # No half-written cache dir left behind to be mistaken for a present clone.
    bad_ref = str(tmp_path / "does-not-exist.git")
    assert not project_ref.cache_dir_for(bad_ref, root=tmp_path / "cache").exists()


# -- trunk discovery -------------------------------------------------------

def test_trunk_of_discovers_via_origin_head(remote, tmp_path):
    local = repo_source.ensure_local(str(remote), root=tmp_path / "cache")
    assert repo_source.trunk_of(local) == "main"
    assert repo_source.default_base(local) == "origin/main"


def test_trunk_of_falls_back_to_probing_origin_branches(remote, tmp_path):
    local = repo_source.ensure_local(str(remote), root=tmp_path / "cache")
    # Drop the origin/HEAD symref so discovery must probe origin/main.
    subprocess.run(["git", "-C", str(local), "symbolic-ref", "-d", "refs/remotes/origin/HEAD"],
                   check=True, capture_output=True)
    assert repo_source.trunk_of(local) == "main"


def test_default_base_falls_back_to_head_without_origin(tmp_path):
    """A remote-less repo has no trunk to track → base is HEAD (unchanged behavior)."""
    repo = tmp_path / "local"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    assert repo_source.default_base(repo) == "HEAD"


# -- worktree carved off origin/<trunk> (the branch point) -----------------

def test_worktree_carved_off_origin_trunk(remote, tmp_path):
    """A worktree carved off origin/main branches from the remote's trunk commit —
    NOT from a divergent local HEAD."""
    local = repo_source.ensure_local(str(remote), root=tmp_path / "cache")

    # Make the cache clone's local HEAD diverge from origin/main, so "HEAD" and
    # "origin/main" are different commits — the assertion then proves we used the trunk.
    _git(local, "config", "user.email", "t@example.com")
    _git(local, "config", "user.name", "T")
    (local / "LOCAL_ONLY.md").write_text("divergent\n")
    _git(local, "add", "-A")
    _git(local, "commit", "-m", "divergent local HEAD")
    assert _git(local, "rev-parse", "HEAD").strip() != _git(local, "rev-parse", "origin/main").strip()

    base = repo_source.default_base(local)
    wt = worktree.create_worktree(local, "task-x", base=base)
    try:
        origin_main = _git(local, "rev-parse", "origin/main").strip()
        # The new branch's start point is the trunk commit, not the divergent HEAD.
        assert _git(wt.path, "rev-parse", "HEAD").strip() == origin_main
    finally:
        worktree.remove_worktree(wt)
    assert len(worktree.list_worktrees(local)) == 1  # no leak after teardown


def test_dispatch_acquires_remote_and_runs_sim_on_fresh_machine(remote, tmp_path):
    """Acceptance: pointing MC at a remote ref with an EMPTY cache clones it, carves a
    worktree off origin/<trunk>, runs a sim, and leaves no worktree leak."""
    from mission_control.worker import StubWorker

    cache_root = tmp_path / "cache"
    assert not cache_root.exists()  # a fresh machine: nothing cached yet

    graph = build_run_graph(
        target_ref=str(remote), cache_root=cache_root, worker=StubWorker()
    )
    final = run_via_graph(graph, Task("sim-remote", TaskType.READ_ONLY, "look around"))

    assert final["outcome"] == "completed"
    # The target was acquired into the per-host cache (not the operator's cwd).
    local = project_ref.cache_dir_for(str(remote), root=cache_root).resolve()
    assert final["local_repo"] == str(local)
    assert local.is_dir()
    # The run carved off the remote trunk and tore down cleanly (leak check holds).
    assert len(worktree.list_worktrees(local)) == 1


def test_create_worktree_defaults_to_head(tmp_path):
    """Without an explicit base, create_worktree still carves off HEAD (unchanged)."""
    repo = tmp_path / "local"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    wt = worktree.create_worktree(repo, "t")
    try:
        assert _git(wt.path, "rev-parse", "HEAD").strip() == _git(repo, "rev-parse", "HEAD").strip()
    finally:
        worktree.remove_worktree(wt)
    assert len(worktree.list_worktrees(repo)) == 1
