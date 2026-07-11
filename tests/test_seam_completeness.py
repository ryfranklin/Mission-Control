"""Phase 5b seam-completeness: the five gaps the CLI-as-first-client surfaced.

Driven over HTTP through the FastAPI service (in-process TestClient) with a mocked
durability substrate (MemorySaver + the shared in-memory runs store from conftest).
Sharing ONE store across two services simulates a restart, so durable replay is
exercised without Docker. The live Postgres path is covered by test_service.py."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

from mission_control import roles
from mission_control.worker import StubWorker
from mission_control.worktree import list_worktrees


# -- helpers ---------------------------------------------------------------

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


def _read_sse(client, run_id, headers=None, timeout=20.0) -> list[dict]:
    events, cur = [], {}
    with client.stream("GET", f"/runs/{run_id}/events", headers=headers or {}, timeout=timeout) as r:
        for raw in r.iter_lines():
            line = raw.rstrip("\r")
            if line.startswith(":"):
                continue
            if line == "":
                if cur:
                    events.append(cur)
                    cur = {}
                continue
            field, _, value = line.partition(":")
            cur[field.strip()] = value.strip()
    if cur:
        events.append(cur)
    return events


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)

    def g(*a):
        subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)

    g("init", "-b", "main")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "T")
    (path / "README.md").write_text("# target\n")
    g("add", "-A")
    g("commit", "-m", "init")
    return path


# -- (1) terminal SSE event ------------------------------------------------

def test_terminal_event_fires_on_each_terminal_path(mem_store, make_service, target_repo):
    client = make_service(mem_store)

    # sim → done
    sim = _launch(client, target_repo, roles.SIM)
    _wait(client, sim, {"done"})
    last = _read_sse(client, sim)[-1]
    assert last["event"] == "terminal"
    data = json.loads(last["data"])
    assert data["status"] == "done" and data["cost_usd"] > 0

    # burn → applied
    burn = _launch(client, target_repo, roles.BURN)
    _wait(client, burn, {"awaiting_gate"})
    client.post(f"/runs/{burn}/approve")
    _wait(client, burn, {"applied"})
    term = json.loads(_read_sse(client, burn)[-1]["data"])
    assert term == {"status": "applied", "cost_usd": term["cost_usd"]} and term["cost_usd"] > 0

    # burn → scrubbed (reject)
    burn2 = _launch(client, target_repo, roles.BURN)
    _wait(client, burn2, {"awaiting_gate"})
    client.post(f"/runs/{burn2}/reject")
    _wait(client, burn2, {"scrubbed"})
    assert json.loads(_read_sse(client, burn2)[-1]["data"])["status"] == "scrubbed"


# -- (2) durable replay honoring Last-Event-ID -----------------------------

def test_last_event_id_replay_reconstructs_full_timeline_after_restart(mem_store, make_service, target_repo):
    svc_a = make_service(mem_store)
    run_id = _launch(svc_a, target_repo, roles.SIM)
    _wait(svc_a, run_id, {"done"})
    full = _read_sse(svc_a, run_id)
    seqs = [int(e["id"]) for e in full]
    assert full[-1]["event"] == "terminal"
    assert seqs == sorted(seqs) and len(seqs) >= 4   # dispatch, run_worker, gate, step_metric, terminal

    # "restart": a fresh service (empty in-memory channels) over the SAME store.
    svc_b = make_service(mem_store)
    replayed = _read_sse(svc_b, run_id)              # no Last-Event-ID → full durable timeline
    assert [e["event"] for e in replayed] == [e["event"] for e in full]
    assert [int(e["id"]) for e in replayed] == seqs  # not just the resume leg — the whole thing

    # Last-Event-ID trims to the tail past that seq.
    cut = seqs[1]
    tail = _read_sse(svc_b, run_id, headers={"Last-Event-ID": str(cut)})
    assert [int(e["id"]) for e in tail] == [s for s in seqs if s > cut]


# -- (3) GET /runs paging + sort + time filter -----------------------------

def test_runs_paging_sort_and_time_filter(mem_store, make_service, target_repo):
    client = make_service(mem_store)
    ids = []
    for _ in range(3):
        rid = _launch(client, target_repo, roles.SIM)
        _wait(client, rid, {"done"})
        ids.append(rid)
        time.sleep(0.02)  # distinct created_at for deterministic ordering

    body = client.get("/runs").json()
    assert body["total"] == 3
    assert [r["run_id"] for r in body["runs"]] == list(reversed(ids))   # newest-first default

    page1 = client.get("/runs", params={"limit": 2, "offset": 0}).json()
    page2 = client.get("/runs", params={"limit": 2, "offset": 2}).json()
    assert len(page1["runs"]) == 2 and page1["total"] == 3 and page1["limit"] == 2
    assert len(page2["runs"]) == 1                                       # last page

    asc = client.get("/runs", params={"order": "asc"}).json()
    assert [r["run_id"] for r in asc["runs"]] == ids                     # oldest-first

    assert client.get("/runs", params={"status": "done"}).json()["total"] == 3
    assert client.get("/runs", params={"status": "failed"}).json()["total"] == 0

    # created_from = second run's timestamp → excludes the first (half-open [from, ∞)).
    boundary = client.get(f"/runs/{ids[1]}").json()["created_at"]
    filtered = client.get("/runs", params={"created_from": boundary}).json()
    got = {r["run_id"] for r in filtered["runs"]}
    assert filtered["total"] == 2 and ids[0] not in got and {ids[1], ids[2]} <= got


# -- (4) real mid-node cancel ----------------------------------------------

def test_cancel_stops_midrun_cleanly(mem_store, make_service, target_repo):
    release = threading.Event()

    class BlockingWorker(StubWorker):
        def investigate(self, task, workdir):
            release.wait(timeout=10)   # hold the run inside run_worker
            return super().investigate(task, workdir)

    client = make_service(mem_store, worker_factory=lambda: BlockingWorker())
    run_id = _launch(client, target_repo, roles.SIM)
    _wait(client, run_id, {"running"})                 # dispatched; blocked in run_worker

    resp = client.post(f"/runs/{run_id}/cancel")
    assert resp.status_code == 200 and resp.json()["accepted"] is True
    release.set()                                       # let the held node finish → cancel takes effect

    final = _wait(client, run_id, {"scrubbed", "failed"})
    assert final["status"] == "scrubbed"
    assert "cancel" in (final["detail"] or "").lower()
    assert len(list_worktrees(target_repo)) == 1        # torn down, no leak
    assert client.get("/runs", params={"target": str(target_repo)}).json()["total"] == 1  # one row


def test_cancel_at_gate_is_rejected(mem_store, make_service, target_repo):
    client = make_service(mem_store)
    run_id = _launch(client, target_repo, roles.BURN)
    _wait(client, run_id, {"awaiting_gate"})
    # cancel is for in-flight runs; a gated run must use reject/scrub.
    assert client.post(f"/runs/{run_id}/cancel").status_code == 409


# -- (5) scoped /metrics ---------------------------------------------------

def test_scoped_metrics_matches_registry_subset(mem_store, make_service, target_repo, tmp_path):
    client = make_service(mem_store)

    on_a = [_launch(client, target_repo, roles.SIM) for _ in range(2)]
    for rid in on_a:
        _wait(client, rid, {"done"})
    unit = client.get(f"/runs/{on_a[0]}").json()["cost_usd"]

    target_b = _init_repo(tmp_path / "target-b")
    rid_b = _launch(client, target_b, roles.SIM)
    _wait(client, rid_b, {"done"})

    scoped_a = client.get("/metrics", params={"target": str(target_repo)}).json()
    assert scoped_a["scope"]["target"] == str(target_repo)
    assert scoped_a["runs_summary"]["runs"] == 2
    assert scoped_a["runs_summary"]["cost_usd"] == round(2 * unit, 8)

    scoped_b = client.get("/metrics", params={"target": str(target_b)}).json()
    assert scoped_b["runs_summary"]["runs"] == 1

    unscoped = client.get("/metrics").json()
    assert unscoped["scope"] is None
    assert unscoped["runs_summary"]["runs"] == 3                      # whole registry
