"""The server-rendered control-room UI (Jinja + htmx) over the same app.

Rendered HTML is asserted with the TestClient (no JS execution). The durability
substrate is the in-memory store from conftest, so these run with no Docker."""

from __future__ import annotations

import time

from mission_control import roles


def _launch(client, target, task_type=roles.SIM) -> str:
    r = client.post("/runs", json={"target": str(target), "task_type": task_type})
    assert r.status_code == 201, r.text
    return r.json()["run_id"]


def _wait_listed(client, run_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get(f"/runs/{run_id}").status_code == 200:
            return
        time.sleep(0.02)


# -- fleet dashboard -------------------------------------------------------

def test_fleet_page_renders_rows_for_runs(mem_store, make_service, target_repo):
    client = make_service(mem_store)
    run_id = _launch(client, target_repo, roles.BURN)
    _wait_listed(client, run_id)

    html = client.get("/ui").text
    assert "<table class=\"fleet\"" in html
    assert str(target_repo) in html                     # the Controller's station
    assert f"badge-{roles.BURN}" in html                # sim/burn badge
    assert "status-" in html                            # color-coded status pill
    assert f"/ui/runs/{run_id}" in html                 # row links to the detail page

    # the polled fragment endpoint returns just the table (+ an OOB live count)
    frag = client.get("/ui/fleet").text
    assert "<table class=\"fleet\"" in frag
    assert 'hx-swap-oob="true"' in frag
    assert "<html" not in frag                          # fragment, not a full page


def test_fleet_paginates(mem_store, make_service, target_repo, monkeypatch):
    monkeypatch.setattr("mission_control.service.web.routes.PAGE_SIZE", 2)
    client = make_service(mem_store)
    ids = []
    for _ in range(3):
        ids.append(_launch(client, target_repo, roles.SIM))
        time.sleep(0.02)
    for rid in ids:
        _wait_listed(client, rid)

    page0 = client.get("/ui?page=0").text
    assert "3 run(s)" in page0                           # total surfaced for paging
    assert ids[2] in page0 and ids[1] in page0 and ids[0] not in page0   # newest 2
    assert 'href="/ui?page=1">next' in page0 and "next &rarr;" in page0
    # prev is disabled on the first page
    assert 'class="disabled" href="/ui?page=-1">&larr; prev' in page0

    page1 = client.get("/ui?page=1").text
    assert ids[0] in page1 and ids[2] not in page1       # the oldest, on page 2
    assert 'class="disabled"' not in page1.split("prev")[0][-40:]  # prev now enabled


# -- launch control --------------------------------------------------------

def test_launch_form_redirects_to_run_page(mem_store, make_service, target_repo):
    client = make_service(mem_store)
    resp = client.post("/ui/launch",
                       data={"target": str(target_repo), "task_type": roles.SIM},
                       follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/ui/runs/run-")

    detail = client.get(location)
    assert detail.status_code == 200
    assert str(target_repo) in detail.text
    assert location.rsplit("/", 1)[-1] in detail.text    # the run id


def test_launch_bad_target_is_400(mem_store, make_service, tmp_path):
    client = make_service(mem_store)
    resp = client.post("/ui/launch",
                       data={"target": str(tmp_path / "nope"), "task_type": roles.SIM},
                       follow_redirects=False)
    assert resp.status_code == 400


def test_targets_endpoint_feeds_the_selector(mem_store, make_service, target_repo):
    client = make_service(mem_store)
    _launch(client, target_repo, roles.SIM)
    assert str(target_repo) in client.get("/targets").json()["targets"]
    # the datalist on the fleet page is populated from it
    assert f'<option value="{target_repo}">' in client.get("/ui").text


# -- metaphor comes from roles.py -----------------------------------------

def test_labels_are_pulled_from_roles(mem_store, make_service, monkeypatch):
    client = make_service(mem_store)

    # default vocabulary shows through
    assert roles.ORCHESTRATOR in client.get("/ui").text

    # swap terms in roles.py → the UI text changes (nothing hardcoded in templates)
    monkeypatch.setattr("mission_control.roles.ORCHESTRATOR", "Launch Commander")
    monkeypatch.setattr("mission_control.roles.WORKER", "Ground Crew")
    html = client.get("/ui").text
    assert "Launch Commander" in html
    assert "Ground Crew" in html
    assert "Flight Director" not in html                 # old term gone
