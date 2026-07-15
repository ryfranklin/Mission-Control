"""LangGraph orchestration shell (offline: StubWorker + MemorySaver).

Covers the graph flow (sim / burn-go / burn-no-go), the durable go/no-go
interrupt, no worktree leaks, and the load-bearing idempotency of the
node-boundary recovery model (apply-burn and dispatch must be safe to re-run)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from mission_control import StubWorker, Task, TaskType, roles
from mission_control.graph import (
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
from mission_control.worktree import list_worktrees

STUB_BURN_FILE = "STUB_BURN.txt"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _tracked(repo: Path) -> list[str]:
    return _git(repo, "ls-files").split()


# -- flows through the compiled graph --------------------------------------

def test_sim_runs_through_graph_clean(target_repo):
    graph = build_run_graph(target_repo, worker=StubWorker())
    before = _head(target_repo)
    final = run_via_graph(graph, Task("sim-1", TaskType.READ_ONLY, "look"))
    assert final["outcome"] == "completed"
    assert final["applied"] is False
    assert final["decision"] is None
    assert _head(target_repo) == before                 # read-only: target untouched
    assert len(list_worktrees(target_repo)) == 1         # no leak


def test_burn_pauses_at_gate_then_go_applies(target_repo):
    graph = build_run_graph(target_repo, worker=StubWorker())
    run_via_graph(graph, Task("burn-1", TaskType.SIDE_EFFECTFUL, "change"), thread_id="burn-1")

    # Durable pause at the gate; NOTHING applied before approval.
    assert awaiting_gate(graph, "burn-1")
    assert STUB_BURN_FILE not in _tracked(target_repo)

    final = resume_gate(graph, "burn-1", roles.GO)
    assert final["applied"] is True
    assert final["decision"] == roles.GO
    assert final["outcome"] == "completed"
    assert STUB_BURN_FILE in _tracked(target_repo)       # applied on go
    assert len(list_worktrees(target_repo)) == 1


def test_ungated_writable_stage_auto_applies_without_gate(target_repo):
    """A writable-but-ungated stage (gated=False) — a v2 design/doc stage — writes its
    output and AUTO-APPLIES: it never halts at the gate, yet the change still lands."""
    graph = build_run_graph(target_repo, worker=StubWorker())
    final = run_via_graph(
        graph,
        Task("write-1", TaskType.SIDE_EFFECTFUL, "produce docs", gated=False),
        thread_id="write-1",
    )
    assert not awaiting_gate(graph, "write-1")            # never paused for a human
    assert final["decision"] == roles.GO                  # auto-approved
    assert final["applied"] is True
    assert STUB_BURN_FILE in _tracked(target_repo)        # its output landed
    assert len(list_worktrees(target_repo)) == 1          # no leak


def test_burn_nogo_scrubs_clean(target_repo):
    graph = build_run_graph(target_repo, worker=StubWorker())
    before = _head(target_repo)
    run_via_graph(graph, Task("burn-2", TaskType.SIDE_EFFECTFUL, "change"), thread_id="burn-2")
    assert awaiting_gate(graph, "burn-2")

    final = resume_gate(graph, "burn-2", roles.NO_GO)
    assert final["applied"] is False
    assert final["decision"] == roles.NO_GO
    assert final["outcome"] == "blocked"
    assert _head(target_repo) == before                  # no-go: target unchanged (scrub)
    assert STUB_BURN_FILE not in _tracked(target_repo)
    assert len(list_worktrees(target_repo)) == 1


def test_burn_never_applied_without_go(target_repo):
    # Any non-go decision must not apply; the guard/routing forbid it.
    graph = build_run_graph(target_repo, worker=StubWorker())
    before = _head(target_repo)
    run_via_graph(graph, Task("burn-3", TaskType.SIDE_EFFECTFUL, "change"), thread_id="burn-3")
    resume_gate(graph, "burn-3", roles.NO_GO)
    assert _head(target_repo) == before
    assert STUB_BURN_FILE not in _tracked(target_repo)


# -- node-boundary idempotency (a crash re-runs the WHOLE node) ------------

def test_apply_burn_node_is_idempotent(target_repo):
    deps = _Deps(Path(target_repo).resolve(), StubWorker())
    st = {"task_id": "b", "task_type": roles.BURN, "prompt": "x",
          "greenfield": False, "decision": roles.GO}
    st.update(_dispatch(deps, st))
    st.update(_run_worker(deps, st))

    st.update(_apply_burn(deps, st))
    head_after_first = _head(target_repo)
    assert STUB_BURN_FILE in _tracked(target_repo)

    _apply_burn(deps, st)  # re-run the whole node, as a crash-resume would
    assert _head(target_repo) == head_after_first        # no double-apply

    _teardown(deps, st)
    assert len(list_worktrees(target_repo)) == 1


def test_dispatch_node_is_idempotent(target_repo):
    deps = _Deps(Path(target_repo).resolve(), StubWorker())
    st = {"task_id": "s", "task_type": roles.SIM, "prompt": "x", "greenfield": False}
    st.update(_dispatch(deps, st))
    first_path = st["worktree_path"]
    assert len(list_worktrees(target_repo)) == 2

    again = _dispatch(deps, st)                          # re-run: reuse, don't leak
    assert again == {}
    assert st["worktree_path"] == first_path
    assert len(list_worktrees(target_repo)) == 2

    _teardown(deps, st)
    assert len(list_worktrees(target_repo)) == 1


def test_thread_id_state_is_retrievable(target_repo):
    graph = build_run_graph(target_repo, worker=StubWorker())
    run_via_graph(graph, Task("sim-tid", TaskType.READ_ONLY, "look"), thread_id="fixed-thread")
    snap = graph.get_state({"configurable": {"thread_id": "fixed-thread"}})
    assert snap.values["outcome"] == "completed"
