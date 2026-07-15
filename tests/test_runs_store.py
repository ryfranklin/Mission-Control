"""The explicit ``runs`` ledger in Postgres (alongside, not derived from, the
LangGraph checkpoint tables). Verifies the status lifecycle, cost accumulation,
upsert-by-run_id idempotency under node re-runs, and row consistency across a
kill + resume. Skipped unless the Dockerized Postgres is reachable."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from mission_control import StubWorker, Task, TaskType, roles
from mission_control.graph import (
    _Deps,
    _dispatch,
    _teardown,
    build_run_graph,
    build_runs_store,
    initial_state,
    postgres_checkpointer,
    resume_gate,
    run_tracked,
    thread_config,
    worker_cost_usd,
)
from mission_control import runs_store as rs
from mission_control.worktree import list_worktrees


@pytest.fixture
def pg_pool():
    """A checkpointer pool + a set-up runs store, or skip if Postgres is down."""
    try:
        cp, pool = postgres_checkpointer(setup=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable (run `docker compose up -d`): {e}")
    store = build_runs_store(pool, setup=True)
    try:
        yield cp, pool, store
    finally:
        pool.close()


def _rid(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


# -- status lifecycle ------------------------------------------------------

def test_full_run_drives_row_through_status_lifecycle(target_repo, pg_pool):
    cp, pool, store = pg_pool
    graph = build_run_graph(target_repo, worker=StubWorker(), checkpointer=cp, runs_store=store)
    task = Task("burn-life", TaskType.SIDE_EFFECTFUL, "change")
    run_id = _rid("burn-life")

    # Launch → run to the durable gate. queued → running → awaiting_gate.
    run_tracked(graph, store, task, thread_id=run_id)
    row = store.get_run(run_id)
    assert row is not None
    assert row.status == rs.STATUS_AWAITING_GATE
    assert row.task_type == roles.BURN
    assert row.target == str(Path(target_repo).resolve())
    assert row.created_at is not None
    assert row.started_at is not None          # dispatch stamped it (running)
    assert row.ended_at is None                # not terminal yet

    # Resume with go → apply → teardown. awaiting_gate → applied (terminal).
    resume_gate(graph, run_id, roles.GO)
    row = store.get_run(run_id)
    assert row.status == rs.STATUS_APPLIED
    assert row.ended_at is not None
    assert row.cost_usd > 0
    assert row.detail                          # worker summary recorded

    # Exactly one row for this run the whole time.
    assert len(store.list_runs({"run_id": run_id})) == 1
    assert len(list_worktrees(target_repo)) == 1


def test_sim_ends_done_and_nogo_ends_scrubbed(target_repo, pg_pool):
    cp, pool, store = pg_pool
    graph = build_run_graph(target_repo, worker=StubWorker(), checkpointer=cp, runs_store=store)

    sim_id = _rid("sim")
    run_tracked(graph, store, Task("sim-x", TaskType.READ_ONLY, "look"), thread_id=sim_id)
    assert store.get_run(sim_id).status == rs.STATUS_DONE   # sim never gates

    burn_id = _rid("burn-nogo")
    run_tracked(graph, store, Task("burn-x", TaskType.SIDE_EFFECTFUL, "change"), thread_id=burn_id)
    resume_gate(graph, burn_id, roles.NO_GO)
    row = store.get_run(burn_id)
    assert row.status == rs.STATUS_SCRUBBED
    assert row.ended_at is not None


# -- cost accumulation -----------------------------------------------------

def test_cost_accumulates_from_priced_step_events(target_repo, pg_pool):
    cp, pool, store = pg_pool
    graph = build_run_graph(target_repo, worker=StubWorker(), checkpointer=cp, runs_store=store)
    run_id = _rid("sim-cost")
    final = run_tracked(graph, store, Task("sim-c", TaskType.READ_ONLY, "look"), thread_id=run_id)

    # The ledger total equals the priced cost of the run's steps, to the cent-fraction.
    assert store.get_run(run_id).cost_usd == pytest.approx(worker_cost_usd(final), abs=1e-9)
    assert store.get_run(run_id).cost_usd > 0


# -- idempotency (node-boundary recovery: re-run node upserts, no dup) -----

def test_rerun_node_upserts_without_duplicating_row(target_repo, pg_pool):
    cp, pool, store = pg_pool
    deps = _Deps(Path(target_repo).resolve(), StubWorker(), runs_store=store)
    run_id = _rid("rerun")
    state = initial_state(Task("rerun", TaskType.SIDE_EFFECTFUL, "x"), run_id=run_id)
    store.launch(run_id, task_type=roles.BURN)

    # Re-run dispatch (a crash re-runs the WHOLE node): started_at stamped once.
    state.update(_dispatch(deps, state))
    first_started = store.get_run(run_id).started_at
    _dispatch(deps, state)                     # replay
    assert store.get_run(run_id).started_at == first_started   # not moved
    assert len(store.list_runs({"run_id": run_id})) == 1        # no duplicate

    # Drive to a terminal state, then replay teardown: still one row, ended_at fixed.
    state["decision"] = roles.GO
    state.update({"applied": True})
    state.update(_teardown(deps, state))
    row1 = store.get_run(run_id)
    assert row1.status == rs.STATUS_APPLIED and row1.ended_at is not None

    _teardown(deps, state)                     # replay the terminal node
    row2 = store.get_run(run_id)
    assert row2.ended_at == row1.ended_at      # once-only stamp held
    assert row2.cost_usd == row1.cost_usd      # absolute cost, not double-counted
    assert len(store.list_runs({"run_id": run_id})) == 1
    assert len(list_worktrees(target_repo)) == 1


# -- durability: resume after kill leaves the row consistent ---------------

def test_resume_after_kill_leaves_row_consistent(target_repo):
    thread = _rid("kill")
    task = Task("burn-kill", TaskType.SIDE_EFFECTFUL, "change")

    # "Process 1": run to the durable gate, then hard-close the pool (a kill).
    try:
        cp1, pool1 = postgres_checkpointer(setup=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable: {e}")
    store1 = build_runs_store(pool1, setup=True)
    g1 = build_run_graph(target_repo, worker=StubWorker(), checkpointer=cp1, runs_store=store1)
    run_tracked(g1, store1, task, thread_id=thread)
    assert store1.get_run(thread).status == rs.STATUS_AWAITING_GATE
    pool1.close()  # kill

    # "Process 2": fresh pool/store/graph. The row survived, still consistent.
    cp2, pool2 = postgres_checkpointer(setup=False)
    store2 = build_runs_store(pool2, setup=False)
    g2 = build_run_graph(target_repo, worker=StubWorker(), checkpointer=cp2, runs_store=store2)
    surviving = store2.get_run(thread)
    assert surviving.status == rs.STATUS_AWAITING_GATE   # not lost, not duplicated
    assert surviving.ended_at is None

    resume_gate(g2, thread, roles.GO)
    final = store2.get_run(thread)
    rows = store2.list_runs({"run_id": thread})
    pool2.close()

    assert final.status == rs.STATUS_APPLIED
    assert final.ended_at is not None
    assert final.cost_usd > 0
    assert len(rows) == 1                               # one row across kill + resume


def test_no_store_runs_behave_as_before(target_repo):
    # Tracking is opt-in: no store wired → nodes never touch Postgres.
    graph = build_run_graph(target_repo, worker=StubWorker())  # runs_store=None
    from mission_control.graph import run_via_graph

    final = run_via_graph(graph, Task("sim-untracked", TaskType.READ_ONLY, "look"))
    assert final["outcome"] == "completed"
    assert len(list_worktrees(target_repo)) == 1


def test_launch_records_subject_for_dispatch_display(pg_pool):
    """A run carries a human subject from launch (dispatch) onward — so the UI shows
    what it's doing before any worker output / terminal summary."""
    _cp, _pool, store = pg_pool
    run_id = _rid("subject")
    store.launch(run_id, task_type=roles.BURN, subject="Infrastructure Design")
    assert store.get_run(run_id).subject == "Infrastructure Design"
