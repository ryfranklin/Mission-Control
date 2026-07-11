"""The per-run station view (U3): server-rendered header/banners + an htmx-SSE
live timeline that replays the durable history before tailing live.

Host-runnable (MemorySaver + the shared in-memory store from conftest); the SSE
stream is read with the TestClient (no JS execution)."""

from __future__ import annotations

import threading
import time

from mission_control import roles


def _launch(client, target, task_type=roles.SIM) -> str:
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


def _read_sse(client, url, headers=None, timeout=20.0) -> list[dict]:
    events, ev = [], {"data": []}

    def flush():
        if ev.get("event") or ev["data"]:
            events.append({"event": ev.get("event"), "id": ev.get("id"),
                           "data": "\n".join(ev["data"])})

    with client.stream("GET", url, headers=headers or {}, timeout=timeout) as r:
        for raw in r.iter_lines():
            line = raw.rstrip("\r")
            if line == "":
                flush()
                ev.clear()
                ev["data"] = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                ev["event"] = line[6:].strip()
            elif line.startswith("data:"):
                ev["data"].append(line[5:].lstrip())
            elif line.startswith("id:"):
                ev["id"] = line[3:].strip()
    flush()
    return events


# -- the page (server-rendered snapshot) -----------------------------------

def test_detail_page_shows_gate_banner_and_honest_cost(mem_store, make_service, target_repo):
    client = make_service(mem_store)
    run_id = _launch(client, target_repo, roles.BURN)
    _wait(client, run_id, {"awaiting_gate"})

    html = client.get(f"/ui/runs/{run_id}").text
    assert str(target_repo) in html
    assert f"badge-{roles.BURN}" in html
    assert "status-awaiting_gate" in html
    assert "not yet reconciled" in html and "$0" not in html          # 5a Q1: honest, never $0
    assert f'sse-connect="/ui/runs/{run_id}/events"' in html          # timeline fed by SSE
    assert 'sse-close="terminal"' in html
    # prominent GO / NO-GO banner, labels from roles.py
    assert "gate-banner" in html and roles.GO in html and roles.NO_GO in html


def test_detail_page_completed_shows_final_banner(mem_store, make_service, target_repo):
    client = make_service(mem_store)
    run_id = _launch(client, target_repo, roles.SIM)
    _wait(client, run_id, {"done"})

    html = client.get(f"/ui/runs/{run_id}").text
    assert "final-banner" in html
    assert "reconciled" in html                                       # terminal → reconciled cost
    assert "status-done" in html


# -- the SSE timeline ------------------------------------------------------

def test_completed_run_sse_replays_full_timeline_then_terminal(mem_store, make_service, target_repo):
    client = make_service(mem_store)
    run_id = _launch(client, target_repo, roles.SIM)
    _wait(client, run_id, {"done"})

    events = _read_sse(client, f"/ui/runs/{run_id}/events")           # bounded: terminal closes it
    nodes = [e for e in events if e["event"] == "node_transition"]
    assert len(nodes) == 4                                            # dispatch, run_worker, gate, teardown
    assert all("phase-history" in e["data"] for e in nodes)          # a completed run: all history
    assert any(e["event"] == "step_metric" and "running $" in e["data"] for e in events)
    terminal = [e for e in events if e["event"] == "terminal"]
    assert terminal and "done" in terminal[0]["data"]
    assert "final-banner" in terminal[0]["data"]                     # OOB final banner


def test_midrun_replays_history_then_tails_live(mem_store, make_service, target_repo):
    client = make_service(mem_store)
    run_id = _launch(client, target_repo, roles.BURN)
    _wait(client, run_id, {"awaiting_gate"})

    # approve shortly after we start reading, so the paused stream tails to terminal
    def _approve_soon():
        time.sleep(0.4)
        client.post(f"/runs/{run_id}/approve")

    t = threading.Thread(target=_approve_soon)
    t.start()
    try:
        events = _read_sse(client, f"/ui/runs/{run_id}/events", timeout=20)
    finally:
        t.join(timeout=5)

    kinds = [(e["event"], e["data"]) for e in events]
    # history first: dispatch/run_worker replayed as durable history
    hist_nodes = [d for (name, d) in kinds if name == "node_transition" and "phase-history" in d]
    assert any("dispatch" in d for d in hist_nodes)
    assert any("run_worker" in d for d in hist_nodes)
    # a live-tail divider appears, then live-phase events (apply_burn) arrive
    assert any("ev-divider" in e["data"] for e in events)
    assert any(e["event"] == "node_transition" and "apply_burn" in e["data"]
               and "phase-live" in e["data"] for e in events)
    # terminal closes the stream with the final status
    terminal = [e for e in events if e["event"] == "terminal"]
    assert terminal and "applied" in terminal[0]["data"]


def test_reopen_after_restart_reconstructs_full_timeline(mem_store, make_service, target_repo):
    svc_a = make_service(mem_store)
    run_id = _launch(svc_a, target_repo, roles.SIM)
    _wait(svc_a, run_id, {"done"})

    # "restart": a fresh service (empty in-memory channels) over the SAME store.
    svc_b = make_service(mem_store)
    events = _read_sse(svc_b, f"/ui/runs/{run_id}/events")
    nodes = [e for e in events if e["event"] == "node_transition"]
    assert len(nodes) == 4                                            # full timeline, not just resume leg
    assert all("phase-history" in e["data"] for e in nodes)          # rebuilt from the durable store
    assert any(e["event"] == "step_metric" for e in events)
    assert any(e["event"] == "terminal" and "done" in e["data"] for e in events)
    # and the page reconstructs on a fresh service too
    assert "status-done" in svc_b.get(f"/ui/runs/{run_id}").text
