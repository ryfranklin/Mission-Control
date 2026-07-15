"""Greenfield bootstrap: a new project gets a real remote before unit 1, so identity +
durability exist from birth — no anonymous local-only workspace in the durable path.

A LOCAL BARE REPO stands in as the "created remote" (bootstrap creates it from a fresh
destination path); a second clone with an empty Postgres stands in for a second machine.
The scheduling is driven with a FAKE run manager (deterministic — no gate/worker), over
the REAL Postgres plan store and REAL git bootstrap/acquire/push/reconcile primitives."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from mission_control import plan_docs, project_ref, repo_source
from mission_control.aidlc import Phase
from mission_control.graph import build_plans_store, postgres_checkpointer
from mission_control import plans_store as ps
from mission_control.repo_source import BootstrapError
from mission_control.runs_store import STATUS_APPLIED
from mission_control.service.plan_builder import PlanBuilder


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def store():
    try:
        _cp, pool = postgres_checkpointer(setup=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable (run `docker compose up -d`): {e}")
    plan_store = build_plans_store(pool, setup=True)
    try:
        yield plan_store
    finally:
        pool.close()


class FakeRuns:
    """A stand-in RunManager: records launches, lets a test declare a run terminal."""

    def __init__(self) -> None:
        self.launched: list[int] = []
        self._runs: dict[str, SimpleNamespace] = {}

    def child_runs(self, plan_id):
        return list(self._runs.values())

    def get_run(self, run_id):
        return self._runs.get(run_id)

    def launch(self, *, target, task_type, prompt, plan_id, plan_unit_seq, workstream=None,
               allow_secrets=False, stage_slug=None, subject=None, gated=True):
        self.launched.append(plan_unit_seq)
        self._runs[f"run-{plan_unit_seq}"] = SimpleNamespace(
            plan_unit_seq=plan_unit_seq, status="running")
        return f"run-{plan_unit_seq}"

    def finish(self, seq: int, status: str) -> str:
        self._runs[f"run-{seq}"] = SimpleNamespace(plan_unit_seq=seq, status=status)
        return f"run-{seq}"


def _greenfield_plan(store, *, remote_dest) -> str:
    """A finalized-ish greenfield plan (INCEPTION stages laid down + one CONSTRUCTION
    unit) with a bootstrap destination but NO target yet."""
    pid = f"plan-{uuid4().hex}"
    store.open_plan(pid, target=None, local_path=None, remote_dest=remote_dest,
                    mode="greenfield", methodology="aidlc", cloud_target="aws")
    store.upsert_unit(pid, 0, title="Workspace Detection", phase=Phase.INCEPTION)
    store.upsert_unit(pid, 1, title="Build the core", phase=Phase.CONSTRUCTION, depends_on=[0])
    store.set_status(pid, ps.STATUS_FINALIZED)
    return pid


def _builder(store, runs, cache) -> PlanBuilder:
    docs_sync = lambda p: plan_docs.sync_to_repo(store, p, cache_root=cache)  # noqa: E731
    return PlanBuilder(store, runs, workspaces_dir=cache / "scratch",
                       docs_sync=docs_sync, cache_root=cache)


def test_greenfield_bootstrap_creates_remote_with_seeded_docs_then_builds(store, tmp_path):
    import asyncio

    dest = tmp_path / "created-remote.git"        # fresh destination — bootstrap creates it
    cache = tmp_path / "cacheA"
    pid = _greenfield_plan(store, remote_dest=str(dest))
    runs = FakeRuns()
    builder = _builder(store, runs, cache)

    asyncio.run(builder.start_build(pid))

    # A real remote now exists, carrying the committed plan (aidlc-docs/inception/).
    assert (dest / "HEAD").exists()
    tree = _git(dest, "ls-tree", "-r", "--name-only", "refs/heads/main").split()
    assert "aidlc-docs/inception/flight-plan.yaml" in tree
    # The plan's target is the portable ref (NOT an anonymous local dir); working copy is
    # the acquired cache clone.
    plan = store.get_plan(pid)
    assert plan.target == project_ref.normalize_remote(str(dest))
    assert plan.local_path and Path(plan.local_path).is_dir() and plan.target != plan.local_path

    # The build proceeds on the same path as brownfield: unit 0 (sim) is dispatched.
    assert runs.launched == [0]

    # Complete unit 0 (its done-mark is pushed to the remote via docs_sync), then unit 1.
    builder.on_run_terminal(runs.finish(0, STATUS_APPLIED), pid)
    assert runs.launched == [0, 1]
    doc_after = plan_docs.load_plan(_fresh_clone(dest, tmp_path / "peek"))
    assert {u.seq: u.status for u in doc_after.units}[0] == "done"   # progress on the remote


def _fresh_clone(remote: Path, dest: Path) -> Path:
    subprocess.run(["git", "clone", str(remote), str(dest)], check=True, capture_output=True)
    return plan_docs.docs_dir(dest)


def test_second_machine_sees_bootstrapped_project(store, tmp_path):
    """A second clone with an EMPTY Postgres reconstructs the bootstrapped project and its
    committed plan from git — nothing lives only on the first machine."""
    import asyncio

    dest = tmp_path / "created-remote.git"
    pid_a = _greenfield_plan(store, remote_dest=str(dest))
    runs = FakeRuns()
    asyncio.run(_builder(store, runs, tmp_path / "cacheA").start_build(pid_a))
    ref = store.get_plan(pid_a).target

    # Machine B: empty Postgres for this project.
    with store._pool.connection() as conn:
        for row in conn.execute("SELECT id FROM plans WHERE target = %s", (ref,)).fetchall():
            for tbl in ("plan_units", "plan_requirements", "plan_turns"):
                conn.execute(f"DELETE FROM {tbl} WHERE plan_id = %s", (row[0],))
            conn.execute("DELETE FROM plans WHERE id = %s", (row[0],))

    pid_b = plan_docs.load_from_repo(
        store, ref, cache_root=tmp_path / "cacheB",
        methodology="aidlc", cloud_target="aws",
        plan_id_factory=lambda: f"plan-{uuid4().hex}",
    )
    assert pid_b is not None and pid_b != pid_a
    units = {u.seq: (u.title, u.phase) for u in store.list_units(pid_b)}
    assert units == {0: ("Workspace Detection", "INCEPTION"),
                     1: ("Build the core", "CONSTRUCTION")}


def test_greenfield_without_destination_fails_loudly(store, tmp_path):
    import asyncio

    pid = f"plan-{uuid4().hex}"
    store.open_plan(pid, target=None, local_path=None, mode="greenfield",
                    methodology="aidlc", cloud_target="aws")   # NO remote_dest
    store.upsert_unit(pid, 0, title="Build", phase=Phase.CONSTRUCTION)
    builder = _builder(store, FakeRuns(), tmp_path / "cache")

    with pytest.raises(BootstrapError):
        asyncio.run(builder.start_build(pid))
    # No anonymous local-only fallback: the plan never got a target or a working copy.
    plan = store.get_plan(pid)
    assert not plan.target and not plan.local_path
