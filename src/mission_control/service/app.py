"""The FastAPI app: a thin HTTP wrapper over the graph.

Endpoints launch / resolve / scrub / query / stream runs — all orchestration is
delegated to ``graph.py`` via the :class:`RunManager`. v1 is localhost-only with
NO auth (see ``__main__`` for the 127.0.0.1 bind).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from .manager import RunConflict, RunManager, RunNotFound
from .metrics import compute_metrics
from .models import (
    DecisionResponse,
    LaunchRequest,
    MetricsResponse,
    RunDetail,
    RunList,
    TargetList,
)
from .plan_models import (
    OpenPlanRequest,
    PlanDetail,
    PlanList,
    PlanSummary,
    TurnRequest,
    TurnResponse,
)
from .planner import DoneEvent, StageEvent, TokenEvent
from .plans import PlanConflict, PlanManager, PlanNotFound, PlanNotReady
from .web import mount_web

# How often to emit an SSE keepalive comment (seconds) — keeps proxies/clients
# from timing out an idle stream and plays nicely with auto-reconnect.
_SSE_PING_SECONDS = 15


def _plan_event_sse(event) -> dict:
    """One engine event → an SSE ``{event, data}`` pair."""
    if isinstance(event, TokenEvent):
        return {"event": "token", "data": json.dumps({"text": event.text})}
    if isinstance(event, StageEvent):
        return {"event": "stage",
                "data": json.dumps({"stage": event.stage, "status": event.status})}
    if isinstance(event, DoneEvent):
        return {"event": "done", "data": json.dumps({
            "turn": {"seq": event.turn.seq, "role": event.turn.role,
                     "content": event.turn.content},
            "plan": {"id": event.plan.id, "status": event.plan.status,
                     "stage": event.plan.stage, "mode": event.plan.mode},
        }, default=str)}
    return {"event": "message", "data": json.dumps({"value": str(event)})}


async def _plan_turn_sse(gen):
    """Drive a sync engine generator in a worker thread, marshaling its events back
    to the event loop through a queue (the manager's pump pattern)."""
    import asyncio

    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    _END = object()

    def pump() -> None:
        try:
            for event in gen:
                asyncio.run_coroutine_threadsafe(q.put(event), loop).result()
        finally:
            asyncio.run_coroutine_threadsafe(q.put(_END), loop).result()

    fut = loop.run_in_executor(None, pump)
    try:
        while True:
            event = await q.get()
            if event is _END:
                break
            yield _plan_event_sse(event)
    finally:
        await fut


def get_manager(request: Request) -> RunManager:
    return request.app.state.manager


def get_plans(request: Request) -> PlanManager:
    plans = getattr(request.app.state, "plans", None)
    if plans is None:  # PLAN seam not wired on this app
        raise HTTPException(status_code=404, detail="plans are not enabled")
    return plans


def create_app(
    manager: RunManager,
    plan_manager: Optional[PlanManager] = None,
    builder=None,
) -> FastAPI:
    """Build the app around a ready :class:`RunManager` (its graph/checkpointer/
    ledger are already wired). Kept injectable so tests supply their own. When a
    :class:`PlanManager` is supplied, the ``/plans`` seam is mounted over it; when a
    :class:`~.plan_builder.PlanBuilder` is also supplied, finalize dispatches the
    plan's units as runs (the hand-off to Mission Control)."""
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # On startup, resume any build left mid-flight by a previous process — the
        # plan store is durable, so a kill+restart continues the build (units whose
        # deps have durably succeeded get dispatched; gate-paused burns resume on go).
        if builder is not None:
            await builder.resume_builds()
        yield
        await manager.aclose()  # cancel in-flight drives on shutdown

    app = FastAPI(title="Mission Control", version="0.0.0", lifespan=lifespan)
    app.state.manager = manager
    app.state.plans = plan_manager
    app.state.builder = builder

    # -- launch ------------------------------------------------------------

    @app.post("/runs", response_model=RunDetail, status_code=201)
    async def launch_run(body: LaunchRequest, mgr: RunManager = Depends(get_manager)) -> RunDetail:
        try:
            run_id = mgr.launch(target=body.target, task_type=body.task_type, prompt=body.prompt)
        except RunConflict as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return RunDetail.from_row(mgr.get_run(run_id))

    # -- gate resolution ---------------------------------------------------

    def _decision(action, run_id: str) -> DecisionResponse:
        try:
            row = action(run_id)
        except RunNotFound:
            raise HTTPException(status_code=404, detail=f"no such run: {run_id}")
        except RunConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return DecisionResponse(run_id=row.run_id, status=row.status)

    @app.post("/runs/{run_id}/approve", response_model=DecisionResponse)
    async def approve(run_id: str, mgr: RunManager = Depends(get_manager)) -> DecisionResponse:
        return _decision(mgr.approve, run_id)

    @app.post("/runs/{run_id}/reject", response_model=DecisionResponse)
    async def reject(run_id: str, mgr: RunManager = Depends(get_manager)) -> DecisionResponse:
        return _decision(mgr.reject, run_id)

    @app.post("/runs/{run_id}/scrub", response_model=DecisionResponse)
    async def scrub(run_id: str, mgr: RunManager = Depends(get_manager)) -> DecisionResponse:
        return _decision(mgr.scrub, run_id)

    @app.post("/runs/{run_id}/cancel", response_model=DecisionResponse)
    async def cancel(run_id: str, mgr: RunManager = Depends(get_manager)) -> DecisionResponse:
        # Distinct from scrub: stops an IN-FLIGHT (mid-node) run at the next node
        # boundary and tears down; scrub only resolves a run paused at the gate.
        return _decision(mgr.cancel, run_id)

    # -- queries -----------------------------------------------------------

    @app.get("/runs", response_model=RunList)
    def list_runs(
        status: Optional[str] = None,
        target: Optional[str] = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        order: str = Query("desc", pattern="^(asc|desc)$"),
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
        mgr: RunManager = Depends(get_manager),
    ) -> RunList:
        rows, total = mgr.list_runs(
            status=status, target=target, limit=limit, offset=offset, order=order,
            created_from=created_from, created_to=created_to,
        )
        return RunList(runs=[RunDetail.from_row(r) for r in rows],
                       total=total, limit=limit, offset=offset)

    @app.get("/runs/{run_id}", response_model=RunDetail)
    def get_run(run_id: str, mgr: RunManager = Depends(get_manager)) -> RunDetail:
        try:
            return RunDetail.from_row(mgr.get_run(run_id))
        except RunNotFound:
            raise HTTPException(status_code=404, detail=f"no such run: {run_id}")

    @app.get("/runs/{run_id}/changes")
    def run_changes(run_id: str, mgr: RunManager = Depends(get_manager)) -> dict:
        """The diff a **go** would apply — the material for a go/no-go decision. 404 if
        the run isn't paused at the gate (no isolated worktree to diff)."""
        try:
            changes = mgr.run_changes(run_id)
        except RunNotFound:
            raise HTTPException(status_code=404, detail=f"no such run: {run_id}")
        if changes is None:
            raise HTTPException(status_code=404,
                                detail="no pending changes (run is not at the gate)")
        return changes

    @app.get("/targets", response_model=TargetList)
    def list_targets(mgr: RunManager = Depends(get_manager)) -> TargetList:
        return TargetList(targets=mgr.list_targets())

    # -- live feed (SSE) ---------------------------------------------------

    @app.get("/runs/{run_id}/events")
    async def run_events(
        run_id: str,
        last_event_id: Optional[str] = Header(default=None),
        mgr: RunManager = Depends(get_manager),
    ):
        try:
            mgr.get_run(run_id)
        except RunNotFound:
            raise HTTPException(status_code=404, detail=f"no such run: {run_id}")
        last_id = int(last_event_id) if last_event_id and last_event_id.isdigit() else None
        return EventSourceResponse(mgr.events(run_id, last_id), ping=_SSE_PING_SECONDS)

    # -- cross-run metrics -------------------------------------------------

    @app.get("/metrics", response_model=MetricsResponse)
    async def metrics(
        target: Optional[str] = None,
        from_: Optional[datetime] = Query(None, alias="from"),
        to: Optional[datetime] = Query(None, alias="to"),
        mgr: RunManager = Depends(get_manager),
    ) -> MetricsResponse:
        # Global DuckDB rollup + an exact registry aggregate scoped to target/window
        # (shared with the /ui/metrics dashboard). Blocking → off the loop.
        data = await run_in_threadpool(
            compute_metrics, mgr, target=target, created_from=from_, created_to=to
        )
        return MetricsResponse(**data)

    # -- plans (the running instance's own operational memory) -------------

    def _plan_detail(plans: PlanManager, plan_id: str) -> PlanDetail:
        """The full plan aggregate + its child runs (status/cost) and rolled-up cost."""
        return PlanDetail.from_aggregate(
            *plans.aggregate(plan_id),
            child_runs=manager.child_runs(plan_id),
            build_cost=manager.plan_cost(plan_id),
        )

    @app.post("/plans", response_model=PlanDetail, status_code=201)
    def open_plan(body: OpenPlanRequest, plans: PlanManager = Depends(get_plans)) -> PlanDetail:
        try:
            row = plans.open_plan(
                target=body.target, mode=body.mode,
                methodology=body.methodology, cloud_target=body.cloud_target,
                workstream=body.workstream,
            )
        except PlanConflict as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _plan_detail(plans, row.id)

    @app.post("/plans/{plan_id}/turns", response_model=TurnResponse)
    async def append_turn(
        plan_id: str, body: TurnRequest, plans: PlanManager = Depends(get_plans)
    ) -> TurnResponse:
        from .plan_models import TurnModel  # local: shape the reply row for the wire

        try:
            # The engine does blocking DB work + (optionally) an LLM call → off-loop.
            reply = await run_in_threadpool(plans.append_turn, plan_id, body.content)
        except PlanNotFound:
            raise HTTPException(status_code=404, detail=f"no such plan: {plan_id}")
        except PlanConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return TurnResponse(plan_id=plan_id, reply=TurnModel.from_row(reply))

    @app.post("/plans/{plan_id}/turns/stream")
    async def stream_turn(
        plan_id: str, body: TurnRequest, plans: PlanManager = Depends(get_plans)
    ):
        # Stream the planner's reply tokens as SSE — same pattern as the run event
        # feed. The engine is a sync generator driven in a worker thread; its events
        # are marshaled back to the loop through a queue.
        try:
            gen = plans.stream_turn(plan_id, body.content)
        except PlanNotFound:
            raise HTTPException(status_code=404, detail=f"no such plan: {plan_id}")
        except PlanConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return EventSourceResponse(_plan_turn_sse(gen), ping=_SSE_PING_SECONDS)

    @app.get("/plans", response_model=PlanList)
    def list_plans(
        status: Optional[str] = None,
        mode: Optional[str] = None,
        target: Optional[str] = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        order: str = Query("desc", pattern="^(asc|desc)$"),
        plans: PlanManager = Depends(get_plans),
    ) -> PlanList:
        rows, total = plans.list_plans(
            status=status, mode=mode, target=target, limit=limit, offset=offset, order=order,
        )
        return PlanList(plans=[PlanSummary.from_row(r) for r in rows],
                        total=total, limit=limit, offset=offset)

    @app.get("/plans/{plan_id}", response_model=PlanDetail)
    def get_plan(plan_id: str, plans: PlanManager = Depends(get_plans)) -> PlanDetail:
        try:
            return _plan_detail(plans, plan_id)
        except PlanNotFound:
            raise HTTPException(status_code=404, detail=f"no such plan: {plan_id}")

    @app.post("/plans/{plan_id}/finalize", response_model=PlanDetail)
    async def finalize_plan(plan_id: str, plans: PlanManager = Depends(get_plans)) -> PlanDetail:
        # Finalize gates on readiness (P1), then hands the plan to Mission Control: the
        # builder translates its units into runs on the existing launch path (async, so
        # the burn gates and background drives run on the loop).
        try:
            await run_in_threadpool(plans.finalize, plan_id)
        except PlanNotFound:
            raise HTTPException(status_code=404, detail=f"no such plan: {plan_id}")
        except PlanNotReady as exc:
            raise HTTPException(status_code=409, detail=f"plan not ready: {exc.reason}")
        if app.state.builder is not None:
            await app.state.builder.start_build(plan_id)
        return _plan_detail(plans, plan_id)

    # Server-rendered control-room UI (htmx, no JS build) over the same seam.
    mount_web(app)

    return app
