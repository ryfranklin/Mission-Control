"""The FastAPI app: a thin HTTP wrapper over the graph.

Endpoints launch / resolve / scrub / query / stream runs — all orchestration is
delegated to ``graph.py`` via the :class:`RunManager`. v1 is localhost-only with
NO auth (see ``__main__`` for the 127.0.0.1 bind).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from .. import analytics
from .manager import RunConflict, RunManager, RunNotFound
from .models import (
    DecisionResponse,
    LaunchRequest,
    MetricsResponse,
    RunDetail,
    RunList,
)

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

    # -- queries -----------------------------------------------------------

    @app.get("/runs", response_model=RunList)
    def list_runs(
        status: Optional[str] = None,
        target: Optional[str] = None,
        mgr: RunManager = Depends(get_manager),
    ) -> RunList:
        rows = mgr.list_runs(status=status, target=target)
        return RunList(runs=[RunDetail.from_row(r) for r in rows])

    @app.get("/runs/{run_id}", response_model=RunDetail)
    def get_run(run_id: str, mgr: RunManager = Depends(get_manager)) -> RunDetail:
        try:
            return RunDetail.from_row(mgr.get_run(run_id))
        except RunNotFound:
            raise HTTPException(status_code=404, detail=f"no such run: {run_id}")

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
    async def metrics() -> MetricsResponse:
        # DuckDB pass is blocking → run off the event loop.
        result = await run_in_threadpool(analytics.analyze)
        return MetricsResponse(**result.to_dict())

    return app
