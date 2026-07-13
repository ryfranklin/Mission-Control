"""The go/no-go review data: what a burn will apply, surfaced at the gate.

Covers ``worktree.changes`` (full pending state incl. uncommitted worktree work) and
the service seam that feeds the review UI (``GET /runs/{id}/changes``). Skipped unless
the Dockerized Postgres is reachable."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mission_control import StubWorker, roles, worktree
from mission_control.graph import build_runs_store, postgres_checkpointer
from mission_control.service import RunManager, create_app


def _git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *a], check=True,
                          capture_output=True, text=True).stdout


# -- worktree.changes: full pending state (committed + uncommitted) --------

def test_changes_captures_committed_and_uncommitted_worktree_state(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@e.co")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("# repo\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")

    wt = worktree.create_worktree(repo, "burn-xyz")
    # One committed file on the branch, one left UNCOMMITTED in the worktree — a go
    # applies both, so the review must show both.
    (wt.path / "committed.py").write_text("x = 1\n")
    worktree.commit_changes(wt, "add committed.py")
    (wt.path / "uncommitted.txt").write_text("draft\n")

    ch = worktree.changes(repo, wt.branch, wt.path)
    paths = {f["path"] for f in ch["files"]}
    assert paths == {"committed.py", "uncommitted.txt"}
    assert "committed.py" in ch["patch"] and "uncommitted.txt" in ch["patch"]
    assert ch["file_count"] == 2 and ch["message"] == "add committed.py"
    worktree.remove_worktree(wt)


# -- the seam: /runs/{id}/changes at the gate ------------------------------

@pytest.fixture
def client(tmp_path):
    try:
        cp, pool = postgres_checkpointer(setup=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable: {e}")
    store = build_runs_store(pool, setup=True)
    mgr = RunManager(checkpointer=cp, runs_store=store,
                     worker_factory=lambda: StubWorker(), telemetry_dir=tmp_path / "t")
    with TestClient(create_app(mgr)) as c:
        yield c
    pool.close()


def _wait(client, rid, wanted, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get(f"/runs/{rid}").json()["status"]
        if s in wanted:
            return s
        time.sleep(0.05)
    raise AssertionError(f"{rid} never reached {wanted}")


def test_changes_endpoint_shows_the_pending_burn_at_the_gate(client, target_repo):
    rid = client.post("/runs", json={"target": str(target_repo), "task_type": roles.BURN,
                                     "prompt": "make a change"}).json()["run_id"]
    _wait(client, rid, {"awaiting_gate"})

    r = client.get(f"/runs/{rid}/changes")
    assert r.status_code == 200
    ch = r.json()
    # The StubWorker's marker file is the pending change a go would apply (uncommitted).
    assert any(f["path"] == "STUB_BURN.txt" for f in ch["files"])
    assert "STUB_BURN.txt" in ch["patch"]

    # A run NOT at the gate has nothing to review → 404.
    sim = client.post(f"/runs", json={"target": str(target_repo), "task_type": roles.SIM}).json()["run_id"]
    _wait(client, sim, {"done"})
    assert client.get(f"/runs/{sim}/changes").status_code == 404


def test_changes_endpoint_404_for_unknown_run(client):
    assert client.get("/runs/run-nope/changes").status_code == 404
