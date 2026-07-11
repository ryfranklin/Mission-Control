"""The CLI as a pure client of the service API.

Every command goes over HTTP to the FastAPI service (driven here through an
in-process httpx TestClient — the same surface as a live server). Proves the
seam is the single entry point: a burn can be launched, watched, approved, and
applied entirely via CLI-over-API, and the CLI module never imports the graph.

The durability substrate is mocked (MemorySaver + the in-memory runs store from
conftest) so the CLI path runs with no Docker. The live Postgres path is covered
by test_service.py. The service, graph, worker, and CLI are all real."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mission_control import cli, roles
from mission_control.runs_store import TERMINAL_STATUSES
from mission_control.worktree import list_worktrees

STUB_BURN_FILE = "STUB_BURN.txt"
_RUN_ID = re.compile(r"run-[0-9a-f]+")


@pytest.fixture
def client(mem_store, make_service):
    return make_service(mem_store)


# -- helpers: run the CLI over the injected client -------------------------

def _cli(client, capsys, *argv) -> tuple[int, str, str]:
    code = cli.main(list(argv), client=client)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _launch(client, capsys, target, task_type) -> str:
    code, out, _ = _cli(client, capsys, "launch", str(target), "--type", task_type)
    assert code == cli.EXIT_OK, out
    m = _RUN_ID.search(out)
    assert m, f"no run_id in launch output: {out!r}"
    return m.group(0)


def _wait_status(client, run_id, wanted, timeout=20.0) -> dict:
    deadline = time.time() + timeout
    detail = {}
    while time.time() < deadline:
        detail = client.get(f"/runs/{run_id}").json()
        if detail["status"] in wanted:
            return detail
        time.sleep(0.05)
    raise AssertionError(f"{run_id} never reached {wanted}; last status={detail.get('status')}")


def _head(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


# -- the full flow ---------------------------------------------------------

def test_burn_launch_watch_approve_applied_via_cli(client, capsys, target_repo):
    run_id = _launch(client, capsys, target_repo, roles.BURN)

    # it's running its way to the gate; `runs` lists it (non-terminal)
    _wait_status(client, run_id, {"awaiting_gate"})
    code, out, _ = _cli(client, capsys, "runs", "--target", str(target_repo))
    assert code == cli.EXIT_OK
    assert run_id in out and "awaiting_gate" in out

    # approve → resume the existing interrupt → apply-burn
    code, out, _ = _cli(client, capsys, "approve", run_id)
    assert code == cli.EXIT_OK
    assert roles.GO in out
    applied = _wait_status(client, run_id, {"applied"})
    assert applied["cost_usd"] > 0
    assert STUB_BURN_FILE in _tracked(target_repo)

    # watch replays the full merged feed and exits 0 on the applied run
    code, out, _ = _cli(client, capsys, "watch", run_id)
    assert code == cli.EXIT_OK
    for node in ("dispatch", "run_worker", "gate", "apply_burn", "teardown"):
        assert f"→ {node}" in out
    assert "awaiting" in out                       # gate-waiting surfaced
    assert "cost $" in out and "+$" in out          # live cost tick from priced telemetry
    assert "applied" in out

    assert len(list_worktrees(target_repo)) == 1   # clean teardown, no leak


def test_sim_watch_follows_live_to_done(client, capsys, target_repo):
    run_id = _launch(client, capsys, target_repo, roles.SIM)
    code, out, _ = _cli(client, capsys, "watch", run_id)   # bounded: a sim terminates
    assert code == cli.EXIT_OK
    assert "→ run_worker" in out
    assert "cost $" in out                          # priced telemetry ticked
    assert "→ teardown" in out and "done" in out
    assert "awaiting" not in out                    # a sim never gates


def test_reject_scrubs_cleanly_via_cli(client, capsys, target_repo):
    before = _head(target_repo)
    run_id = _launch(client, capsys, target_repo, roles.BURN)
    _wait_status(client, run_id, {"awaiting_gate"})

    code, out, _ = _cli(client, capsys, "reject", run_id)
    assert code == cli.EXIT_OK and roles.NO_GO in out
    _wait_status(client, run_id, {"scrubbed"})
    assert _head(target_repo) == before             # no-go: target untouched
    assert STUB_BURN_FILE not in _tracked(target_repo)
    assert len(list_worktrees(target_repo)) == 1


def test_scrub_tears_down_no_leak_via_cli(client, capsys, target_repo):
    run_id = _launch(client, capsys, target_repo, roles.BURN)
    _wait_status(client, run_id, {"awaiting_gate"})

    code, out, _ = _cli(client, capsys, "scrub", run_id)
    assert code == cli.EXIT_OK and roles.SCRUB in out
    _wait_status(client, run_id, {"scrubbed"})
    assert len(list_worktrees(target_repo)) == 1


# -- queries + errors ------------------------------------------------------

def test_metrics_returns_analytics_shape(client):
    body = client.get("/metrics").json()
    assert {"per_run", "by_task_type", "worker_vs_judge",
            "quality_trend", "telemetry_rollup"} <= set(body)


def test_unknown_run_is_404(client):
    assert client.get("/runs/run-nope").status_code == 404
    assert client.post("/runs/run-nope/approve").status_code == 404


def test_approve_before_gate_is_409(client, target_repo):
    run_id = client.post("/runs", json={"target": str(target_repo),
                                         "task_type": roles.SIM}).json()["run_id"]
    _wait_status(client, run_id, TERMINAL_STATUSES)          # a sim finishes without a gate
    assert client.post(f"/runs/{run_id}/approve").status_code == 409


def test_launch_bad_target_is_400(client, tmp_path):
    r = client.post("/runs", json={"target": str(tmp_path / "nope"), "task_type": roles.SIM})
    assert r.status_code == 400


# -- the seam invariant: the CLI never imports the graph -------------------

def test_cli_is_a_pure_client_never_imports_graph():
    probe = (
        "import mission_control.cli, sys; "
        "assert 'mission_control.graph' not in sys.modules, 'CLI must not import the graph'; "
        "import httpx; print('ok')"
    )
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"


def _tracked(repo: Path) -> list[str]:
    return subprocess.run(["git", "-C", str(repo), "ls-files"],
                          check=True, capture_output=True, text=True).stdout.split()
