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
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import content_guard, live, pricing, project_ref, repo_source, roles, runs_store, worktree
from .plans_store import PlanStore
from .runs_store import RunStore
from .tasks import Task, TaskType
from .telemetry import StepUsage, TelemetrySink, events_from_steps
from .worker import StubWorker, Worker

# Default local Postgres (docker-compose.yml); overridden by MC_POSTGRES_URL / .env.
DEFAULT_PG_URL = "postgresql://mc:mc@localhost:5432/mission_control?sslmode=disable"

# Default location for per-run JSONL telemetry files (mirrors orchestrator.py).
DEFAULT_TELEMETRY_DIR = Path("telemetry")

_TASK_TYPE_BY_VALUE = {t.value: t for t in TaskType}

# Functional outcome labels (mirror orchestrator.py; not metaphor vocabulary).
OUTCOME_COMPLETED = "completed"
OUTCOME_BLOCKED = "blocked"

# Push outcome for an applied burn (the write hop back to the remote).
PUSH_PUSHED = "pushed"       # merged AND pushed to origin (trunk or workstream branch)
PUSH_REJECTED = "rejected"   # push refused as non-fast-forward
PUSH_CONFLICT = "conflict"   # integrating the remote advance produced a merge conflict
PUSH_BLOCKED = "blocked"     # egress content guard blocked the commit (secret/PII)
PUSH_ERROR = "error"         # push failed on creds / network (surfaced loudly)
PUSH_SKIPPED = "skipped"     # no remote to push to (a remote-less local target)

class RunState(TypedDict, total=False):
    """Serializable run state (primitives only, for checkpointing in L2)."""

    run_id: str  # the LangGraph thread_id; key of the runs-ledger row (see runs_store)
    task_id: str
    task_type: str  # roles.SIM / roles.BURN
    prompt: str
    greenfield: bool
    workstream: str  # optional workstream name → the mc/ws/<name> branch this run builds on
    allow_secrets: bool  # explicit operator override of the egress content guard (audited)
    guard_override: str  # audit note when the content guard was overridden on this run
    local_repo: str  # the resolved local working path (acquired at dispatch from the ref)
    worktree_path: str
    worktree_branch: str
    worktree_holder: str
    worker_summary: str
    made_changes: bool
    steps: list  # list[dict] — StepUsage as dicts (cost data preserved for L2)
    decision: Optional[str]  # roles.GO / roles.NO_GO / None (sim)
    applied: bool
    push_status: str  # one of the PUSH_* labels (only set for an applied burn)
    push_detail: str  # human-legible reason when a push was rejected / errored
    outcome: str


@dataclass
class _Deps:
    """Non-serializable dependencies, held out of the graph state."""

    # A pre-resolved local working path hint (legacy / direct-node tests). The
    # authoritative local path is resolved from ``target_ref`` at dispatch time and
    # stored in the run state — this is only a fallback. Kept FIRST + positional so
    # ``_Deps(target_repo, worker)`` still works.
    target_repo: Optional[Path]
    worker: Worker
    # The PORTABLE identity (a normalized remote ref) — the run's true identity, and
    # what ``ensure_local`` acquires from. The local working path is DERIVED from it.
    target_ref: Optional[str] = None
    # Cache-root override for acquisition (``ensure_local``). None → project_ref default.
    cache_root: Optional[Path] = None
    # Where per-run JSONL is written. None → skip the durable spine (live view
    # only); the priced events are still emitted into the custom stream.
    telemetry_dir: Optional[Path] = None
    # The runs ledger (Postgres). None → don't track status/cost (offline runs).
    runs_store: Optional[RunStore] = None


# -- state <-> domain helpers ---------------------------------------------

def _task(state: RunState) -> Task:
    return Task(
        task_id=state["task_id"],
        task_type=_TASK_TYPE_BY_VALUE[state["task_type"]],
        prompt=state["prompt"],
        greenfield=state.get("greenfield", False),
    )


