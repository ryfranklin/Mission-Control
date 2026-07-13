"""Server-rendered control-room UI (Jinja + htmx) over the same service.

Handlers are functionally named; the Mission Control metaphor is presentation only
and every metaphor label is pulled from ``roles.py`` at request time (so a term
swap in roles.py changes the UI, and nothing hardcodes the vocabulary here).

The fleet is a cheap POLLED snapshot (htmx ``hx-get`` on a timer, tab-hidden aware);
SSE is reserved for the per-run live view. v1 = localhost / no auth.
"""

from __future__ import annotations

from pathlib import Path

from datetime import datetime

from fastapi import APIRouter, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import escape
from sse_starlette.sse import EventSourceResponse

from ... import aidlc, roles
from ..manager import RunConflict, RunManager, RunNotFound
from ..metrics import compute_metrics
from ..planner import DoneEvent, TokenEvent
from ..plans import PlanConflict, PlanNotFound, PlanNotReady

_SSE_PING_SECONDS = 15

_BASE = Path(__file__).parent
TEMPLATES_DIR = _BASE / "templates"
STATIC_DIR = _BASE / "static"

PAGE_SIZE = 25
_DEFAULT_PROMPT = "Investigate the target repository and report your findings."

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _fmt_ts(value) -> str:
    if not value:
        return "—"
    try:
        return value.strftime("%m-%d %H:%M:%S")
    except AttributeError:
        return str(value)


templates.env.filters["ts"] = _fmt_ts


def _labels() -> dict:
    """The metaphor vocabulary, read from roles.py at request time."""
    return {
        "orchestrator": roles.ORCHESTRATOR,
        "worker": roles.WORKER,
        "sim": roles.SIM,
        "burn": roles.BURN,
        "go": roles.GO,
        "no_go": roles.NO_GO,
        "scrub": roles.SCRUB,
        "plan": roles.PLAN,
        "planner": roles.PLANNER,
    }


router = APIRouter()


def _mgr(request: Request) -> RunManager:
    return request.app.state.manager


def _fleet_ctx(request: Request, page: int) -> dict:
    mgr = _mgr(request)
    page = max(0, page)
    rows, total = mgr.list_runs(limit=PAGE_SIZE, offset=page * PAGE_SIZE, order="desc")
    return {
        "labels": _labels(),
        "runs": rows,
        "total": total,
        "page": page,
        "page_size": PAGE_SIZE,
        "has_next": (page + 1) * PAGE_SIZE < total,
        "live_count": mgr.active_count(),
    }


# -- fleet dashboard (the control-room shell) ------------------------------

@router.get("/", response_class=HTMLResponse)
@router.get("/ui", response_class=HTMLResponse)
def fleet_page(request: Request, page: int = 0):
    ctx = _fleet_ctx(request, page)
    ctx["targets"] = _mgr(request).list_targets()
    return templates.TemplateResponse(request=request, name="fleet.html", context=ctx)


@router.get("/ui/fleet", response_class=HTMLResponse)
def fleet_table(request: Request, page: int = 0):
    """The polled fleet fragment (htmx swaps this into the page every few seconds)."""
    ctx = _fleet_ctx(request, page)
    ctx["oob"] = True  # also refresh the header's live count out-of-band
    return templates.TemplateResponse(request=request, name="_fleet_table.html", context=ctx)


# -- cost / performance dashboard (a client of the /metrics logic) ---------

def _metrics_ctx(request: Request, target, from_, to) -> dict:
    mgr = _mgr(request)
    data = compute_metrics(mgr, target=target or None, created_from=from_, created_to=to)
    return {
        "labels": _labels(),
        "m": data,
        "rs": data["runs_summary"],
        "targets": mgr.list_targets(),
        "live_count": mgr.active_count(),
        "scope_target": target or "",
        "scope_from": from_.strftime("%Y-%m-%dT%H:%M") if from_ else "",
        "scope_to": to.strftime("%Y-%m-%dT%H:%M") if to else "",
    }


@router.get("/ui/metrics", response_class=HTMLResponse)
def metrics_page(
    request: Request,
    target: str | None = None,
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None, alias="to"),
):
    return templates.TemplateResponse(
        request=request, name="metrics.html", context=_metrics_ctx(request, target, from_, to))


@router.get("/ui/metrics/panel", response_class=HTMLResponse)
def metrics_panel(
    request: Request,
    target: str | None = None,
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None, alias="to"),
):
    """The re-queryable dashboard panel (htmx swaps this when a filter changes)."""
    return templates.TemplateResponse(
        request=request, name="_metrics_panel.html", context=_metrics_ctx(request, target, from_, to))


# -- launch control --------------------------------------------------------

