"""The Planner web surface (Jinja + htmx + htmx-SSE), a client of the P1–P3 endpoints.

Drives the control-room UI over TestClient: open a Flight Plan from the new-plan
control, chat with the planner (the reply streams over SSE), watch units accrue in
the live panel, and confirm the hand-off action is disabled until readiness is met
then enabled. All metaphor labels are pulled from roles.py. Skipped unless the
Dockerized Postgres is reachable."""

from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from mission_control import StubWorker, roles
from mission_control.graph import (
    build_plans_store,
    build_runs_store,
    postgres_checkpointer,
)
from mission_control.service import PlanManager, RunManager, create_app
from mission_control.service.planner import PlannerEngine


@pytest.fixture
def web(tmp_path, monkeypatch):
    """A TestClient over the full app (runs + the mounted planner web surface). The
    engine's sim-runner is the RunManager, so a brownfield session can reverse-engineer.
    Stub brain → deterministic walk."""
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
    plan_manager = PlanManager(plan_store, engine=PlannerEngine(plan_store, sim_runner=manager))
    with TestClient(create_app(manager, plan_manager)) as c:
        yield c
    pool.close()


# -- helpers ---------------------------------------------------------------

def _open_plan(client, target: str = "") -> str:
    """Open a plan via the new-plan control; return its id (from the redirect)."""
    r = client.post("/ui/plans",
                    data={"target": target, "methodology": "aidlc", "cloud": "aws"},
                    follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"].rsplit("/", 1)[-1]


def _chat(client, pid: str, content: str) -> None:
    """Post an operator turn, then drive the SSE reply stream to completion (as the
    browser's EventSource would) so the planner's reply is generated."""
    assert client.post(f"/ui/plans/{pid}/turns", data={"content": content}).status_code == 200
    with client.stream("GET", f"/ui/plans/{pid}/reply") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for _ in r.iter_lines():
            pass  # consume to completion (server closes after the terminal `done`)


def _panel(client, pid: str) -> str:
    r = client.get(f"/ui/plans/{pid}/panel")
    assert r.status_code == 200
    return r.text


def _handoff_disabled(html: str) -> bool:
    """Is the hand-off button disabled in this panel HTML?"""
    i = html.index('class="handoff"')
    return "disabled" in html[i:html.index("</button>", i)]


# -- the greenfield session: create → chat → units accrue → hand-off gate --

def test_create_chat_units_accrue_and_handoff_gates_on_readiness(web):
    # The list page carries the NEW-PLAN control with editable methodology/cloud defaults.
    listing = web.get("/ui/plans")
    assert listing.status_code == 200
    assert 'name="methodology"' in listing.text and 'value="aidlc"' in listing.text
    assert 'value="aws"' in listing.text

    pid = _open_plan(web, target="")  # blank → new / greenfield
    session = web.get(f"/ui/plans/{pid}")
    assert session.status_code == 200
    # Before any progress the hand-off is disabled (readiness not met).
    assert _handoff_disabled(_panel(web, pid)) is True

    # Walk the greenfield stages via chat; the panel accrues stages then units.
    _chat(web, pid, "A brand-new CLI tool")
    panel = _panel(web, pid)
    assert "Workspace Detection" in panel                    # stage laid down 'in place'
    assert _handoff_disabled(panel) is True                  # not finalizable yet

    _chat(web, pid, "Parse logs and compute metrics; performance matters")
    _chat(web, pid, "Thin end-to-end slice first")
    _chat(web, pid, "Yes, generate the units")               # units generation → ready

    panel = _panel(web, pid)
    # CONSTRUCTION units surfaced with their task_type badge (sim/burn from roles.py).
    assert f"badge-{roles.BURN}" in panel
    assert "Implement the core logic" in panel
    # Readiness is met → the hand-off is now ENABLED.
    assert _handoff_disabled(panel) is False

    # The hand-off locks the plan and reports it handed to Mission Control.
    handed = web.post(f"/ui/plans/{pid}/finalize")
    assert handed.status_code == 200
    assert "handed to Mission Control" in handed.text
    assert web.get(f"/plans/{pid}").json()["status"] == "finalized"


# -- labels come from roles.py ---------------------------------------------

def test_labels_come_from_roles(web):
    pid = _open_plan(web)
    _chat(web, pid, "A new service")  # produces a planner bubble
    html = web.get(f"/ui/plans/{pid}").text
    assert roles.PLAN in html         # "Flight Plan" — the plan metaphor
    assert roles.PLANNER in html      # "Flight Planner" — the planner persona
    # Nav on every page is labelled from roles too.
    assert roles.PLAN in web.get("/ui/plans").text


# -- brownfield: the panel shows the readiness criteria with pass/fail ------

@pytest.fixture
def code_repo(tmp_path):
    repo = tmp_path / "code"
    repo.mkdir()
    for args in (("init", "-b", "main"), ("config", "user.email", "t@e.co"),
                 ("config", "user.name", "T")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "app.py").write_text("def main():\n    return 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    return repo


def test_brownfield_panel_shows_readiness_criteria(web, code_repo):
    pid = _open_plan(web, target=str(code_repo))
    _chat(web, pid, "Work on this existing repository")   # detection → brownfield + RE sim

    panel = _panel(web, pid)
    assert "badge-brownfield" in panel                     # mode badge
    assert "Readiness" in panel                            # the brownfield readiness block
    # The gate criteria render with pass/fail; scope is still failing here.
    assert "Scope is bounded" in panel
    assert _handoff_disabled(panel) is True                # gate red → hand-off blocked
