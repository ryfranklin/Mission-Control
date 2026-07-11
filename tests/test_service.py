"""The FastAPI service seam over the graph (httpx TestClient).

Covers the full operator flow: launch → list shows it running → SSE carries the
merged live feed (node transitions + priced telemetry + gate-waiting) → approve
resolves the durable gate → the run reaches applied; plus reject/scrub tearing
down cleanly with no worktree leak. Skipped unless the Dockerized Postgres (the
runs ledger) is reachable."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

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
    assert set(body) == {"per_run", "by_task_type", "worker_vs_judge",
                         "quality_trend", "telemetry_rollup"}


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
