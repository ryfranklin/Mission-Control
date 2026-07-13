"""The interactive planner engine behind ``POST /plans/{id}/turns`` (offline stub).

Drives a greenfield session through the AI-DLC INCEPTION stages via Q&A to a
finalizable plan whose units are well-formed (INCEPTION → sim, CONSTRUCTION → burn);
checks the AWS/.aidlc defaults hold unless overridden; and proves the engine is
read-only — a session over a real target repo leaves it byte-for-byte unchanged.
Skipped unless the Dockerized Postgres is reachable."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mission_control import StubWorker, roles
from mission_control.aidlc import Phase
from mission_control.graph import (
    build_plans_store,
    build_runs_store,
    postgres_checkpointer,
)
from mission_control import plans_store as ps
from mission_control.service import PlanManager, RunManager, create_app


def test_decomposition_prompt_asks_for_fine_grained_units():
    # No Postgres / LLM needed — a pure check that the units step instructs the planner
    # to emit SMALL, single-run-sized units (not feature-sized ones that blow the
    # worker's turn budget) with a multi-unit example.
    from mission_control import aidlc
    from mission_control.service.planner import StageContext, _stage_prompt

    def ctx(stage=None, criterion=None):
        return StageContext(plan=None, operator_content="go", steering_text="",
                            cloud_target="aws", stage=stage, criterion=criterion)

    ug = aidlc.INCEPTION_STAGE_BY_KEY["units_generation"]
    p = _stage_prompt(ctx(stage=ug))
    assert "SMALLEST" in p and "TOO BIG" in p           # granularity guidance present
    assert p.count("CONSTRUCTION") >= 4                  # multi-unit (fine) example
    assert "MUST be a non-empty list" in p              # units mandatory at decomposition

    ra = aidlc.INCEPTION_STAGE_BY_KEY["requirements_analysis"]
    p2 = _stage_prompt(ctx(stage=ra))
    assert "MUST be a non-empty list" not in p2         # earlier stages don't force units


@pytest.fixture
def plan_env(tmp_path, monkeypatch):
    """A TestClient with the PLAN seam + the underlying store. Clears MC_PLANNER_*
    so the instance defaults resolve to 'aidlc'/'aws'. Uses the default (stub) brain,
    so the walk is deterministic and offline."""
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
    plan_manager = PlanManager(plan_store)  # default StubPlannerBrain engine
    with TestClient(create_app(manager, plan_manager)) as c:
        yield c, plan_store
    pool.close()


def _turn(client, pid: str, content: str) -> dict:
    r = client.post(f"/plans/{pid}/turns", json={"content": content})
    assert r.status_code == 200, r.text
    return r.json()["reply"]


# -- the greenfield walk to a finalizable plan -----------------------------

def test_greenfield_walk_reaches_finalizable_plan_with_wellformed_units(plan_env):
    client, store = plan_env
    pid = client.post("/plans", json={"mode": "greenfield"}).json()["id"]

    # The planner asks AI-DLC-format questions and advances a stage per answer.
    r1 = _turn(client, pid, "I want to build a brand-new CLI tool")
    assert "[Answer]:" in r1["content"]                       # AI-DLC question format
    # Requirements with NO user-facing signal → user stories are skipped.
    _turn(client, pid, "Parse logs and compute metrics; performance and reliability matter")
    _turn(client, pid, "Thin end-to-end slice first")
    _turn(client, pid, "Yes, generate the units")

    plan = client.get(f"/plans/{pid}").json()
    assert plan["status"] == ps.STATUS_READY                  # walk complete → ready
    assert plan["mode"] == "greenfield"

    units = store.list_units(pid)
    titles = [u.title for u in units]
    # Every required INCEPTION stage is laid down (and user stories were skipped).
    assert "Workspace Detection" in titles
    assert "Requirements Analysis" in titles
    assert "Workflow Planning" in titles
    assert "User Stories" not in titles
    assert "Units Generation" in titles

    # Units are well-formed: seqs are unique/ascending, and task_type is DERIVED from
    # phase (INCEPTION → sim, CONSTRUCTION → burn) — never mismatched.
    assert [u.seq for u in units] == sorted({u.seq for u in units})
    for u in units:
        assert u.phase in (Phase.INCEPTION.value, Phase.CONSTRUCTION.value)
        expected = roles.SIM if u.phase == Phase.INCEPTION.value else roles.BURN
        assert u.task_type == expected
    # There is a real CONSTRUCTION work-list (burns) after the INCEPTION stages.
    burns = [u for u in units if u.phase == Phase.CONSTRUCTION.value]
    assert burns and all(u.task_type == roles.BURN for u in burns)

    # Readiness now passes → finalize locks the plan.
    fin = client.post(f"/plans/{pid}/finalize")
    assert fin.status_code == 200 and fin.json()["status"] == ps.STATUS_FINALIZED


def test_units_generated_once_even_if_brain_emits_them_every_stage(plan_env):
    # Regression: a real LLM brain may volunteer a `units` block on stages other than
    # units-generation. The engine must append the CONSTRUCTION work-list ONCE (at
    # units-generation), never duplicating it across turns.
    from uuid import uuid4

    from mission_control.service.planner import PlannerEngine, StageOutcome

    _, store = plan_env

    class _UnitsEverywhere:
        """Completes every stage AND emits a construction unit each turn."""
        def advance(self, ctx):
            yield "ok. "
            outcome = StageOutcome(stage_complete=True,
                                   units=[("stray unit", Phase.CONSTRUCTION, [])])
            if ctx.stage and ctx.stage.key == "workspace_detection":
                outcome.mode = "greenfield"
            return outcome

    engine = PlannerEngine(store, brain=_UnitsEverywhere())
    pid = f"plan-{uuid4().hex}"
    store.open_plan(pid, target=None, mode="greenfield",
                    methodology="aidlc", cloud_target="aws")
    for _ in range(8):
        list(engine.run_turn(pid, "go"))
        if store.get_plan(pid).status == ps.STATUS_READY:
            break

    construction = [u for u in store.list_units(pid) if u.phase == Phase.CONSTRUCTION.value]
    assert len(construction) == 1          # once, at units-generation — not once per stage
    assert store.get_plan(pid).status == ps.STATUS_READY


def test_user_stories_stage_runs_only_when_warranted(plan_env):
    client, store = plan_env
    pid = client.post("/plans", json={"mode": "greenfield"}).json()["id"]
    _turn(client, pid, "A new web app")
    # A user-facing requirement warrants the conditional User Stories stage.
    _turn(client, pid, "Users log in and manage their profile")
    _turn(client, pid, "Describe the primary personas and their goals")  # user stories
    _turn(client, pid, "Backend then frontend")                          # workflow planning
    _turn(client, pid, "Yes, generate the units")                        # units generation

    titles = [u.title for u in store.list_units(pid)]
    assert "User Stories" in titles
    assert client.get(f"/plans/{pid}").json()["status"] == ps.STATUS_READY


# -- defaults: AWS / .aidlc unless overridden ------------------------------

def test_defaults_are_aws_aidlc_unless_overridden(plan_env):
    client, _store = plan_env
    default = client.post("/plans", json={"mode": "greenfield"}).json()
    assert default["methodology"] == "aidlc" and default["cloud_target"] == "aws"

    overridden = client.post("/plans", json={
        "mode": "greenfield", "methodology": "custom-mm", "cloud_target": "gcp",
    }).json()
    assert overridden["methodology"] == "custom-mm"
    assert overridden["cloud_target"] == "gcp"
    # The override sticks through a turn (the engine plans against the given cloud).
    reply = _turn(client, overridden["id"], "A new service")
    assert client.get(f"/plans/{overridden['id']}").json()["cloud_target"] == "gcp"
    assert reply["role"] == "planner"


# -- read-only: INCEPTION never mutates the target -------------------------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout


def test_engine_never_mutates_the_target(plan_env, target_repo):
    client, _store = plan_env
    before_head = _git(target_repo, "rev-parse", "HEAD").strip()
    before_files = sorted(p.name for p in target_repo.iterdir())

    pid = client.post("/plans", json={
        "target": str(target_repo), "mode": "greenfield",
    }).json()["id"]
    # A couple of turns — the engine probes the target for AI-DLC steering (read-only).
    _turn(client, pid, "A brand-new tool in this repo")
    _turn(client, pid, "Parse logs and compute metrics; performance matters")

    # Nothing changed: no working-tree diff, no new/removed files, same HEAD.
    assert _git(target_repo, "status", "--porcelain") == ""
    assert _git(target_repo, "rev-parse", "HEAD").strip() == before_head
    assert sorted(p.name for p in target_repo.iterdir()) == before_files


# -- SSE: the reply streams as tokens, then a terminal done event ----------

def test_turn_stream_emits_tokens_then_done(plan_env):
    client, _store = plan_env
    pid = client.post("/plans", json={"mode": "greenfield"}).json()["id"]

    events: list[tuple[str, dict]] = []
    with client.stream("POST", f"/plans/{pid}/turns/stream",
                       json={"content": "A new CLI tool"}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        cur_event = None
        for raw in r.iter_lines():
            line = raw.rstrip("\r")
            if line.startswith("event:"):
                cur_event = line.partition(":")[2].strip()
            elif line.startswith("data:"):
                events.append((cur_event, json.loads(line.partition(":")[2].strip())))

    kinds = [e[0] for e in events]
    assert kinds.count("token") >= 2                          # streamed token-by-token
    assert kinds[-1] == "done"                                # terminal event last
    done = events[-1][1]
    assert done["plan"]["status"] in (ps.STATUS_DRAFTING, ps.STATUS_READY)
    assert done["turn"]["role"] == "planner"
    # The reassembled reply carries the next stage's AI-DLC questions.
    reply = "".join(d["text"] for k, d in events if k == "token")
    assert "[Answer]:" in reply
