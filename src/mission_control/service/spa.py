"""Serving the single-page-app bundle built in a separate repo.

This is an ADDITIVE, opt-in surface: the seam only serves a bundle when the
operator points ``MC_SPA_DIST`` at an existing directory (see ``mount_spa``).
Nothing here touches orchestration, runs, telemetry, or the gate.

Two small pieces:

* :func:`configure_cors` — permit a browser origin allow-list read from
  ``MC_UI_DEV_ORIGINS`` (comma-separated; empty by default → no change).
* :func:`mount_spa` — mount the built static assets from ``MC_SPA_DIST`` with
  history (deep-link) fallback to ``index.html`` at a prefix that does not
  shadow any API route.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

# Env keys (repo-agnostic; no host/account/path baked in).
CORS_ORIGINS_ENV = "MC_UI_DEV_ORIGINS"
SPA_DIST_ENV = "MC_SPA_DIST"

# The prefix the SPA is served under. Chosen to NOT shadow any existing API
# route (/runs, /plans, /metrics, /targets, the legacy /ui* htmx surface,
# /openapi.json, or the "/" web root).
SPA_MOUNT_PATH = "/app"

# The methods/headers the SPA needs: JSON POSTs plus the SSE reconnect header.
_ALLOW_METHODS = ["GET", "POST", "OPTIONS"]
_ALLOW_HEADERS = ["Content-Type", "Accept", "Last-Event-ID"]


def _dev_origins(env: dict | None = None) -> list[str]:
    """The configured browser origin allow-list, or ``[]`` when unset/blank."""
    raw = (env or os.environ).get(CORS_ORIGINS_ENV, "")
    return [o.strip() for o in raw.split(",") if o.strip()]


def configure_cors(app: FastAPI, env: dict | None = None) -> None:
    """Permit the configured dev origins to reach the seam from a browser.

    No-op unless ``MC_UI_DEV_ORIGINS`` names at least one origin, so the default
    posture is unchanged. Never uses ``*`` and never enables credentials — this
    only relaxes the same-origin barrier for an explicitly allow-listed origin,
    it does not touch the loopback bind or the (absent) auth story.
    """
    origins = _dev_origins(env)
    if not origins:
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=_ALLOW_METHODS,
        allow_headers=_ALLOW_HEADERS,
    )


class _SpaStaticFiles(StaticFiles):
    """``StaticFiles`` with SPA history fallback: an unknown, extension-less path
    (a client-side route like ``/app/runs/abc``) resolves to ``index.html`` so
    the browser can boot the app and route on its own. Missing real assets still
    404 as usual."""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and not Path(path).suffix:
                return await super().get_response("index.html", scope)
            raise


def mount_spa(app: FastAPI, env: dict | None = None) -> bool:
    """Mount the built SPA bundle iff ``MC_SPA_DIST`` points at a real directory.

    Returns ``True`` when a bundle was mounted, ``False`` when the env is unset
    or the directory is missing (in which case the seam is byte-for-byte
    unchanged). The directory is whatever the operator points at — the bundle is
    produced in the SPA's own repo; nothing is baked in here.
    """
    raw = (env or os.environ).get(SPA_DIST_ENV, "").strip()
    if not raw:
        return False
    dist = Path(raw)
    if not dist.is_dir():
        return False
    app.mount(
        SPA_MOUNT_PATH,
        _SpaStaticFiles(directory=str(dist), html=True),
        name="spa",
    )
    return True
