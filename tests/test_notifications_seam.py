"""Phase 5c seam-completeness: the fleet-wide notification outbox + the per-run Slack
profile selector threaded through the launch path.

Driven over HTTP through the FastAPI service (in-process TestClient) with a mocked
durability substrate (MemorySaver + the shared in-memory runs store from conftest).
Sharing ONE store across two services simulates a restart, so durable replay and the
resume-doesn't-double-emit discipline are exercised without Docker. The live Postgres
path is covered by test_service.py."""

from __future__ import annotations

import json
import time

from mission_control import roles
from mission_control.slack_registry import SlackProfile, SlackRegistry


# -- helpers ---------------------------------------------------------------

def _registry() -> SlackRegistry:
    """A non-secret registry with one profile whose token lives in an ENV VAR NAME —
    the service never needs the value."""
    return SlackRegistry.from_profiles([
        SlackProfile(name="eng", channel="#eng-missions",
                     approvers=["U0ENG"], token_env="SLACK_TOKEN_ENG"),
        SlackProfile(name="ops", channel="#ops", token_env="SLACK_TOKEN_OPS"),
    ])


def _launch(client, target, task_type=roles.SIM, *, slack_profile=None, prompt=None) -> dict:
    body = {"target": str(target), "task_type": task_type}
    if slack_profile is not None:
        body["slack_profile"] = slack_profile
    if prompt is not None:
        body["prompt"] = prompt
    return client.post("/runs", json=body)


