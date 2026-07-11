"""The FastAPI service seam over the graph (httpx TestClient).

Covers the full operator flow: launch → list shows it running → SSE carries the
merged live feed (node transitions + priced telemetry + gate-waiting) → approve
resolves the durable gate → the run reaches applied; plus reject/scrub tearing
down cleanly with no worktree leak. Skipped unless the Dockerized Postgres (the
runs ledger) is reachable."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mission_control import StubWorker, roles
from mission_control.graph import build_runs_store, postgres_checkpointer
from mission_control.runs_store import TERMINAL_STATUSES
from mission_control.service import RunManager, create_app
from mission_control.worktree import list_worktrees

STUB_BURN_FILE = "STUB_BURN.txt"


@pytest.fixture
def client(tmp_path):
    """A TestClient over a service wired exactly like production: the durable sync
    PostgresSaver checkpointer + the Postgres runs ledger + a StubWorker. Skips if
    Postgres is down. (Using the real saver keeps the test honest — the sync saver's
    async API is unimplemented, so this exercises the sync-driven feed.)"""
    try:
        checkpointer, pool = postgres_checkpointer(setup=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable (run `docker compose up -d`): {e}")
    store = build_runs_store(pool, setup=True)
    manager = RunManager(
        checkpointer=checkpointer,
        runs_store=store,
        worker_factory=lambda: StubWorker(),
        telemetry_dir=tmp_path / "telemetry",
    )
    with TestClient(create_app(manager)) as c:
        yield c
    pool.close()


# -- helpers ---------------------------------------------------------------

def _wait_status(client, run_id, wanted, timeout=20.0) -> dict:
    deadline = time.time() + timeout
    detail = {}
    while time.time() < deadline:
        detail = client.get(f"/runs/{run_id}").json()
        if detail["status"] in wanted:
            return detail
        time.sleep(0.05)
    raise AssertionError(f"{run_id} never reached {wanted}; last status={detail.get('status')}")


def _read_sse(client, run_id, timeout=20.0) -> list[dict]:
    """Read a run's SSE feed to completion (the stream closes on a terminal run)."""
    events: list[dict] = []
    cur: dict = {}
    with client.stream("GET", f"/runs/{run_id}/events", timeout=timeout) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for raw in r.iter_lines():
            line = raw.rstrip("\r")
            if line.startswith(":"):          # keepalive/ping comment
                continue
            if line == "":                    # blank line terminates one event
                if cur:
                    events.append(cur)
                    cur = {}
                continue
            field, _, value = line.partition(":")
            cur[field.strip()] = value.strip()
    if cur:
        events.append(cur)
    return events


