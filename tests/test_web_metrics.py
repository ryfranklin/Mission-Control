"""U5 — the cost/perf dashboard at GET /ui/metrics (a client of the /metrics logic).

Host-runnable (MemorySaver + the in-memory store from conftest). The registry
rollup is scoped and hand-computable; the DuckDB historical trend is global (repo
JSONL) so it's only checked for presence, not exact numbers."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from mission_control import roles


def _launch(client, target, task_type=roles.SIM) -> str:
    r = client.post("/runs", json={"target": str(target), "task_type": task_type})
    assert r.status_code == 201, r.text
    return r.json()["run_id"]


def _wait(client, run_id, wanted, timeout=20.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = client.get(f"/runs/{run_id}").json()
        if detail["status"] in wanted:
            return detail
        time.sleep(0.02)
    raise AssertionError(f"{run_id} never reached {wanted}; last={detail.get('status')}")


def _drive_burn_to_applied(client, target) -> str:
    run_id = _launch(client, target, roles.BURN)
    _wait(client, run_id, {"awaiting_gate"})
    client.post(f"/ui/runs/{run_id}/approve")
    _wait(client, run_id, {"applied"})
    return run_id


def _repo(base: Path, name: str) -> Path:
    repo = base / name
    repo.mkdir(parents=True)

    def g(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)

    g("init", "-b", "main")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "T")
    (repo / "README.md").write_text("# t\n")
    g("add", "-A")
    g("commit", "-m", "init")
    return repo


# -- global rollup ---------------------------------------------------------

def test_dashboard_renders_global_rollup(mem_store, make_service, target_repo):
    client = make_service(mem_store)
    sims = [_launch(client, target_repo, roles.SIM) for _ in range(2)]
    for rid in sims:
        _wait(client, rid, {"done"})
    _drive_burn_to_applied(client, target_repo)
    unit = client.get(f"/runs/{sims[0]}").json()["cost_usd"]     # per-run reconciled cost

    html = client.get("/ui/metrics").text
    assert "Cost / performance dashboard" in html
    assert "Rollup · global" in html
    # 3 runs, and the sim/burn split + per-target section render
    assert ">3<" in html                                          # total runs
    assert f"badge-{roles.SIM}" in html and f"badge-{roles.BURN}" in html
    assert str(target_repo) in html                               # per-target breakdown
    # hand-computed reconciled total = 3 runs × unit cost
    assert f"${3 * unit:.6f}" in html
    # honest cost wording (5a Q1)
    assert "unreconciled, not free" in html
    # historical trend section present (DuckDB over JSONL, not the SSE feed)
    assert "Historical trend" in html and "JSONL spine" in html


def test_target_filter_narrows_to_that_target(mem_store, make_service, tmp_path):
    client = make_service(mem_store)
    a = _repo(tmp_path, "target-a")
    b = _repo(tmp_path, "target-b")
    for _ in range(2):
        _wait(client, _launch(client, a, roles.SIM), {"done"})
    _wait(client, _launch(client, b, roles.SIM), {"done"})

    # global panel: 3 runs
    glob = client.get("/ui/metrics/panel").text
    assert "<html" not in glob                                    # a fragment, not a full page
    assert ">3<" in glob

    # scoped to A: 2 runs, and B does not appear in the per-target breakdown
    scoped = client.get("/ui/metrics/panel", params={"target": str(a)}).text
    assert "Rollup · scoped" in scoped
    assert ">2<" in scoped
    assert str(a) in scoped and str(b) not in scoped


def test_time_range_filter_narrows_window(mem_store, make_service, target_repo):
    client = make_service(mem_store)
    first = _launch(client, target_repo, roles.SIM)
    _wait(client, first, {"done"})
    time.sleep(0.05)
    second = _launch(client, target_repo, roles.SIM)
    _wait(client, second, {"done"})

    # from = the second run's creation instant → only the second run is in-window
    boundary = client.get(f"/runs/{second}").json()["created_at"]
    panel = client.get("/ui/metrics/panel", params={"from": boundary}).text
    assert "Rollup · scoped" in panel
    assert ">1<" in panel                                         # exactly one run in the window
    unit = client.get(f"/runs/{second}").json()["cost_usd"]
    assert f"${unit:.6f}" in panel                                # its cost only


def test_scoped_numbers_match_registry_subset(mem_store, make_service, tmp_path):
    client = make_service(mem_store)
    a = _repo(tmp_path, "ta")
    ids = [_launch(client, a, roles.SIM) for _ in range(3)]
    for rid in ids:
        _wait(client, rid, {"done"})
    unit = client.get(f"/runs/{ids[0]}").json()["cost_usd"]

    # the dashboard's scoped rollup equals the JSON /metrics runs_summary (same source)
    summary = client.get("/metrics", params={"target": str(a)}).json()["runs_summary"]
    assert summary["runs"] == 3
    assert summary["cost_usd"] == round(3 * unit, 8)
    assert summary["steps"] == 3                                  # one priced step per sim
    assert sum(r["runs"] for r in summary["by_task_type"]) == 3
    assert {r["task_type"] for r in summary["by_task_type"]} == {roles.SIM}
