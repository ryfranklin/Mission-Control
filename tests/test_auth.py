"""Bearer-token auth on the mutating run endpoints (approve/reject/scrub/cancel).

The seam is localhost no-auth by default; when MC_API_TOKEN is configured, a gate
decision requires an authenticated principal even off the Slack surface. Covers the
three modes: no-auth-configured (open), authorized, unauthorized. Driven over HTTP
through the FastAPI service with the shared in-memory store from conftest."""

from __future__ import annotations

import time

from mission_control import roles

TOKEN = "s3cr3t-bearer-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _launch(client, target, task_type=roles.BURN) -> str:
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


def _burn_at_gate(client, target_repo) -> str:
    run_id = _launch(client, target_repo, roles.BURN)
    _wait(client, run_id, {"awaiting_gate"})
    return run_id


# -- (1) no auth configured → open (backward-compatible) -------------------

def test_no_auth_configured_endpoints_open(mem_store, make_service, target_repo):
    client = make_service(mem_store)                        # no auth_token
    run_id = _burn_at_gate(client, target_repo)
    # Mutating endpoint works with NO Authorization header — unchanged dev posture.
    r = client.post(f"/runs/{run_id}/approve")
    assert r.status_code == 200 and r.json()["run_id"] == run_id
    _wait(client, run_id, {"applied"})


# -- (2) authorized → the correct bearer token passes ----------------------

def test_authorized_bearer_token_passes(mem_store, make_service, target_repo):
    client = make_service(mem_store, auth_token=TOKEN)
    run_id = _burn_at_gate(client, target_repo)
    r = client.post(f"/runs/{run_id}/approve", headers=AUTH)
    assert r.status_code == 200 and r.json()["run_id"] == run_id
    _wait(client, run_id, {"applied"})


# -- (3) unauthorized → missing / wrong / malformed token is 401 -----------

def test_unauthorized_is_rejected_without_mutating(mem_store, make_service, target_repo):
    client = make_service(mem_store, auth_token=TOKEN)
    run_id = _burn_at_gate(client, target_repo)

    # missing header
    assert client.post(f"/runs/{run_id}/approve").status_code == 401
    # wrong token
    assert client.post(f"/runs/{run_id}/approve",
                       headers={"Authorization": "Bearer nope"}).status_code == 401
    # wrong scheme
    assert client.post(f"/runs/{run_id}/approve",
                       headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401

    # None of the rejected attempts resolved the gate — the run is still waiting.
    assert client.get(f"/runs/{run_id}").json()["status"] == "awaiting_gate"
    # The correct token still works afterward → the gate was never consumed.
    assert client.post(f"/runs/{run_id}/approve", headers=AUTH).status_code == 200
    _wait(client, run_id, {"applied"})


def test_all_decision_endpoints_gated_reads_open(mem_store, make_service, target_repo):
    client = make_service(mem_store, auth_token=TOKEN)
    # reject / scrub / cancel are gated too (all mutate a run).
    for action in ("reject", "scrub", "cancel"):
        r = client.post(f"/runs/{_burn_at_gate(client, target_repo)}/{action}")
        assert r.status_code == 401, f"{action} should require the token"
    # Reads stay OPEN even with a token configured (no header needed).
    assert client.get("/runs").status_code == 200
    assert client.get("/slack/profiles").status_code == 200