def _head(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


# -- the full flow ---------------------------------------------------------

def test_launch_list_stream_approve_reaches_applied(client, target_repo):
    # launch a burn
    r = client.post("/runs", json={"target": str(target_repo), "task_type": roles.BURN,
                                    "prompt": "make a change"})
    assert r.status_code == 201
    run_id = r.json()["run_id"]
    assert r.json()["target"] == str(target_repo.resolve())
    assert r.json()["task_type"] == roles.BURN

    # list shows it (running its way to the gate)
    listed = client.get("/runs", params={"target": str(target_repo)}).json()["runs"]
    assert any(x["run_id"] == run_id for x in listed)

    # it durably pauses at the go/no-go gate
    paused = _wait_status(client, run_id, {"awaiting_gate"})
    assert paused["started_at"] is not None and paused["ended_at"] is None
    assert STUB_BURN_FILE not in _tracked(target_repo)          # nothing applied yet
    # the status filter surfaces it as a live (non-terminal) run
    assert any(x["run_id"] == run_id
               for x in client.get("/runs", params={"status": "awaiting_gate"}).json()["runs"])

    # approve → resume the existing interrupt → apply-burn → terminal
    ok = client.post(f"/runs/{run_id}/approve")
    assert ok.status_code == 200 and ok.json()["accepted"] is True
    done = _wait_status(client, run_id, {"applied"})
    assert done["ended_at"] is not None
    assert done["cost_usd"] > 0
    assert STUB_BURN_FILE in _tracked(target_repo)              # applied on go

    # the SSE feed carried the merged live view: transitions + telemetry + gate
    events = _read_sse(client, run_id)
    kinds = {e["event"] for e in events}
    assert {"node_transition", "step_metric", "gate_waiting"} <= kinds
    nodes = {json.loads(e["data"])["node"] for e in events if e["event"] == "node_transition"}
    assert {"dispatch", "run_worker", "gate", "apply_burn", "teardown"} <= nodes
    metric = next(e for e in events if e["event"] == "step_metric")
    assert json.loads(metric["data"])["event"]["cost_usd"] > 0

    assert len(list_worktrees(target_repo)) == 1               # clean teardown, no leak


def test_reject_scrubs_cleanly(client, target_repo):
    before = _head(target_repo)
    run_id = client.post("/runs", json={"target": str(target_repo),
                                         "task_type": roles.BURN}).json()["run_id"]
    _wait_status(client, run_id, {"awaiting_gate"})

    assert client.post(f"/runs/{run_id}/reject").status_code == 200
    final = _wait_status(client, run_id, {"scrubbed"})
    assert final["ended_at"] is not None
    assert _head(target_repo) == before                        # no-go: target untouched
    assert STUB_BURN_FILE not in _tracked(target_repo)
    assert len(list_worktrees(target_repo)) == 1               # scrub left no leak


def test_scrub_tears_down_with_no_leak(client, target_repo):
    run_id = client.post("/runs", json={"target": str(target_repo),
                                         "task_type": roles.BURN}).json()["run_id"]
    _wait_status(client, run_id, {"awaiting_gate"})

    assert client.post(f"/runs/{run_id}/scrub").status_code == 200
    final = _wait_status(client, run_id, {"scrubbed"})
    assert final["ended_at"] is not None
    assert len(list_worktrees(target_repo)) == 1


def test_sim_streams_transitions_and_priced_telemetry(client, target_repo):
    run_id = client.post("/runs", json={"target": str(target_repo),
                                         "task_type": roles.SIM}).json()["run_id"]
    events = _read_sse(client, run_id)                          # bounded: a sim terminates
    kinds = {e["event"] for e in events}
    assert "node_transition" in kinds and "step_metric" in kinds
    assert "gate_waiting" not in kinds                          # a sim never gates
    assert _wait_status(client, run_id, {"done"})["cost_usd"] > 0
    assert len(list_worktrees(target_repo)) == 1


# -- queries + errors ------------------------------------------------------

def test_metrics_returns_analytics_shape(client):
    body = client.get("/metrics").json()
    assert {"per_run", "by_task_type", "worker_vs_judge",
            "quality_trend", "telemetry_rollup"} <= set(body)
    assert "runs_summary" in body and body["scope"] is None   # unscoped call


def test_unknown_run_is_404(client):
    assert client.get("/runs/run-nope").status_code == 404
    assert client.post("/runs/run-nope/approve").status_code == 404


def test_approve_before_gate_is_409(client, target_repo):
    run_id = client.post("/runs", json={"target": str(target_repo),
                                         "task_type": roles.SIM}).json()["run_id"]
    _wait_status(client, run_id, TERMINAL_STATUSES)            # a sim finishes without a gate
    assert client.post(f"/runs/{run_id}/approve").status_code == 409


def test_launch_bad_target_is_400(client, tmp_path):
    r = client.post("/runs", json={"target": str(tmp_path / "nope"), "task_type": roles.SIM})
    assert r.status_code == 400


def _tracked(repo: Path) -> list[str]:
    return subprocess.run(["git", "-C", str(repo), "ls-files"],
                          check=True, capture_output=True, text=True).stdout.split()


# ==========================================================================
# Phase 5b seam-completeness, against real Postgres (durable event log, etc.)
# ==========================================================================

@pytest.fixture
def pg(tmp_path):
    """A Postgres-backed store + a factory that builds services over it. Build two
    to simulate a restart (fresh in-memory channels, same durable event log)."""
    try:
        checkpointer, pool = postgres_checkpointer(setup=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable (run `docker compose up -d`): {e}")
    store = build_runs_store(pool, setup=True)
    clients = []

    def make(*, worker_factory=None):
        manager = RunManager(
            checkpointer=checkpointer, runs_store=store,
            worker_factory=worker_factory or (lambda: StubWorker()),
            telemetry_dir=tmp_path / "telemetry",
        )
        c = TestClient(create_app(manager))
        c.__enter__()
        clients.append(c)
        return c

    try:
        yield store, make
    finally:
        for c in clients:
            c.__exit__(None, None, None)
        pool.close()


def _fresh_repo(base: Path) -> Path:
    """A uniquely-named target repo. The Postgres volume persists across in-container
    test runs (and the container's /tmp resets identically), so exact target-scoped
    counts must use a globally-unique target to stay isolated."""
    repo = base / f"repo-{uuid4().hex}"
    repo.mkdir(parents=True)

    def g(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)

    g("init", "-b", "main")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "T")
    (repo / "README.md").write_text("# target\n")
    g("add", "-A")
    g("commit", "-m", "init")
    return repo


def _events(client, run_id, headers=None, timeout=20.0) -> list[dict]:
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


def test_terminal_event_carries_status_and_cost(pg, target_repo):
    _store, make = pg
    client = make()
    run_id = client.post("/runs", json={"target": str(target_repo), "task_type": roles.SIM}).json()["run_id"]
    _wait_status(client, run_id, {"done"})
    last = _events(client, run_id)[-1]
    assert last["event"] == "terminal"
    data = json.loads(last["data"])
    assert data["status"] == "done" and data["cost_usd"] > 0


def test_durable_replay_across_restart_from_postgres(pg, target_repo):
    _store, make = pg
    svc_a = make()
    run_id = svc_a.post("/runs", json={"target": str(target_repo), "task_type": roles.SIM}).json()["run_id"]
    _wait_status(svc_a, run_id, {"done"})
    full = _events(svc_a, run_id)
    seqs = [int(e["id"]) for e in full]
    assert full[-1]["event"] == "terminal" and len(seqs) >= 4

    # "restart": a fresh service (empty in-memory channels) over the SAME Postgres.
    svc_b = make()
    replayed = _events(svc_b, run_id)                       # rebuilt purely from run_events
    assert [e["event"] for e in replayed] == [e["event"] for e in full]
    assert [int(e["id"]) for e in replayed] == seqs

    cut = seqs[1]
    tail = _events(svc_b, run_id, headers={"Last-Event-ID": str(cut)})
    assert [int(e["id"]) for e in tail] == [s for s in seqs if s > cut]


def test_runs_paging_sort_filter_postgres(pg, tmp_path):
    _store, make = pg
    client = make()
    target = _fresh_repo(tmp_path)
    ids = []
    for _ in range(3):
        rid = client.post("/runs", json={"target": str(target), "task_type": roles.SIM}).json()["run_id"]
        _wait_status(client, rid, {"done"})
        ids.append(rid)
        time.sleep(0.02)

    body = client.get("/runs", params={"target": str(target)}).json()
    assert body["total"] == 3
    assert [r["run_id"] for r in body["runs"]] == list(reversed(ids))

    page = client.get("/runs", params={"target": str(target), "limit": 2}).json()
    assert len(page["runs"]) == 2 and page["total"] == 3
    asc = client.get("/runs", params={"target": str(target), "order": "asc"}).json()
    assert [r["run_id"] for r in asc["runs"]] == ids

    boundary = client.get(f"/runs/{ids[1]}").json()["created_at"]
    filtered = client.get("/runs", params={"target": str(target), "created_from": boundary}).json()
    got = {r["run_id"] for r in filtered["runs"]}
    assert filtered["total"] == 2 and ids[0] not in got and {ids[1], ids[2]} <= got


def test_cancel_midrun_no_leak_postgres(pg, tmp_path):
    _store, make = pg
    target = _fresh_repo(tmp_path)
    release = threading.Event()

    class BlockingWorker(StubWorker):
        def investigate(self, task, workdir):
            release.wait(timeout=10)
            return super().investigate(task, workdir)

    client = make(worker_factory=lambda: BlockingWorker())
    run_id = client.post("/runs", json={"target": str(target), "task_type": roles.SIM}).json()["run_id"]
    _wait_status(client, run_id, {"running"})
    assert client.post(f"/runs/{run_id}/cancel").status_code == 200
    release.set()
    final = _wait_status(client, run_id, {"scrubbed", "failed"})
    assert final["status"] == "scrubbed"
    assert len(list_worktrees(target)) == 1
    assert client.get("/runs", params={"target": str(target)}).json()["total"] == 1


def test_targets_endpoint_postgres(pg, tmp_path):
    _store, make = pg
    client = make()
    target = _fresh_repo(tmp_path)
    client.post("/runs", json={"target": str(target), "task_type": roles.SIM})
    assert str(target.resolve()) in client.get("/targets").json()["targets"]


def test_scoped_metrics_postgres(pg, tmp_path):
    _store, make = pg
    client = make()
    target = _fresh_repo(tmp_path)
    on_a = []
    for _ in range(2):
        rid = client.post("/runs", json={"target": str(target), "task_type": roles.SIM}).json()["run_id"]
        _wait_status(client, rid, {"done"})
        on_a.append(rid)
    unit = client.get(f"/runs/{on_a[0]}").json()["cost_usd"]

    scoped = client.get("/metrics", params={"target": str(target)}).json()
    assert scoped["scope"]["target"] == str(target)
    assert scoped["runs_summary"]["runs"] == 2
    assert scoped["runs_summary"]["cost_usd"] == round(2 * unit, 8)

    empty = client.get("/metrics", params={"target": str(tmp_path / f"nowhere-{uuid4().hex}")}).json()
    assert empty["runs_summary"]["runs"] == 0
