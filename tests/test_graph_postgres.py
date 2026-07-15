"""Durability via PostgresSaver (needs the Dockerized Postgres; skipped if down).

Proves: state persists across a checkpointer/"process" restart, resume continues
from the last completed node, and completed worker steps are NOT re-executed
(no re-pay). Uses `interrupt_before=["gate"]` to stop the graph right after the
worker node has checkpointed, then resumes with a fresh checkpointer + graph.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from mission_control import StubWorker, TaskType, roles
from mission_control.graph import (
    awaiting_gate,
    build_run_graph,
    postgres_checkpointer,
    resume_gate,
    run_via_graph,
    worker_cost_usd,
)
from mission_control.tasks import Task
from mission_control.worker import WorkerResult
from mission_control.worktree import list_worktrees

STUB_BURN_FILE = "STUB_BURN.txt"


def _tracked(repo) -> list[str]:
    return subprocess.run(["git", "-C", str(repo), "ls-files"],
                          check=True, capture_output=True, text=True).stdout.split()


@pytest.fixture
def pg_ready():
    """Skip the module's tests unless the Dockerized Postgres is reachable."""
    try:
        _cp, pool = postgres_checkpointer(setup=True)
        pool.close()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable (run `docker compose up -d`): {e}")


class _CountingWorker(StubWorker):
    def __init__(self) -> None:
        self.calls = 0

    def investigate(self, task, workdir) -> WorkerResult:
        self.calls += 1
        return super().investigate(task, workdir)


def _initial():
    return {
        "task_id": "pg-run",
        "task_type": TaskType.READ_ONLY.value,
        "prompt": "look",
        "greenfield": False,
        "decision": None,
        "applied": False,
    }


def _cfg(thread):
    return {"configurable": {"thread_id": thread}}


