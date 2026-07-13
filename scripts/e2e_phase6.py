#!/usr/bin/env python
"""End-to-end Phase 6 flow driven ENTIRELY through the service seam.

Two modes in one file (à la e2e_phase5a):

* ``--serve`` runs the FULL FastAPI service (durable PostgresSaver + runs ledger +
  PLAN store + planner engine + PlanBuilder) on 127.0.0.1:$MC_E2E_PORT, StubWorker.
* driver (default) spawns the server as a subprocess and drives the whole Phase 6
  story over HTTP:

  - GREENFIELD: open a plan on the AWS/.aidlc defaults → interactive INCEPTION Q&A
    until the plan is in place → hand to Mission Control → it scaffolds a workspace
    and builds via sim/burn behind go/no-go → applied cleanly, no worktree leak.
  - OVERRIDE: a session can override methodology/cloud, and it sticks.
  - BROWNFIELD: point a plan at an existing repo → workspace detection flags
    brownfield → a reverse-engineering sim runs (recorded in the runs registry) →
    requirements loop until the readiness gate passes → hand off → build.
  - DURABILITY: mid-session (interactive planning), HARD-KILL the server, RESTART it
    against the SAME Postgres, and confirm the plan + transcript + requirements +
    units are intact and the session resumes (the next turn continues the walk).

Emits one machine-readable line ``E2E_REPORT {json}`` with the data behind
docs/PHASE6_FINDINGS.md. v1 security model: localhost / no auth.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


# -- git helpers -----------------------------------------------------------

def _git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *a], check=True,
                          capture_output=True, text=True).stdout.strip()


def _init_repo(path: Path, *, with_code: bool) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "e2e@example.com")
    _git(path, "config", "user.name", "E2E")
    (path / "README.md").write_text("# target\n")
    if with_code:
        (path / "app.py").write_text("def main():\n    return 42\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")


# -- server mode -----------------------------------------------------------

def serve() -> None:
    import uvicorn

    from mission_control.service import build_default_manager, create_app

    # A shared workspaces dir under cwd so greenfield scaffolds survive a restart.
    os.environ.setdefault("MC_PLAN_WORKSPACES", str(Path.cwd() / "plan-workspaces"))
    manager, plan_manager, builder, pool = build_default_manager()
    app = create_app(manager, plan_manager, builder)
    try:
        uvicorn.run(app, host="127.0.0.1",
                    port=int(os.environ.get("MC_E2E_PORT", "8000")), log_level="warning")
    finally:
        pool.close()


# -- driver helpers --------------------------------------------------------

def _wait_ready(client: httpx.Client, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if client.get("/plans", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError("service never became ready")


def _turn(client, pid, content) -> dict:
    r = client.post(f"/plans/{pid}/turns", json={"content": content})
    r.raise_for_status()
    return r.json()


def _drive_build(client, pid, timeout=90.0) -> dict:
    """Approve every burn as it reaches the gate until the plan reaches done."""
    deadline = time.time() + timeout
    gated = 0
    while time.time() < deadline:
        d = client.get(f"/plans/{pid}").json()
        if d["status"] == "done":
            d["_gates_approved"] = gated
            return d
        for r in d["child_runs"]:
            if r["status"] == "awaiting_gate":
                client.post(f"/runs/{r['run_id']}/approve")
                gated += 1
        time.sleep(0.05)
    raise RuntimeError(f"build never finished; last={client.get(f'/plans/{pid}').json()['status']}")


# -- driver ----------------------------------------------------------------

def driver() -> int:
    work = Path(tempfile.mkdtemp(prefix="e2e6-work-"))
    greenfield_target = None  # scaffolded by the service
    brown = Path(tempfile.mkdtemp(prefix="e2e6-brown-")) / "repo"
    _init_repo(brown, with_code=True)
    port = os.environ.get("MC_E2E_PORT", "8000")
    base = f"http://127.0.0.1:{port}"

    def spawn() -> subprocess.Popen:
        env = {**os.environ, "MC_E2E_PORT": port}
        return subprocess.Popen([sys.executable, os.path.abspath(__file__), "--serve"],
                                cwd=str(work), env=env)

    client = httpx.Client(base_url=base)
    report: dict = {}
    proc = spawn()
    try:
        _wait_ready(client)

        # ============ GREENFIELD: defaults → Q&A → hand off → build ============
        gp = client.post("/plans", json={"mode": "greenfield"}).json()
        pid = gp["id"]
        report["greenfield_defaults"] = {"methodology": gp["methodology"], "cloud_target": gp["cloud_target"]}

        walk = []
        for msg in ("A brand-new CLI tool",
                    "Parse logs and compute metrics; performance matters",
                    "Thin end-to-end slice first",
                    "Yes, generate the units"):
            reply = _turn(client, pid, msg)["reply"]
            walk.append({"op": msg, "reply_has_questions": "[Answer]:" in reply["content"]})
        ready = client.get(f"/plans/{pid}").json()
        report["greenfield_plan"] = {
            "status_before_handoff": ready["status"],
            "ready": ready["ready"],
            "turns": len(ready["turns"]),
            "inception_units": [u["title"] for u in ready["units"] if u["phase"] == "INCEPTION"],
            "construction_units": [u["title"] for u in ready["units"] if u["phase"] == "CONSTRUCTION"],
            "unit_task_types": sorted({u["task_type"] for u in ready["units"]}),
            "walk": walk,
        }

        handed = client.post(f"/plans/{pid}/finalize").json()
        greenfield_target = handed["target"]
        built = _drive_build(client, pid)
        gt = Path(greenfield_target)
        report["greenfield_build"] = {
            "scaffolded_target": greenfield_target,
            "status_after_handoff": handed["status"],
            "child_run_types": [r["task_type"] for r in sorted(built["child_runs"], key=lambda r: r["unit_seq"])],
            "child_run_statuses": [r["status"] for r in sorted(built["child_runs"], key=lambda r: r["unit_seq"])],
            "gates_approved": built["_gates_approved"],
            "final_status": built["status"],
            "build_cost_usd": built["build_cost"],
            "applied_marker_present": "STUB_BURN.txt" in _git(gt, "ls-files"),
            "worktrees_after_build": len(_git(gt, "worktree", "list").splitlines()),  # 1 ⇒ no leak
        }

        # ============ OVERRIDE: methodology/cloud stick per session ============
        ov = client.post("/plans", json={"mode": "greenfield", "methodology": "custom-mm",
                                          "cloud_target": "gcp"}).json()
        rt = client.get(f"/plans/{ov['id']}").json()
        report["override"] = {"methodology": rt["methodology"], "cloud_target": rt["cloud_target"]}

        # ============ BROWNFIELD: detection → RE sim → gate → build ============
        bp = client.post("/plans", json={"mode": "greenfield", "target": str(brown)}).json()
        bpid = bp["id"]
        report["brownfield_open_mode"] = bp["mode"]  # opened greenfield; detection will flip it
        _turn(client, bpid, "Work on this existing repository")  # workspace detection + RE sim
        after_detect = client.get(f"/plans/{bpid}").json()
        re_reqs = [r for r in after_detect["requirements"] if r["key"].startswith("reverse_engineering")]
        report["brownfield_detection"] = {
            "mode_after_detection": after_detect["mode"],
            "reverse_engineering_reqs": [r["key"] for r in re_reqs],
            "re_stage_unit": any(u["title"] == "Reverse Engineering" for u in after_detect["units"]),
            "readiness_before_loop": {c["key"]: c["met"] for c in after_detect["readiness"]},
        }
        # The RE sim is a real recorded run.
        re_run = next((r["value"] for r in after_detect["requirements"]
                       if r["key"] == "reverse_engineering:run"), None)
        report["brownfield_re_sim"] = client.get(f"/runs/{re_run}").json() if re_run else None

        # Requirements-readiness loop until the gate is green.
        for msg in ("Add a --json flag; nothing else in scope",
                    "It touches the CLI entrypoint and the formatter",
                    "Done when --json emits valid JSON and tests pass",
                    "Yes, generate the units"):
            _turn(client, bpid, msg)
        bready = client.get(f"/plans/{bpid}").json()
        report["brownfield_ready"] = {
            "status": bready["status"], "ready": bready["ready"],
            "readiness": {c["key"]: c["met"] for c in bready["readiness"]},
        }
        bhand = client.post(f"/plans/{bpid}/finalize").json()
        bbuilt = _drive_build(client, bpid)
        report["brownfield_build"] = {
            "status_after_handoff": bhand["status"],
            "child_run_types": [r["task_type"] for r in sorted(bbuilt["child_runs"], key=lambda r: r["unit_seq"])],
            "final_status": bbuilt["status"],
            "build_cost_usd": bbuilt["build_cost"],
            "applied_marker_present": "STUB_BURN.txt" in _git(brown, "ls-files"),
            "worktrees_after_build": len(_git(brown, "worktree", "list").splitlines()),
        }

        # ============ DURABILITY: kill mid-session, restart, resume ============
        dp = client.post("/plans", json={"mode": "greenfield", "target": str(brown)}).json()
        dpid = dp["id"]
        _turn(client, dpid, "Work on this repo")              # brownfield + RE sim
        _turn(client, dpid, "Scope: add a --verbose flag")    # scope captured
        before = client.get(f"/plans/{dpid}").json()
        report["durability_before_kill"] = {
            "status": before["status"], "mode": before["mode"],
            "turns": len(before["turns"]), "requirements": len(before["requirements"]),
            "units": len(before["units"]),
        }

        proc.kill(); proc.wait()                              # HARD kill mid-session
        proc = spawn()
        _wait_ready(client)

        after = client.get(f"/plans/{dpid}").json()           # SAME Postgres — must survive
        report["durability_after_restart"] = {
            "status": after["status"], "mode": after["mode"],
            "turns": len(after["turns"]), "requirements": len(after["requirements"]),
            "units": len(after["units"]),
            "transcript_intact": [t["content"] for t in after["turns"]] ==
                                 [t["content"] for t in before["turns"]],
        }
        # The session resumes: another turn continues the walk from where it left off.
        _turn(client, dpid, "It touches the arg parser")
        _turn(client, dpid, "Done when --verbose prints debug lines")
        _turn(client, dpid, "Yes, generate the units")
        resumed = client.get(f"/plans/{dpid}").json()
        report["durability_resumed"] = {
            "status": resumed["status"], "ready": resumed["ready"],
            "turns": len(resumed["turns"]),
        }
    finally:
        if proc.poll() is None:
            proc.kill()
        client.close()

    print("E2E_REPORT " + json.dumps(report))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    args = ap.parse_args()
    if args.serve:
        serve()
        return 0
    return driver()


if __name__ == "__main__":
    sys.exit(main())