def _wait(client, run_id, wanted, timeout=20.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = client.get(f"/runs/{run_id}").json()
        if detail["status"] in wanted:
            return detail
        time.sleep(0.02)
    raise AssertionError(f"{run_id} never reached {wanted}; last={detail.get('status')}")


def _notifications(client, *, after=0, limit=100) -> dict:
    return client.get("/notifications", params={"after": after, "limit": limit}).json()


def _wait_kinds(client, run_id, wanted_kinds, timeout=20.0) -> list[dict]:
    """Poll the outbox until every kind in ``wanted_kinds`` is present for ``run_id``."""
    deadline = time.time() + timeout
    rows: list[dict] = []
    while time.time() < deadline:
        rows = [n for n in _notifications(client)["notifications"] if n["run_id"] == run_id]
        if wanted_kinds <= {n["kind"] for n in rows}:
            return rows
        time.sleep(0.02)
    raise AssertionError(f"{run_id} outbox never had {wanted_kinds}; had {[n['kind'] for n in rows]}")


# -- (1) the selector: validate at launch, store on the row ----------------

def test_unknown_slack_profile_rejected_none_accepted_valid_stored(mem_store, make_service, target_repo):
    client = make_service(mem_store, slack_registry=_registry())

    # unknown name → rejected early (nothing launched)
    bad = _launch(client, target_repo, slack_profile="nope")
    assert bad.status_code == 400
    assert "nope" in bad.json()["detail"]
    assert client.get("/runs").json()["total"] == 0     # no row was written

    # None (omitted) → accepted, silent run, stored as null
    silent = _launch(client, target_repo)
    assert silent.status_code == 201
    assert silent.json()["slack_profile"] is None

    # a valid name → accepted and stored on the run record
    named = _launch(client, target_repo, slack_profile="eng")
    assert named.status_code == 201
    run_id = named.json()["run_id"]
    assert named.json()["slack_profile"] == "eng"
    assert client.get(f"/runs/{run_id}").json()["slack_profile"] == "eng"


def test_default_profile_stamps_launches_that_name_none(mem_store, make_service, target_repo):
    # A fleet default (MC_DEFAULT_SLACK_PROFILE) makes every launch that doesn't name a
    # profile inherit it — so runs stamp it across CLI/UI/API without per-call changes.
    client = make_service(mem_store, slack_registry=_registry(), default_slack_profile="eng")

    inherited = _launch(client, target_repo)                 # no slack_profile in the body
    assert inherited.status_code == 201
    assert inherited.json()["slack_profile"] == "eng"        # stamped from the default

    # An explicit profile still wins over the default.
    explicit = _launch(client, target_repo, slack_profile="ops")
    assert explicit.json()["slack_profile"] == "ops"

    # An unknown default would fail loudly (validated at launch, same as an explicit one).
    bad = make_service(mem_store, slack_registry=_registry(), default_slack_profile="nope")
    assert _launch(bad, target_repo).status_code == 400


def test_profile_rejected_when_no_registry_configured(mem_store, make_service, target_repo):
    # No registry (env unset) → an empty registry → any non-null profile is rejected,
    # but a silent run still launches. Slack is strictly opt-in.
    client = make_service(mem_store, slack_registry=SlackRegistry.empty())
    assert _launch(client, target_repo, slack_profile="eng").status_code == 400
    assert _launch(client, target_repo).status_code == 201


# -- (2) profiles endpoint: names + channel, NEVER tokens ------------------

def test_slack_profiles_returns_names_no_tokens(mem_store, make_service, target_repo, monkeypatch):
    # A real token value is present in the environment under the registry's env-var name.
    monkeypatch.setenv("SLACK_TOKEN_ENG", "xoxb-super-secret-value")
    client = make_service(mem_store, slack_registry=_registry())

    body = client.get("/slack/profiles").json()
    names = {p["name"] for p in body["profiles"]}
    assert names == {"eng", "ops"}
    assert {p["channel"] for p in body["profiles"]} == {"#eng-missions", "#ops"}
    assert body["none"] == "None"                        # canonical opt-out concept

    # No token value NOR env-var name leaks through the wire.
    blob = json.dumps(body)
    assert "xoxb-super-secret-value" not in blob
    assert "SLACK_TOKEN_ENG" not in blob and "SLACK_TOKEN_OPS" not in blob


# -- (3) one outbox row per milestone, carrying the run's profile ----------

def test_each_milestone_appends_one_row_carrying_profile(mem_store, make_service, target_repo):
    client = make_service(mem_store, slack_registry=_registry())

    # A burn traverses launch → gate → terminal, so it hits all three milestones.
    run_id = _launch(client, target_repo, roles.BURN, slack_profile="eng").json()["run_id"]
    _wait(client, run_id, {"awaiting_gate"})
    _wait_kinds(client, run_id, {"run_launched", "gate_awaiting"})
    client.post(f"/runs/{run_id}/approve")
    _wait(client, run_id, {"applied"})
    rows = _wait_kinds(client, run_id, {"run_launched", "gate_awaiting", "run_terminal"})

    kinds = [n["kind"] for n in rows]
    assert sorted(kinds) == ["gate_awaiting", "run_launched", "run_terminal"]  # exactly one each
    assert all(n["slack_profile"] == "eng" for n in rows)                      # profile carried

    # The terminal milestone carries the final status + cost (metadata only).
    terminal = next(n for n in rows if n["kind"] == "run_terminal")
    assert terminal["payload"]["status"] == "applied"
    assert terminal["payload"]["cost_usd"] > 0
    assert terminal["payload"]["task_type"] == roles.BURN


def test_milestones_emitted_for_silent_runs_too(mem_store, make_service, target_repo):
    # Profile null (opt-out) still emits milestones — the bridge is what filters/routes.
    client = make_service(mem_store, slack_registry=_registry())
    run_id = _launch(client, target_repo, roles.SIM).json()["run_id"]
    _wait(client, run_id, {"done"})
    rows = _wait_kinds(client, run_id, {"run_launched", "run_terminal"})
    assert all(n["slack_profile"] is None for n in rows)


# -- (4) kill → restart → resume does NOT double-append --------------------

def test_resume_after_restart_does_not_double_append(mem_store, make_service, target_repo):
    from langgraph.checkpoint.memory import MemorySaver

    reg = _registry()
    cp = MemorySaver()  # the durable checkpoint substrate that survives the "restart"
    svc_a = make_service(mem_store, slack_registry=reg, checkpointer=cp)
    run_id = _launch(svc_a, target_repo, roles.BURN, slack_profile="ops").json()["run_id"]
    _wait(svc_a, run_id, {"awaiting_gate"})
    _wait_kinds(svc_a, run_id, {"run_launched", "gate_awaiting"})

    # "restart": a fresh service (empty in-memory channels) over the SAME store + cp.
    svc_b = make_service(mem_store, slack_registry=reg, checkpointer=cp)
    svc_b.post(f"/runs/{run_id}/approve")
    _wait(svc_b, run_id, {"applied"})
    rows = _wait_kinds(svc_b, run_id, {"run_launched", "gate_awaiting", "run_terminal"})

    # Each once-only milestone appears EXACTLY once despite the restart + resume.
    kinds = [n["kind"] for n in rows]
    for kind in ("run_launched", "gate_awaiting", "run_terminal"):
        assert kinds.count(kind) == 1, f"{kind} double-appended: {kinds}"


def test_append_notification_is_idempotent_by_run_kind(mem_store):
    # Direct store-level dedupe: a re-crossed boundary re-appends the same (run_id, kind)
    # → no-op, and the global seq does not grow.
    payload = {"status": "queued"}
    assert mem_store.append_notification("run-x", "run_launched",
                                         slack_profile="eng", payload=payload) is True
    assert mem_store.append_notification("run-x", "run_launched",
                                         slack_profile="eng", payload=payload) is False
    assert mem_store.notifications_summary()["total"] == 1


# -- (5) pull contract: tail past `after` + advancing cursor ---------------

def test_notifications_pull_tail_and_cursor(mem_store, make_service, target_repo):
    client = make_service(mem_store, slack_registry=_registry())

    ids = []
    for _ in range(2):
        rid = _launch(client, target_repo, roles.SIM).json()["run_id"]
        _wait(client, rid, {"done"})
        _wait_kinds(client, rid, {"run_launched", "run_terminal"})
        ids.append(rid)

    full = _notifications(client)
    seqs = [n["seq"] for n in full["notifications"]]
    assert seqs == sorted(seqs)                           # oldest-first, ascending seq
    assert full["total"] == len(seqs) == 4               # 2 runs x (launched + terminal)
    assert full["last_seq"] == seqs[-1]

    # after=<seq> returns only the strictly-greater tail.
    cut = seqs[1]
    tail = _notifications(client, after=cut)
    assert [n["seq"] for n in tail["notifications"]] == [s for s in seqs if s > cut]
    assert tail["total"] == 4 and tail["last_seq"] == seqs[-1]

    # A consumer that has advanced its cursor to last_seq sees an empty, stable tail.
    caught_up = _notifications(client, after=full["last_seq"])
    assert caught_up["notifications"] == []
    assert caught_up["total"] == 4 and caught_up["last_seq"] == full["last_seq"]


# -- (6) payloads are metadata-only (no run content) -----------------------

def test_payloads_carry_no_run_content(mem_store, make_service, target_repo):
    client = make_service(mem_store, slack_registry=_registry())
    secret = "TOP-SECRET-PROMPT-do-not-leak-42"
    run_id = _launch(client, target_repo, roles.SIM, prompt=secret).json()["run_id"]
    _wait(client, run_id, {"done"})
    rows = _wait_kinds(client, run_id, {"run_launched", "run_terminal"})

    # The whitelist: run metadata + (for alerts) threshold/budget/axis metadata. Every
    # field is a name or a number/bool — there is NO field for prompt/code/diff content.
    allowed = {"target", "task_type", "status", "cost_usd", "node",
               "created_at", "started_at", "ended_at",
               "reason", "threshold", "budget", "window_total", "window_hours",
               "axes", "k", "n"}
    for n in rows:
        # Metadata-only BY CONSTRUCTION: no field outside the allowed metadata set...
        assert set(n["payload"]) <= allowed
        # ...and the prompt text never appears anywhere in the serialized milestone.
        assert secret not in json.dumps(n)
