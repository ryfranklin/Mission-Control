"""The Mission Control service seam — a FastAPI wrapper over the graph.

The HTTP layer launches / resolves / streams / queries runs; it owns no
orchestration logic. See :func:`create_app` and :class:`RunManager`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..graph import DEFAULT_TELEMETRY_DIR, build_runs_store, postgres_checkpointer
from .app import create_app
from .manager import RunConflict, RunManager, RunNotFound

__all__ = [
    "create_app",
    "RunManager",
    "RunNotFound",
    "RunConflict",
    "build_default_manager",
]


def build_default_manager(
    *,
    telemetry_dir: Optional[Path] = None,
    use_sdk: bool = False,
):
    """A production manager: a durable PostgresSaver checkpointer + the runs ledger
    over the SAME pool (docker-compose Postgres). Returns ``(manager, pool)`` —
    close the pool on shutdown. ``use_sdk`` swaps the StubWorker for the real
    SdkWorker."""
    checkpointer, pool = postgres_checkpointer(setup=True)
    store = build_runs_store(pool, setup=True)

    if use_sdk:
        from ..sdk_worker import SdkWorker

        worker_factory = lambda: SdkWorker()  # noqa: E731
    else:
        from ..worker import StubWorker

        worker_factory = lambda: StubWorker()  # noqa: E731

    manager = RunManager(
        checkpointer=checkpointer,
        runs_store=store,
        worker_factory=worker_factory,
        telemetry_dir=telemetry_dir if telemetry_dir is not None else DEFAULT_TELEMETRY_DIR,
    )
    return manager, pool
