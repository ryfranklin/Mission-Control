"""The run manager: the service's only stateful piece.

It wraps ``graph.py`` — it does NOT re-implement orchestration. Its jobs:

* launch a run by driving the EXISTING graph (via :func:`live.stream_run_sync` in a
  worker thread) in a background task, keyed by ``thread_id``;
* fan the merged live feed (node transitions + priced telemetry + gate-waiting +
  an explicit terminal event) out to SSE subscribers, and DURABLY persist every
  event so a reconnect after a restart can replay the full timeline (live tail
  from the in-process channel; replay from the Postgres event log);
* resolve the durable go/no-go by resuming the EXISTING ``interrupt()`` (approve →
  go, reject → no-go);
* cancel an in-flight run at the next node boundary, tearing its worktree down; and
* answer status/detail/list/summary straight from the S2 runs ledger.

The only "runtime" thing it does beyond launching/querying the graph is worktree
teardown on the cancel/failure paths — the graph's own teardown node can't run
when we stop the run mid-flight, so the seam reuses the existing worktree helper
to avoid a leak. No dispatch/gate/apply logic lives here.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Optional
from uuid import uuid4

from langgraph.types import Command

from .. import roles, worktree
from ..graph import build_run_graph, initial_state, run_tracked, worker_cost_usd
from ..live import GateWaiting, NodeTransition, StepMetric, stream_run_sync
from ..runs_store import (
    STATUS_AWAITING_GATE,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SCRUBBED,
    TERMINAL_STATUSES,
    RunRow,
    RunStore,
)

# Statuses that mean a run is still in flight (for the header's live count).
_ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_AWAITING_GATE)
from ..tasks import Task, TaskType
from ..worker import StubWorker

# Wire value ("sim"/"burn") -> TaskType, sourced from the enum (metaphor stays in roles).
_TASK_TYPE = {t.value: t for t in TaskType}

# Sentinel pushed to subscriber queues when a run's feed is complete.
_CLOSED = object()

# Feed event names (functional labels, not metaphor vocabulary).
EVENT_TERMINAL = "terminal"


@dataclass
class SimResult:
    """The outcome of a synchronous sim run (used by the planner's reverse-engineering
    step): the run id (for traceability in the runs registry), the worker's summary,
    and the terminal ledger status."""

    run_id: str
    summary: str
    status: str


class RunNotFound(Exception):
    """No run with the given id in the ledger."""


class RunConflict(Exception):
    """The requested transition isn't valid for the run's current status."""

    def __init__(self, message: str, status: str) -> None:
        super().__init__(message)
        self.status = status


class _Channel:
    """Per-run LIVE fan-out. Purely in-process and ephemeral — the durable timeline
    lives in the ``run_events`` table; this only tails events to attached SSE
    subscribers. ``seq`` is a global per-run counter seeded from the store so
    numbering continues across a process restart."""

    def __init__(self, start_seq: int) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self.closed = False
        self._next_seq = start_seq
        self._seq_lock = threading.Lock()

    def next_seq(self) -> int:
        with self._seq_lock:
            seq = self._next_seq
            self._next_seq += 1
            return seq

    async def push(self, event: dict) -> None:
        for q in list(self._subscribers):
            q.put_nowait(event)

    async def close(self) -> None:
        self.closed = True
        for q in list(self._subscribers):
            q.put_nowait(_CLOSED)

    def attach(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def detach(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)


def _serialize(event) -> dict:
    """One merged-feed event → an SSE-ready ``{event, data}`` pair."""
    if isinstance(event, NodeTransition):
        return {"event": "node_transition", "data": {"node": event.node, "update": event.update}}
    if isinstance(event, StepMetric):
        return {"event": "step_metric", "data": {"event": asdict(event.event)}}
    if isinstance(event, GateWaiting):
        return {"event": "gate_waiting", "data": {"value": event.value}}
    return {"event": "message", "data": {"value": str(event)}}


class RunManager:
    """Launches / resolves / cancels / streams / queries runs over the existing graph."""

    def __init__(
        self,
        *,
        checkpointer,
        runs_store: RunStore,
        worker_factory=None,
        telemetry_dir: Optional[Path] = None,
    ) -> None:
        self._checkpointer = checkpointer
        self._store = runs_store
        self._worker_factory = worker_factory or (lambda: StubWorker())
        self._telemetry_dir = Path(telemetry_dir) if telemetry_dir else None
        self._graphs: dict[str, object] = {}       # per-target compiled graph (shared cp/store)
        self._channels: dict[str, _Channel] = {}    # per-run live fan-out
        self._cancels: dict[str, threading.Event] = {}  # per-run cooperative cancel flag
        self._resolving: set[str] = set()           # gates mid-resolution (one-shot guard)
        self._tasks: set[asyncio.Task] = set()      # background drives (keep refs alive)
        self._run_observer = None                   # notified (on the loop) when a run ends

    def set_run_observer(self, observer) -> None:
        """Register a callback ``observer(run_id)`` invoked ON THE LOOP each time a run
        reaches a terminal state. The plan builder uses this to advance a plan's build
        (dispatch newly-ready units, roll up status) without any new orchestration."""
        self._run_observer = observer

    # -- graph wiring ------------------------------------------------------

    def _graph_for(self, target: Path):
        key = str(Path(target).resolve())
        graph = self._graphs.get(key)
        if graph is None:
            graph = build_run_graph(
                Path(key),
                worker=self._worker_factory(),
                checkpointer=self._checkpointer,
                runs_store=self._store,
                telemetry_dir=self._telemetry_dir,
            )
            self._graphs[key] = graph
        return graph

    def _channel_for(self, run_id: str) -> _Channel:
        channel = self._channels.get(run_id)
        if channel is None:
            channel = _Channel(self._store.max_event_seq(run_id) + 1)
            self._channels[run_id] = channel
        return channel

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- launch ------------------------------------------------------------

    def launch(
        self,
        *,
        target: str,
        task_type: str,
        prompt: str,
        plan_id: Optional[str] = None,
        plan_unit_seq: Optional[int] = None,
    ) -> str:
        """Register a queued run and kick off the graph in the background, keyed by
        its thread_id (== run_id). Returns the run_id immediately. When built from a
        plan, ``plan_id``/``plan_unit_seq`` record the link on the run row."""
        target_path = Path(target).expanduser()
        if not target_path.is_dir():
            raise RunConflict(f"target is not a directory: {target}", status="rejected")
        # Must be its OWN git repo root — never a subdir of a parent repo (a worktree
        # carved there would land in the PARENT). Refuse rather than pollute it.
        if not worktree.is_git_repo(target_path):
            raise RunConflict(
                f"target is not a git repository root (init it, or it is nested inside "
                f"a parent repo): {target}", status="rejected")
        tt = _TASK_TYPE[task_type]  # validated by the request model

        run_id = f"run-{uuid4().hex}"
        task = Task(task_id=f"{task_type}-{uuid4().hex[:8]}", task_type=tt, prompt=prompt)
        self._store.launch(run_id, task_type=task_type, target=str(target_path.resolve()),
                           plan_id=plan_id, plan_unit_seq=plan_unit_seq)

        self._channel_for(run_id)
        self._spawn(self._drive(run_id, target_path, initial_state(task, run_id=run_id)))
        return run_id

    # -- plan-child run queries (the plan owns its runs) -------------------

    def child_runs(self, plan_id: str) -> list[RunRow]:
        """A plan's child runs, ordered by the unit seq they were built from."""
        return self._store.plan_runs(plan_id)

    def plan_cost(self, plan_id: str) -> float:
        """Rolled-up reconciled cost across a plan's child runs."""
        return self._store.plan_cost(plan_id)

    # -- go/no-go review: what a burn will apply --------------------------

    def run_changes(self, run_id: str) -> Optional[dict]:
        """What a burn has produced in its worktree — the material for a go/no-go
        decision (at the gate) OR a live work-in-progress view (while ``running``, so a
        long burn isn't a black box). ``None`` for runs with no live worktree. Reads the
        branch/path from the durable checkpoint, so it survives a restart; the worktree
        peek uses a throwaway index and never mutates the worker's work."""
        row = self._require(run_id)
        if row.status not in (STATUS_RUNNING, STATUS_AWAITING_GATE) or not row.target:
            return None
        config = {"configurable": {"thread_id": run_id}}
        state = self._state(Path(row.target), config)
        branch = state.get("worktree_branch")
        if not branch:
            return None
        try:
            changes = worktree.changes(Path(row.target), branch, state.get("worktree_path"))
        except Exception:  # noqa: BLE001 — an observability aid must never break a run
            return None
        changes["status"] = row.status
        if row.started_at:
            started = row.started_at
            now = datetime.now(started.tzinfo) if started.tzinfo else datetime.utcnow()
            changes["elapsed_s"] = max(0, int((now - started).total_seconds()))
        return changes

    # -- synchronous sim (the reverse-engineering read-only investigation) ---

    def run_sim(self, *, target: str, prompt: str) -> "SimResult":
        """Launch a read-only sim against ``target`` and run it to completion, returning
        its worker summary. Reuses the EXACT launch path (the run graph + runs ledger)
        — no second code-reading path — so the sim is a first-class, recorded run. A sim
        never gates, so this returns synchronously; safe to call from a worker thread
        (the planner engine drives it off-loop)."""
        target_path = Path(target).expanduser()
        if not target_path.is_dir():
            raise RunConflict(f"target is not a directory: {target}", status="rejected")
        if not worktree.is_git_repo(target_path):
            raise RunConflict(
                f"target is not a git repository root: {target}", status="rejected")
        graph = self._graph_for(target_path)
        run_id = f"run-{uuid4().hex}"
        task = Task(
            task_id=f"{roles.SIM}-{uuid4().hex[:8]}",
            task_type=TaskType.READ_ONLY,
            prompt=prompt,
        )
        final = run_tracked(graph, self._store, task, thread_id=run_id)
        row = self._store.get_run(run_id)
        return SimResult(
            run_id=run_id,
            summary=str(final.get("worker_summary", "")).strip(),
            status=row.status if row else "",
        )

    # -- gate resolution (reuse the existing interrupt/resume) -------------

    def approve(self, run_id: str) -> RunRow:
        """Resume the durable gate with a go → proceed into apply-burn."""
        return self._resolve(run_id, roles.GO)

    def reject(self, run_id: str) -> RunRow:
        """Resume the durable gate with a no-go → scrub (teardown, nothing applied)."""
        return self._resolve(run_id, roles.NO_GO)

    def scrub(self, run_id: str) -> RunRow:
        """Resolve a run paused at the gate with a no-go (clean teardown via the
        graph). An already-finished run is a no-op. For an in-flight (mid-node) run,
        use :meth:`cancel`."""
        row = self._require(run_id)
        if row.status in TERMINAL_STATUSES:
            return row
        return self._resolve(run_id, roles.NO_GO)

    def _resolve(self, run_id: str, decision: str) -> RunRow:
        row = self._require(run_id)
        if row.status != STATUS_AWAITING_GATE:
            raise RunConflict(
                f"run is not awaiting a gate decision (status={row.status})", status=row.status
            )
        # One-shot: a gate is resolved exactly once. The check-and-set is atomic on
        # the event loop (no await between), so a double-submit's second call is
        # rejected here — before a second resume could double-apply the burn.
        if run_id in self._resolving:
            raise RunConflict("gate decision already in progress", status=row.status)
        self._resolving.add(run_id)
        self._channel_for(run_id)
        self._spawn(self._resume(run_id, Path(row.target), Command(resume=decision)))
        return self._require(run_id)

    async def _resume(self, run_id: str, target: Path, payload) -> None:
        try:
            await self._drive(run_id, target, payload)
        finally:
            self._resolving.discard(run_id)

    # -- cancel (mid-node, cooperative) ------------------------------------

    def cancel(self, run_id: str) -> RunRow:
        """Stop an in-flight run at the next node boundary (distinct from scrub,
        which resolves the gate). Sets a cooperative flag the driver checks between
        nodes; the driver then tears the worktree down and marks the run scrubbed.
        Cancellation completes asynchronously — poll status or the terminal event."""
        row = self._require(run_id)
        if row.status in TERMINAL_STATUSES:
            raise RunConflict(f"run already finished (status={row.status})", status=row.status)
        if row.status == STATUS_AWAITING_GATE:
            raise RunConflict(
                "run is paused at the gate — use reject/scrub, not cancel", status=row.status
            )
        self._cancels.setdefault(run_id, threading.Event()).set()
        return row

    # -- the background driver --------------------------------------------

    async def _drive(self, run_id: str, target: Path, payload) -> None:
        """Drive one leg of the graph, persisting + fanning out its merged feed.

        A leg that ends at the gate leaves the feed open (more comes on resume). A
        leg that ends terminal emits an explicit terminal event and closes. A leg
        stopped by cancel breaks at the next node boundary, then tears down + marks
        scrubbed. A leg that errors is marked failed (best-effort teardown)."""
        graph = self._graph_for(target)
        config = {"configurable": {"thread_id": run_id}}
        channel = self._channels[run_id]
        loop = asyncio.get_running_loop()
        cancel_event = self._cancels.setdefault(run_id, threading.Event())

        def pump() -> str:
            for event in stream_run_sync(graph, payload, config):
                self._persist_and_push(run_id, channel, loop, event)
                if cancel_event.is_set():
                    return "cancelled"
            return "ok"

        try:
            result = await asyncio.to_thread(pump)
        except Exception as exc:  # noqa: BLE001 — record, tear down, surface, end the feed
            self._store.mark_failed(run_id, f"{type(exc).__name__}: {exc}")
            await self._safe_teardown(target, config)
            await self._finalize(run_id, channel)
            return

        if result == "cancelled":
            await self._finalize_cancel(run_id, target, config, channel)
            return

        row = self._store.get_run(run_id)
        if row is not None and row.status in TERMINAL_STATUSES:
            await self._finalize(run_id, channel)  # explicit terminal event, then close
        # else: paused at the gate — leave the feed open for the resume leg.

    def _persist_and_push(self, run_id: str, channel: _Channel, loop, event) -> None:
        """(pump thread) assign a global seq, persist to the durable log, tail live."""
        payload = _serialize(event)
        seq = channel.next_seq()
        self._store.append_event(run_id, seq, payload["event"], payload["data"])
        wire = {"seq": seq, "event": payload["event"], "data": payload["data"]}
        asyncio.run_coroutine_threadsafe(channel.push(wire), loop).result()

    async def _emit(self, run_id: str, channel: _Channel, event_name: str, data: dict) -> None:
        """(loop) persist + tail one manager-synthesized event (e.g. the terminal)."""
        seq = channel.next_seq()
        await asyncio.to_thread(self._store.append_event, run_id, seq, event_name, data)
        await channel.push({"seq": seq, "event": event_name, "data": data})

    async def _finalize(self, run_id: str, channel: _Channel) -> None:
        """Emit the explicit terminal event with the run's final status + cost, then
        close the feed so clients learn the outcome from the stream itself. This is the
        single terminal choke point for every run, so it's where a plan-child run
        notifies its builder to advance the build."""
        row = self._store.get_run(run_id)
        if row is not None:
            await self._emit(run_id, channel, EVENT_TERMINAL,
                             {"status": row.status, "cost_usd": row.cost_usd})
        await channel.close()
        if self._run_observer is not None and row is not None and row.plan_id is not None:
            self._run_observer(run_id, row.plan_id)  # advance the owning plan's build

    async def _finalize_cancel(self, run_id: str, target: Path, config: dict, channel: _Channel) -> None:
        state = await asyncio.to_thread(self._state, target, config)
        await asyncio.to_thread(self._remove_worktree, target, state)
        cost = worker_cost_usd(state)
        await asyncio.to_thread(
            self._store.finish, run_id,
            status=STATUS_SCRUBBED, cost_usd=cost, detail="cancelled mid-run",
        )
        await self._finalize(run_id, channel)

    def _state(self, target: Path, config: dict) -> dict:
        """Read the run's checkpointed state (for worktree path + cost)."""
        try:
            return self._graph_for(target).get_state(config).values
        except Exception:  # noqa: BLE001
            return {}

    async def _safe_teardown(self, target: Path, config: dict) -> None:
        """Remove the run's worktree if one was created (idempotent). Mirrors the
        graph's teardown node for the paths where that node can't run (cancel/fail)."""
        state = await asyncio.to_thread(self._state, target, config)
        await asyncio.to_thread(self._remove_worktree, target, state)

    def _remove_worktree(self, target: Path, state: dict) -> None:
        path = state.get("worktree_path")
        if not path or not Path(path).exists():
            return
        wt = worktree.Worktree(
            path=Path(path),
            branch=state.get("worktree_branch", ""),
            target_repo=Path(target).resolve(),
            _holder=Path(state.get("worktree_holder", path)),
        )
        worktree.remove_worktree(wt)

    # -- queries -----------------------------------------------------------

    def get_run(self, run_id: str) -> RunRow:
        return self._require(run_id)

    def list_runs(
        self,
        *,
        status: Optional[str] = None,
        target: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        order: str = "desc",
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
    ) -> tuple[list[RunRow], int]:
        filt = {"status": status, "target": self._target_key(target)}
        rows = self._store.list_runs(
            filt, limit=limit, offset=offset, order=order,
            created_from=created_from, created_to=created_to,
        )
        total = self._store.count_runs(filt, created_from=created_from, created_to=created_to)
        return rows, total

    def cost_summary(
        self,
        *,
        target: Optional[str] = None,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
    ) -> dict:
        return self._store.cost_summary(
            {"target": self._target_key(target)},
            created_from=created_from, created_to=created_to,
        )

    def list_targets(self) -> list[str]:
        """Targets the registry knows about (for the UI launch selector)."""
        return self._store.list_targets()

    def active_count(self) -> int:
        """Runs still in flight (queued/running/awaiting_gate) — the header count."""
        return sum(self._store.count_runs({"status": s}) for s in _ACTIVE_STATUSES)

    async def iter_events(self, run_id: str, last_id: Optional[int]) -> AsyncIterator[tuple]:
        """Yield ``(phase, event)`` for a run: ``phase="history"`` for durable replay
        (everything after ``last_id``, from the event log) seamlessly handed off to
        ``phase="live"`` from the in-process tail. Attaching to the live channel
        BEFORE reading the log, plus a monotonic ``seq`` watermark, guarantees no gap
        and no duplicate across the replay→tail boundary."""
        self._require(run_id)  # 404 if unknown
        channel = self._channel_for(run_id)
        q = channel.attach()
        highest = last_id if last_id is not None else -1
        try:
            stored = await asyncio.to_thread(self._store.read_events, run_id, after_seq=last_id)
            for ev in stored:
                if ev["seq"] > highest:
                    yield "history", ev
                    highest = ev["seq"]

            terminal = await asyncio.to_thread(self._is_terminal, run_id)
            if channel.closed or terminal:
                while not q.empty():  # drain the tiny replay↔live overlap window
                    item = q.get_nowait()
                    if item is not _CLOSED and item["seq"] > highest:
                        yield "live", item
                        highest = item["seq"]
                return

            while True:
                item = await q.get()
                if item is _CLOSED:
                    return
                if item["seq"] > highest:
                    yield "live", item
                    highest = item["seq"]
        finally:
            channel.detach(q)

    async def events(self, run_id: str, last_id: Optional[int]) -> AsyncIterator[dict]:
        """SSE-dict event source (JSON payloads) for API/CLI clients."""
        async for _phase, ev in self.iter_events(run_id, last_id):
            yield self._sse(ev)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _sse(ev: dict) -> dict:
        return {
            "event": ev["event"],
            "id": str(ev["seq"]),
            "data": json.dumps(ev["data"], default=str, sort_keys=True),
        }

    @staticmethod
    def _target_key(target: Optional[str]) -> Optional[str]:
        return str(Path(target).expanduser().resolve()) if target else None

    def _is_terminal(self, run_id: str) -> bool:
        row = self._store.get_run(run_id)
        return row is not None and row.status in TERMINAL_STATUSES

    def _require(self, run_id: str) -> RunRow:
        row = self._store.get_run(run_id)
        if row is None:
            raise RunNotFound(run_id)
        return row

    async def aclose(self) -> None:
        """Cancel in-flight drives (used on app shutdown)."""
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
