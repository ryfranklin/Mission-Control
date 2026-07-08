"""LangGraph durable orchestration shell (Phase 4, L1).

Wraps the run lifecycle as a LangGraph ``StateGraph`` WITHOUT touching the worker:

    dispatch → run_worker → gate (go/no-go, stub) → apply_burn (own node) → teardown

Nodes call the EXISTING ``Worker`` interface (``SdkWorker`` unchanged); the graph
is just a durable shell around the same orchestration steps the ``Orchestrator``
performed imperatively. Metaphor vocabulary stays in ``roles.py`` — node names and
state fields are functional; the metaphor surfaces only via ``roles.*`` constants
(the go/no-go decision, human-facing labels).

CRITICAL (Phase 4 decision doc): LangGraph recovers at **node boundaries** — a
crash re-runs the *whole* node — so every node is **idempotent**:

* ``dispatch``   — reuses an existing worktree if this run already made one.
* ``run_worker`` — edits only the disposable worktree; re-run overwrites, never
                   escapes the worktree.
* ``gate``       — a pure decision.
* ``apply_burn`` — ITS OWN NODE and safe to re-run: ``commit`` is a no-op when
                   there's nothing to commit, and ``git merge`` no-ops when the
                   branch is already merged, so re-execution never double-applies.
* ``teardown``   — worktree removal is forgiving/idempotent.

L1 uses ``MemorySaver`` (no persistence). ``PostgresSaver`` + crash/resume land in
L2; Postgres is stood up now via ``docker-compose.yml``.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import pricing, roles, worktree
from .tasks import Task, TaskType
from .worker import StubWorker, Worker

# Default local Postgres (docker-compose.yml); overridden by MC_POSTGRES_URL / .env.
DEFAULT_PG_URL = "postgresql://mc:mc@localhost:5432/mission_control?sslmode=disable"

_TASK_TYPE_BY_VALUE = {t.value: t for t in TaskType}

# Functional outcome labels (mirror orchestrator.py; not metaphor vocabulary).
OUTCOME_COMPLETED = "completed"
OUTCOME_BLOCKED = "blocked"

class RunState(TypedDict, total=False):
    """Serializable run state (primitives only, for checkpointing in L2)."""

    task_id: str
    task_type: str  # roles.SIM / roles.BURN
    prompt: str
    greenfield: bool
    worktree_path: str
    worktree_branch: str
    worktree_holder: str
    worker_summary: str
    made_changes: bool
    steps: list  # list[dict] — StepUsage as dicts (cost data preserved for L2)
    decision: Optional[str]  # roles.GO / roles.NO_GO / None (sim)
    applied: bool
    outcome: str


@dataclass
class _Deps:
    """Non-serializable dependencies, held out of the graph state."""

    target_repo: Path
    worker: Worker


# -- state <-> domain helpers ---------------------------------------------

def _task(state: RunState) -> Task:
    return Task(
        task_id=state["task_id"],
        task_type=_TASK_TYPE_BY_VALUE[state["task_type"]],
        prompt=state["prompt"],
        greenfield=state.get("greenfield", False),
    )


def _worktree(deps: _Deps, state: RunState) -> worktree.Worktree:
    return worktree.Worktree(
        path=Path(state["worktree_path"]),
        branch=state["worktree_branch"],
        target_repo=deps.target_repo,
        _holder=Path(state["worktree_holder"]),
    )


def _outcome(task_type: str, decision: Optional[str], applied: bool) -> str:
    if task_type == roles.BURN and decision == roles.NO_GO:
        return OUTCOME_BLOCKED
    return OUTCOME_COMPLETED


# -- nodes (idempotent; module-level so they're directly testable) --------

def _dispatch(deps: _Deps, state: RunState) -> dict:
    """Create the isolated worktree. Idempotent: reuse if this run already made one."""
    existing = state.get("worktree_path")
    if existing and Path(existing).exists():
        return {}  # already dispatched — re-run is a no-op
    wt = worktree.create_worktree(deps.target_repo, state["task_id"])
    return {
        "worktree_path": str(wt.path),
        "worktree_branch": wt.branch,
        "worktree_holder": str(wt._holder),
    }


def _run_worker(deps: _Deps, state: RunState) -> dict:
    """Run the existing Worker in the worktree. Side effects stay in the worktree."""
    result = deps.worker.investigate(_task(state), _worktree(deps, state).path)
    return {
        "worker_summary": result.summary,
        "made_changes": result.made_changes,
        "steps": [asdict(s) for s in result.steps],
    }


def _gate(deps: _Deps, state: RunState) -> dict:
    """Durable go/no-go (``roles.GO`` / ``roles.NO_GO``). Read-only sims never gate.

    For a burn this calls LangGraph ``interrupt()``: the graph HALTS here (before
    apply_burn), persists to the checkpointer, and waits for a human decision
    supplied on resume via ``Command(resume=...)``. Anything that isn't an explicit
    go is treated as no-go — a burn is never applied without an approval on record.
    """
    if state["task_type"] != roles.BURN:
        return {"decision": None}
    verdict = interrupt(
        {"gate": "go/no-go", "task_id": state["task_id"], "summary": state.get("worker_summary", "")}
    )
    approved = verdict in (roles.GO, True)
    return {"decision": roles.GO if approved else roles.NO_GO}


def _apply_burn(deps: _Deps, state: RunState) -> dict:
    """Apply the burn's changes to the target repo. OWN NODE, idempotent:
    commit no-ops when clean; merge no-ops when already merged — so a crash that
    re-runs this whole node never double-applies.

    Guard: never apply without a recorded go decision (defense in depth beyond the
    routing edge)."""
    if state.get("decision") != roles.GO:
        raise RuntimeError("apply_burn reached without a recorded go decision")
    wt = _worktree(deps, state)
    worktree.commit_changes(wt, f"apply task {state['task_id']}")
    worktree.merge_into_target(wt, f"apply task {state['task_id']}")
    return {"applied": True}


def _teardown(deps: _Deps, state: RunState) -> dict:
    """Tear down the worktree (forgiving/idempotent) and record the outcome.

    On a no-go this is the ``roles.SCRUB``: tear down without applying."""
    worktree.remove_worktree(_worktree(deps, state))
    return {
        "outcome": _outcome(state["task_type"], state.get("decision"), state.get("applied", False)),
    }


def _route_after_gate(state: RunState) -> str:
    """go on a burn → apply_burn; everything else (sim, or no-go scrub) → teardown."""
    if state["task_type"] == roles.BURN and state.get("decision") == roles.GO:
        return "apply_burn"
    return "teardown"


# -- graph assembly --------------------------------------------------------

def build_run_graph(
    target_repo: Path,
    *,
    worker: Optional[Worker] = None,
    checkpointer=None,
    interrupt_before=None,
):
    """Compile the durable run graph.

    ``checkpointer`` defaults to MemorySaver; pass a PostgresSaver (see
    :func:`postgres_checkpointer`) for durable, resumable runs. A burn pauses at
    the gate via ``interrupt()`` until resumed with a go/no-go decision (see
    :func:`resume_gate`). ``interrupt_before`` additionally pauses before the named
    nodes.
    """
    deps = _Deps(
        target_repo=Path(target_repo).resolve(),
        worker=worker if worker is not None else StubWorker(),
    )

    g = StateGraph(RunState)
    g.add_node("dispatch", lambda s: _dispatch(deps, s))
    g.add_node("run_worker", lambda s: _run_worker(deps, s))
    g.add_node("gate", lambda s: _gate(deps, s))
    g.add_node("apply_burn", lambda s: _apply_burn(deps, s))
    g.add_node("teardown", lambda s: _teardown(deps, s))

    g.add_edge(START, "dispatch")
    g.add_edge("dispatch", "run_worker")
    g.add_edge("run_worker", "gate")
    g.add_conditional_edges(
        "gate", _route_after_gate, {"apply_burn": "apply_burn", "teardown": "teardown"}
    )
    g.add_edge("apply_burn", "teardown")
    g.add_edge("teardown", END)

    return g.compile(
        checkpointer=checkpointer or MemorySaver(),
        interrupt_before=list(interrupt_before or []),
    )


def postgres_checkpointer(conn_url: Optional[str] = None, *, setup: bool = True, max_size: int = 10):
    """Build a LangGraph ``PostgresSaver`` over a psycopg connection pool.

    Calls ``.setup()`` by default (idempotent — creates the checkpoint tables on
    first run). Returns ``(checkpointer, pool)``; close the pool when done.
    """
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg_pool import ConnectionPool

    url = conn_url or os.environ.get("MC_POSTGRES_URL") or DEFAULT_PG_URL
    pool = ConnectionPool(
        conninfo=url,
        max_size=max_size,
        kwargs={"autocommit": True, "prepare_threshold": 0},
        open=True,
    )
    checkpointer = PostgresSaver(pool)
    if setup:
        checkpointer.setup()
    return checkpointer, pool


def worker_cost_usd(state: RunState) -> float:
    """Dollars spent on the worker step(s) in this state, priced from the telemetry
    (``steps``). On resume these nodes are skipped — this is the re-pay avoided."""
    total = 0.0
    for s in state.get("steps") or []:
        total += pricing.cost_usd(
            s["model"],
            input_tokens=s.get("input_tokens", 0),
            output_tokens=s.get("output_tokens", 0),
            cache_read_tokens=s.get("cache_read_tokens", 0),
            cache_creation_5m_tokens=s.get("cache_creation_5m_tokens", 0),
            cache_creation_1h_tokens=s.get("cache_creation_1h_tokens", 0),
        )
    return round(total, 8)


def run_via_graph(graph, task: Task, *, thread_id: Optional[str] = None) -> dict:
    """Invoke the compiled graph for one task, wiring a thread_id per run."""
    thread_id = thread_id or f"run-{task.task_id}-{uuid.uuid4().hex[:8]}"
    initial: RunState = {
        "task_id": task.task_id,
        "task_type": task.task_type.value,
        "prompt": task.prompt,
        "greenfield": task.greenfield,
        "decision": None,
        "applied": False,
    }
    return graph.invoke(initial, config={"configurable": {"thread_id": thread_id}})


def awaiting_gate(graph, thread_id: str) -> bool:
    """True if the run is durably paused at the go/no-go gate, waiting on a human."""
    return graph.get_state({"configurable": {"thread_id": thread_id}}).next == ("gate",)


def resume_gate(graph, thread_id: str, decision: str) -> dict:
    """Resume a run paused at the gate with a go/no-go decision (``roles.GO`` /
    ``roles.NO_GO``). ``go`` proceeds into apply_burn; ``no-go`` scrubs."""
    return graph.invoke(
        Command(resume=decision), config={"configurable": {"thread_id": thread_id}}
    )
