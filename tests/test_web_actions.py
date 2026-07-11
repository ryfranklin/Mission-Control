"""U4 — write actions wired on the run detail page as htmx POSTs to the existing
endpoints (no new orchestration). Verifies approve/reject/scrub/cancel behaviour,
state-appropriate controls, and one-shot gate resolution under double-submit.

Host-runnable (MemorySaver + the in-memory store from conftest)."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from mission_control import roles
from mission_control.worker import StubWorker
from mission_control.worktree import list_worktrees

STUB_BURN_FILE = "STUB_BURN.txt"


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


def _head(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


def _tracked(repo: Path) -> list[str]:
    return subprocess.run(["git", "-C", str(repo), "ls-files"],
                          check=True, capture_output=True, text=True).stdout.split()


# -- controls reflect the run's current state ------------------------------

def test_controls_show_gate_buttons_at_gate(mem_store, make_service, target_repo):
    client = make_service(mem_store)
    run_id = _launch(client, target_repo, roles.BURN)
    _wait(client, run_id, {"awaiting_gate"})
    html = client.get(f"/ui/runs/{run_id}").text

    for action in ("approve", "reject", "scrub"):
        assert f'hx-post="/ui/runs/{run_id}/{action}"' in html
    assert roles.GO in html and roles.NO_GO in html and roles.SCRUB in html
    assert 'hx-disabled-elt=".act-btn"' in html          # double-submit guard
    assert f'hx-post="/ui/runs/{run_id}/cancel"' not in html   # cancel is not a gate control


def test_controls_show_cancel_while_running(mem_store, make_service, target_repo):
    release = threading.Event()

    class BlockingWorker(StubWorker):
        def investigate(self, task, workdir):
            release.wait(timeout=10)
            return super().investigate(task, workdir)

    client = make_service(mem_store, worker_factory=lambda: BlockingWorker())
    run_id = _launch(client, target_repo, roles.SIM)
    _wait(client, run_id, {"running"})
    try:
        html = client.get(f"/ui/runs/{run_id}").text
        assert f'hx-post="/ui/runs/{run_id}/cancel"' in html      # cancel while running
        assert f'hx-post="/ui/runs/{run_id}/approve"' not in html  # no gate controls
    finally:
        release.set()
    _wait(client, run_id, {"done"})


# -- the actions themselves ------------------------------------------------

def test_approve_from_ui_drives_burn_to_applied(mem_store, make_service, target_repo):
    client = make_service(mem_store)
    run_id = _launch(client, target_repo, roles.BURN)
    _wait(client, run_id, {"awaiting_gate"})

    resp = client.post(f"/ui/runs/{run_id}/approve")
    assert resp.status_code == 200
    assert "resuming" in resp.text and 'hx-post' not in resp.text   # controls removed → no re-submit

    applied = _wait(client, run_id, {"applied"})
    assert applied["cost_usd"] > 0
    assert STUB_BURN_FILE in _tracked(target_repo)
    assert len(list_worktrees(target_repo)) == 1


def test_reject_from_ui_scrubs_at_gate(mem_store, make_service, target_repo):
    client = make_service(mem_store)
    before = _head(target_repo)
    run_id = _launch(client, target_repo, roles.BURN)
    _wait(client, run_id, {"awaiting_gate"})

    assert client.post(f"/ui/runs/{run_id}/reject").status_code == 200
    _wait(client, run_id, {"scrubbed"})
    assert _head(target_repo) == before                  # no-go: target untouched
    assert STUB_BURN_FILE not in _tracked(target_repo)
    assert len(list_worktrees(target_repo)) == 1


def test_scrub_from_ui_scrubs_at_gate(mem_store, make_service, target_repo):
    client = make_service(mem_store)
    run_id = _launch(client, target_repo, roles.BURN)
    _wait(client, run_id, {"awaiting_gate"})

    assert client.post(f"/ui/runs/{run_id}/scrub").status_code == 200
    _wait(client, run_id, {"scrubbed"})
    assert len(list_worktrees(target_repo)) == 1


def test_cancel_from_ui_stops_midrun_no_leak(mem_store, make_service, target_repo):
    release = threading.Event()

    class BlockingWorker(StubWorker):
        def investigate(self, task, workdir):
            release.wait(timeout=10)
            return super().investigate(task, workdir)

    client = make_service(mem_store, worker_factory=lambda: BlockingWorker())
    run_id = _launch(client, target_repo, roles.SIM)
    _wait(client, run_id, {"running"})

    resp = client.post(f"/ui/runs/{run_id}/cancel")
    assert resp.status_code == 200 and "cancel requested" in resp.text
    release.set()

    _wait(client, run_id, {"scrubbed"})
    assert len(list_worktrees(target_repo)) == 1         # clean teardown, no leak


# -- one-shot gate resolution under double-submit --------------------------

def test_double_clicked_gate_resolves_exactly_once(mem_store, make_service, target_repo):
    client = make_service(mem_store)
    run_id = _launch(client, target_repo, roles.BURN)
    _wait(client, run_id, {"awaiting_gate"})

    first = client.post(f"/ui/runs/{run_id}/approve")
    second = client.post(f"/ui/runs/{run_id}/approve")      # the double-click
    assert first.status_code == 200 and second.status_code == 200
    bodies = first.text + " || " + second.text
    assert "resuming" in bodies and "already resolved" in bodies   # 2nd was rejected as a dup

    applied = _wait(client, run_id, {"applied"})
    assert applied["cost_usd"] > 0
    assert _tracked(target_repo).count(STUB_BURN_FILE) == 1  # applied exactly once
    assert len(list_worktrees(target_repo)) == 1

    # and a further attempt on the finished run is still refused (idempotent)
    assert "already resolved" in client.post(f"/ui/runs/{run_id}/approve").text
