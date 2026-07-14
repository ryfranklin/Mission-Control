#!/usr/bin/env python
"""End-to-end Phase 8: MC runs AI-DLC **v2** as methodology-in-target.

Drives the whole v2 story IN-PROCESS (a FastAPI TestClient over the real Postgres seam
+ real git bare-remote acquire/gate/push, with the deterministic offline StubWorker):

  - a GREENFIELD plan opens against a throwaway target that has AI-DLC v2 installed
    (.aidlc/), committed to a real (bare) remote;
  - the interactive INCEPTION walk is DERIVED FROM THE VENDORED CATALOG (the kind=="plan"
    stages), not the built-in stage list → a finalized plan;
  - the work-list is the catalog's non-plan stages: sim design stages + burn code stages,
    with the operation-phase stages RECORDED-BUT-DEFERRED (need cloud creds in v1);
  - the build dispatches through at least one `sim` design stage and one `burn` code
    stage behind a REAL go/no-go GO; the produced change + aidlc-state.md markers land in
    git and the burn PUSHES to the remote.

MC owns orchestration, the gate, and state — it never runs v2's hooks/tools.

Emits one machine-readable line ``E2E_REPORT {json}``. v1 security model: localhost /
no auth / single host. Requires the Dockerized Postgres (``docker compose up -d``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient


# -- git helpers -----------------------------------------------------------

def _git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *a], check=True,
                          capture_output=True, text=True).stdout.strip()


def _expect(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _v2_target(tmp: Path) -> tuple[Path, Path]:
    """A throwaway target that carries AI-DLC v2, backed by a real (bare) remote.

    Returns ``(work_clone, bare_remote)`` — the plan opens against ``work_clone`` (a
    working copy with .aidlc/ + an origin), so v2 is detected during PLANNING; the build
    acquires its own cache clone of the same remote and pushes back to it."""
    from mission_control.aidlc_v2 import install as install_v2

    seed = tmp / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "e2e@example.com")
    _git(seed, "config", "user.name", "E2E")
    (seed / "README.md").write_text("# greenfield target (AI-DLC v2 installed)\n")
    install_v2(seed)                       # writes the vendored .aidlc/ methodology tree
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed + aidlc v2")

    bare = tmp / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(seed), str(bare)],
                   check=True, capture_output=True)
    _git(bare, "remote", "remove", "origin")
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")

    work = tmp / "work"
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "e2e@example.com")
    _git(work, "config", "user.name", "E2E")
    return work, bare


# -- in-process app --------------------------------------------------------

def _build_app(tmp: Path):
    """The production service wired with the StubWorker (offline, deterministic) and an
    isolated acquisition cache under ``tmp``. Returns ``(app, plan_store, pool)`` — the
    caller enters ``TestClient(app)`` as a context manager so the app lifespan is active
    and background run drives / the terminal observer actually fire."""
    from mission_control import plan_docs, project_ref
    from mission_control.graph import (
        build_plans_store,
        build_runs_store,
        postgres_checkpointer,
    )
    from mission_control.service import PlanBuilder, PlanManager, RunManager, create_app
    from mission_control.service.planner import PlannerEngine
    from mission_control.worker import StubWorker

    for var in ("MC_PLANNER_METHODOLOGY", "MC_PLANNER_CLOUD", "MC_GREENFIELD_REMOTE"):
        os.environ.pop(var, None)
    cache = tmp / "cache"
    # The run graph's acquire (ensure_local) reads DEFAULT_CACHE_ROOT at call time —
    # point it at the isolated cache so nothing touches ~/.mission-control.
    project_ref.DEFAULT_CACHE_ROOT = cache

    checkpointer, pool = postgres_checkpointer(setup=True)
    runs = build_runs_store(pool, setup=True)
    plan_store = build_plans_store(pool, setup=True)
    manager = RunManager(checkpointer=checkpointer, runs_store=runs,
                         worker_factory=lambda: StubWorker(), telemetry_dir=tmp / "tel")
    docs_sync = lambda pid: plan_docs.sync_to_repo(plan_store, pid, cache_root=cache)  # noqa: E731
    engine = PlannerEngine(plan_store, docs_sync=docs_sync)  # default (offline) brain
    plan_manager = PlanManager(plan_store, engine=engine, docs_sync=docs_sync)
    builder = PlanBuilder(plan_store, manager, docs_sync=docs_sync, cache_root=cache)
    manager.set_run_observer(builder.on_run_terminal)
    return create_app(manager, plan_manager, builder), plan_store, pool


# -- driver ----------------------------------------------------------------

def _turn(client, pid: str, content: str) -> dict:
    r = client.post(f"/plans/{pid}/turns", json={"content": content})
    r.raise_for_status()
    return r.json()


def _walk_to_ready(client, pid: str, max_turns: int = 40) -> int:
    """Advance the catalog-driven INCEPTION walk (one plan stage per turn) to ready."""
    for i in range(max_turns):
        if client.get(f"/plans/{pid}").json()["status"] == "ready":
            return i
        _turn(client, pid, "Proceed with this stage")
    _expect(client.get(f"/plans/{pid}").json()["status"] == "ready",
            "plan never reached ready during the v2 INCEPTION walk")
    return max_turns


def _drive_build(client, pid: str, timeout: float = 120.0) -> dict:
    """GO on every burn as it reaches the gate until the plan reaches done."""
    deadline = time.time() + timeout
    approved: set[str] = set()          # GO each gate exactly once (honest count)
    while time.time() < deadline:
        d = client.get(f"/plans/{pid}").json()
        if d["status"] == "done":
            d["_gates_approved"] = len(approved)
            return d
        for r in d["child_runs"]:
            if r["status"] == "awaiting_gate" and r["run_id"] not in approved:
                client.post(f"/runs/{r['run_id']}/approve")
                approved.add(r["run_id"])
        time.sleep(0.05)
    raise RuntimeError(f"build never finished; last={client.get(f'/plans/{pid}').json()['status']}")


def driver() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="e2e8-"))
    work, bare = _v2_target(tmp)
    app, store, pool = _build_app(tmp)
    report: dict = {}
    with TestClient(app) as client:  # enter the lifespan → background drives + observer run
      try:
        # ---- open a GREENFIELD plan against the v2 target ----
        gp = client.post("/plans", json={"mode": "greenfield", "target": str(work)}).json()
        pid = gp["id"]
        report["greenfield_defaults"] = {"methodology": gp["methodology"],
                                         "cloud_target": gp["cloud_target"]}

        # ---- catalog-driven INCEPTION walk → finalized plan ----
        turns = _walk_to_ready(client, pid)
        ready = client.get(f"/plans/{pid}").json()
        units = ready["units"]
        inception = [u for u in units if u["phase"] == "INCEPTION"]
        construction = [u for u in units if u["phase"] == "construction"]
        operation = [u for u in units if u["phase"] == "operation"]
        report["inception_walk"] = {
            "turns_to_ready": turns,
            "plan_stages_laid_down": len(inception),
            # v2 fidelity: plan stages carry their catalog slug (NOT built-in titles)
            "all_inception_have_stage_slug": all(u["stage_slug"] for u in inception),
            "sample_stage_slugs": [u["stage_slug"] for u in inception[:4]],
            "ready": ready["ready"],
            "status": ready["status"],
        }
        report["worklist"] = {
            "construction_units": len(construction),
            "operation_units": len(operation),
            "sim_stages": sorted(u["stage_slug"] for u in construction if u["task_type"] == "sim"),
            "burn_stages": sorted(u["stage_slug"] for u in construction if u["task_type"] == "burn"),
            # operation stages are RECORDED but DEFERRED (need cloud creds in v1)
            "operation_all_deferred": bool(operation) and all(u["status"] == "deferred" for u in operation),
            "deferred_reason": next((r["value"] for r in ready["requirements"]
                                     if r["key"] == "operation:deferred"), None),
        }
        _expect(report["inception_walk"]["all_inception_have_stage_slug"],
                "INCEPTION units are not catalog-derived (missing stage_slug)")
        _expect(report["worklist"]["operation_all_deferred"],
                "operation stages were not all deferred")
        _expect({"functional-design", "nfr-requirements", "nfr-design"}
                <= set(report["worklist"]["sim_stages"]), "sim design stages missing")
        _expect("code-generation" in report["worklist"]["burn_stages"],
                "burn code stage (code-generation) missing")

        # ---- finalize → build through a real sim + gated burn (GO) ----
        handed = client.post(f"/plans/{pid}/finalize").json()
        _expect(handed["status"] in ("building", "finalized"),
                f"unexpected status after finalize: {handed['status']}")
        built = _drive_build(client, pid)

        by_slug = {u["stage_slug"]: u for u in built["units"]}
        runs_by_seq = {r["unit_seq"]: r for r in built["child_runs"]}
        seq_to_slug = {u["seq"]: u["stage_slug"] for u in built["units"]}
        dispatched_slugs = {seq_to_slug.get(seq) for seq in runs_by_seq}
        fd_run = runs_by_seq.get(by_slug["functional-design"]["seq"])
        cg_run = runs_by_seq.get(by_slug["code-generation"]["seq"])
        report["build"] = {
            "final_status": built["status"],
            "gates_approved": built["_gates_approved"],
            "sim_design_stage_ran": bool(fd_run) and fd_run["task_type"] == "sim"
            and fd_run["status"] == "done",
            "burn_code_stage_applied": bool(cg_run) and cg_run["task_type"] == "burn"
            and cg_run["status"] == "applied",
            "operation_never_dispatched": all(
                by_slug[u["stage_slug"]]["seq"] not in runs_by_seq for u in operation),
            "build_cost_usd": built["build_cost"],
        }
        _expect(built["status"] == "done", "plan did not reach done")
        _expect(report["build"]["sim_design_stage_ran"], "no sim design stage completed")
        _expect(report["build"]["burn_code_stage_applied"], "no burn code stage applied via GO")
        _expect(report["build"]["gates_approved"] >= 1, "no real go/no-go GO happened")
        _expect(report["build"]["operation_never_dispatched"],
                "a deferred operation stage was dispatched")

        # ---- artifacts + aidlc-state.md landed in git AND pushed to the remote ----
        local = Path(client.get(f"/plans/{pid}").json()["local_path"])
        verify = tmp / "verify"
        subprocess.run(["git", "clone", str(bare), str(verify)], check=True, capture_output=True)
        tracked = _git(verify, "ls-files").splitlines()
        state = (verify / "aidlc-docs" / "aidlc-state.md").read_text()
        report["git_landing"] = {
            "aidlc_state_committed": "aidlc-docs/aidlc-state.md" in tracked,
            "flight_plan_committed": "aidlc-docs/inception/flight-plan.yaml" in tracked,
            # the burn's produced change reached the remote (the burn PUSHED)
            "burn_change_pushed": "STUB_BURN.txt" in tracked,
            # completed stages flipped to [x]; a deferred operation stage stays [ ]
            "code_generation_marked_x": "- [x] code-generation" in state,
            "functional_design_marked_x": "- [x] functional-design" in state,
            "operation_stage_not_x": "- [ ] incident-response" in state,
            "worktrees_after_build": len(_git(local, "worktree", "list").splitlines()),
        }
        gl = report["git_landing"]
        _expect(gl["aidlc_state_committed"], "aidlc-state.md not committed to the remote")
        _expect(gl["burn_change_pushed"], "the burn did not push its change to the remote")
        _expect(gl["code_generation_marked_x"] and gl["functional_design_marked_x"],
                "completed stages not marked [x] in aidlc-state.md")
        _expect(gl["operation_stage_not_x"], "a deferred operation stage was marked complete")
        _expect(gl["worktrees_after_build"] == 1, "worktree leak after build")

        report["ok"] = True
      finally:
        pool.close()

    print("E2E_REPORT " + json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(driver())
