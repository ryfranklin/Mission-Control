#!/usr/bin/env python
"""End-to-end Phase 5a flow driven ENTIRELY through the service seam.

Two modes in one file (à la demo_phase4's --child pattern):

* ``--serve``  runs the FastAPI service (durable PostgresSaver + runs ledger + a
  call-counting StubWorker) on 127.0.0.1:$MC_E2E_PORT.
* driver (default) spawns the server as a subprocess and drives the whole flow
  over HTTP: launch a sim (continuous watch → cost tick), then a burn (watch to
  the gate → durable pause → **kill the server** → **restart** → resume via the
  API → apply-once → clean teardown), then GET /runs, /runs/{id}, /metrics.

The call-counting worker writes one line per real ``investigate`` to
``$MC_E2E_COUNTER``; it survives the restart, so "did resume re-pay the completed
worker step?" is answered by counting lines (expect 1).

Emits a single machine-readable line ``E2E_REPORT {json}`` with the data behind
docs/PHASE5A_FINDINGS.md. Designed to run inside a container on the compose
network (service reaches Postgres by name; client uses container loopback), which
sidesteps a broken host→container port-forward without changing any behaviour.
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


# -- shared git helpers ----------------------------------------------------

def _git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *a], check=True,
                          capture_output=True, text=True).stdout.strip()


def _init_repo(path: Path) -> None:
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "e2e@example.com")
    _git(path, "config", "user.name", "E2E")
    (path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")


# -- server mode -----------------------------------------------------------

def serve() -> None:
    import uvicorn

    from mission_control.graph import postgres_checkpointer, build_runs_store
    from mission_control.service import RunManager, create_app
    from mission_control.worker import StubWorker

    counter = os.environ["MC_E2E_COUNTER"]

    class CountingWorker(StubWorker):
        """StubWorker that records each real investigate() call durably (to a file),
        so a resume that re-ran the worker would show up as an extra line."""

        def investigate(self, task, workdir):
            with open(counter, "a", encoding="utf-8") as fh:
                fh.write(task.task_id + "\n")
            return super().investigate(task, workdir)

    checkpointer, pool = postgres_checkpointer(setup=True)
    store = build_runs_store(pool, setup=True)
    manager = RunManager(
        checkpointer=checkpointer,
        runs_store=store,
        worker_factory=lambda: CountingWorker(),
        telemetry_dir=Path("telemetry"),  # relative to cwd (a temp work dir)
    )
    uvicorn.run(create_app(manager), host="127.0.0.1",
                port=int(os.environ.get("MC_E2E_PORT", "8000")), log_level="warning")


# -- driver helpers --------------------------------------------------------

def _wait_ready(client: httpx.Client, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if client.get("/runs", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError("service never became ready")


def _wait_status(client: httpx.Client, run_id: str, wanted: set, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = client.get(f"/runs/{run_id}").json()
        if detail["status"] in wanted:
            return detail
        time.sleep(0.05)
    raise RuntimeError(f"{run_id} never reached {wanted}; last={detail.get('status')}")


def _read_sse(client: httpx.Client, run_id: str, *, stop_on=None, timeout: float = 30.0) -> list[dict]:
    """Collect SSE events. If ``stop_on`` is given, stop right after the first event
    of that type (used to read a burn up to its gate pause, where the feed would
    otherwise block); else read until the stream closes (a terminal run)."""
    events: list[dict] = []
    cur: dict = {}
    started = time.time()
    with client.stream("GET", f"/runs/{run_id}/events", timeout=timeout) as resp:
        for raw in resp.iter_lines():
            line = raw.rstrip("\r")
            if line.startswith(":"):
                continue
            if line == "":
                if cur:
                    events.append({**cur, "t_ms": int((time.time() - started) * 1000)})
                    done = stop_on is not None and cur.get("event") == stop_on
                    cur = {}
                    if done:
                        return events
                continue
            field, _, value = line.partition(":")
            cur[field.strip()] = value.strip()
    return events


def _kinds(events: list[dict]) -> list[str]:
    return [e.get("event", "?") for e in events]


def _cost_ticks(events: list[dict]) -> list[float]:
    ticks = []
    for e in events:
        if e.get("event") == "step_metric":
            ticks.append(json.loads(e["data"])["event"]["cost_usd"])
    return ticks


# -- driver ----------------------------------------------------------------

def driver() -> int:
    work = Path(tempfile.mkdtemp(prefix="e2e-work-"))
    target = Path(tempfile.mkdtemp(prefix="e2e-target-")) / "repo"
    target.mkdir()
    _init_repo(target)
    head0 = _git(target, "rev-parse", "HEAD")
    counter = work / "worker_calls.log"
    counter.write_text("")
    port = os.environ.get("MC_E2E_PORT", "8000")
    base = f"http://127.0.0.1:{port}"

    def spawn() -> subprocess.Popen:
        env = {**os.environ, "MC_E2E_COUNTER": str(counter), "MC_E2E_PORT": port}
        return subprocess.Popen([sys.executable, os.path.abspath(__file__), "--serve"],
                                cwd=str(work), env=env)

    def worktrees() -> int:
        return len(_git(target, "worktree", "list").splitlines())

    client = httpx.Client(base_url=base)
    report: dict = {"target": str(target)}
    proc = spawn()
    try:
        _wait_ready(client)

        # ---- (1) a sim: one continuous watch showing transitions + a cost tick ----
        sim_id = client.post("/runs", json={"target": str(target), "task_type": "sim"}).json()["run_id"]
        sim_events = _read_sse(client, sim_id)  # a sim terminates → the stream closes
        sim_final = _wait_status(client, sim_id, {"done", "failed"})
        report["sim"] = {
            "run_id": sim_id,
            "feed_order": _kinds(sim_events),
            "cost_ticks": _cost_ticks(sim_events),
            "final_status": sim_final["status"],
            "cost_usd": sim_final["cost_usd"],
        }

        # ---- (2) a burn: watch to the durable gate ----
        burn_id = client.post("/runs", json={"target": str(target), "task_type": "burn"}).json()["run_id"]
        pre_gate = _read_sse(client, burn_id, stop_on="gate_waiting")
        at_gate = _wait_status(client, burn_id, {"awaiting_gate"})
        report["burn_pre_gate"] = {
            "run_id": burn_id,
            "feed_order": _kinds(pre_gate),
            "cost_ticks_before_gate": _cost_ticks(pre_gate),
            "registry_status": at_gate["status"],
            "started_at": at_gate["started_at"],
            "ended_at": at_gate["ended_at"],
            "worker_calls": counter.read_text().count("\n"),
            "applied_before_approval": "STUB_BURN.txt" in _git(target, "ls-files"),
            "worktrees_at_gate": worktrees(),
        }

        # ---- (3) DURABILITY: hard-kill the server while paused at the gate ----
        proc.kill()
        proc.wait()
        report["kill"] = {
            "target_head_unchanged": _git(target, "rev-parse", "HEAD") == head0,
            "worktrees_after_kill": worktrees(),  # leaked by the kill (expected)
        }

        # ---- (4) RESTART: fresh process, same Postgres — state must survive ----
        proc = spawn()
        _wait_ready(client)
        survived = client.get(f"/runs/{burn_id}").json()
        report["restart"] = {
            "registry_status_after_restart": survived["status"],  # expect awaiting_gate
            "ended_at": survived["ended_at"],
        }

        # ---- (5) APPROVE over the API → apply-burn once ----
        approve = client.post(f"/runs/{burn_id}/approve").json()
        final = _wait_status(client, burn_id, {"applied", "failed", "scrubbed"})
        post_gate = _read_sse(client, burn_id)  # resume-leg feed (fresh channel, closes at terminal)
        report["approve_resume"] = {
            "approve_response": approve,
            "post_gate_feed_order": _kinds(post_gate),
            "cost_ticks_after_gate": _cost_ticks(post_gate),
            "final_status": final["status"],
            "final_cost_usd": final["cost_usd"],
            "started_at": final["started_at"],
            "ended_at": final["ended_at"],
            "worker_calls_total": counter.read_text().count("\n"),  # 1 ⇒ resume did NOT re-pay
            "applied_after_approval": "STUB_BURN.txt" in _git(target, "ls-files"),
            "apply_commits": _git(target, "log", "--oneline").count("\n") + 1,
            "worktrees_after_teardown": worktrees(),  # 1 ⇒ clean, no leak
            "target_head_changed": _git(target, "rev-parse", "HEAD") != head0,
        }

        # ---- (6) queries reflect final state; metrics summarize across runs ----
        listing = client.get("/runs").json()["runs"]
        report["get_runs"] = {
            "count": len(listing),
            "statuses": sorted({r["status"] for r in listing}),
            "burn_row_count": sum(1 for r in listing if r["run_id"] == burn_id),  # 1 ⇒ no dup
        }
        report["get_run_detail"] = client.get(f"/runs/{burn_id}").json()
        metrics = client.get("/metrics").json()
        report["metrics"] = {
            "telemetry_rollup": metrics["telemetry_rollup"],
            "keys": sorted(metrics.keys()),
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