def _local_repo(deps: _Deps, state: RunState) -> Path:
    """The resolved local working path for this run. Prefers what ``_dispatch`` stored
    in the state (durable across restart); falls back to the ``_Deps`` hint, then to
    locating it from the ref. Post-dispatch nodes read the stored value and so never
    re-acquire (no surprise fetch during apply/teardown)."""
    stored = state.get("local_repo")
    if stored:
        return Path(stored)
    if deps.target_repo is not None:
        return Path(deps.target_repo)
    return repo_source.ensure_local(deps.target_ref, root=deps.cache_root)


def _worktree(deps: _Deps, state: RunState) -> worktree.Worktree:
    return worktree.Worktree(
        path=Path(state["worktree_path"]),
        branch=state["worktree_branch"],
        target_repo=_local_repo(deps, state),
        _holder=Path(state["worktree_holder"]),
    )


def _outcome(task_type: str, decision: Optional[str], applied: bool) -> str:
    if task_type == roles.BURN and decision == roles.NO_GO:
        return OUTCOME_BLOCKED
    return OUTCOME_COMPLETED


def _terminal_status(
    task_type: str, decision: Optional[str], applied: bool, push_status: Optional[str] = None
) -> str:
    """Map a finished run to its terminal ledger status. A burn on a go that merged AND
    pushed (or had nothing to push) is ``applied``; if integrating the remote conflicted
    it is the distinct ``merge_conflict``; a non-fast-forward / creds failure is
    ``push_rejected``; a no-go burn is ``scrubbed``; a sim is ``done``."""
    if task_type == roles.BURN:
        if decision == roles.GO:
            if push_status == PUSH_BLOCKED:
                return runs_store.STATUS_BLOCKED_SECRETS
            if push_status == PUSH_CONFLICT:
                return runs_store.STATUS_MERGE_CONFLICT
            if push_status in (PUSH_REJECTED, PUSH_ERROR):
                return runs_store.STATUS_PUSH_REJECTED
            if applied:
                return runs_store.STATUS_APPLIED
        return runs_store.STATUS_SCRUBBED
    return runs_store.STATUS_DONE


def _ledger(deps: _Deps, state: RunState) -> tuple[Optional[RunStore], Optional[str]]:
    """The runs ledger + this run's id, or (None, None) when tracking is off (no
    store wired, or no run_id in state — e.g. a node invoked directly in a test)."""
    run_id = state.get("run_id")
    if deps.runs_store is None or not run_id:
        return None, None
    return deps.runs_store, run_id


# -- nodes (idempotent; module-level so they're directly testable) --------

