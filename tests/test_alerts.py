"""Phase 5c S5: alert-class notifications (cost/budget, regression) + the per-profile
fleet digest — metadata-only, routed to the run's profile, quiet (thresholds not spam).

Driven over HTTP through the FastAPI service (in-process TestClient) with the shared
in-memory store from conftest. The bridge-side rendering/routing lives in
test_slack_bridge.py; here we exercise EMISSION (the service seam)."""

from __future__ import annotations

import json
import time

from mission_control import roles
from mission_control.service.alerts import CostAlertConfig


# -- helpers ---------------------------------------------------------------

def _launch(client, target, task_type=roles.SIM, *, slack_profile=None) -> str:
    body = {"target": str(target), "task_type": task_type}
    if slack_profile is not None:
        body["slack_profile"] = slack_profile
    r = client.post("/runs", json=body)
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


def _notes_of_kind(client, kind, *, run_id=None) -> list[dict]:
    rows = client.get("/notifications", params={"limit": 500}).json()["notifications"]
    return [n for n in rows if n["kind"] == kind and (run_id is None or n["run_id"] == run_id)]


def _reg(target="/repo/x") -> dict:
    """A minimal Phase-3 gate result with a quality regression (and a NON-regressed cost
    axis). The ``runs`` field carries a sentinel to prove eval content never leaks."""
    return {
        "passed": False, "exit_code": 1, "k": 2, "n": 3,
        "axes": {
            "quality_total": {"current": 0.61, "baseline_mean": 0.80, "baseline_stddev": 0.05,
                              "threshold": 0.70, "higher_is_worse": False, "regressed": True},
            "cost_usd": {"current": 0.30, "baseline_mean": 0.28, "baseline_stddev": 0.02,
                         "threshold": 0.32, "higher_is_worse": True, "regressed": False},
        },
        "runs": [{"eval_output": "EVAL-CONTENT-must-not-leak", "target": target}],
    }


# -- cost / budget alerts --------------------------------------------------

def test_cost_threshold_crossing_emits_one_alert_routed(mem_store, make_service, target_repo):
    # A tiny per-run threshold → any real (nonzero) run cost crosses it.
    client = make_service(mem_store, slack_registry=_reg_registry(),
                          cost_alerts=CostAlertConfig(per_run=1e-9))
    run_id = _launch(client, target_repo, roles.SIM, slack_profile="A")
    _wait(client, run_id, {"done"})

    # Poll the outbox until the terminal + cost alert have been appended.
    alerts = _wait_notes(client, "cost_threshold", run_id)
    assert len(alerts) == 1                              # exactly one — quiet, not spam
    alert = alerts[0]
    assert alert["slack_profile"] == "A"                # routed to the run's profile
    p = alert["payload"]
    assert p["reason"] == "per_run" and p["threshold"] == 1e-9
    assert p["cost_usd"] > 0                             # metadata: the reconciled cost
    # No run content anywhere in the alert.
    assert "prompt" not in json.dumps(alert).lower()


def test_no_cost_alert_when_below_threshold(mem_store, make_service, target_repo):
    client = make_service(mem_store, slack_registry=_reg_registry(),
                          cost_alerts=CostAlertConfig(per_run=1e9))  # unreachably high
    run_id = _launch(client, target_repo, roles.SIM, slack_profile="A")
    _wait(client, run_id, {"done"})
    _wait_notes(client, "run_terminal", run_id)         # terminal arrived…
    assert _notes_of_kind(client, "cost_threshold", run_id=run_id) == []  # …but no alert


def test_null_profile_generates_no_cost_alert(mem_store, make_service, target_repo):
    client = make_service(mem_store, slack_registry=_reg_registry(),
                          cost_alerts=CostAlertConfig(per_run=1e-9))
    run_id = _launch(client, target_repo, roles.SIM)    # opt-out (no profile)
    _wait(client, run_id, {"done"})
    _wait_notes(client, "run_terminal", run_id)
    assert _notes_of_kind(client, "cost_threshold", run_id=run_id) == []  # opt-out → silent


