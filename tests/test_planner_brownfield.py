"""The brownfield planner path: reverse-engineering sim + requirements-readiness loop.

When workspace detection finds existing code it sets mode=brownfield and (a) launches
a REAL read-only sim (the existing launch path) to reverse-engineer the target, folding
the summary into requirements, then (b) loops requirements clarification until the
readiness gate is green (scope, components, acceptance, well-formed units). finalize
stays refused until every criterion is met; the unmet criteria surface in GET
/plans/{id}. Skipped unless the Dockerized Postgres is reachable."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mission_control import StubWorker, aidlc, roles
from mission_control.graph import (
    build_plans_store,
    build_runs_store,
    postgres_checkpointer,
)
from mission_control import plans_store as ps
from mission_control.runs_store import STATUS_DONE
from mission_control.service import PlanManager, RunManager, create_app
from mission_control.service.planner import PlannerEngine


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def code_repo(tmp_path):
    """A git repo that already contains code — so workspace detection reads it as
    brownfield."""
    repo = tmp_path / "code"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "app.py").write_text("def main():\n    return 42\n")
    (repo / "README.md").write_text("# code\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture
def bf_env(tmp_path, monkeypatch):
    """A TestClient whose planner engine is wired to the RunManager as its sim-runner
    (so the reverse-engineering step launches a real sim). Stub brain → deterministic."""
    monkeypatch.delenv("MC_PLANNER_METHODOLOGY", raising=False)
    monkeypatch.delenv("MC_PLANNER_CLOUD", raising=False)
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
    engine = PlannerEngine(plan_store, sim_runner=manager)  # default StubPlannerBrain
    plan_manager = PlanManager(plan_store, engine=engine)
    with TestClient(create_app(manager, plan_manager)) as c:
        yield c, plan_store, manager
    pool.close()


def _turn(client, pid, content):
    r = client.post(f"/plans/{pid}/turns", json={"content": content})
    assert r.status_code == 200, r.text
    return r.json()


def _unmet(detail: dict) -> set[str]:
    return {c["key"] for c in detail["readiness"] if not c["met"]}


# -- (a) existing code → brownfield + reverse-engineering sim ---------------

def test_existing_code_triggers_brownfield_and_reverse_engineering_sim(bf_env, code_repo):
    client, store, manager = bf_env
    # Opened greenfield, but the target already has code → detection flips to brownfield.
    pid = client.post("/plans", json={"target": str(code_repo), "mode": "greenfield"}).json()["id"]
    _turn(client, pid, "Let's work on this existing repository")

    detail = client.get(f"/plans/{pid}").json()
    assert detail["mode"] == "brownfield"                    # workspace detection set it

    # A real sim ran (existing launch path) and is recorded in the runs registry.
    reqs = {r["key"]: r["value"] for r in detail["requirements"]}
    assert aidlc.REQ_KEY_RE_SUMMARY in reqs                  # RE artifacts folded in
    assert reqs[aidlc.REQ_KEY_RE_SUMMARY]                    # non-empty codebase summary
    run_id = reqs[aidlc.REQ_KEY_RE_RUN]
    sim = client.get(f"/runs/{run_id}").json()
    assert sim["task_type"] == roles.SIM
    assert sim["target"] == str(code_repo.resolve())
    assert sim["status"] == STATUS_DONE                      # a sim never gates → done

    # The Reverse Engineering stage is laid down as a read-only (sim) INCEPTION unit.
    re_units = [u for u in detail["units"] if u["title"] == aidlc.REVERSE_ENGINEERING_TITLE]
    assert re_units and re_units[0]["task_type"] == roles.SIM

    # The sim was read-only: the target working tree is unchanged.
    assert _git(code_repo, "status", "--porcelain") == ""


# -- (b) the requirements-readiness loop gates finalize --------------------

def test_readiness_loop_blocks_finalize_until_every_criterion_met(bf_env, code_repo):
    client, store, manager = bf_env
    pid = client.post("/plans", json={"target": str(code_repo), "mode": "brownfield"}).json()["id"]

    # Turn 1: workspace detection + reverse engineering. Gate is red (nothing gathered).
    _turn(client, pid, "Work on this repo")
    detail = client.get(f"/plans/{pid}").json()
    assert detail["ready"] is False
    assert _unmet(detail) == {"scope", "components", "acceptance", "units"}
    assert client.post(f"/plans/{pid}/finalize").status_code == 409

    # Loop the clarification: each answer satisfies exactly one criterion, and finalize
    # stays refused until the last one lands.
    _turn(client, pid, "Add a --json output flag; nothing else changes")   # scope
    assert _unmet(client.get(f"/plans/{pid}").json()) == {"components", "acceptance", "units"}
    assert client.post(f"/plans/{pid}/finalize").status_code == 409

    _turn(client, pid, "It touches the CLI entrypoint and the formatter module")  # components
    assert _unmet(client.get(f"/plans/{pid}").json()) == {"acceptance", "units"}
    assert client.post(f"/plans/{pid}/finalize").status_code == 409

    _turn(client, pid, "Done when --json emits valid JSON and tests pass")   # acceptance
    assert _unmet(client.get(f"/plans/{pid}").json()) == {"units"}
    assert client.post(f"/plans/{pid}/finalize").status_code == 409

    _turn(client, pid, "Yes, generate the units")                            # units → gate green
    detail = client.get(f"/plans/{pid}").json()
    assert detail["ready"] is True and _unmet(detail) == set()
    assert detail["status"] == ps.STATUS_READY

    # Every CONSTRUCTION unit is well-formed, tagged burn.
    burns = [u for u in detail["units"] if u["phase"] == "CONSTRUCTION"]
    assert burns and all(u["task_type"] == roles.BURN and u["title"] for u in burns)

    # Now — and only now — finalize is allowed.
    fin = client.post(f"/plans/{pid}/finalize")
    assert fin.status_code == 200 and fin.json()["status"] == ps.STATUS_FINALIZED


def test_finalize_refusal_names_the_unmet_criteria(bf_env, code_repo):
    client, _store, _mgr = bf_env
    pid = client.post("/plans", json={"target": str(code_repo), "mode": "brownfield"}).json()["id"]
    _turn(client, pid, "Work on this repo")
    refused = client.post(f"/plans/{pid}/finalize")
    assert refused.status_code == 409
    # The refusal surfaces what's still blocking (for the operator / UI).
    assert "Scope" in refused.json()["detail"]
