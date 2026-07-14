"""Portable build resume: the builder honors committed unit status (flight-plan.yaml),
so a build resumed on a different machine dispatches only the not-done units.

The scheduling logic is exercised with a FAKE run manager (deterministic — no graph /
gate / worker), over the REAL Postgres plan store and REAL git sync + reconciliation
(Prompt 3/4) against a local bare-repo remote. The full run/gate/push path itself is
covered by test_plan_build / test_push."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from mission_control import plan_docs, project_ref, roles
from mission_control.aidlc import Phase
from mission_control.graph import build_plans_store, postgres_checkpointer
from mission_control import plans_store as ps
from mission_control.runs_store import STATUS_APPLIED, STATUS_SCRUBBED
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


@pytest.fixture
def remote(tmp_path):
    """A pristine bare-repo remote (no origin of its own — see test_plan_docs)."""
    work = tmp_path / "seed"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    (work / "README.md").write_text("# seed\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "trunk")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(work), str(bare)], check=True, capture_output=True)
    _git(bare, "remote", "remove", "origin")
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    return bare


class FakeRuns:
    """A stand-in RunManager: records launches and lets a test declare a run's terminal
    status. The builder needs only child_runs / get_run / launch."""

    def __init__(self) -> None:
        self.launched: list[int] = []
        self._runs: dict[str, SimpleNamespace] = {}

    def child_runs(self, plan_id):
        return list(self._runs.values())

    def get_run(self, run_id):
        return self._runs.get(run_id)

    def launch(self, *, target, task_type, prompt, plan_id, plan_unit_seq, workstream=None,
               allow_secrets=False, stage_slug=None):
        self.launched.append(plan_unit_seq)
        rid = f"run-{plan_unit_seq}"
        # A freshly launched run is in-flight (not terminal) → dedups re-dispatch.
        self._runs[rid] = SimpleNamespace(plan_unit_seq=plan_unit_seq, status="running")
        return rid

    def finish(self, seq: int, status: str) -> str:
        """Mark a launched unit's run terminal (success/failure) and return its run_id."""
        rid = f"run-{seq}"
        self._runs[rid] = SimpleNamespace(plan_unit_seq=seq, status=status)
        return rid


def _chain_plan(store, *, target=None, local_path=None) -> str:
    """A 4-unit CONSTRUCTION chain 0→1→2→3 (each depends on the previous), all pending."""
    pid = f"plan-{uuid4().hex}"
    store.open_plan(pid, target=target, local_path=local_path, mode="brownfield",
                    methodology="aidlc", cloud_target="aws")
    store.upsert_unit(pid, 0, title="unit 0", phase=Phase.CONSTRUCTION, depends_on=[])
    store.upsert_unit(pid, 1, title="unit 1", phase=Phase.CONSTRUCTION, depends_on=[0])
    store.upsert_unit(pid, 2, title="unit 2", phase=Phase.CONSTRUCTION, depends_on=[1])
    store.upsert_unit(pid, 3, title="unit 3", phase=Phase.CONSTRUCTION, depends_on=[2])
    store.set_status(pid, ps.STATUS_BUILDING)
    return pid