def test_cost_alerts_off_by_default(mem_store, make_service, target_repo):
    client = make_service(mem_store, slack_registry=_reg_registry(),
                          cost_alerts=CostAlertConfig())  # both thresholds None → disabled
    run_id = _launch(client, target_repo, roles.SIM, slack_profile="A")
    _wait(client, run_id, {"done"})
    _wait_notes(client, "run_terminal", run_id)
    assert _notes_of_kind(client, "cost_threshold", run_id=run_id) == []


# -- regression alerts (client of the existing gate result) ----------------

def test_regression_result_emits_one_alert_with_metadata(mem_store, make_service, target_repo):
    client = make_service(mem_store, slack_registry=_reg_registry())
    run_id = _launch(client, target_repo, roles.SIM, slack_profile="A")
    _wait(client, run_id, {"done"})

    r = client.post(f"/runs/{run_id}/alerts/regression", json=_reg())
    assert r.status_code == 200 and r.json()["emitted"] is True

    alerts = _notes_of_kind(client, "regression", run_id=run_id)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["slack_profile"] == "A"
    axes = alert["payload"]["axes"]
    assert [a["metric"] for a in axes] == ["quality_total"]        # only the regressed axis
    axis = axes[0]
    assert axis["current"] == 0.61 and axis["baseline_mean"] == 0.80 and axis["threshold"] == 0.70
    assert alert["payload"]["k"] == 2 and alert["payload"]["n"] == 3
    # Eval CONTENT (the gate's per-run output) must never appear.
    assert "EVAL-CONTENT-must-not-leak" not in json.dumps(alert)

    # Idempotent: re-posting the same gate result adds nothing new.
    client.post(f"/runs/{run_id}/alerts/regression", json=_reg())
    assert len(_notes_of_kind(client, "regression", run_id=run_id)) == 1


def test_passing_gate_emits_no_regression(mem_store, make_service, target_repo):
    client = make_service(mem_store, slack_registry=_reg_registry())
    run_id = _launch(client, target_repo, roles.SIM, slack_profile="A")
    _wait(client, run_id, {"done"})
    passed = _reg()
    passed["axes"]["quality_total"]["regressed"] = False          # nothing regressed now
    r = client.post(f"/runs/{run_id}/alerts/regression", json=passed)
    assert r.json()["emitted"] is False
    assert _notes_of_kind(client, "regression", run_id=run_id) == []


def test_null_profile_generates_no_regression(mem_store, make_service, target_repo):
    client = make_service(mem_store, slack_registry=_reg_registry())
    run_id = _launch(client, target_repo, roles.SIM)             # opt-out
    _wait(client, run_id, {"done"})
    r = client.post(f"/runs/{run_id}/alerts/regression", json=_reg())
    assert r.json()["emitted"] is False
    assert _notes_of_kind(client, "regression", run_id=run_id) == []


# -- per-profile fleet digest ----------------------------------------------

def test_profile_digest_aggregates_only_that_profile(mem_store, make_service, target_repo):
    client = make_service(mem_store, slack_registry=_reg_registry())
    a1 = _launch(client, target_repo, roles.SIM, slack_profile="A")
    a2 = _launch(client, target_repo, roles.SIM, slack_profile="A")
    b1 = _launch(client, target_repo, roles.SIM, slack_profile="B")
    null = _launch(client, target_repo, roles.SIM)              # in NO digest
    for rid in (a1, a2, b1, null):
        _wait(client, rid, {"done"})

    da = client.get("/notifications/digest", params={"profile": "A"}).json()
    assert da["profile"] == "A" and da["runs"] == 2             # only A's runs
    assert da["by_status"].get("done") == 2
    assert da["cost_usd"] > 0

    db = client.get("/notifications/digest", params={"profile": "B"}).json()
    assert db["runs"] == 1                                      # only B's run

    # The null-profile run appears in neither digest.
    assert da["runs"] + db["runs"] == 3


# -- local fixtures --------------------------------------------------------

def _reg_registry():
    from mission_control.slack_registry import SlackProfile, SlackRegistry
    return SlackRegistry.from_profiles([
        SlackProfile(name="A", channel="#a"), SlackProfile(name="B", channel="#b"),
    ])


def _wait_notes(client, kind, run_id, timeout=20.0) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = _notes_of_kind(client, kind, run_id=run_id)
        if got:
            return got
        time.sleep(0.02)
    raise AssertionError(f"no {kind} notification for {run_id} within {timeout}s")