@router.post("/ui/launch")
async def launch(request: Request, target: str = Form(...), task_type: str = Form(...)):
    """Dispatch a Controller from the launch form, then redirect to its station.

    Async because launching spawns the background driver task (needs the loop)."""
    try:
        run_id = _mgr(request).launch(target=target, task_type=task_type, prompt=_DEFAULT_PROMPT)
    except (RunConflict, KeyError) as exc:
        raise HTTPException(status_code=400, detail=f"cannot launch: {exc}")
    return RedirectResponse(url=f"/ui/runs/{run_id}", status_code=303)


# -- run detail (station view; the SSE live feed is U3) --------------------

def _cost_label(run) -> str:
    """Honest cost wording (5a Q1): reconciled only at a terminal state; never $0."""
    if run.ended_at:
        return f"${run.cost_usd:.6f} · reconciled"
    return "not yet reconciled"


@router.get("/ui/runs/{run_id}/changes", response_class=HTMLResponse)
def run_changes(request: Request, run_id: str):
    """The go/no-go review panel: the diff a burn will apply (files, rationale, patch).
    Loaded when a run reaches the gate so the operator reviews before deciding."""
    mgr = _mgr(request)
    try:
        changes = mgr.run_changes(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail=f"no such run: {run_id}")
    return templates.TemplateResponse(
        request=request, name="_run_changes.html",
        context={"labels": _labels(), "changes": changes, "run_id": run_id})


@router.get("/ui/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: str):
    mgr = _mgr(request)
    try:
        run = mgr.get_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail=f"no such run: {run_id}")
    return templates.TemplateResponse(
        request=request, name="run_detail.html",
        context={"labels": _labels(), "run": run, "cost_label": _cost_label(run),
                 "live_count": mgr.active_count()},
    )


# -- per-run live feed: HTML fragments for the htmx SSE timeline -----------

def _item_html(**ctx) -> str:
    return templates.get_template("_timeline_item.html").render(labels=_labels(), **ctx)


def _oob(elem_id: str, inner: str) -> str:
    return f'<div id="{elem_id}" hx-swap-oob="true">{inner}</div>'


def _final_banner_html(status: str, cost: float) -> str:
    return templates.get_template("_final_banner.html").render(
        fstatus=status, fcost=cost)


@router.get("/ui/runs/{run_id}/events")
async def run_events(
    request: Request,
    run_id: str,
    after: int | None = None,
    last_event_id: str | None = Header(default=None),
):
    """SSE feed of the run's timeline as HTML fragments for the htmx SSE extension.

    Replays the durable history (from the event log) as ``phase-history`` items,
    then tails the live channel as ``phase-live`` items — the full timeline is
    reconstructed on connect (the 5a gap), before the live tail. A reconnect sends
    Last-Event-ID and resumes from there."""
    mgr = _mgr(request)
    try:
        mgr.get_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail=f"no such run: {run_id}")

    if last_event_id and last_event_id.isdigit():
        last_id = int(last_event_id)
    elif after is not None:
        last_id = after
    else:
        last_id = None

    async def stream():
        running = 0.0
        seen_live = False
        async for phase, ev in mgr.iter_events(run_id, last_id):
            name, data, seq = ev["event"], ev["data"], ev["seq"]
            if name == "step_metric":
                running += float(data["event"]["cost_usd"])
            divider = phase == "live" and not seen_live
            if divider:
                seen_live = True
            frag = _item_html(event=name, data=data, phase=phase, seq=seq,
                              running=running, divider=divider)
            if name == "gate_waiting":
                # reveal the GO / NO-GO / SCRUB controls when the run hits the gate live
                frag += _controls_html(run_id, "awaiting_gate", False, oob=True)
            elif name == "terminal":
                frag += _oob("final-banner", _final_banner_html(data["status"], data["cost_usd"]))
                frag += _controls_html(run_id, data["status"], True, oob=True)  # clear controls
            yield {"event": name, "id": str(seq), "data": frag}

    return EventSourceResponse(stream(), ping=_SSE_PING_SECONDS)


def _controls_html(run_id, status, ended, *, resolving=False, message=None, oob=False) -> str:
    return templates.get_template("_run_controls.html").render(
        labels=_labels(), run_id=run_id, status=status, ended=ended,
        resolving=resolving, message=message, oob=oob)


def _write_action(request: Request, run_id: str, fn, message: str) -> HTMLResponse:
    """Run a write action, then return the re-rendered #run-controls fragment (htmx
    swaps it in). A conflicting double-submit renders an 'already resolved' state
    rather than acting twice — the manager's one-shot guard is the real guarantee."""
    mgr = _mgr(request)
    try:
        run = mgr.get_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail=f"no such run: {run_id}")
    try:
        fn(run_id)
    except RunConflict:
        message = "already resolved"
    return HTMLResponse(_controls_html(run_id, run.status, bool(run.ended_at),
                                       resolving=True, message=message))