def _local_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    (path / "README.md").write_text("# r\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    return path


def _status_by_seq(store, pid):
    return {u.seq: u.status for u in store.list_units(pid)}


# -- committed status drives dispatch --------------------------------------

def test_advance_dispatches_only_not_done_dependency_satisfied_units(store, tmp_path):
    """With units 0,1 committed done, only unit 2 (dep 1 done) is runnable — not the
    done units, not unit 3 (dep 2 not done)."""
    pid = _chain_plan(store, local_path=str(_local_repo(tmp_path / "repo")))
    store.set_unit_status(pid, 0, ps.UNIT_DONE)
    store.set_unit_status(pid, 1, ps.UNIT_DONE)

    runs = FakeRuns()
    builder = PlanBuilder(store, runs)
    builder._advance(pid)
    assert runs.launched == [2]                      # only the first not-done, ready unit

    # Unit 2 completes → unit 3 becomes runnable; 0,1 never revisited.
    runs.finish(2, STATUS_APPLIED)
    builder.on_run_terminal("run-2", pid)
    assert runs.launched == [2, 3]
    assert store.get_plan(pid).status == ps.STATUS_BUILDING

    runs.finish(3, STATUS_APPLIED)
    builder.on_run_terminal("run-3", pid)
    assert runs.launched == [2, 3]                    # nothing re-dispatched
    assert store.get_plan(pid).status == ps.STATUS_DONE


def test_successful_run_marks_unit_done_and_pushes(store, remote, tmp_path):
    """On a successful terminal run the unit is marked done AND that lands in git
    (flight-plan.yaml on the remote) — the portable progress record. Re-marking is a
    no-op."""
    ref, _ = project_ref.resolve_target(str(remote))
    pid = _chain_plan(store, target=ref)
    cache = tmp_path / "cacheA"
    docs_sync = lambda p: plan_docs.sync_to_repo(store, p, cache_root=cache)  # noqa: E731

    runs = FakeRuns()
    runs.launched.append(0)  # pretend unit 0 was dispatched
    rid = runs.finish(0, STATUS_APPLIED)
    builder = PlanBuilder(store, runs, docs_sync=docs_sync)
    builder.on_run_terminal(rid, pid)

    assert store.list_units(pid)[0].status == ps.UNIT_DONE
    # The done-mark reached the REMOTE.
    fresh = tmp_path / "verify"
    subprocess.run(["git", "clone", str(remote), str(fresh)], check=True, capture_output=True)
    doc = plan_docs.load_plan(plan_docs.docs_dir(fresh))
    assert {u.seq: u.status for u in doc.units}[0] == "done"

    # Idempotent: re-marking the already-done unit makes no new commit on the cache clone.
    local = project_ref.cache_dir_for(ref, root=cache).resolve()
    head_before = _git(local, "rev-parse", "HEAD").strip()
    builder.on_run_terminal(rid, pid)                 # crash-retry of an already-done unit
    assert store.list_units(pid)[0].status == ps.UNIT_DONE
    assert _git(local, "rev-parse", "HEAD").strip() == head_before   # no redundant commit


def test_no_go_run_does_not_advance_status(store, tmp_path):
    """A no-go/failed run never marks its unit done; the unit stays not-done and its
    dependents stay blocked (the plan still completes)."""
    pid = _chain_plan(store, local_path=str(_local_repo(tmp_path / "repo")))
    runs = FakeRuns()
    builder = PlanBuilder(store, runs)
    builder._advance(pid)                             # dispatches unit 0
    assert runs.launched == [0]

    runs.finish(0, STATUS_SCRUBBED)                   # no-go
    builder.on_run_terminal("run-0", pid)
    assert store.list_units(pid)[0].status == ps.UNIT_PENDING   # NOT advanced
    assert runs.launched == [0]                       # dependents never dispatched
    assert store.get_plan(pid).status == ps.STATUS_DONE         # plan resolves (all dead)


# -- the acceptance: move machines, resume from the first not-done unit -----

def test_resume_on_fresh_host_runs_only_not_done_units(store, remote, tmp_path):
    """Clone A runs 2 of 4 units and pushes progress. Clone B (empty Postgres) loads
    from git and dispatches ONLY units 2 and 3 — units 0 and 1 are never re-run."""
    ref, _ = project_ref.resolve_target(str(remote))

    # -- Clone A: build partway (units 0,1 done) and push progress to the remote.
    pid_a = _chain_plan(store, target=ref)
    docs_sync_a = lambda p: plan_docs.sync_to_repo(store, p, cache_root=tmp_path / "cacheA")  # noqa: E731
    runs_a = FakeRuns()
    builder_a = PlanBuilder(store, runs_a, docs_sync=docs_sync_a)
    for seq in (0, 1):
        runs_a.launched.append(seq)
        builder_a.on_run_terminal(runs_a.finish(seq, STATUS_APPLIED), pid_a)
    assert _status_by_seq(store, pid_a) == {0: "done", 1: "done", 2: "pending", 3: "pending"}

    # -- "Move machines": empty Postgres for this project (clone B has nothing cached).
    with store._pool.connection() as conn:
        for row in conn.execute("SELECT id FROM plans WHERE target = %s", (ref,)).fetchall():
            for tbl in ("plan_units", "plan_requirements", "plan_turns"):
                conn.execute(f"DELETE FROM {tbl} WHERE plan_id = %s", (row[0],))
            conn.execute("DELETE FROM plans WHERE id = %s", (row[0],))

    # -- Clone B: reconstruct the plan from git (GIT WINS), then resume the build.
    pid_b = plan_docs.load_from_repo(
        store, str(remote), cache_root=tmp_path / "cacheB",
        methodology="aidlc", cloud_target="aws",
        plan_id_factory=lambda: f"plan-{uuid4().hex}",
    )
    assert pid_b is not None and pid_b != pid_a
    assert _status_by_seq(store, pid_b) == {0: "done", 1: "done", 2: "pending", 3: "pending"}

    runs_b = FakeRuns()
    builder_b = PlanBuilder(store, runs_b,
                            docs_sync=lambda p: plan_docs.sync_to_repo(store, p, cache_root=tmp_path / "cacheB"))
    builder_b._advance(pid_b)                          # resume on the fresh host
    assert runs_b.launched == [2]                      # ONLY unit 2 — never 0 or 1

    # Finish 2 → 3 dispatches; still never 0/1. This continues from the first not-done.
    builder_b.on_run_terminal(runs_b.finish(2, STATUS_APPLIED), pid_b)
    assert runs_b.launched == [2, 3]
    builder_b.on_run_terminal(runs_b.finish(3, STATUS_APPLIED), pid_b)
    assert runs_b.launched == [2, 3]
    assert store.get_plan(pid_b).status == ps.STATUS_DONE


def test_midflight_unit_killed_on_A_reruns_only_that_unit_on_B(store, remote, tmp_path):
    """A unit dispatched but not finished on A (its done-mark never written) is NOT done
    in git; on B it re-runs — just that one unit, never the completed ones."""
    ref, _ = project_ref.resolve_target(str(remote))
    pid_a = _chain_plan(store, target=ref)
    docs_sync_a = lambda p: plan_docs.sync_to_repo(store, p, cache_root=tmp_path / "cacheA")  # noqa: E731
    runs_a = FakeRuns()
    builder_a = PlanBuilder(store, runs_a, docs_sync=docs_sync_a)
    # Unit 0 completes; unit 1 is dispatched but killed mid-flight (never marked done).
    runs_a.launched.append(0)
    builder_a.on_run_terminal(runs_a.finish(0, STATUS_APPLIED), pid_a)
    builder_a._advance(pid_a)                          # dispatches unit 1 (now in-flight)
    assert runs_a.launched == [0, 1]
    assert _status_by_seq(store, pid_a)[1] == "pending"   # 1 never reached done

    # Move machines (empty Postgres).
    with store._pool.connection() as conn:
        for row in conn.execute("SELECT id FROM plans WHERE target = %s", (ref,)).fetchall():
            for tbl in ("plan_units", "plan_requirements", "plan_turns"):
                conn.execute(f"DELETE FROM {tbl} WHERE plan_id = %s", (row[0],))
            conn.execute("DELETE FROM plans WHERE id = %s", (row[0],))

    pid_b = plan_docs.load_from_repo(
        store, str(remote), cache_root=tmp_path / "cacheB",
        methodology="aidlc", cloud_target="aws",
        plan_id_factory=lambda: f"plan-{uuid4().hex}",
    )
    runs_b = FakeRuns()
    builder_b = PlanBuilder(store, runs_b)
    builder_b._advance(pid_b)
    assert runs_b.launched == [1]                      # re-runs ONLY the mid-flight unit
