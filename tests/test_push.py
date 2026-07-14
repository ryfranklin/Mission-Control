"""Push (the write direction): an approved burn's merged result leaves the host.

A LOCAL BARE REPO stands in for the remote — no network. The invariants under test:
go → merge → push updates the remote; no-go / sim push nothing; a non-fast-forward
push surfaces a distinct terminal state and NEVER force-pushes; a crash-resume re-push
is a clean no-op."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mission_control import project_ref, repo_source, roles, worktree
from mission_control.graph import (
    PUSH_CONFLICT,
    PUSH_PUSHED,
    _Deps,
    _apply_burn,
    _dispatch,
    _run_worker,
    _teardown,
    awaiting_gate,
    build_run_graph,
    resume_gate,
    run_via_graph,
)
from mission_control.runs_store import (
    STATUS_APPLIED,
    STATUS_DONE,
    STATUS_MERGE_CONFLICT,
    STATUS_SCRUBBED,
)
from mission_control.tasks import Task, TaskType
from mission_control.worker import StubWorker

STUB_BURN_FILE = "STUB_BURN.txt"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def remote(tmp_path):
    """A bare repo (the stand-in 'remote') with a ``main`` trunk holding one commit."""
    work = tmp_path / "seed"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    (work / "README.md").write_text("# seed\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "trunk")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(work), str(bare)],
                   check=True, capture_output=True)
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    return bare


def _remote_tip(bare: Path) -> str:
    return _git(bare, "rev-parse", "refs/heads/main").strip()


def _remote_tree(bare: Path) -> list[str]:
    return _git(bare, "ls-tree", "-r", "--name-only", "refs/heads/main").split()


def _advance_remote(bare: Path, tmp_path: Path, filename: str, content: str) -> None:
    """Push a fresh commit to the bare remote's main from a throwaway clone."""
    work = tmp_path / f"advance-{filename}"
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    (work / filename).write_text(content)
    _git(work, "add", "-A")
    _git(work, "commit", "-m", f"advance {filename}")
    _git(work, "push", "origin", "main")


# -- full-graph flows: go pushes; no-go / sim push nothing ------------------

def test_go_merges_and_pushes_to_remote(remote, tmp_path, mem_store):
    graph = build_run_graph(target_ref=str(remote), cache_root=tmp_path / "cache",
                            worker=StubWorker(), runs_store=mem_store)
    run_via_graph(graph, Task("burn-go", TaskType.SIDE_EFFECTFUL, "change"), thread_id="burn-go")
    assert awaiting_gate(graph, "burn-go")
    assert STUB_BURN_FILE not in _remote_tree(remote)   # nothing pushed before approval

    final = resume_gate(graph, "burn-go", roles.GO)
    assert final["applied"] is True
    assert final["push_status"] == PUSH_PUSHED
    assert STUB_BURN_FILE in _remote_tree(remote)         # the burn reached the remote
    assert mem_store.get_run("burn-go").status == STATUS_APPLIED
    local = project_ref.cache_dir_for(str(remote), root=tmp_path / "cache").resolve()
    assert len(worktree.list_worktrees(local)) == 1       # clean teardown, no leak


def test_nogo_pushes_nothing(remote, tmp_path, mem_store):
    before = _remote_tip(remote)
    graph = build_run_graph(target_ref=str(remote), cache_root=tmp_path / "cache",
                            worker=StubWorker(), runs_store=mem_store)
    run_via_graph(graph, Task("burn-nogo", TaskType.SIDE_EFFECTFUL, "change"), thread_id="burn-nogo")
    final = resume_gate(graph, "burn-nogo", roles.NO_GO)
    assert final["applied"] is False
    assert final.get("push_status") is None               # never reached the push
    assert _remote_tip(remote) == before                  # remote untouched
    assert mem_store.get_run("burn-nogo").status == STATUS_SCRUBBED


def test_sim_pushes_nothing(remote, tmp_path, mem_store):
    before = _remote_tip(remote)
    graph = build_run_graph(target_ref=str(remote), cache_root=tmp_path / "cache",
                            worker=StubWorker(), runs_store=mem_store)
    final = run_via_graph(graph, Task("sim-1", TaskType.READ_ONLY, "look"), thread_id="sim-1")
    assert final["outcome"] == "completed"
    assert final.get("push_status") is None
    assert _remote_tip(remote) == before                  # a sim never writes to the remote
    assert mem_store.get_run("sim-1").status == STATUS_DONE