@router.post("/ui/runs/{run_id}/approve")
async def ui_approve(request: Request, run_id: str):
    return _write_action(request, run_id, _mgr(request).approve, f"{roles.GO} sent — resuming")


@router.post("/ui/runs/{run_id}/reject")
async def ui_reject(request: Request, run_id: str):
    return _write_action(request, run_id, _mgr(request).reject, f"{roles.NO_GO} sent — scrubbing")


@router.post("/ui/runs/{run_id}/scrub")
async def ui_scrub(request: Request, run_id: str):
    # Scrub = decline AT THE GATE (no-go). Distinct from cancel (mid-node stop).
    return _write_action(request, run_id, _mgr(request).scrub, f"{roles.SCRUB} sent — scrubbing")


@router.post("/ui/runs/{run_id}/cancel")
async def ui_cancel(request: Request, run_id: str):
    # Cancel = stop an IN-FLIGHT run mid-node (clean teardown). Distinct from scrub.
    return _write_action(request, run_id, _mgr(request).cancel, "cancel requested — stopping")


# ==========================================================================
# The Planner surface — a client of the P1–P3 plan endpoints.
#
# The interactive session reads like a chat: the operator posts a turn, and the
# planner's reply STREAMS in over SSE (the same htmx-SSE pattern as the run feed).
# A live panel beside it shows the plan taking shape (stages, requirements, units,
# readiness). Route names stay functional; the plan metaphor is presentation only,
# pulled from roles.py at request time (nothing here hardcodes the vocabulary).
# ==========================================================================

def _plans(request: Request):
    plans = getattr(request.app.state, "plans", None)
    if plans is None:  # the PLAN seam isn't mounted on this app
        raise HTTPException(status_code=404, detail="plans are not enabled")
    return plans


def _stage_checklist(mode: str, units) -> list[dict]:
    """The INCEPTION stage checklist: each stage + whether it is laid down 'in place'.
    Reverse Engineering is a brownfield-only stage, shown once it applies."""
    in_place = {u.title for u in units if u.phase == aidlc.Phase.INCEPTION.value}
    checklist = [{"title": s.title, "in_place": s.title in in_place}
                 for s in aidlc.INCEPTION_STAGES]
    if mode == aidlc.MODE_BROWNFIELD or aidlc.REVERSE_ENGINEERING_TITLE in in_place:
        checklist.insert(1, {"title": aidlc.REVERSE_ENGINEERING_TITLE,
                             "in_place": aidlc.REVERSE_ENGINEERING_TITLE in in_place})
    return checklist


def _panel_ctx(request: Request, plan_id: str, *, message: str | None = None) -> dict:
    plans = _plans(request)
    mgr = _mgr(request)
    plan, _turns, requirements, units, readiness = plans.aggregate(plan_id)
    return {
        "labels": _labels(),
        "plan": plan,
        "is_brownfield": plan.mode == aidlc.MODE_BROWNFIELD,
        "stages": _stage_checklist(plan.mode, units),
        "requirements": requirements,
        "units": units,
        "readiness": readiness,
        "ready": aidlc.is_ready(readiness),
        "child_runs": mgr.child_runs(plan_id),   # units dispatched as runs (build)
        "build_cost": mgr.plan_cost(plan_id),     # rolled-up cost across child runs
        "message": message,
    }


@router.get("/ui/plans", response_class=HTMLResponse)
def plans_page(request: Request, page: int = 0):
    """The plan list + the NEW-PLAN control (target / methodology / cloud)."""
    plans = _plans(request)
    page = max(0, page)
    rows, total = plans.list_plans(limit=PAGE_SIZE, offset=page * PAGE_SIZE, order="desc")
    ctx = {
        "labels": _labels(),
        "plans": rows,
        "total": total,
        "page": page,
        "page_size": PAGE_SIZE,
        "has_next": (page + 1) * PAGE_SIZE < total,
        "live_count": _mgr(request).active_count(),
        "default_methodology": plans.methodology_default,
        "default_cloud": plans.cloud_default,
    }
    return templates.TemplateResponse(request=request, name="plans.html", context=ctx)


