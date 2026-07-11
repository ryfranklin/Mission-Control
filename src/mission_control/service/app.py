"""The FastAPI app: a thin HTTP wrapper over the graph.

Endpoints launch / resolve / scrub / query / stream runs — all orchestration is
delegated to ``graph.py`` via the :class:`RunManager`. v1 is localhost-only with
NO auth (see ``__main__`` for the 127.0.0.1 bind).
"""

from __future__ import annotations

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
from .web import mount_web

# How often to emit an SSE keepalive comment (seconds) — keeps proxies/clients
# from timing out an idle stream and plays nicely with auto-reconnect.
_SSE_PING_SECONDS = 15


def get_manager(request: Request) -> RunManager:
    return request.app.state.manager


def create_app(manager: RunManager) -> FastAPI:
    """Build the app around a ready :class:`RunManager` (its graph/checkpointer/
    ledger are already wired). Kept injectable so tests supply their own."""
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await manager.aclose()  # cancel in-flight drives on shutdown

    app = FastAPI(title="Mission Control", version="0.0.0", lifespan=lifespan)
    app.state.manager = manager

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

    # Server-rendered control-room UI (htmx, no JS build) over the same seam.
    mount_web(app)

    return app
