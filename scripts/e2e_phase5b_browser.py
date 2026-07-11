#!/usr/bin/env python
"""End-to-end Phase 5b flow in a REAL browser (Playwright + Chromium) against the
service — exercising htmx + the htmx SSE extension for real (JS executes).

Two modes (à la demo_phase4 / e2e_phase5a):

* ``--serve`` runs the FastAPI service (durable PostgresSaver + runs ledger + a
  call-counting StubWorker with an artificial per-step delay so the SSE timeline
  streams at an observable pace).
* driver (default) spawns the server, drives Chromium through the whole flow, and
  confirms the 5a gaps are closed THROUGH THE UI:
    A. fleet → launch burn → run page SSE timeline → go/no-go banner → GO →
       applied → final banner → fleet + /ui/metrics reflect it (no worktree leak).
    B. reload the run page mid-run AND after a service restart → the timeline
       reconstructs from the durable store (not just the resume leg).
    C. launch a mid-node run and CANCEL it → stops with no worktree leak.
    D. page the fleet with many runs.
    E. scope /ui/metrics by target and time-range (via the htmx filter).

Emits one ``E2E_REPORT {json}`` line with the data behind docs/PHASE5B_FINDINGS.md.
Runs entirely inside one container on the compose network (browser + service on
container loopback; service → Postgres by name), sidestepping a broken host
port-forward without changing behaviour.
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


# -- git helpers -----------------------------------------------------------

def _git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *a], check=True,
                          capture_output=True, text=True).stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "e2e@example.com")
    _git(path, "config", "user.name", "E2E")
    (path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    return path


def _worktrees(repo: Path) -> int:
    return len(_git(repo, "worktree", "list").splitlines())


# -- server mode -----------------------------------------------------------

def serve() -> None:
    import uvicorn

    from mission_control.graph import build_runs_store, postgres_checkpointer
    from mission_control.service import RunManager, create_app
    from mission_control.worker import StubWorker

    counter = os.environ["MC_E2E_COUNTER"]
    delay = float(os.environ.get("MC_E2E_DELAY", "0"))

    class DemoWorker(StubWorker):
        def investigate(self, task, workdir):
            with open(counter, "a", encoding="utf-8") as fh:
                fh.write(task.task_id + "\n")
            if delay:
                time.sleep(delay)   # pace the SSE feed so it's observable in the browser
            return super().investigate(task, workdir)

    checkpointer, pool = postgres_checkpointer(setup=True)
    store = build_runs_store(pool, setup=True)
    manager = RunManager(checkpointer=checkpointer, runs_store=store,
                         worker_factory=lambda: DemoWorker(), telemetry_dir=Path("telemetry"))
    uvicorn.run(create_app(manager), host="127.0.0.1",
                port=int(os.environ.get("MC_E2E_PORT", "8000")), log_level="warning")


# -- driver ----------------------------------------------------------------

def driver() -> int:
    from playwright.sync_api import sync_playwright

    work = Path(tempfile.mkdtemp(prefix="e2e5b-work-"))
    counter = work / "worker_calls.log"
    counter.write_text("")
    targets = {name: _init_repo(Path(tempfile.mkdtemp(prefix=f"e2e5b-{name}-")) / "repo")
               for name in ("a", "b", "c")}
    port = os.environ.get("MC_E2E_PORT", "8000")
    base = f"http://127.0.0.1:{port}"

    def spawn() -> subprocess.Popen:
        env = {**os.environ, "MC_E2E_COUNTER": str(counter), "MC_E2E_PORT": port, "MC_E2E_DELAY": "2.0"}
        return subprocess.Popen([sys.executable, os.path.abspath(__file__), "--serve"],
                                cwd=str(work), env=env)

    def wait_http(pw_request) -> None:
        for _ in range(120):
            try:
                if pw_request.get(f"{base}/runs").ok:
                    return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.25)
        raise RuntimeError("service never became ready")

    R: dict = {}
    proc = spawn()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context()
        wait_http(ctx.request)
        page = ctx.new_page()

        def launch_run(target: Path, task_type: str) -> str:
            page.goto(f"{base}/ui")
            page.fill("form.launch input[name=target]", str(target))
            page.select_option("form.launch select[name=task_type]", task_type)
            page.click("form.launch button[type=submit]")
            page.wait_for_url("**/ui/runs/*")
            return page.url.rstrip("/").split("/ui/runs/")[1]

        def timeline() -> list[str]:
            return page.locator("#timeline li").all_inner_texts()

        def nodes_seen(items) -> list[str]:
            want = ["dispatch", "run_worker", "gate", "apply_burn", "teardown"]
            joined = " ".join(items)
            return [n for n in want if n in joined]

        # ---- A. fleet → launch burn → SSE → gate → GO → applied ----
        page.goto(f"{base}/ui")
        masthead = page.locator("header.masthead").inner_text()
        R["A_fleet"] = {"has_director": "Flight Director" in masthead,
                        "has_fleet_table": page.locator("table.fleet").count() == 1}

        t0 = time.time()
        run_id = launch_run(targets["a"], "burn")
        page.wait_for_selector("button[hx-post$='/approve']", timeout=20000)  # go/no-go banner
        t_gate = time.time()
        page.wait_for_selector("#timeline li", timeout=20000)
        pre_gate = timeline()
        R["A_pre_gate"] = {"run_id": run_id, "nodes": nodes_seen(pre_gate),
                           "gate_banner": page.locator(".gate-banner").count() >= 1,
                           "cost_ticks_pre_gate": sum("+$" in i for i in pre_gate),
                           "ms_to_gate": int((t_gate - t0) * 1000)}

        t_go = time.time()
        page.click("button[hx-post$='/approve']")
        page.wait_for_selector(".final-banner", timeout=20000)
        t_final = time.time()
        final = page.locator(".final-banner").inner_text()
        post = timeline()
        R["A_applied"] = {
            "final_banner": " ".join(final.split()),
            "nodes": nodes_seen(post),
            "cost_ticks": sum("+$" in i for i in post),
            "ms_go_to_final": int((t_final - t_go) * 1000),
            "worktrees": _worktrees(targets["a"]),
            "applied_in_git": "STUB_BURN.txt" in _git(targets["a"], "ls-files"),
        }

        page.goto(f"{base}/ui")
        R["A_fleet_reflects"] = page.locator(f"a[href='/ui/runs/{run_id}']").count() >= 1
        metrics_txt = page.goto(f"{base}/ui/metrics?target={targets['a']}").text()
        R["A_metrics_reflects"] = {"scoped": "Rollup · scoped" in metrics_txt,
                                   "target_shown": str(targets["a"]) in metrics_txt}

        # ---- B. reload mid-run + after restart → durable replay ----
        run_b = launch_run(targets["b"], "burn")
        page.wait_for_selector("button[hx-post$='/approve']", timeout=20000)
        page.wait_for_selector("#timeline li", timeout=20000)
        page.reload()                                    # reload MID-RUN (still at gate)
        page.wait_for_selector("#timeline li:has-text('run_worker')", timeout=20000)
        R["B_reload_midrun_nodes"] = nodes_seen(timeline())

        proc.kill(); proc.wait()                         # RESTART the service
        proc = spawn()
        wait_http(ctx.request)
        # completed run A, reopened on the FRESH process → full timeline from the store
        page.goto(f"{base}/ui/runs/{run_id}")
        page.wait_for_selector(".final-banner", timeout=20000)
        R["B_restart_replay_completed_nodes"] = nodes_seen(timeline())
        # the mid-run run B survived the restart; approve now → history + resume leg = FULL
        page.goto(f"{base}/ui/runs/{run_b}")
        page.wait_for_selector("button[hx-post$='/approve']", timeout=20000)
        page.click("button[hx-post$='/approve']")
        page.wait_for_selector(".final-banner", timeout=20000)
        R["B_restart_then_go"] = {"nodes": nodes_seen(timeline()),
                                  "worktrees": _worktrees(targets["b"]),
                                  "final": " ".join(page.locator(".final-banner").inner_text().split())}

        # ---- C. cancel a mid-node run ----
        run_c = launch_run(targets["c"], "sim")          # sim: no gate, blocks in run_worker (delay)
        page.wait_for_selector("button[hx-post$='/cancel']", timeout=20000)
        page.click("button[hx-post$='/cancel']")
        page.wait_for_selector(".final-banner", timeout=20000)
        R["C_cancel"] = {"final": " ".join(page.locator(".final-banner").inner_text().split()),
                         "worktrees": _worktrees(targets["c"]),
                         "run_id": run_c}

        # ---- D. fleet paging (many runs accumulated in Postgres) ----
        page.goto(f"{base}/ui?page=0")
        p0 = page.locator("table.fleet tbody tr").count()
        p0_first = page.locator("table.fleet tbody tr a").first.get_attribute("href")
        page.goto(f"{base}/ui?page=1")
        p1_first = page.locator("table.fleet tbody tr a").first.get_attribute("href")
        R["D_paging"] = {"page0_rows": p0, "page_indicator": page.locator(".pager").inner_text().split("·")[0].strip(),
                         "page0_differs_page1": p0_first != p1_first}

        # ---- E. metrics scoping via the htmx filter ----
        page.goto(f"{base}/ui/metrics")
        R["E_global_rollup"] = "Rollup · global" in page.content()
        page.fill("form.filters input[name=target]", str(targets["a"]))
        page.locator("form.filters input[name=target]").dispatch_event("change")  # htmx re-query
        page.wait_for_selector("#metrics-panel:has-text('scoped')", timeout=20000)
        panel = page.locator("#metrics-panel").inner_text()
        R["E_scoped_by_target"] = {"scoped": "scoped" in panel, "target_shown": str(targets["a"]) in panel}

        R["worker_calls_total"] = counter.read_text().count("\n")
        browser.close()

    if proc.poll() is None:
        proc.kill()
    print("E2E_REPORT " + json.dumps(R))
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