def _head(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


def test_state_persists_across_restart_and_resumes_from_last_node(target_repo, pg_ready):
    thread = f"t-{uuid4().hex}"

    # Run 1: stop right after run_worker checkpoints (interrupt before gate).
    w1 = _CountingWorker()
    cp1, pool1 = postgres_checkpointer(setup=True)
    g1 = build_run_graph(target_repo, worker=w1, checkpointer=cp1, interrupt_before=["gate"])
    g1.invoke(_initial(), config=_cfg(thread))
    assert w1.calls == 1
    pool1.close()  # end of "process 1"

    # Fresh checkpointer + graph ("process 2") reads the persisted state.
    cp2, pool2 = postgres_checkpointer(setup=False)
    g2 = build_run_graph(target_repo, worker=StubWorker(), checkpointer=cp2)
    snap = g2.get_state(_cfg(thread))
    assert snap.next == ("gate",)                 # stopped after run_worker
    assert snap.values.get("worker_summary")      # state survived the restart
    assert len(list_worktrees(target_repo)) == 2  # worktree still live

    # Resume continues from the last completed node → completes cleanly.
    final = g2.invoke(None, config=_cfg(thread))
    pool2.close()
    assert final["outcome"] == "completed"
    assert len(list_worktrees(target_repo)) == 1  # resume left no worktree leak


def test_completed_worker_steps_are_not_reexecuted_on_resume(target_repo, pg_ready):
    thread = f"t-{uuid4().hex}"

    w1 = _CountingWorker()
    cp1, pool1 = postgres_checkpointer(setup=True)
    g1 = build_run_graph(target_repo, worker=w1, checkpointer=cp1, interrupt_before=["gate"])
    g1.invoke(_initial(), config=_cfg(thread))
    assert w1.calls == 1                           # paid once
    saved = worker_cost_usd(g1.get_state(_cfg(thread)).values)
    assert saved > 0                               # there is a cost that resume avoids
    pool1.close()

    # Resume in a fresh process with a fresh worker: it must NOT be called.
    w2 = _CountingWorker()
    cp2, pool2 = postgres_checkpointer(setup=False)
    g2 = build_run_graph(target_repo, worker=w2, checkpointer=cp2)
    final = g2.invoke(None, config=_cfg(thread))
    pool2.close()

    assert w2.calls == 0                           # completed worker step NOT re-paid
    assert final["outcome"] == "completed"
    assert len(list_worktrees(target_repo)) == 1   # no leak across restart+resume


# -- L3: durable go/no-go interrupt across a process restart ---------------

def test_gate_interrupt_persists_across_restart_then_go_applies_once(target_repo, pg_ready):
    thread = f"gate-{uuid4().hex}"
    task = Task(f"burn-{uuid4().hex[:6]}", TaskType.SIDE_EFFECTFUL, "change")

    # Run 1: burn pauses at the durable gate interrupt; process ends.
    cp1, pool1 = postgres_checkpointer(setup=True)
    g1 = build_run_graph(target_repo, worker=StubWorker(), checkpointer=cp1)
    run_via_graph(g1, task, thread_id=thread)
    assert awaiting_gate(g1, thread)                       # paused at gate
    assert STUB_BURN_FILE not in _tracked(target_repo)     # NOT applied without approval
    pool1.close()                                          # "kill" the process

    # Restart: fresh checkpointer/graph reads the persisted interrupt.
    cp2, pool2 = postgres_checkpointer(setup=False)
    g2 = build_run_graph(target_repo, worker=StubWorker(), checkpointer=cp2)
    assert awaiting_gate(g2, thread)                       # interrupt survived restart
    assert STUB_BURN_FILE not in _tracked(target_repo)     # still nothing applied

    final = resume_gate(g2, thread, roles.GO)              # approve after restart
    pool2.close()
    assert final["decision"] == roles.GO
    assert final["applied"] is True                        # proceeded into apply-burn
    assert STUB_BURN_FILE in _tracked(target_repo)         # applied exactly once
    assert len(list_worktrees(target_repo)) == 1           # clean teardown, no leak


def test_gate_nogo_scrubs_across_restart(target_repo, pg_ready):
    thread = f"gate-{uuid4().hex}"
    task = Task(f"burn-{uuid4().hex[:6]}", TaskType.SIDE_EFFECTFUL, "change")
    before = _head(target_repo)

    cp1, pool1 = postgres_checkpointer(setup=True)
    g1 = build_run_graph(target_repo, worker=StubWorker(), checkpointer=cp1)
    run_via_graph(g1, task, thread_id=thread)
    assert awaiting_gate(g1, thread)
    pool1.close()

    cp2, pool2 = postgres_checkpointer(setup=False)
    g2 = build_run_graph(target_repo, worker=StubWorker(), checkpointer=cp2)
    final = resume_gate(g2, thread, roles.NO_GO)           # reject after restart
    pool2.close()
    assert final["decision"] == roles.NO_GO
    assert final["applied"] is False                       # never applied
    assert final["outcome"] == "blocked"
    assert _head(target_repo) == before                    # scrub: target unchanged
    assert STUB_BURN_FILE not in _tracked(target_repo)
    assert len(list_worktrees(target_repo)) == 1           # scrub left no leak


# -- a failed worker still records what it spent (no silent $0) -------------

def test_failed_worker_records_cost_and_telemetry(pg_ready, target_repo, tmp_path):
    """Regression: a worker that fails AFTER burning tokens (e.g. hit the turn cap)
    must have that cost recorded on the ledger and emitted to the JSONL spine — not
    dropped as $0."""
    from mission_control.graph import build_run_graph, build_runs_store, run_tracked
    from mission_control.runs_store import STATUS_FAILED
    from mission_control.sdk_worker import WorkerError
    from mission_control.telemetry import StepUsage

    _cp, pool = postgres_checkpointer(setup=True)
    store = build_runs_store(pool, setup=True)

    class _FailedAfterSpending(StubWorker):
        def investigate(self, task, workdir):
            # a real run that ran out of turns still consumed tokens
            raise WorkerError(
                "worker error: Reached maximum number of turns (200)",
                steps=[StepUsage(model="claude-haiku-4-5", input_tokens=120_000,
                                 output_tokens=6_000, cache_read_tokens=40_000,
                                 latency_ms=240_000)],
            )

    tel = tmp_path / "tel"
    tel.mkdir()
    graph = build_run_graph(target_repo, worker=_FailedAfterSpending(),
                            telemetry_dir=tel, runs_store=store)
    rid = f"fail-cost-{uuid4().hex[:8]}"
    try:
        with pytest.raises(Exception):
            run_tracked(graph, store,
                        Task(rid, TaskType.SIDE_EFFECTFUL, "a big change"), thread_id=rid)

        row = store.get_run(rid)
        assert row.status == STATUS_FAILED
        assert row.cost_usd > 0                      # the burned tokens ARE priced
        assert list(tel.glob("*.jsonl"))             # ...and land on the spine
    finally:
        pool.close()
