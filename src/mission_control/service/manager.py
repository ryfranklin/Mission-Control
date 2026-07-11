"""The run manager: the service's only stateful piece.

It wraps ``graph.py`` — it does NOT re-implement orchestration. Its jobs:

* launch a run by driving the EXISTING graph (via :func:`live.stream_run`) in a
  background task, keyed by ``thread_id``;
* fan the merged live feed (node transitions + priced telemetry + gate-waiting)
  out to any number of SSE subscribers, with full replay for late/reconnecting
  clients;
* resolve the durable go/no-go by resuming the EXISTING ``interrupt()`` with a
  decision (approve → go, reject/scrub → no-go); and
* answer status/detail/list straight from the S2 runs ledger.

One process, in-memory fan-out; durability + the runs ledger live in Postgres.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import AsyncIterator, Optional
from uuid import uuid4

from langgraph.types import Command

from .. import roles
from ..graph import build_run_graph, initial_state
from ..live import GateWaiting, NodeTransition, StepMetric, stream_run_sync
from ..runs_store import TERMINAL_STATUSES, RunRow, RunStore
from ..tasks import Task, TaskType
from ..worker import StubWorker

# Wire value ("sim"/"burn") -> TaskType, sourced from the enum (metaphor stays in roles).
_TASK_TYPE = {t.value: t for t in TaskType}

# Sentinel pushed to subscriber queues when a run's feed is complete.
_CLOSED = object()


class RunNotFound(Exception):
    """No run with the given id in the ledger."""


class RunConflict(Exception):
    """The requested transition isn't valid for the run's current status."""

    def __init__(self, message: str, status: str) -> None:
        super().__init__(message)
        self.status = status


class _Channel:
    """A per-run event log with live fan-out. Retains full history so a client
    that connects late (or reconnects) can replay from any point, then follow."""

    def __init__(self) -> None:
        self.history: list[dict] = []
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()
        self.closed = False

    async def publish(self, payload: dict) -> None:
        async with self._lock:
            event = {**payload, "id": len(self.history)}
            self.history.append(event)
            for q in self._subscribers:
                q.put_nowait(event)

    async def close(self) -> None:
        async with self._lock:
            self.closed = True
            for q in self._subscribers:
                q.put_nowait(_CLOSED)

    async def subscribe(self, last_id: Optional[int]) -> AsyncIterator[dict]:
        """Replay events after ``last_id`` (or from the start), then follow live
        until the run's feed closes. Registration snapshots history under the lock
        so no event is dropped or duplicated across the replay→follow handover."""
        q: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            start = 0 if last_id is None else last_id + 1
            backlog = self.history[start:]
            was_closed = self.closed
            self._subscribers.add(q)
        try:
            for event in backlog:
                yield event
            if was_closed:
                return
            while True:
                item = await q.get()
                if item is _CLOSED:
                    return
                yield item
        finally:
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
    """Launches / resolves / streams / queries runs over the existing graph."""

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
        self._channels: dict[str, _Channel] = {}    # per-run live feed
        self._tasks: set[asyncio.Task] = set()      # background drives (keep refs alive)

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

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- launch ------------------------------------------------------------

    def launch(self, *, target: str, task_type: str, prompt: str) -> str:
        """Register a queued run and kick off the graph in the background, keyed by
        its thread_id (== run_id). Returns the run_id immediately."""
        target_path = Path(target).expanduser()
        if not target_path.is_dir():
            raise RunConflict(f"target is not a directory: {target}", status="rejected")
        tt = _TASK_TYPE[task_type]  # validated by the request model

        run_id = f"run-{uuid4().hex}"
        task = Task(task_id=f"{task_type}-{uuid4().hex[:8]}", task_type=tt, prompt=prompt)
        self._store.launch(run_id, task_type=task_type, target=str(target_path.resolve()))

        self._channels[run_id] = _Channel()
        self._spawn(self._drive(run_id, target_path, initial_state(task, run_id=run_id)))
        return run_id

    # -- gate resolution (reuse the existing interrupt/resume) -------------

    def approve(self, run_id: str) -> RunRow:
        """Resume the durable gate with a go → proceed into apply-burn."""
        return self._resolve(run_id, roles.GO)

    def reject(self, run_id: str) -> RunRow:
        """Resume the durable gate with a no-go → scrub (teardown, nothing applied)."""
        return self._resolve(run_id, roles.NO_GO)

    def scrub(self, run_id: str) -> RunRow:
        """Kill a run with clean teardown. A run paused at the gate is resumed
        no-go (drives the graph to teardown); an already-finished run is a no-op."""
        row = self._require(run_id)
        if row.status in TERMINAL_STATUSES:
            return row  # already torn down
        return self._resolve(run_id, roles.NO_GO)

    def _resolve(self, run_id: str, decision: str) -> RunRow:
        row = self._require(run_id)
        from ..runs_store import STATUS_AWAITING_GATE

        if row.status != STATUS_AWAITING_GATE:
            raise RunConflict(
                f"run is not awaiting a gate decision (status={row.status})", status=row.status
            )
        self._channels.setdefault(run_id, _Channel())
        self._spawn(self._drive(run_id, Path(row.target), Command(resume=decision)))
        return self._require(run_id)

    # -- the background driver --------------------------------------------

    async def _drive(self, run_id: str, target: Path, payload) -> None:
        """Drive one leg of the graph and publish its merged feed. A leg that ends
        at the gate leaves the channel open (more comes on resume); a leg that ends
        terminal (or errors) closes it.

        The graph runs on the sync ``PostgresSaver`` (whose async API is
        unimplemented), so we drive the SYNC merged feed in a worker thread and
        marshal each event back onto this loop — keeping node/telemetry ordering
        and applying backpressure via ``.result()``."""
        graph = self._graph_for(target)
        config = {"configurable": {"thread_id": run_id}}
        channel = self._channels[run_id]
        loop = asyncio.get_running_loop()

        def pump() -> None:
            for event in stream_run_sync(graph, payload, config):
                asyncio.run_coroutine_threadsafe(
                    channel.publish(_serialize(event)), loop
                ).result()

        try:
            await asyncio.to_thread(pump)
        except Exception as exc:  # noqa: BLE001 — record, surface, and end the feed
            self._store.mark_failed(run_id, f"{type(exc).__name__}: {exc}")
            await channel.publish({"event": "error", "data": {"message": str(exc)}})
            await channel.close()
            return
        row = self._store.get_run(run_id)
        if row is not None and row.status in TERMINAL_STATUSES:
            await channel.close()

    # -- queries -----------------------------------------------------------

    def get_run(self, run_id: str) -> RunRow:
        return self._require(run_id)

    def list_runs(self, *, status: Optional[str] = None, target: Optional[str] = None) -> list[RunRow]:
        target_key = str(Path(target).expanduser().resolve()) if target else None
        return self._store.list_runs({"status": status, "target": target_key})

    async def events(self, run_id: str, last_id: Optional[int]) -> AsyncIterator[dict]:
        """The SSE event source for a run. Yields SSE-ready dicts; formatting +
        keepalive pings are the endpoint's job (via EventSourceResponse)."""
        self._require(run_id)  # 404 if unknown
        channel = self._channels.get(run_id)
        if channel is None:  # known to the ledger but not streamed in this process
            channel = self._channels.setdefault(run_id, _Channel())
            if self._store.get_run(run_id).status in TERMINAL_STATUSES:
                await channel.close()
        async for event in channel.subscribe(last_id):
            yield {"event": event["event"], "id": str(event["id"]),
                   "data": json.dumps(event["data"], default=str, sort_keys=True)}

    # -- helpers -----------------------------------------------------------

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