@router.post("/ui/plans")
def create_plan(
    request: Request,
    target: str = Form(""),
    methodology: str = Form(""),
    cloud: str = Form(""),
):
    """Open a session from the new-plan control. A blank / 'new' / 'greenfield' target
    means a fresh build; a path means an existing repo (workspace detection decides
    greenfield vs brownfield on the first turn)."""
    plans = _plans(request)
    t = target.strip()
    tgt = None if (not t or t.lower() in {"new", "greenfield", "new/greenfield"}) else t
    try:
        row = plans.open_plan(
            target=tgt, mode=aidlc.MODE_GREENFIELD,
            methodology=methodology.strip() or None, cloud_target=cloud.strip() or None,
        )
    except PlanConflict as exc:
        raise HTTPException(status_code=400, detail=f"cannot open plan: {exc}")
    return RedirectResponse(url=f"/ui/plans/{row.id}", status_code=303)


@router.get("/ui/plans/{plan_id}", response_class=HTMLResponse)
def plan_session(request: Request, plan_id: str):
    """The session: the interactive transcript beside the live plan panel."""
    try:
        plan, turns, *_ = _plans(request).aggregate(plan_id)
    except PlanNotFound:
        raise HTTPException(status_code=404, detail=f"no such plan: {plan_id}")
    ctx = _panel_ctx(request, plan_id)
    ctx["turns"] = turns
    ctx["live_count"] = _mgr(request).active_count()
    return templates.TemplateResponse(request=request, name="plan_detail.html", context=ctx)


@router.get("/ui/plans/{plan_id}/panel", response_class=HTMLResponse)
def plan_panel(request: Request, plan_id: str):
    """The live plan panel fragment (htmx refreshes it after each streamed reply)."""
    try:
        return templates.TemplateResponse(
            request=request, name="_plan_panel.html", context=_panel_ctx(request, plan_id))
    except PlanNotFound:
        raise HTTPException(status_code=404, detail=f"no such plan: {plan_id}")


@router.post("/ui/plans/{plan_id}/turns", response_class=HTMLResponse)
def plan_turn(request: Request, plan_id: str, content: str = Form(...)):
    """Post an operator turn: record it, then hand back the operator bubble + an empty
    planner bubble that opens the SSE reply stream (htmx appends the tokens)."""
    plans = _plans(request)
    try:
        plans.record_operator_turn(plan_id, content)
    except PlanNotFound:
        raise HTTPException(status_code=404, detail=f"no such plan: {plan_id}")
    except PlanConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return templates.TemplateResponse(
        request=request, name="_plan_reply.html",
        context={"labels": _labels(), "plan_id": plan_id, "content": content})


@router.get("/ui/plans/{plan_id}/reply")
async def plan_reply(request: Request, plan_id: str):
    """SSE: stream the planner's reply to the pending operator turn as ``token``
    fragments (htmx appends them into the bubble), then a ``done`` event carrying the
    refreshed plan panel out-of-band. Same htmx-SSE shape as the run feed."""
    plans = _plans(request)
    try:
        gen = plans.stream_reply(plan_id)
    except PlanNotFound:
        raise HTTPException(status_code=404, detail=f"no such plan: {plan_id}")

    async def stream():
        import asyncio

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        end = object()

        def pump() -> None:
            try:
                for event in gen:
                    asyncio.run_coroutine_threadsafe(queue.put(event), loop).result()
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(end), loop).result()

        fut = loop.run_in_executor(None, pump)
        try:
            while True:
                event = await queue.get()
                if event is end:
                    break
                if isinstance(event, TokenEvent):
                    yield {"event": "token", "data": str(escape(event.text))}
                elif isinstance(event, DoneEvent):
                    panel = templates.get_template("_plan_panel.html").render(
                        **_panel_ctx(request, plan_id), oob=True)
                    yield {"event": "done", "data": panel}
            # Always close the stream (htmx `sse-close="done"` stops reconnection).
            yield {"event": "done", "data": ""}
        finally:
            await fut

    return EventSourceResponse(stream(), ping=_SSE_PING_SECONDS)


@router.post("/ui/plans/{plan_id}/finalize", response_class=HTMLResponse)
async def plan_finalize(request: Request, plan_id: str):
    """The hand-off: lock the plan and hand it to Mission Control — the builder
    translates its units into runs on the launch path. Only succeeds when readiness is
    met; a refusal re-renders the panel with what's blocking. Returns the re-rendered
    panel either way (htmx swaps #plan-panel)."""
    plans = _plans(request)
    message = None
    try:
        plans.finalize(plan_id)
    except PlanNotFound:
        raise HTTPException(status_code=404, detail=f"no such plan: {plan_id}")
    except PlanNotReady as exc:
        message = f"not ready — {exc.reason}"
    builder = getattr(request.app.state, "builder", None)
    if message is None and builder is not None:
        await builder.start_build(plan_id)  # dispatch the units as sim/burn runs
    return templates.TemplateResponse(
        request=request, name="_plan_panel.html",
        context=_panel_ctx(request, plan_id, message=message))
