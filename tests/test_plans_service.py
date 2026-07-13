"""The PLAN seam over the FastAPI service (httpx TestClient).

Covers the operator flow: open a plan (AWS/.aidlc defaults), append turns that come
back in transcript order, and finalize gated on the methodology's readiness rule
(refused until the INCEPTION stages / requirements gate passes). Plus: a per-request
methodology/cloud override sticks. Skipped unless the Dockerized Postgres is
reachable."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mission_control import StubWorker
from mission_control.aidlc import Phase
from mission_control.graph import (
    build_plans_store,
    build_runs_store,
    postgres_checkpointer,
)
from mission_control import plans_store as ps
from mission_control.service import PlanManager, RunManager, create_app


@pytest.fixture
def plan_client(tmp_path, monkeypatch):
    """A TestClient over the service with the PLAN seam mounted, plus the underlying
    PLAN store (for setting up readiness preconditions the way the P2 engine will).
    Clears the MC_PLANNER_* env so the instance defaults resolve to 'aidlc'/'aws'."""
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
    plan_manager = PlanManager(plan_store)
    with TestClient(create_app(manager, plan_manager)) as c:
        yield c, plan_store
    pool.close()


# -- open: AWS/.aidlc defaults ---------------------------------------------

def test_plan_persists_with_aws_aidlc_defaults(plan_client):
    client, _store = plan_client
    r = client.post("/plans", json={"target": "/tmp/target", "mode": "greenfield"})
    assert r.status_code == 201
    body = r.json()
    assert body["methodology"] == "aidlc"
    assert body["cloud_target"] == "aws"
    assert body["mode"] == "greenfield"
    assert body["status"] == ps.STATUS_DRAFTING
    assert body["turns"] == [] and body["units"] == [] and body["requirements"] == []

    # It round-trips through GET.
    got = client.get(f"/plans/{body['id']}").json()
    assert got["id"] == body["id"] and got["cloud_target"] == "aws"


def test_unknown_mode_is_rejected(plan_client):
    client, _store = plan_client
    r = client.post("/plans", json={"target": "/t", "mode": "sideways"})
    assert r.status_code == 422  # pydantic validation


# -- turns append in order -------------------------------------------------

def test_turns_append_in_order(plan_client):
    client, _store = plan_client
    pid = client.post("/plans", json={"mode": "greenfield"}).json()["id"]

    r1 = client.post(f"/plans/{pid}/turns", json={"content": "build me a CLI"})
    assert r1.status_code == 200
    assert r1.json()["reply"]["role"] == "planner"

    client.post(f"/plans/{pid}/turns", json={"content": "with subcommands"})

    turns = client.get(f"/plans/{pid}").json()["turns"]
    assert [t["seq"] for t in turns] == [0, 1, 2, 3]                 # ordered, gap-free
    assert [t["role"] for t in turns] == ["operator", "planner", "operator", "planner"]
    assert turns[0]["content"] == "build me a CLI"
    assert turns[2]["content"] == "with subcommands"


def test_turn_on_missing_plan_404s(plan_client):
    client, _store = plan_client
    r = client.post("/plans/plan-nope/turns", json={"content": "hi"})
    assert r.status_code == 404


# -- finalize: refused until readiness -------------------------------------

def test_finalize_refused_until_greenfield_inception_ready(plan_client):
    client, store = plan_client
    pid = client.post("/plans", json={"mode": "greenfield"}).json()["id"]

    # No INCEPTION stages laid down yet → readiness refuses the lock.
    refused = client.post(f"/plans/{pid}/finalize")
    assert refused.status_code == 409
    assert "not ready" in refused.json()["detail"]
    assert client.get(f"/plans/{pid}").json()["status"] == ps.STATUS_DRAFTING

    # Lay down the always-execute INCEPTION stages (as the engine will).
    for seq, title in enumerate(
        ("Workspace Detection", "Requirements Analysis", "Workflow Planning")
    ):
        store.upsert_unit(pid, seq, title=title, phase=Phase.INCEPTION)
    # Stages present but NO work-list yet → still refused (can't hand off nothing).
    assert client.post(f"/plans/{pid}/finalize").status_code == 409
    store.upsert_unit(pid, 3, title="Build it", phase=Phase.CONSTRUCTION)

    ok = client.post(f"/plans/{pid}/finalize")
    assert ok.status_code == 200
    assert ok.json()["status"] == ps.STATUS_FINALIZED
    # Idempotent: finalizing again is a no-op, still finalized.
    assert client.post(f"/plans/{pid}/finalize").json()["status"] == ps.STATUS_FINALIZED


def test_finalize_brownfield_requirements_gate(plan_client):
    client, store = plan_client
    pid = client.post("/plans", json={"mode": "brownfield"}).json()["id"]

    # The brownfield gate needs scope + affected components + acceptance criteria +
    # a well-formed CONSTRUCTION work-list. Nothing captured yet → refused.
    assert client.post(f"/plans/{pid}/finalize").status_code == 409

    from mission_control import aidlc

    store.upsert_requirement(pid, aidlc.REQ_KEY_SCOPE, value="bounded", state="ready")
    store.upsert_requirement(pid, aidlc.REQ_KEY_COMPONENTS, value="module a, b", state="ready")
    assert client.post(f"/plans/{pid}/finalize").status_code == 409  # acceptance + units missing
    store.upsert_requirement(pid, aidlc.REQ_KEY_ACCEPTANCE, value="tests pass", state="ready")
    assert client.post(f"/plans/{pid}/finalize").status_code == 409  # still no CONSTRUCTION unit

    store.upsert_unit(pid, 0, title="Implement the change", phase="CONSTRUCTION")
    assert client.post(f"/plans/{pid}/finalize").status_code == 200


# -- overrides stick -------------------------------------------------------

def test_methodology_and_cloud_override_sticks(plan_client):
    client, _store = plan_client
    r = client.post("/plans", json={
        "mode": "greenfield", "methodology": "custom-mm", "cloud_target": "gcp",
    })
    body = r.json()
    assert body["methodology"] == "custom-mm"
    assert body["cloud_target"] == "gcp"
    assert client.get(f"/plans/{body['id']}").json()["cloud_target"] == "gcp"


# -- listing (paged like /runs) --------------------------------------------

def test_list_plans_is_paged(plan_client):
    client, _store = plan_client
    ids = {client.post("/plans", json={"mode": "greenfield"}).json()["id"] for _ in range(3)}
    listing = client.get("/plans", params={"limit": 2}).json()
    assert listing["limit"] == 2 and listing["total"] >= 3
    assert len(listing["plans"]) == 2
    # Filter by mode narrows.
    assert all(p["mode"] == "greenfield" for p in
               client.get("/plans", params={"mode": "greenfield"}).json()["plans"])
