"""The converged live feed: one ordered async iterator carrying both node
transitions and priced telemetry, over the SAME graph — while the JSONL bronze
spine stays byte-identical to the imperative orchestrator's output."""

from __future__ import annotations

import asyncio
from pathlib import Path

from mission_control import Orchestrator, StubWorker, Task, TaskType, roles
from mission_control.graph import build_run_graph, initial_state, thread_config
from mission_control.live import (
    GateWaiting,
    NodeTransition,
    StepMetric,
    stream_run,
)
from mission_control.telemetry import StepEvent
from mission_control.worktree import list_worktrees
from langgraph.types import Command


def _collect(graph, inp, config) -> list:
    """Drain one leg of the live feed into a list of typed events."""

    async def go():
        return [ev async for ev in stream_run(graph, inp, config)]

    return asyncio.run(go())


# -- one merged, ordered feed ---------------------------------------------

def test_sim_feed_merges_transitions_and_telemetry_in_order(target_repo, tmp_path):
    graph = build_run_graph(target_repo, worker=StubWorker(), telemetry_dir=tmp_path / "t")
    task = Task("sim-live", TaskType.READ_ONLY, "look")
    events = _collect(graph, initial_state(task), thread_config(task, "sim-live"))

    kinds = [type(e).__name__ for e in events]
    # Both event families are present in the single iterator.
    assert "NodeTransition" in kinds
    assert "StepMetric" in kinds

    nodes = [e.node for e in events if isinstance(e, NodeTransition)]
    assert nodes == ["dispatch", "run_worker", "gate", "teardown"]

    metrics = [e for e in events if isinstance(e, StepMetric)]
    assert metrics and all(isinstance(m.event, StepEvent) for m in metrics)
    assert all(m.event.cost_usd > 0 for m in metrics)

    # Ordering: priced telemetry is enriched at teardown (final outcome known),
    # so each StepMetric lands after run_worker and before the teardown transition.
    idx_run = next(i for i, e in enumerate(events)
                   if isinstance(e, NodeTransition) and e.node == "run_worker")
    idx_teardown = next(i for i, e in enumerate(events)
                        if isinstance(e, NodeTransition) and e.node == "teardown")
    metric_positions = [i for i, e in enumerate(events) if isinstance(e, StepMetric)]
    assert all(idx_run < i < idx_teardown for i in metric_positions)

    assert len(list_worktrees(target_repo)) == 1  # clean teardown, no leak


def test_burn_feed_surfaces_gate_then_telemetry_on_resume(target_repo, tmp_path):
    graph = build_run_graph(target_repo, worker=StubWorker(), telemetry_dir=tmp_path / "t")
    task = Task("burn-live", TaskType.SIDE_EFFECTFUL, "change")
    cfg = thread_config(task, "burn-live")

    # Leg 1: runs up to the durable gate and yields GateWaiting as its terminal event.
    leg1 = _collect(graph, initial_state(task), cfg)
    assert isinstance(leg1[-1], GateWaiting)
    assert leg1[-1].value["task_id"] == "burn-live"
    # Nothing priced yet — telemetry is emitted at teardown, after the decision.
    assert not any(isinstance(e, StepMetric) for e in leg1)

    # Leg 2: resume with 'go'. The gate node re-runs (returning the decision),
    # then apply_burn + teardown transitions arrive AND the priced telemetry.
    leg2 = _collect(graph, Command(resume=roles.GO), cfg)
    nodes = [e.node for e in leg2 if isinstance(e, NodeTransition)]
    assert nodes == ["gate", "apply_burn", "teardown"]
    assert any(isinstance(e, StepMetric) for e in leg2)
    assert all(e.event.outcome == "completed"
               for e in leg2 if isinstance(e, StepMetric))
    assert len(list_worktrees(target_repo)) == 1


# -- JSONL spine unchanged (byte-identical to the imperative path) ---------

def _sole_jsonl(d: Path) -> Path:
    files = list(d.glob("*.jsonl"))
    assert len(files) == 1, files
    return files[0]


def _run_orchestrator(repo: Path, tele: Path, task: Task) -> Path:
    orch = Orchestrator(repo, worker=StubWorker(), telemetry_dir=tele)
    result = orch.run_task(task, approval=lambda run: True)
    return result.telemetry.path


def test_graph_jsonl_is_byte_identical_to_orchestrator_sim(target_repo, tmp_path):
    task = Task("sim-x", TaskType.READ_ONLY, "look")

    orch_path = _run_orchestrator(target_repo, tmp_path / "orch", task)

    graph_dir = tmp_path / "graph"
    graph = build_run_graph(target_repo, worker=StubWorker(), telemetry_dir=graph_dir)
    _collect(graph, initial_state(task), thread_config(task, "sim-x"))

    # Content (JSONL lines) is byte-for-byte identical; only the filename (which
    # carries a wall-clock stamp + random suffix) differs between runs.
    assert _sole_jsonl(graph_dir).read_bytes() == orch_path.read_bytes()


def test_graph_jsonl_is_byte_identical_to_orchestrator_burn(target_repo, tmp_path):
    task = Task("burn-x", TaskType.SIDE_EFFECTFUL, "change")

    orch_path = _run_orchestrator(target_repo, tmp_path / "orch", task)

    graph_dir = tmp_path / "graph"
    graph = build_run_graph(target_repo, worker=StubWorker(), telemetry_dir=graph_dir)
    cfg = thread_config(task, "burn-x")
    _collect(graph, initial_state(task), cfg)          # runs to the gate
    _collect(graph, Command(resume=roles.GO), cfg)     # go → apply + teardown (writes JSONL)

    assert _sole_jsonl(graph_dir).read_bytes() == orch_path.read_bytes()


def test_no_telemetry_dir_writes_no_file_but_still_streams(target_repo, tmp_path):
    # Opt-out of the spine: no JSONL written, live telemetry still flows.
    graph = build_run_graph(target_repo, worker=StubWorker())  # telemetry_dir=None
    task = Task("sim-nofile", TaskType.READ_ONLY, "look")
    events = _collect(graph, initial_state(task), thread_config(task, "sim-nofile"))

    assert any(isinstance(e, StepMetric) for e in events)   # live view intact
    assert not list(tmp_path.glob("**/*.jsonl"))            # nothing persisted