# -- push integrates a non-conflicting remote advance ----------------------

def test_push_integrates_nonconflicting_remote_advance(remote, tmp_path):
    """The simple case: the remote advanced on an unrelated file; push fetches +
    integrates it and still lands (no stale-ref failure)."""
    local = repo_source.ensure_local(str(remote), root=tmp_path / "cache")
    _git(local, "config", "user.email", "t@example.com")
    _git(local, "config", "user.name", "T")
    (local / "ours.txt").write_text("ours\n")             # stand-in for a merged burn
    _git(local, "add", "-A")
    _git(local, "commit", "-m", "our change")

    _advance_remote(remote, tmp_path, "theirs.txt", "theirs\n")  # remote moved ahead

    repo_source.push_to_remote(local, "main")
    tree = _remote_tree(remote)
    assert "ours.txt" in tree and "theirs.txt" in tree    # both integrated and pushed


# -- conflicting remote advance → distinct merge_conflict state, no force-push --

def test_merge_conflict_is_distinct_state_and_never_forces(remote, tmp_path, mem_store):
    cache = tmp_path / "cache"
    deps = _Deps(None, StubWorker(), target_ref=str(remote), cache_root=cache,
                 runs_store=mem_store)
    st = {"run_id": "burn-cfl", "task_id": "burn-cfl", "task_type": roles.BURN,
          "prompt": "change", "decision": roles.GO}
    st.update(_dispatch(deps, st))     # clone + carve worktree off origin/main
    st.update(_run_worker(deps, st))   # burn writes STUB_BURN.txt (content A)

    # While "at the gate", the remote advances with a CONFLICTING STUB_BURN.txt.
    _advance_remote(remote, tmp_path, STUB_BURN_FILE, "conflicting content B\n")
    remote_before = _remote_tip(remote)

    st.update(_apply_burn(deps, st))   # merge locally, then push → integrate CONFLICT
    assert st["push_status"] == PUSH_CONFLICT
    assert STUB_BURN_FILE in st["push_detail"]            # the conflicting file is surfaced
    assert _remote_tip(remote) == remote_before           # NOT force-pushed / overwritten
    assert STUB_BURN_FILE in _remote_tree(remote)         # remote still has THEIR version
    assert "conflicting content B" in _git(remote, "show", "refs/heads/main:" + STUB_BURN_FILE)

    st.update(_teardown(deps, st))
    assert mem_store.get_run("burn-cfl").status == STATUS_MERGE_CONFLICT
    local = project_ref.cache_dir_for(str(remote), root=cache).resolve()
    assert len(worktree.list_worktrees(local)) == 1       # no leak even on conflict


# -- idempotent re-push (a crash between merge and push, or after) ----------

def test_repush_after_crash_is_clean_noop(remote, tmp_path, mem_store):
    cache = tmp_path / "cache"
    deps = _Deps(None, StubWorker(), target_ref=str(remote), cache_root=cache,
                 runs_store=mem_store)
    st = {"run_id": "burn-idem", "task_id": "burn-idem", "task_type": roles.BURN,
          "prompt": "change", "decision": roles.GO}
    st.update(_dispatch(deps, st))
    st.update(_run_worker(deps, st))

    st.update(_apply_burn(deps, st))                      # first apply: merge + push
    assert st["push_status"] == PUSH_PUSHED
    tip_after_first = _remote_tip(remote)

    # Re-run the WHOLE idempotent node, as a crash-resume would.
    again = _apply_burn(deps, st)
    assert again["applied"] is True
    assert again["push_status"] == PUSH_PUSHED            # already-pushed → no-op success
    assert _remote_tip(remote) == tip_after_first          # exactly one push; not doubled

    st.update(_teardown(deps, st))
    assert mem_store.get_run("burn-idem").status == STATUS_APPLIED
    local = project_ref.cache_dir_for(str(remote), root=cache).resolve()
    assert len(worktree.list_worktrees(local)) == 1
