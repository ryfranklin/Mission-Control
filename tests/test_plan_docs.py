"""Plan docs: git is the source of truth, Postgres is a rebuildable cache.

Pure round-trip tests need no services. The reconciliation tests use a LOCAL BARE
REPO as the remote (no network) and the Dockerized Postgres (skipped if down)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from mission_control import plan_docs, project_ref, repo_source
from mission_control.aidlc import Phase
from mission_control.graph import build_plans_store, postgres_checkpointer
from mission_control.plan_docs import PlanDoc, RequirementDoc, UnitDoc


# -- pure round-trip (no services) -----------------------------------------

def _sample_plan() -> PlanDoc:
    return PlanDoc(
        mode="brownfield",
        status="ready",
        units=[
            UnitDoc(0, "Workspace Detection", Phase.INCEPTION.value, [], "done"),
            UnitDoc(1, "Reverse Engineering", Phase.INCEPTION.value, [0], "done"),
            UnitDoc(2, "Add the config module", Phase.CONSTRUCTION.value, [1], "pending"),
            UnitDoc(3, "Wire the pipeline", Phase.CONSTRUCTION.value, [2], "pending"),
        ],
        requirements=[
            RequirementDoc("scope", "bounded to the ingestion path; UI is out of scope", "ready"),
            RequirementDoc("acceptance_criteria", "all new files ingested within 5s", "ready"),
        ],
    )


def test_dump_load_roundtrips_multi_unit_plan(tmp_path):
    plan = _sample_plan()
    plan_docs.dump_plan(plan, tmp_path)
    loaded = plan_docs.load_plan(tmp_path)
    assert loaded.mode == plan.mode and loaded.status == plan.status
    assert loaded.units == plan.units                       # seq-ordered; deps + statuses kept
    # Requirements round-trip by content (dump canonicalizes their order by key).
    key = lambda r: r.key  # noqa: E731
    assert sorted(loaded.requirements, key=key) == sorted(plan.requirements, key=key)
    # task_type is derived from phase, not trusted from the file.
    assert [u.task_type for u in loaded.units] == ["sim", "sim", "burn", "burn"]


def test_dump_is_deterministic(tmp_path):
    plan = _sample_plan()
    a = tmp_path / "a"
    b = tmp_path / "b"
    plan_docs.dump_plan(plan, a)
    plan_docs.dump_plan(plan, b)
    assert (a / plan_docs.FLIGHT_PLAN_FILE).read_bytes() == (b / plan_docs.FLIGHT_PLAN_FILE).read_bytes()


def test_flight_plan_and_requirements_files_exist(tmp_path):
    plan_docs.dump_plan(_sample_plan(), tmp_path)
    assert (tmp_path / plan_docs.FLIGHT_PLAN_FILE).is_file()
    assert (tmp_path / plan_docs.REQUIREMENTS_FILE).is_file()


def test_load_missing_plan_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        plan_docs.load_plan(tmp_path)


def test_v2_units_roundtrip_with_stage_slug_and_task_type(tmp_path):
    """A v2 unit lives in a v2 phase (e.g. a design stage in ``construction``) where the
    phase alone can't determine sim vs. burn — so stage_slug + task_type must survive the
    round-trip, and task_type is trusted (not derived) for the non-v1 phase."""
    plan = PlanDoc(
        mode="greenfield",
        status="ready",
        units=[
            # a design stage: v2 phase 'construction' but kind sim → task_type 'sim'
            UnitDoc(0, "Functional Design", "construction", [], "pending",
                    stage_slug="functional-design", stored_task_type="sim"),
            # a code stage: 'construction' + burn
            UnitDoc(1, "Code Generation", "construction", [0], "pending",
                    stage_slug="code-generation", stored_task_type="burn"),
            # an operation stage: recorded but deferred
            UnitDoc(2, "Deployment Execution", "operation", [1], "deferred",
                    stage_slug="deployment-execution", stored_task_type="burn"),
        ],
    )
    plan_docs.dump_plan(plan, tmp_path)
    loaded = plan_docs.load_plan(tmp_path)
    assert loaded.units == plan.units
    assert [u.task_type for u in loaded.units] == ["sim", "burn", "burn"]
    assert [u.stage_slug for u in loaded.units] == [
        "functional-design", "code-generation", "deployment-execution"]
    assert loaded.units[2].status == "deferred"


# -- Postgres + git reconciliation -----------------------------------------

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
    """A bare repo (the stand-in remote) with a ``main`` trunk holding one commit."""
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
    # A pristine remote has no origin of its own — otherwise resolve_target would
    # follow the bare clone's origin back to the seed instead of treating it as THE
    # remote (a real target is a URL, which has no such indirection).
    _git(bare, "remote", "remove", "origin")
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    return bare


def _build_plan(store, ref: str) -> str:
    """A ready, multi-unit plan in the Postgres cache, targeting ``ref``."""
    pid = f"plan-{uuid4().hex}"
    store.open_plan(pid, target=ref, local_path=None, mode="brownfield",
                    methodology="aidlc", cloud_target="aws")
    store.upsert_unit(pid, 0, title="Workspace Detection", phase=Phase.INCEPTION)
    store.upsert_unit(pid, 1, title="Add the config module", phase=Phase.CONSTRUCTION, depends_on=[])
    store.upsert_unit(pid, 2, title="Wire the pipeline", phase=Phase.CONSTRUCTION, depends_on=[1])
    store.upsert_requirement(pid, "scope", value="bounded to ingestion", state="ready")
    store.set_status(pid, "ready")
    return pid


def _unit_tuples(store, pid):
    return [(u.seq, u.title, u.phase, u.task_type, list(u.depends_on), u.status)
            for u in store.list_units(pid)]


def _wipe_plan_cache(store, ref: str) -> None:
    """Simulate a FRESH host (empty Postgres) for this project: drop its cached plan
    rows so load must reconstruct everything from git."""
    with store._pool.connection() as conn:
        for pid_row in conn.execute("SELECT id FROM plans WHERE target = %s", (ref,)).fetchall():
            pid = pid_row[0]
            conn.execute("DELETE FROM plan_units WHERE plan_id = %s", (pid,))
            conn.execute("DELETE FROM plan_requirements WHERE plan_id = %s", (pid,))
            conn.execute("DELETE FROM plan_turns WHERE plan_id = %s", (pid,))
            conn.execute("DELETE FROM plans WHERE id = %s", (pid,))


def test_sync_writes_and_pushes_plan_docs_to_remote(store, remote, tmp_path):
    ref, _ = project_ref.resolve_target(str(remote))       # the canonical stored identity
    pid = _build_plan(store, ref)

    assert plan_docs.sync_to_repo(store, pid, cache_root=tmp_path / "cacheA") is True

    # The plan doc landed on the REMOTE (not just a local cache).
    tree = _git(remote, "ls-tree", "-r", "--name-only", "refs/heads/main").split()
    assert "aidlc-docs/inception/flight-plan.yaml" in tree
    assert "aidlc-docs/inception/requirements.md" in tree

    # A fresh clone of the remote can read the plan back.
    fresh = tmp_path / "verify"
    subprocess.run(["git", "clone", str(remote), str(fresh)], check=True, capture_output=True)
    doc = plan_docs.load_plan(plan_docs.docs_dir(fresh))
    assert [(u.seq, u.title, u.phase) for u in doc.units] == [
        (0, "Workspace Detection", "INCEPTION"),
        (1, "Add the config module", "CONSTRUCTION"),
        (2, "Wire the pipeline", "CONSTRUCTION"),
    ]


def test_fresh_host_reconstructs_plan_from_git(store, remote, tmp_path):
    """Acceptance: a plan built on clone A + pushed is fully reconstructed on clone B
    from an EMPTY Postgres — units, deps, and statuses identical — without re-running
    INCEPTION."""
    ref, _ = project_ref.resolve_target(str(remote))
    pid_a = _build_plan(store, ref)
    plan_docs.sync_to_repo(store, pid_a, cache_root=tmp_path / "cacheA")
    expected = _unit_tuples(store, pid_a)

    _wipe_plan_cache(store, ref)  # clone B: nothing in Postgres for this project

    pid_b = plan_docs.load_from_repo(
        store, ref, cache_root=tmp_path / "cacheB",
        methodology="aidlc", cloud_target="aws",
        plan_id_factory=lambda: f"plan-{uuid4().hex}",
    )
    assert pid_b is not None and pid_b != pid_a          # a fresh local cache id
    assert _unit_tuples(store, pid_b) == expected         # identical units / deps / status
    assert store.get_plan(pid_b).mode == "brownfield"
    assert store.get_plan(pid_b).status == "ready"
    reqs = {r.key: (r.value, r.state) for r in store.list_requirements(pid_b)}
    assert reqs["scope"] == ("bounded to ingestion", "ready")


def test_postgres_git_divergence_resolves_to_git(store, remote, tmp_path):
    """Git is authoritative: a Postgres cache that has drifted from the committed plan
    is overwritten to match git on reconcile."""
    ref, _ = project_ref.resolve_target(str(remote))
    pid = _build_plan(store, ref)
    plan_docs.sync_to_repo(store, pid, cache_root=tmp_path / "cacheA")
    git_version = _unit_tuples(store, pid)

    # Diverge the Postgres cache: change a unit, add an extra one, flip the status.
    store.upsert_unit(pid, 1, title="TAMPERED title", phase=Phase.CONSTRUCTION, depends_on=[])
    store.upsert_unit(pid, 99, title="phantom unit", phase=Phase.CONSTRUCTION, depends_on=[])
    store.set_status(pid, "drafting")
    assert _unit_tuples(store, pid) != git_version

    # Reconcile from git → the divergence resolves to the committed version.
    plan_docs.load_from_repo(
        store, ref, cache_root=tmp_path / "cacheA",
        methodology="aidlc", cloud_target="aws",
        plan_id_factory=lambda: f"plan-{uuid4().hex}",
    )
    assert _unit_tuples(store, pid) == git_version        # tamper + phantom gone
    assert store.get_plan(pid).status == "ready"


def test_engine_syncs_docs_at_each_checkpoint(store, tmp_path):
    """The planner persists the plan to git at each INCEPTION checkpoint: the engine
    invokes ``docs_sync(plan_id)`` when a stage is laid down."""
    from mission_control.service.planner import PlannerEngine, StubPlannerBrain

    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-b", "main")
    _git(empty, "config", "user.email", "t@example.com")
    _git(empty, "config", "user.name", "T")
    (empty / "README.md").write_text("# empty\n")  # docs-only → greenfield (no code)
    _git(empty, "add", "-A")
    _git(empty, "commit", "-m", "init")

    pid = f"plan-{uuid4().hex}"
    store.open_plan(pid, target=None, local_path=str(empty), mode="greenfield",
                    methodology="aidlc", cloud_target="aws")

    recorded: list = []
    engine = PlannerEngine(store, brain=StubPlannerBrain(),
                           docs_sync=lambda p: recorded.append(p))
    list(engine.run_turn(pid, "Greenfield — a new project"))  # completes workspace detection
    assert recorded == [pid]                                   # checkpoint → docs synced
