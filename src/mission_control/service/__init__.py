"""The Mission Control service seam — a FastAPI wrapper over the graph.

The HTTP layer launches / resolves / streams / queries runs; it owns no
orchestration logic. See :func:`create_app` and :class:`RunManager`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

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
    # The planner engine walks INCEPTION interactively: the real LLM brain when the
    # service runs against the SDK, else the deterministic offline stub. Its
    # reverse-engineering step reuses the RunManager's launch path (run a real sim
    # against the target) — no second code-reading path.
    brain = SdkPlannerBrain() if use_sdk else StubPlannerBrain()
    engine = PlannerEngine(plan_store, brain=brain, sim_runner=manager)
    plan_manager = PlanManager(plan_store, engine=engine)

    # The builder hands a finalized plan to Mission Control: it translates units into
    # runs on the launch path and advances the build as each run terminates.
    builder = PlanBuilder(plan_store, manager)
    manager.set_run_observer(builder.on_run_terminal)
    return manager, plan_manager, builder, pool
