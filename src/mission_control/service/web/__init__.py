"""The control-room web UI — Jinja + htmx, mounted on the same FastAPI app."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .routes import STATIC_DIR, router

__all__ = ["mount_web"]


def mount_web(app: FastAPI) -> None:
    """Attach the UI routes and static assets to an existing app."""
    app.mount("/ui/static", StaticFiles(directory=str(STATIC_DIR)), name="ui-static")
    app.include_router(router)
