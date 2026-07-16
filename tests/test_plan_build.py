"""Handing a finalized Flight Plan to Mission Control: on finalize (readiness met), a
plan's units are dispatched as runs on the EXISTING launch path — INCEPTION units as
sims, CONSTRUCTION units as burns behind the go/no-go gate — respecting depends_on
ordering, with the plan owning its child runs and rolling up their status + cost.

Skipped unless the Dockerized Postgres is reachable."""

from __future__ import annotations

import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mission_control import StubWorker, aidlc, roles
from mission_control.aidlc import Phase
from mission_control.graph import (
    build_plans_store,
    build_runs_store,
    postgres_checkpointer,
)
from mission_control import plans_store as ps
from mission_control.runs_store import STATUS_APPLIED, STATUS_DONE, STATUS_SCRUBBED
from mission_control.service import PlanBuilder, PlanManager, RunManager, create_app

STUB_BURN_FILE = "STUB_BURN.txt"


@pytest.fixture
def build_env(tmp_path, monkeypatch):
    """A TestClient whose finalize hands off to a wired PlanBuilder (units → runs).
    Greenfield builds scaffold their workspace under a temp dir."""
    monkeypatch.delenv("MC_PLANNER_METHODOLOGY", raising=False)
    monkeypatch.delenv("MC_PLANNER_CLOUD", raising=False)
    monkeypatch.delenv("MC_GREENFIELD_REMOTE", raising=False)
    # Isolate the acquisition cache to a temp dir: the run graph acquires via
    # ensure_local() using project_ref.DEFAULT_CACHE_ROOT (read at call time), so a
    # bootstrapped greenfield remote is cloned here, never into the real ~/.mission-control.
    from mission_control import project_ref
    monkeypatch.setattr(project_ref, "DEFAULT_CACHE_ROOT", tmp_path / "cache")
    try:
        checkpointer, pool = postgres_checkpointer(setup=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable (run `docker compose up -d`): {e}")
    runs = build_runs_store(pool, setup=True)
    plan_store = build_plans_store(pool, setup=True)
    manager = RunManager(
        checkpointer=checkpointer, runs_store=runs,
        worker_factory=lambda: StubWorker(), telemetry_dir=tmp_path / "telemetry",
    )
    builder = PlanBuilder(plan_store, manager, workspaces_dir=tmp_path / "workspaces",
                          cache_root=tmp_path / "cache")
    manager.set_run_observer(builder.on_run_terminal)
    plan_manager = PlanManager(plan_store)
    with TestClient(create_app(manager, plan_manager, builder)) as c:
        yield c, plan_store
    pool.close()


# -- helpers ---------------------------------------------------------------

def _ready_brownfield_plan(client, store, target, units) -> str:
    """Open a brownfield plan against ``target``, satisfy the readiness gate, and lay
    down ``units`` (list of (seq, title, Phase, depends_on)). Returns the plan id."""
    pid = client.post("/plans", json={"target": str(target), "mode": "brownfield"}).json()["id"]
    for key in (aidlc.REQ_KEY_SCOPE, aidlc.REQ_KEY_COMPONENTS, aidlc.REQ_KEY_ACCEPTANCE):
        store.upsert_requirement(pid, key, value="ok", state=aidlc.REQ_READY)
    for seq, title, phase, deps in units:
        store.upsert_unit(pid, seq, title=title, phase=phase, depends_on=deps)
    return pid


def _wait(client, pid, pred, timeout=40.0):
    deadline = time.time() + timeout
    detail = {}
    while time.time() < deadline:
        detail = client.get(f"/plans/{pid}").json()
        if pred(detail):
            return detail
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting on plan {pid}; last={detail}")


def _by_seq(detail) -> dict:
    return {r["unit_seq"]: r for r in detail["child_runs"]}


# -- dependency-ordered dispatch + gate + rollup ---------------------------

def test_finalize_dispatches_sim_then_burns_in_dependency_order(build_env, target_repo):
    client, store = build_env
    # seq0 INCEPTION (sim) → seq1 CONSTRUCTION (burn, needs 0) → seq2 (burn, needs 1).
    pid = _ready_brownfield_plan(client, store, target_repo, [
        (0, "Validate the area", Phase.INCEPTION, []),
        (1, "Make change A", Phase.CONSTRUCTION, [0]),
        (2, "Make change B", Phase.CONSTRUCTION, [1]),
    ])

    fin = client.post(f"/plans/{pid}/finalize")
    assert fin.status_code == 200
    assert fin.json()["status"] == ps.STATUS_BUILDING          # handed off → building

    # The sim runs first; only once it's done does the first burn dispatch — and it
    # stops at the gate. The second burn is still blocked (its dep hasn't applied).
    d = _wait(client, pid, lambda x: 1 in _by_seq(x)
              and _by_seq(x)[1]["status"] == "awaiting_gate")
    runs = _by_seq(d)
    assert runs[0]["task_type"] == roles.SIM and runs[0]["status"] == "done"
    assert runs[1]["task_type"] == roles.BURN
    assert 2 not in runs                                        # dep-blocked → not dispatched

    # Nothing applied without a go: the burn paused at the gate has changed nothing.
    assert not (target_repo / STUB_BURN_FILE).exists()

    # Go on the first burn → it applies, and the second burn now dispatches to its gate.
    assert client.post(f"/runs/{runs[1]['run_id']}/approve").status_code == 200
    d = _wait(client, pid, lambda x: 2 in _by_seq(x)
              and _by_seq(x)[2]["status"] == "awaiting_gate")
    assert _by_seq(d)[1]["status"] == "applied"

    # Go on the second burn → the whole plan reaches done.
    assert client.post(f"/runs/{_by_seq(d)[2]['run_id']}/approve").status_code == 200
    done = _wait(client, pid, lambda x: x["status"] == ps.STATUS_DONE)

    final = _by_seq(done)
    assert final[0]["status"] == "done"
    assert final[1]["status"] == "applied" and final[2]["status"] == "applied"
    # The plan rolls up its child runs' cost.
    assert done["build_cost"] > 0
    assert done["build_cost"] == pytest.approx(sum(r["cost_usd"] for r in done["child_runs"]))


# -- the burn respects the gate: nothing applied without a go --------------

def test_burn_unit_waits_at_gate_until_go(build_env, target_repo):
    client, store = build_env
    pid = _ready_brownfield_plan(client, store, target_repo, [
        (0, "The only change", Phase.CONSTRUCTION, []),
    ])
    client.post(f"/plans/{pid}/finalize")

    d = _wait(client, pid, lambda x: _by_seq(x).get(0, {}).get("status") == "awaiting_gate")
    run_id = _by_seq(d)[0]["run_id"]
    assert not (target_repo / STUB_BURN_FILE).exists()          # gate holds the change

    assert client.post(f"/runs/{run_id}/approve").status_code == 200
    _wait(client, pid, lambda x: _by_seq(x)[0]["status"] == "applied")
    assert (target_repo / STUB_BURN_FILE).exists()              # applied only after go
    assert _wait(client, pid, lambda x: x["status"] == ps.STATUS_DONE)


# -- a rejected gate scrubs just that unit, not the plan -------------------

def test_rejected_gate_scrubs_the_unit_not_the_plan(build_env, target_repo):
    client, store = build_env
    # seq1 depends on seq0's burn; rejecting seq0 must scrub seq0 and block seq1 — but
    # the plan itself completes (done), it is not scrubbed.
    pid = _ready_brownfield_plan(client, store, target_repo, [
        (0, "Risky change", Phase.CONSTRUCTION, []),
        (1, "Depends on risky", Phase.CONSTRUCTION, [0]),
    ])
    client.post(f"/plans/{pid}/finalize")

    d = _wait(client, pid, lambda x: _by_seq(x).get(0, {}).get("status") == "awaiting_gate")
    run0 = _by_seq(d)[0]["run_id"]

    # NO-GO on seq0 → its run scrubs; the dependent seq1 never dispatches.
    assert client.post(f"/runs/{run0}/reject").status_code == 200
    done = _wait(client, pid, lambda x: x["status"] == ps.STATUS_DONE)

    runs = _by_seq(done)
    assert runs[0]["status"] == "scrubbed"                      # just this unit
    assert 2 not in runs and 1 not in runs                      # dependent stayed blocked
    assert done["status"] == ps.STATUS_DONE                     # the plan is NOT scrubbed
    assert not (target_repo / STUB_BURN_FILE).exists()          # nothing applied


# -- deferred (v2 operation) units are recorded but never dispatched -------

@pytest.fixture
def plan_store_pg():
    try:
        _checkpointer, pool = postgres_checkpointer(setup=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable (run `docker compose up -d`): {e}")
    yield build_plans_store(pool, setup=True)
    pool.close()


class _FakeRuns:
    """Records launches; never actually runs anything (enough to drive the scheduler)."""

    def __init__(self):
        self.launched = []

    def child_runs(self, plan_id):
        return []

    def launch(self, *, plan_unit_seq, **kwargs):
        self.launched.append(plan_unit_seq)


def _open_building(store, target, units):
    """Open a BUILDING plan on ``target`` with ``units`` = list of
    (seq, phase, task_type, status, stage_slug, depends_on)."""
    pid = f"plan-{uuid4().hex}"
    store.open_plan(pid, target=str(target), local_path=str(target), mode="greenfield",
                    methodology="aidlc", cloud_target="aws", status=ps.STATUS_BUILDING)
    for seq, phase, task_type, status, slug, deps in units:
        store.upsert_unit(pid, seq, title=f"unit {seq}", phase=phase, task_type=task_type,
                          status=status, stage_slug=slug, depends_on=deps)
    return pid


def test_deferred_unit_is_never_dispatched(plan_store_pg, target_repo, tmp_path):
    store = plan_store_pg
    runs = _FakeRuns()
    builder = PlanBuilder(store, runs, workspaces_dir=tmp_path / "ws",
                          cache_root=tmp_path / "cache")
    # seq0 a dispatchable construction burn; seq1 a DEFERRED operation burn.
    pid = _open_building(store, target_repo, [
        (0, "construction", roles.BURN, ps.UNIT_PENDING, "code-generation", []),
        (1, "operation", roles.BURN, ps.UNIT_DEFERRED, "deployment-execution", []),
    ])
    builder._advance(pid)
    assert runs.launched == [0]            # only the non-deferred unit dispatched
    assert 1 not in runs.launched          # the deferred operation unit never launched


def test_plan_of_only_deferred_units_completes(plan_store_pg, target_repo, tmp_path):
    store = plan_store_pg
    runs = _FakeRuns()
    builder = PlanBuilder(store, runs, workspaces_dir=tmp_path / "ws",
                          cache_root=tmp_path / "cache")
    pid = _open_building(store, target_repo, [
        (0, "operation", roles.BURN, ps.UNIT_DEFERRED, "incident-response", []),
    ])
    builder._advance(pid)
    assert runs.launched == []                              # nothing dispatched
    assert store.get_plan(pid).status == ps.STATUS_DONE     # deferred counts as resolved


class _RetryRuns:
    """A run manager that models one run per dispatch (per attempt), so the CAPCOM re-run
    loop can be driven: launch records the seq + creates a fresh in-flight run; a test
    marks a specific run terminal (with/without output) and calls on_run_terminal."""

    def __init__(self):
        self.launched = []            # seq per dispatch (len grows on each retry)
        self._runs = {}               # run_id -> namespace (insertion order = dispatch order)

    def child_runs(self, plan_id):
        return list(self._runs.values())   # latest run per seq is last → wins in by_seq

    def get_run(self, run_id):
        return self._runs.get(run_id)

    def launch(self, *, plan_unit_seq, **kwargs):
        self.launched.append(plan_unit_seq)
        n = sum(1 for s in self.launched if s == plan_unit_seq)
        rid = f"run-{plan_unit_seq}-{n}"
        self._runs[rid] = SimpleNamespace(run_id=rid, plan_unit_seq=plan_unit_seq,
                                          status="running", changes_json=None)
        return rid

    def finish(self, rid, *, changes_json=None):
        r = self._runs[rid]
        r.status = STATUS_APPLIED
        r.changes_json = changes_json


def test_capcom_reruns_a_no_output_stage_then_holds_it(plan_store_pg, target_repo, tmp_path):
    """CAPCOM's bounded re-run loop: a producing stage that writes nothing is RE-RUN
    (escalated), and only after the attempt cap is it HELD (blocked) + surfaced."""
    store = plan_store_pg
    runs = _RetryRuns()
    builder = PlanBuilder(store, runs, workspaces_dir=tmp_path / "ws",
                          cache_root=tmp_path / "cache", docs_sync=None)
    pid = _open_building(store, target_repo, [
        (0, "construction", roles.BURN, ps.UNIT_PENDING, "functional-design", []),
    ])
    builder._advance(pid)                                # attempt 1 → run-0-1
    assert runs.launched == [0]

    runs.finish("run-0-1", changes_json=None)            # produced nothing
    builder.on_run_terminal("run-0-1", pid)              # < cap → RE-RUN (attempt 2)
    assert runs.launched == [0, 0]                       # re-dispatched, not blocked
    assert store.list_units(pid)[0].status != ps.UNIT_BLOCKED
    assert "functional-design:retry" in {r.key for r in store.list_requirements(pid)}

    runs.finish("run-0-2", changes_json=None)            # still nothing (cap reached)
    builder.on_run_terminal("run-0-2", pid)              # → HELD
    assert store.list_units(pid)[0].status == ps.UNIT_BLOCKED
    assert "functional-design:no-output" in {r.key for r in store.list_requirements(pid)}


def test_capcom_rerun_that_produces_marks_done(plan_store_pg, target_repo, tmp_path):
    """If the re-run PRODUCES, the stage completes normally (the loop resolved it)."""
    store = plan_store_pg
    runs = _RetryRuns()
    builder = PlanBuilder(store, runs, workspaces_dir=tmp_path / "ws",
                          cache_root=tmp_path / "cache", docs_sync=None)
    pid = _open_building(store, target_repo, [
        (0, "construction", roles.BURN, ps.UNIT_PENDING, "functional-design", []),
    ])
    builder._advance(pid)
    runs.finish("run-0-1", changes_json=None)            # attempt 1: nothing
    builder.on_run_terminal("run-0-1", pid)              # → re-run
    runs.finish("run-0-2", changes_json={"files": ["business-logic-model.md"]})  # attempt 2: wrote it
    builder.on_run_terminal("run-0-2", pid)
    assert store.list_units(pid)[0].status == ps.UNIT_DONE


def _changes(*paths):
    return {"files": [{"path": p, "status": "?"} for p in paths]}


def _codegen_plan(store, target):
    """units-generation + requirements-analysis (both done producers) → code-generation."""
    return _open_building(store, target, [
        (0, "inception", roles.BURN, ps.UNIT_DONE, "units-generation", []),
        (1, "inception", roles.BURN, ps.UNIT_DONE, "requirements-analysis", []),
        (2, "construction", roles.BURN, ps.UNIT_PENDING, "code-generation", [0, 1]),
    ])


def test_capcom_regenerates_a_producer_that_omitted_its_artifact(plan_store_pg,
                                                                 target_repo, tmp_path):
    """Layer 2: a producer that is DONE but did NOT write a required artifact is
    RE-ACTIVATED — CAPCOM regenerates the exact upstream that omitted the input, not the
    one that delivered it."""
    from mission_control.aidlc_v2 import install as install_v2
    install_v2(target_repo)
    store = plan_store_pg
    runs = _RetryRuns()
    builder = PlanBuilder(store, runs, workspaces_dir=tmp_path / "ws",
                          cache_root=tmp_path / "cache", docs_sync=None)
    pid = _codegen_plan(store, target_repo)
    # units-generation is done but wrote only the story-map, NOT unit-of-work (omitted).
    # requirements-analysis is done and DID write requirements.
    runs._runs["run-0-1"] = SimpleNamespace(run_id="run-0-1", plan_unit_seq=0,
        status=STATUS_APPLIED, changes_json=_changes("a/unit-of-work-story-map.md"))
    runs._runs["run-1-1"] = SimpleNamespace(run_id="run-1-1", plan_unit_seq=1,
        status=STATUS_APPLIED, changes_json=_changes("a/requirements.md"))
    runs._runs["run-2-1"] = SimpleNamespace(run_id="run-2-1", plan_unit_seq=2,
        status=STATUS_APPLIED, changes_json=None)            # code-gen produced nothing
    builder.on_run_terminal("run-2-1", pid)

    by = {u.seq: u for u in store.list_units(pid)}
    assert by[0].status == ps.UNIT_PENDING                   # omitted unit-of-work → regenerated
    assert by[1].status == ps.UNIT_DONE                      # delivered requirements → untouched
    assert 0 in runs.launched                                # producer re-dispatched
    assert "code-generation:awaiting-inputs" in {r.key for r in store.list_requirements(pid)}


def test_capcom_no_false_regen_when_producers_wrote_their_artifacts(plan_store_pg,
                                                                    target_repo, tmp_path):
    """Closes the original gap: when producers are done AND wrote their required artifacts
    (even under sub-paths), CAPCOM does NOT falsely regenerate them — it just re-runs the
    consumer itself."""
    from mission_control.aidlc_v2 import install as install_v2
    install_v2(target_repo)
    store = plan_store_pg
    runs = _RetryRuns()
    builder = PlanBuilder(store, runs, workspaces_dir=tmp_path / "ws",
                          cache_root=tmp_path / "cache", docs_sync=None)
    pid = _codegen_plan(store, target_repo)
    # both producers wrote their required artifacts (unit-of-work, requirements)
    runs._runs["run-0-1"] = SimpleNamespace(run_id="run-0-1", plan_unit_seq=0,
        status=STATUS_APPLIED,
        changes_json=_changes("x/unit-of-work.md", "x/unit-of-work-story-map.md"))
    runs._runs["run-1-1"] = SimpleNamespace(run_id="run-1-1", plan_unit_seq=1,
        status=STATUS_APPLIED, changes_json=_changes("y/requirements.md"))
    runs._runs["run-2-1"] = SimpleNamespace(run_id="run-2-1", plan_unit_seq=2,
        status=STATUS_APPLIED, changes_json=None)
    builder.on_run_terminal("run-2-1", pid)

    by = {u.seq: u for u in store.list_units(pid)}
    assert by[0].status == ps.UNIT_DONE                      # NOT regenerated (it delivered)
    assert by[1].status == ps.UNIT_DONE                      # NOT regenerated (it delivered)
    reqs = {r.key for r in store.list_requirements(pid)}
    assert "code-generation:awaiting-inputs" not in reqs     # no false regeneration
    assert "code-generation:retry" in reqs                   # consumer re-run instead
    assert 2 in runs.launched                                # ...and it was the consumer


def test_capcom_holds_dependents_when_a_stage_never_produces(plan_store_pg,
                                                             target_repo, tmp_path):
    """After the retries exhaust, a produced-nothing stage is BLOCKED and its dependent
    is never dispatched — the fleet is not deployed onto missing inputs."""
    store = plan_store_pg
    runs = _RetryRuns()
    builder = PlanBuilder(store, runs, workspaces_dir=tmp_path / "ws",
                          cache_root=tmp_path / "cache", docs_sync=None)
    pid = _open_building(store, target_repo, [
        (0, "construction", roles.BURN, ps.UNIT_PENDING, "functional-design", []),
        (1, "construction", roles.BURN, ps.UNIT_PENDING, "code-generation", [0]),
    ])
    builder._advance(pid)                                # seq0 attempt 1
    # drive seq0 through all its attempts, each producing nothing
    for n in range(1, ps_max_attempts() + 1):
        runs.finish(f"run-0-{n}", changes_json=None)
        builder.on_run_terminal(f"run-0-{n}", pid)

    assert store.list_units(pid)[0].status == ps.UNIT_BLOCKED   # held after the cap
    assert 1 not in runs.launched                               # dependent NEVER deployed


def ps_max_attempts():
    from mission_control.service.plan_builder import MAX_STAGE_ATTEMPTS
    return MAX_STAGE_ATTEMPTS


def test_no_go_records_request_changes_and_leaves_stage_incomplete(plan_store_pg,
                                                                    target_repo, tmp_path):
    """A NO-GO at a stage's gate records the gate feedback as 'request changes', leaves
    the stage not-done (incomplete), and scrubs only that unit — the plan is not failed."""
    store = plan_store_pg

    class _Runs:
        def __init__(self):
            self.launched = []
            self.run = None

        def child_runs(self, plan_id):
            return [self.run] if self.run else []

        def get_run(self, run_id):
            return self.run

        def launch(self, **kwargs):
            self.launched.append(kwargs.get("plan_unit_seq"))

    runs = _Runs()
    # docs_sync None → record the requirement without touching git (unit-level check)
    builder = PlanBuilder(store, runs, workspaces_dir=tmp_path / "ws",
                          cache_root=tmp_path / "cache", docs_sync=None)
    pid = _open_building(store, target_repo, [
        (0, "construction", roles.BURN, ps.UNIT_PENDING, "code-generation", []),
    ])
    # the burn was rejected at the gate → scrubbed, with the operator's feedback on detail
    runs.run = SimpleNamespace(run_id="run-0", plan_unit_seq=0,
                               status=STATUS_SCRUBBED, detail="fix the API contract")
    builder.on_run_terminal("run-0", pid)

    reqs = {r.key: r for r in store.list_requirements(pid)}
    assert "code-generation:changes-requested" in reqs         # feedback recorded
    assert reqs["code-generation:changes-requested"].value == "fix the API contract"
    assert store.list_units(pid)[0].status != ps.UNIT_DONE      # stage stays incomplete
    assert store.get_plan(pid).status in (ps.STATUS_BUILDING, ps.STATUS_DONE)  # not failed


# -- greenfield bootstraps a real remote (portable identity from unit 1) ----

def test_greenfield_bootstraps_a_remote_and_builds(build_env, tmp_path):
    from mission_control import project_ref
    client, store = build_env
    dest = tmp_path / "created-remote.git"     # a fresh destination; bootstrap creates it
    pid = client.post("/plans", json={"mode": "greenfield", "remote_dest": str(dest)}).json()["id"]
    for seq, title in enumerate(("Workspace Detection", "Requirements Analysis",
                                 "Workflow Planning")):
        store.upsert_unit(pid, seq, title=title, phase=Phase.INCEPTION)
    store.upsert_unit(pid, 3, title="Build the app", phase=Phase.CONSTRUCTION, depends_on=[2])

    fin = client.post(f"/plans/{pid}/finalize")
    assert fin.status_code == 200 and fin.json()["status"] == ps.STATUS_BUILDING
    # Greenfield now has a PORTABLE identity from birth: target is the bootstrapped ref,
    # NOT an anonymous local scaffold.
    assert fin.json()["target"] == project_ref.normalize_remote(str(dest))
    assert (dest / "HEAD").exists()                             # the remote was created

    # The INCEPTION stages run as sims; the change waits at the gate as a burn.
    d = _wait(client, pid, lambda x: _by_seq(x).get(3, {}).get("status") == "awaiting_gate")
    runs = _by_seq(d)
    assert runs[0]["task_type"] == roles.SIM and runs[0]["status"] == "done"
    assert runs[3]["task_type"] == roles.BURN

    assert client.post(f"/runs/{runs[3]['run_id']}/approve").status_code == 200
    done = _wait(client, pid, lambda x: x["status"] == ps.STATUS_DONE)
    assert _by_seq(done)[3]["status"] == "applied"              # built + pushed to the remote


def test_greenfield_without_destination_fails_loudly(build_env):
    client, store = build_env
    pid = client.post("/plans", json={"mode": "greenfield"}).json()["id"]  # NO remote_dest
    for seq, title in enumerate(("Workspace Detection", "Requirements Analysis",
                                 "Workflow Planning")):
        store.upsert_unit(pid, seq, title=title, phase=Phase.INCEPTION)
    store.upsert_unit(pid, 3, title="Build the app", phase=Phase.CONSTRUCTION, depends_on=[2])

    # Finalize refuses to build: greenfield must not fall back to a local-only workspace.
    fin = client.post(f"/plans/{pid}/finalize")
    assert fin.status_code == 400
    assert "destination" in fin.json()["detail"].lower()


def test_direct_run_launch_rejects_non_git_and_nested_targets(build_env, tmp_path):
    # The direct-run launch path (/runs, /ui/launch) has the same worktree footgun as
    # the plan builder — it must refuse a target that isn't its OWN git root, rather
    # than dispatch a run that fails at (or pollutes a parent via) worktree creation.
    import subprocess as sp
    client, _store = build_env

    plain = tmp_path / "plain"
    plain.mkdir()                                       # a directory, but not a git repo
    r = client.post("/runs", json={"target": str(plain), "task_type": "sim"})
    assert r.status_code == 400 and "git repository" in r.json()["detail"]

    parent = tmp_path / "parent"
    parent.mkdir()
    sp.run(["git", "-C", str(parent), "init", "-b", "main"], check=True, capture_output=True)
    nested = parent / "sub"
    nested.mkdir()                                      # a subdir INSIDE a parent repo
    r = client.post("/runs", json={"target": str(nested), "task_type": "sim"})
    assert r.status_code == 400 and "git repository" in r.json()["detail"]


def test_is_git_repo_requires_own_root_not_an_ancestor(tmp_path):
    # Safety: a directory nested inside a parent repo is NOT a build target — only a
    # directory that is its OWN git root. (No Postgres needed.)
    import subprocess as sp
    from mission_control.service.plan_builder import _is_git_repo

    parent = tmp_path / "parent"
    parent.mkdir()
    sp.run(["git", "-C", str(parent), "init", "-b", "main"], check=True, capture_output=True)
    child = parent / "child"
    child.mkdir()                                       # plain dir INSIDE the parent repo

    assert _is_git_repo(parent) is True                 # its own root
    assert _is_git_repo(child) is False                 # inside parent, not its own root
    sp.run(["git", "-C", str(child), "init", "-b", "main"], check=True, capture_output=True)
    assert _is_git_repo(child) is True                  # now its own root → accepted


# -- Fix 2: a build left mid-flight resumes on restart --------------------

def test_build_resumes_after_restart(tmp_path, monkeypatch, target_repo):
    monkeypatch.delenv("MC_PLANNER_METHODOLOGY", raising=False)
    monkeypatch.delenv("MC_PLANNER_CLOUD", raising=False)
    try:
        cp, pool = postgres_checkpointer(setup=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable: {e}")
    runs = build_runs_store(pool, setup=True)
    plan_store = build_plans_store(pool, setup=True)

    def make_app():
        m = RunManager(checkpointer=cp, runs_store=runs,
                       worker_factory=lambda: StubWorker(), telemetry_dir=tmp_path / "t")
        b = PlanBuilder(plan_store, m, workspaces_dir=tmp_path / "ws")
        m.set_run_observer(b.on_run_terminal)
        return TestClient(create_app(m, PlanManager(plan_store), b))

    # Craft a durable mid-build state: seq0's sim already SUCCEEDED, but seq1 (which
    # depends on it) was never dispatched — the process died in that window.
    pid = f"plan-{uuid4().hex}"
    plan_store.open_plan(pid, target=str(target_repo), mode="brownfield",
                         methodology="aidlc", cloud_target="aws")
    for key in (aidlc.REQ_KEY_SCOPE, aidlc.REQ_KEY_COMPONENTS, aidlc.REQ_KEY_ACCEPTANCE):
        plan_store.upsert_requirement(pid, key, value="ok", state=aidlc.REQ_READY)
    plan_store.upsert_unit(pid, 0, title="Validate", phase=Phase.INCEPTION, depends_on=[])
    plan_store.upsert_unit(pid, 1, title="Change", phase=Phase.CONSTRUCTION, depends_on=[0])
    plan_store.set_status(pid, ps.STATUS_BUILDING)
    rid0 = f"run-{uuid4().hex}"
    runs.launch(rid0, task_type=roles.SIM, target=str(target_repo.resolve()),
                plan_id=pid, plan_unit_seq=0)
    runs.finish(rid0, status=STATUS_DONE, cost_usd=0.001)

    # "Restart": a fresh app over the SAME store resumes the build on startup — seq1's
    # dep has durably succeeded, so it now gets dispatched (as a burn, to the gate).
    with make_app() as client:
        d = _wait(client, pid, lambda x: _by_seq(x).get(1, {}).get("status") == "awaiting_gate")
        assert _by_seq(d)[1]["task_type"] == roles.BURN
        assert client.post(f"/runs/{_by_seq(d)[1]['run_id']}/approve").status_code == 200
        done = _wait(client, pid, lambda x: x["status"] == ps.STATUS_DONE)
        assert _by_seq(done)[1]["status"] == "applied"
    pool.close()
