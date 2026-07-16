"""The Mission Control service seam — a FastAPI wrapper over the graph.

The HTTP layer launches / resolves / streams / queries runs; it owns no
orchestration logic. See :func:`create_app` and :class:`RunManager`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# The SERVICE defaults its build worker to the strongest tier — a coarse CONSTRUCTION
# unit needs the headroom to finish in-budget with quality output. Overridable per
# service via MC_WORKER_MODEL. (The low-level sdk_worker.DEFAULT_MODEL stays cheap for
# the eval harness, whose judge must remain a stronger tier than the worker.)
SERVICE_WORKER_MODEL = "claude-opus-4-8"

from ..graph import (
    DEFAULT_TELEMETRY_DIR,
    build_plans_store,
    build_runs_store,
    postgres_checkpointer,
)
from .app import create_app
from .manager import RunConflict, RunManager, RunNotFound
from .plan_builder import PlanBuilder
from .planner import PlannerEngine, SdkPlannerBrain, StubPlannerBrain
from .plans import PlanConflict, PlanManager, PlanNotFound, PlanNotReady

__all__ = [
    "create_app",
    "RunManager",
    "RunNotFound",
    "RunConflict",
    "PlanManager",
    "PlanNotFound",
    "PlanNotReady",
    "PlanConflict",
    "PlannerEngine",
    "StubPlannerBrain",
    "SdkPlannerBrain",
    "PlanBuilder",
    "build_default_manager",
]


def build_default_manager(
    *,
    telemetry_dir: Optional[Path] = None,
    use_sdk: bool = False,
):
    """A production manager: a durable PostgresSaver checkpointer + the runs ledger
    + the PLAN store, all over the SAME pool (docker-compose Postgres). Returns
    ``(manager, plan_manager, builder, pool)`` — close the pool on shutdown.
    ``use_sdk`` swaps the StubWorker for the real SdkWorker."""
    checkpointer, pool = postgres_checkpointer(setup=True)
    store = build_runs_store(pool, setup=True)
    plan_store = build_plans_store(pool, setup=True)

    if use_sdk:
        from ..sdk_worker import SdkWorker

        # Service build worker defaults to Opus (MC_WORKER_MODEL overrides).
        model = os.environ.get("MC_WORKER_MODEL") or SERVICE_WORKER_MODEL
        worker_factory = lambda: SdkWorker(model=model)  # noqa: E731
    else:
        from ..worker import StubWorker

        worker_factory = lambda: StubWorker()  # noqa: E731

    manager = RunManager(
        checkpointer=checkpointer,
        runs_store=store,
        worker_factory=worker_factory,
        telemetry_dir=telemetry_dir if telemetry_dir is not None else DEFAULT_TELEMETRY_DIR,
    )
    # The planner engine walks INCEPTION interactively: the real LLM brain when the
    # service runs against the SDK, else the deterministic offline stub. Its
    # reverse-engineering step reuses the RunManager's launch path (run a real sim
    # against the target) — no second code-reading path.
    brain = SdkPlannerBrain() if use_sdk else StubPlannerBrain()
    # Persist the plan (INCEPTION output) into the target repo as committed git docs at
    # each checkpoint + finalize, so it travels with the project and Postgres is just a
    # rebuildable cache (git authoritative). See plan_docs.
    from .. import plan_docs
    docs_sync = lambda pid: plan_docs.sync_to_repo(plan_store, pid)  # noqa: E731
    engine = PlannerEngine(plan_store, brain=brain, sim_runner=manager, docs_sync=docs_sync)
    plan_manager = PlanManager(plan_store, engine=engine, docs_sync=docs_sync)

    # The builder hands a finalized plan to Mission Control: it translates units into
    # runs on the launch path and advances the build as each run terminates. It marks a
    # unit ``done`` in git (docs_sync) on each success, so build progress is portable, and
    # bootstraps a greenfield plan's remote at build start (same default cache root).
    builder = PlanBuilder(plan_store, manager, docs_sync=docs_sync)
    manager.set_run_observer(builder.on_run_terminal)
    return manager, plan_manager, builder, pool