def _dispatch(deps: _Deps, state: RunState) -> dict:
    """Acquire the target locally, then carve the isolated worktree off the fetched
    remote trunk. Idempotent: reuse if this run already made one.

    The local working path is resolved from the ref HERE (at dispatch), not stored as
    identity: ``ensure_local`` clones the target on a fresh machine or fetches an
    existing cache clone, so the worktree is built on the current remote state rather
    than whatever a stale local HEAD points at. A clone/fetch failure raises loudly —
    the run fails rather than proceeding on an empty or stale copy."""
    ref = deps.target_ref or (str(deps.target_repo) if deps.target_repo else None)
    if not ref:
        raise RuntimeError("dispatch has no target ref/repo to acquire")
    local = repo_source.ensure_local(ref, root=deps.cache_root)

    store, run_id = _ledger(deps, state)
    if store is not None:  # first node → the run is now running; stamp started_at
        store.mark_running(run_id, target=ref, local_path=str(local))

    existing = state.get("worktree_path")
    if existing and Path(existing).exists():
        return {}  # already dispatched — re-run is a no-op

    # A workstream run branches off its long-lived mc/ws/<name> line (ensured on the
    # remote first); otherwise off origin/<trunk> (or HEAD for a remote-less target).
    ws = state.get("workstream")
    if ws and repo_source.has_origin(local):
        base = f"origin/{repo_source.ensure_workstream_branch(local, ws)}"
    else:
        base = repo_source.default_base(local)
    wt = worktree.create_worktree(local, state["task_id"], base=base)
    return {
        "local_repo": str(local),
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
    store, run_id = _ledger(deps, state)
    if store is not None:  # about to halt for a human — reflect that in the ledger
        store.mark_awaiting_gate(run_id)
    verdict = interrupt(
        {"gate": "go/no-go", "task_id": state["task_id"], "summary": state.get("worker_summary", "")}
    )
    approved = verdict in (roles.GO, True)
    return {"decision": roles.GO if approved else roles.NO_GO}


def _persist_changes(deps: _Deps, state: RunState, local: Path, wt: worktree.Worktree) -> None:
    """Capture the pending changed-files diff and store it on the run row, so it stays
    viewable after the worktree is torn down. Best-effort (an observability aid must
    never break apply) and additive (only ``changes_json`` is written). Persists only
    when the burn actually changed files — a no-op burn leaves the column NULL."""
    store, run_id = _ledger(deps, state)
    if store is None:
        return
    try:
        diff = worktree.changes(local, wt.branch, wt.path)
    except Exception:  # noqa: BLE001 — diffing must never break the apply
        return
    if not diff.get("files"):
        return
    try:
        store.set_changes(run_id, diff)
    except Exception:  # noqa: BLE001
        return


def _apply_burn(deps: _Deps, state: RunState) -> dict:
    """Apply the burn's changes to the target repo AND push them to the remote. OWN
    NODE, idempotent: commit no-ops when clean; merge no-ops when already merged; the
    push is a no-op once landed — so a crash that re-runs this whole node never
    double-applies and always completes the push on resume.

    The push is the write hop that lets an approved burn leave the host, and it fires
    ONLY here — reached only via the go edge, so never on a sim, a no-go, or a burn
    no-op. A rejected push (non-fast-forward / conflict), a creds/network failure, or an
    egress content-guard block is recorded as a distinct outcome (not raised) so teardown
    still runs and the run ends on a legible terminal state rather than leaking a
    worktree. We never force-push and never auto-redact.

    Guard: never apply without a recorded go decision (defense in depth beyond the
    routing edge)."""
    if state.get("decision") != roles.GO:
        raise RuntimeError("apply_burn reached without a recorded go decision")
    wt = _worktree(deps, state)
    local = _local_repo(deps, state)
    ws = state.get("workstream")

    # Persist the changed-files diff BEFORE it is applied — this is the last point the
    # worktree still exists (teardown removes it), so it's where worktree.changes() is
    # computable. Durable storage then keeps the diff viewable after the run leaves the
    # gate. Additive + best-effort: never break apply; only a burn that changed files
    # gets a stored payload (a no-op burn persists nothing).
    _persist_changes(deps, state, local, wt)

    # EGRESS GUARD: scan the staged unit output before it is committed + pushed. A
    # secret/PII blocks the burn as a distinct terminal state (not raised — teardown must
    # run); an explicit operator override lets it through and is recorded for audit.
    override_note: list = []
    try:
        worktree.commit_changes(
            wt, f"apply task {state['task_id']}",
            allow_secrets=bool(state.get("allow_secrets")),
            audit=lambda findings: override_note.append(content_guard.summarize(findings)),
        )
    except content_guard.GuardViolation as exc:
        return {"applied": False, "push_status": PUSH_BLOCKED, "push_detail": str(exc)[:500]}
    guard_override = ("content guard OVERRIDDEN by operator ack — " + override_note[0]) \
        if override_note else None

    base: dict = {"guard_override": guard_override} if guard_override else {}
    if ws and repo_source.has_origin(local):
        # Workstream run: reconcile onto the mc/ws/<name> branch (NOT trunk — trunk only
        # advances via an explicit promote). The change is committed in the isolated task
        # worktree; push it (integrating any remote advance on the workstream branch)
        # from there, so the shared clone's HEAD is never touched. "applied" means it
        # landed on the workstream branch — false on a conflict/rejection (nothing did).
        return _push_result(base, local, repo_source.workstream_branch(ws), work_dir=wt.path)

    # Trunk run (or remote-less target): merge into the local trunk, then push it.
    worktree.merge_into_target(wt, f"apply task {state['task_id']}")
    base["applied"] = True
    if not repo_source.has_origin(local):
        base["push_status"] = PUSH_SKIPPED  # nothing to push (remote-less target)
        return base
    return _push_result(base, local, repo_source.current_branch(local), work_dir=local)


def _push_result(result: dict, local: Path, branch: str, *, work_dir) -> dict:
    """Push ``work_dir``'s HEAD to ``origin/<branch>`` and fold the outcome into
    ``result`` (push_status / push_detail / applied). A rejected push or merge conflict
    is recorded (not raised) so teardown still runs and the run ends on a legible
    terminal state; we never force-push. ``result`` already reflects any local merge."""
    try:
        repo_source.push_to_remote(work_dir, branch, lock_repo=local)
        result["push_status"] = PUSH_PUSHED
        result["applied"] = True
    except repo_source.MergeConflict as exc:
        result["push_status"] = PUSH_CONFLICT
        result["push_detail"] = "conflicting files: " + (", ".join(exc.files) or "(unknown)")
    except repo_source.PushRejected as exc:
        result["push_status"] = PUSH_REJECTED
        result["push_detail"] = str(exc)
    except repo_source.PushError as exc:
        result["push_status"] = PUSH_ERROR
        result["push_detail"] = str(exc)
    result.setdefault("applied", False)
    return result


def _teardown(deps: _Deps, state: RunState) -> dict:
    """Tear down the worktree (forgiving/idempotent), record the outcome, and
    surface the run's priced telemetry.

    On a no-go this is the ``roles.SCRUB``: tear down without applying.

    Telemetry is enriched HERE, at the one point the final outcome is known (a
    burn's outcome depends on the gate). The priced events are (a) emitted into
    the LangGraph ``custom`` stream for the live feed and (b) written to the JSONL
    bronze spine — from the SAME enriched list, so the live view and the durable
    record are identical.
    """
    worktree.remove_worktree(_worktree(deps, state))
    outcome = _outcome(state["task_type"], state.get("decision"), state.get("applied", False))
    cost_usd = _emit_telemetry(deps, state, outcome)

    # Terminal ledger transition: final status, the run's total cost, and a short
    # summary; stamps ended_at. Idempotent (upsert; absolute cost) so a re-run of
    # this node never duplicates the row or double-counts the cost.
    store, run_id = _ledger(deps, state)
    if store is not None:
        push_status = state.get("push_status")
        # A push that didn't land (rejected, errored, merge conflict, or a content-guard
        # block) is the run's headline fact — put its reason / offending file(s) in the
        # detail so the control room / CLI / Slack can show why. An overridden content
        # guard is recorded here too, so the operator ack is auditable. Otherwise the
        # worker summary.
        if push_status in (PUSH_REJECTED, PUSH_ERROR, PUSH_CONFLICT, PUSH_BLOCKED):
            detail = (state.get("push_detail") or "push did not land")[:500]
        elif state.get("guard_override"):
            detail = state["guard_override"][:500]
        else:
            detail = (state.get("worker_summary") or "")[:500] or None
        store.finish(
            run_id,
            status=_terminal_status(
                state["task_type"], state.get("decision"), state.get("applied", False),
                push_status,
            ),
            cost_usd=cost_usd,
            detail=detail,
        )
    return {"outcome": outcome}


def _emit_telemetry(deps: _Deps, state: RunState, outcome: str) -> float:
    """Enrich the worker's raw steps into priced events, then emit them live and
    (if a telemetry dir is configured) persist them to JSONL. Byte-identical to the
    imperative orchestrator: both enrich via :func:`events_from_steps`.

    Returns the run's total cost (sum of the priced step events) — the single
    figure the runs ledger records, drawn from the very same events."""
    raw = state.get("steps") or []
    if not raw:
        return 0.0
    events = events_from_steps(
        (StepUsage(**s) for s in raw),
        task_id=state["task_id"],
        task_type=state["task_type"],  # metaphor string, already from roles
        outcome=outcome,
    )

    # Live view: push each priced event into the custom stream. Silently skipped
    # when there's no runnable context (a node called directly, outside the graph);
    # a no-op under plain invoke where nothing consumes the custom stream.
    try:
        writer = get_stream_writer()
    except RuntimeError:
        writer = None
    if writer is not None:
        for event in events:
            writer(live.encode_step_metric(event))

    # Durable bronze spine: one JSONL file per run, one line per step.
    if deps.telemetry_dir is not None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = deps.telemetry_dir / f"run-{state['task_id']}-{stamp}-{uuid.uuid4().hex[:6]}.jsonl"
        with TelemetrySink(path) as sink:
            for event in events:
                sink.record(event)

    return round(sum(e.cost_usd for e in events), 8)


def _route_after_gate(state: RunState) -> str:
    """go on a burn → apply_burn; everything else (sim, or no-go scrub) → teardown."""
    if state["task_type"] == roles.BURN and state.get("decision") == roles.GO:
        return "apply_burn"
    return "teardown"


# -- graph assembly --------------------------------------------------------

def build_run_graph(
    target_repo: Optional[Path] = None,
    *,
    target_ref: Optional[str] = None,
    cache_root: Optional[Path] = None,
    worker: Optional[Worker] = None,
    checkpointer=None,
    interrupt_before=None,
    telemetry_dir: Optional[Path] = None,
    runs_store: Optional[RunStore] = None,
):
    """Compile the durable run graph.

    ``checkpointer`` defaults to MemorySaver; pass a PostgresSaver (see
    :func:`postgres_checkpointer`) for durable, resumable runs. A burn pauses at
    the gate via ``interrupt()`` until resumed with a go/no-go decision (see
    :func:`resume_gate`). ``interrupt_before`` additionally pauses before the named
    nodes.

    ``telemetry_dir`` opts the run into the JSONL bronze spine (one file per run,
    byte-identical to the imperative orchestrator). When ``None`` the run still
    emits priced telemetry into the live custom stream but writes no file.

    ``runs_store`` opts the run into the Postgres runs ledger: nodes write status
    transitions and the terminal cost/summary as the run progresses. When ``None``
    the run isn't tracked (offline / MemorySaver runs behave exactly as before).

    ``target_ref`` is the PORTABLE identity (a normalized remote ref) and the run's
    true target: ``dispatch`` acquires it locally (clone/fetch) and carves the
    worktree off ``origin/<trunk>``. Supply ``target_ref`` alone to run a remote the
    machine has no local checkout of. When it's ``None`` it's derived from
    ``target_repo``'s ``origin`` remote, falling back to the resolved path for a
    remote-less repo. At least one of ``target_repo`` / ``target_ref`` is required.
    ``cache_root`` overrides where remotes are cached (else the project_ref default).
    """
    if target_repo is None and target_ref is None:
        raise ValueError("build_run_graph requires target_repo or target_ref")
    resolved = Path(target_repo).resolve() if target_repo is not None else None
    if target_ref is None:
        try:
            target_ref = project_ref.remote_of(resolved)
        except project_ref.NoRemoteError:
            target_ref = str(resolved)
    deps = _Deps(
        target_repo=resolved,
        target_ref=target_ref,
        cache_root=Path(cache_root) if cache_root is not None else None,
        worker=worker if worker is not None else StubWorker(),
        telemetry_dir=Path(telemetry_dir) if telemetry_dir is not None else None,
        runs_store=runs_store,
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
        try:
            checkpointer.setup()
        except BaseException:
            # Don't leak the pool's connections if setup can't reach the DB —
            # a leaked open pool keeps retrying and can saturate a shared server.
            pool.close()
            raise
    return checkpointer, pool


def build_runs_store(pool, *, setup: bool = True) -> RunStore:
    """Build the runs ledger over the SAME pool the checkpointer uses.

    ``setup`` runs the idempotent DDL (``CREATE TABLE IF NOT EXISTS``), the same
    migration approach as ``PostgresSaver.setup()`` — safe to call every start.
    """
    store = RunStore(pool)
    if setup:
        store.setup()
    return store


def build_plans_store(pool, *, setup: bool = True) -> PlanStore:
    """Build the PLAN store over the SAME pool the checkpointer / runs ledger use.

    ``setup`` runs the idempotent DDL (``CREATE TABLE IF NOT EXISTS``), the same
    migration approach as ``PostgresSaver.setup()`` — safe to call every start.
    """
    store = PlanStore(pool)
    if setup:
        store.setup()
    return store


def run_tracked(graph, store: RunStore, task: Task, *, thread_id: Optional[str] = None) -> dict:
    """Invoke the graph for a task with runs-ledger tracking: insert the row on
    launch, let the nodes drive its status/cost, and mark it ``failed`` if the run
    raises. The graph must have been compiled with this same ``store``."""
    config = thread_config(task, thread_id)
    run_id = config["configurable"]["thread_id"]
    store.launch(run_id, task_type=task.task_type.value)
    try:
        return graph.invoke(initial_state(task, run_id=run_id), config=config)
    except Exception as exc:  # noqa: BLE001 — surface the failure in the ledger, then re-raise
        store.mark_failed(run_id, f"{type(exc).__name__}: {exc}")
        raise


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


def initial_state(task: Task, *, run_id: Optional[str] = None) -> RunState:
    """The starting ``RunState`` for a task (shared by invoke + live streaming).

    ``run_id`` (the thread_id) is threaded into the state so nodes can key their
    runs-ledger writes without needing the graph config."""
    state: RunState = {
        "task_id": task.task_id,
        "task_type": task.task_type.value,
        "prompt": task.prompt,
        "greenfield": task.greenfield,
        "decision": None,
        "applied": False,
    }
    if task.workstream:
        state["workstream"] = task.workstream
    if task.allow_secrets:
        state["allow_secrets"] = True
    if run_id is not None:
        state["run_id"] = run_id
    return state


def thread_config(task: Task, thread_id: Optional[str] = None) -> dict:
    """The per-run graph config, wiring a thread_id (one generated if absent)."""
    thread_id = thread_id or f"run-{task.task_id}-{uuid.uuid4().hex[:8]}"
    return {"configurable": {"thread_id": thread_id}}


def run_via_graph(graph, task: Task, *, thread_id: Optional[str] = None) -> dict:
    """Invoke the compiled graph for one task, wiring a thread_id per run."""
    config = thread_config(task, thread_id)
    run_id = config["configurable"]["thread_id"]
    return graph.invoke(initial_state(task, run_id=run_id), config=config)


def awaiting_gate(graph, thread_id: str) -> bool:
    """True if the run is durably paused at the go/no-go gate, waiting on a human."""
    return graph.get_state({"configurable": {"thread_id": thread_id}}).next == ("gate",)


def resume_gate(graph, thread_id: str, decision: str) -> dict:
    """Resume a run paused at the gate with a go/no-go decision (``roles.GO`` /
    ``roles.NO_GO``). ``go`` proceeds into apply_burn; ``no-go`` scrubs."""
    return graph.invoke(
        Command(resume=decision), config={"configurable": {"thread_id": thread_id}}
    )
