"""The one additive backend touch for the control-room SPA (built in a separate
repo): env-configured CORS for a dev origin, and an opt-in production static
mount for the built bundle.

Both are host-runnable with no Docker/LLM: the service is constructed via its
factory over the in-memory store (see ``make_service`` in conftest). Nothing
here exercises orchestration — the point is that the seam is UNCHANGED unless
the operator opts in via ``MC_UI_DEV_ORIGINS`` / ``MC_SPA_DIST``.
"""

from __future__ import annotations

from mission_control import roles

ALLOWED_ORIGIN = "http://localhost:5173"
OTHER_ORIGIN = "http://localhost:9999"


# -- CORS: env-configured dev-origin allow-list ----------------------------

def test_cors_reflects_allowed_configured_origin(
    mem_store, make_service, monkeypatch
):
    monkeypatch.setenv("MC_UI_DEV_ORIGINS", ALLOWED_ORIGIN)
    client = make_service(mem_store)

    # A simple cross-origin GET from the allow-listed origin gets the header back.
    resp = client.get("/targets", headers={"Origin": ALLOWED_ORIGIN})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN

    # And a preflight for the SPA's POST + SSE-reconnect header is permitted.
    pre = client.options(
        "/runs",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,last-event-id",
        },
    )
    assert pre.status_code == 200
    assert pre.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
    assert "POST" in pre.headers.get("access-control-allow-methods", "")


def test_cors_does_not_reflect_disallowed_origin(
    mem_store, make_service, monkeypatch
):
    monkeypatch.setenv("MC_UI_DEV_ORIGINS", ALLOWED_ORIGIN)
    client = make_service(mem_store)

    resp = client.get("/targets", headers={"Origin": OTHER_ORIGIN})
    assert resp.status_code == 200
    # The disallowed origin is neither reflected nor answered with a wildcard.
    acao = resp.headers.get("access-control-allow-origin")
    assert acao != OTHER_ORIGIN
    assert acao != "*"


def test_cors_absent_when_unconfigured(mem_store, make_service, monkeypatch):
    # Default posture: no allow-list env → no CORS headers at all (unchanged seam).
    monkeypatch.delenv("MC_UI_DEV_ORIGINS", raising=False)
    client = make_service(mem_store)

    resp = client.get("/targets", headers={"Origin": ALLOWED_ORIGIN})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_cors_supports_comma_separated_allow_list(
    mem_store, make_service, monkeypatch
):
    monkeypatch.setenv("MC_UI_DEV_ORIGINS", f"{ALLOWED_ORIGIN}, {OTHER_ORIGIN}")
    client = make_service(mem_store)

    for origin in (ALLOWED_ORIGIN, OTHER_ORIGIN):
        resp = client.get("/targets", headers={"Origin": origin})
        assert resp.headers.get("access-control-allow-origin") == origin


# -- static bundle: opt-in, no-op by default -------------------------------

def test_spa_mount_is_noop_when_unset(mem_store, make_service, monkeypatch):
    monkeypatch.delenv("MC_SPA_DIST", raising=False)
    client = make_service(mem_store)

    # Nothing mounted under the SPA prefix; the seam is byte-for-byte unchanged.
    assert client.get("/app/").status_code == 404
    # The existing web root and API still answer.
    assert client.get("/").status_code == 200
    assert client.get("/targets").status_code == 200


def test_spa_mount_is_noop_when_dir_missing(
    mem_store, make_service, monkeypatch, tmp_path
):
    monkeypatch.setenv("MC_SPA_DIST", str(tmp_path / "does-not-exist"))
    client = make_service(mem_store)

    assert client.get("/app/").status_code == 404
    assert client.get("/targets").status_code == 200


def test_spa_bundle_served_without_shadowing_api(
    mem_store, make_service, monkeypatch, tmp_path, target_repo
):
    dist = tmp_path / "spa-dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>MC SPA</title>")
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("console.log('mc');")

    monkeypatch.setenv("MC_SPA_DIST", str(dist))
    client = make_service(mem_store)

    # The bundle is served at its prefix, including a real hashed asset.
    index = client.get("/app/")
    assert index.status_code == 200
    assert "MC SPA" in index.text
    asset = client.get("/app/assets/app.js")
    assert asset.status_code == 200
    assert "console.log" in asset.text

    # SPA history fallback: an unknown client-side route yields index.html so the
    # browser can boot and route itself (a real missing asset still 404s).
    deep = client.get("/app/runs/some-client-route")
    assert deep.status_code == 200
    assert "MC SPA" in deep.text
    assert client.get("/app/assets/missing.js").status_code == 404

    # No shadowing: every existing API/UI surface still resolves as before.
    assert client.get("/").status_code == 200
    assert client.get("/ui").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/targets").status_code == 200
    launched = client.post(
        "/runs", json={"target": str(target_repo), "task_type": roles.SIM}
    )
    assert launched.status_code == 201
