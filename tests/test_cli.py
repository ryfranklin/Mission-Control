"""The CLI as a pure client of the service API.

Every command goes over HTTP to the FastAPI service (driven here through an
in-process httpx TestClient — the same surface as a live server). Proves the
seam is the single entry point: a burn can be launched, watched, approved, and
applied entirely via CLI-over-API, and the CLI module never imports the graph.

The task allows a mock or live backend. We MOCK the durability substrate — an
in-process ``MemorySaver`` checkpointer + an in-memory runs registry — so the CLI
path is exercised end-to-end over HTTP with no Docker and no chance of leaking
Postgres connections. The live Postgres path is covered by ``test_service.py`` and
``test_runs_store.py``. The service, graph, worker, and CLI are all real."""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from mission_control import StubWorker, roles
from mission_control import cli
from mission_control.runs_store import RunRow, TERMINAL_STATUSES
from mission_control.service import RunManager, create_app
from mission_control.worktree import list_worktrees

STUB_BURN_FILE = "STUB_BURN.txt"
_RUN_ID = re.compile(r"run-[0-9a-f]+")


class InMemoryRunStore:
    """A mock of :class:`~mission_control.runs_store.RunStore` — same method surface
    and idempotent upsert-by-run_id semantics (once-only started/ended stamps,
    absolute cost), but backed by a dict. Returns real ``RunRow`` objects so the
    service's response models are byte-for-byte what production would emit."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _ensure(self, run_id: str) -> dict:
        return self._rows.setdefault(run_id, {
            "run_id": run_id, "thread_id": run_id, "target": None, "task_type": None,
            "status": "queued", "cost_usd": 0.0, "created_at": self._now(),
            "started_at": None, "ended_at": None, "detail": None,
        })

    def launch(self, run_id, *, task_type=None, target=None):
        with self._lock:
            if run_id not in self._rows:              # ON CONFLICT DO NOTHING
                row = self._ensure(run_id)
                row["task_type"], row["target"] = task_type, target

    def mark_running(self, run_id, *, target=None):
        with self._lock:
            row = self._ensure(run_id)
            row["status"] = "running"
            row["started_at"] = row["started_at"] or self._now()   # COALESCE
            row["target"] = target or row["target"]

    def mark_awaiting_gate(self, run_id):
        with self._lock:
            self._ensure(run_id)["status"] = "awaiting_gate"

    def finish(self, run_id, *, status, cost_usd, detail=None):
        with self._lock:
            row = self._ensure(run_id)
            row["status"], row["cost_usd"] = status, cost_usd
            row["detail"] = detail or row["detail"]
            row["ended_at"] = row["ended_at"] or self._now()       # COALESCE

    def mark_failed(self, run_id, detail):
        with self._lock:
            row = self._ensure(run_id)
            row["status"], row["detail"] = "failed", detail
            row["ended_at"] = row["ended_at"] or self._now()

    def get_run(self, run_id):
        with self._lock:
            row = self._rows.get(run_id)
            return RunRow(**row) if row else None

    def list_runs(self, filter=None, *, limit=100):
        filter = {k: v for k, v in (filter or {}).items() if v is not None}
        with self._lock:
            rows = [r for r in self._rows.values()
                    if all(r.get(k) == v for k, v in filter.items())]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [RunRow(**r) for r in rows[:limit]]


@pytest.fixture
def client(tmp_path):
    """A TestClient over the real service/graph/worker, with a mocked durability
    substrate (MemorySaver + in-memory registry). No Docker required."""
    manager = RunManager(
        checkpointer=MemorySaver(),
        runs_store=InMemoryRunStore(),
        worker_factory=lambda: StubWorker(),
        telemetry_dir=tmp_path / "telemetry",
    )
    with TestClient(create_app(manager)) as c:
        yield c


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


def _wait(client, run_id, wanted, timeout=20.0) -> dict:
    deadline = time.time() + timeout
    detail = {}
    while time.time() < deadline:
        detail = client.get(f"/runs/{run_id}").json()
        if detail["status"] in wanted:
            return detail
        time.sleep(0.05)
    raise AssertionError(f"{run_id} never reached {wanted}; last={detail.get('status')}")


def _head(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


# -- the headline: burn launched → watched → approved → applied, all via CLI --

def test_burn_launch_watch_approve_applied_via_cli(client, capsys, target_repo):
    run_id = _launch(client, capsys, target_repo, roles.BURN)

    # it's running its way to the gate; `runs` lists it (non-terminal)
    _wait(client, run_id, {"awaiting_gate"})
    code, out, _ = _cli(client, capsys, "runs", "--target", str(target_repo))
    assert code == cli.EXIT_OK
    assert run_id in out and "awaiting_gate" in out

    # approve → resumes the existing interrupt → apply-burn
    code, out, _ = _cli(client, capsys, "approve", run_id)
    assert code == cli.EXIT_OK
    assert roles.GO in out
    applied = _wait(client, run_id, {"applied"})
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
    _wait(client, run_id, {"awaiting_gate"})

    code, out, _ = _cli(client, capsys, "reject", run_id)
    assert code == cli.EXIT_OK and roles.NO_GO in out
    _wait(client, run_id, {"scrubbed"})
    assert _head(target_repo) == before             # no-go: target untouched
    assert STUB_BURN_FILE not in _tracked(target_repo)
    assert len(list_worktrees(target_repo)) == 1


def test_scrub_tears_down_no_leak_via_cli(client, capsys, target_repo):
    run_id = _launch(client, capsys, target_repo, roles.BURN)
    _wait(client, run_id, {"awaiting_gate"})

    code, out, _ = _cli(client, capsys, "scrub", run_id)
    assert code == cli.EXIT_OK and roles.SCRUB in out
    _wait(client, run_id, {"scrubbed"})
    assert len(list_worktrees(target_repo)) == 1


# -- exit-code contract ----------------------------------------------------

def test_watch_unknown_run_exits_failure(client, capsys):
    code, _, err = _cli(client, capsys, "watch", "run-does-not-exist")
    assert code == cli.EXIT_FAILURE
    assert "no such run" in err


def test_approve_before_gate_is_failure(client, capsys, target_repo):
    run_id = _launch(client, capsys, target_repo, roles.SIM)
    _wait(client, run_id, TERMINAL_STATUSES)         # a sim finishes without a gate
    code, _, err = _cli(client, capsys, "approve", run_id)
    assert code == cli.EXIT_FAILURE                  # 409 → failure exit


def test_launch_bad_target_is_failure(client, capsys, tmp_path):
    code, _, err = _cli(client, capsys, "launch", str(tmp_path / "nope"), "--type", roles.SIM)
    assert code == cli.EXIT_FAILURE


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
