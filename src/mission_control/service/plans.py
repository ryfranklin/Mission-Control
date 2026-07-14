"""The plan manager: the PLAN seam's stateful piece.

It wraps the :class:`~mission_control.plans_store.PlanStore` and applies the
methodology's readiness rule. It adds NO orchestration to ``graph.py`` — it is a
client of the existing runtime that opens sessions, appends transcript turns,
serves the aggregate, and gates finalize on readiness.

The planner *engine* — the thing that reads an operator turn and accretes
requirements / lays down the work-list — lands in P2. Until then, appending a turn
records the operator's message and returns a deterministic placeholder reply, so
the transcript and the seam are exercisable end-to-end now.
"""

from __future__ import annotations

import os
from typing import Optional
from uuid import uuid4

from .. import aidlc, plan_docs, project_ref
from ..plans_store import (
    ROLE_OPERATOR,
    STATUS_FINALIZED,
    PlanRow,
    PlanStore,
    PlanTurn,
)
from ..aidlc_v2 import plan as v2plan
from .planner import DocsSync, DoneEvent, PlannerEngine, _v2_catalog_for

# Instance defaults: env, themselves defaulting to the methodology's own defaults.
# Read lazily (at manager construction) so tests can set the env per case.
DEFAULT_METHODOLOGY = "aidlc"
DEFAULT_CLOUD = "aws"


class PlanNotFound(Exception):
    """No plan with the given id in the store."""


class PlanNotReady(Exception):
    """Finalize was refused because the readiness rule did not pass."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PlanConflict(Exception):
    """The request isn't valid for the plan (e.g. an unknown mode)."""


class PlanManager:
    """Opens / advances / serves / finalizes plans over the PLAN store."""

    def __init__(
        self,
        store: PlanStore,
        *,
        engine: Optional[PlannerEngine] = None,
        methodology: Optional[str] = None,
        cloud_target: Optional[str] = None,
        docs_sync: Optional[DocsSync] = None,
        cache_root: Optional[str] = None,
    ) -> None:
        self._store = store
        self._engine = engine or PlannerEngine(store)
        self._methodology = methodology or os.environ.get(
            "MC_PLANNER_METHODOLOGY"
        ) or DEFAULT_METHODOLOGY
        self._cloud = cloud_target or os.environ.get("MC_PLANNER_CLOUD") or DEFAULT_CLOUD
        # Persists plan docs to git at finalize (and load_from_git rebuilds the cache
        # from git). None → no git sync (offline / unit tests).
        self._docs_sync = docs_sync
        self._cache_root = cache_root

    @property
    def methodology_default(self) -> str:
        """The instance's default methodology (shown as an editable default in the UI)."""
        return self._methodology

    @property
    def cloud_default(self) -> str:
        """The instance's default cloud target (shown as an editable default in the UI)."""
        return self._cloud

    # -- open --------------------------------------------------------------

    def open_plan(
        self,
        *,
        target: Optional[str],
        mode: str,
        methodology: Optional[str] = None,
        cloud_target: Optional[str] = None,
        workstream: Optional[str] = None,
        remote_dest: Optional[str] = None,
        allow_secrets: bool = False,
    ) -> PlanRow:
        """Open a planning session. methodology/cloud fall back to the instance
        defaults (env → 'aidlc'/'aws') unless overridden per request. An optional
        ``workstream`` makes the build reconcile through the mc/ws/<name> branch;
        ``remote_dest`` is the greenfield remote to bootstrap (create + push) at build
        start so the project is portable from unit 1."""
        if mode not in aidlc.MODES:
            raise PlanConflict(f"mode must be one of {aidlc.MODES}")
        plan_id = f"plan-{uuid4().hex}"
        # Store the PORTABLE identity (a normalized remote ref) as target; keep the
        # derived local working dir separately. A blank target (greenfield "new")
        # stays None until the build scaffolds a workspace.
        ref, local_path = (None, None)
        if target:
            r, lp = project_ref.resolve_target(target)
            ref, local_path = r, (str(lp) if lp is not None else None)
        self._store.open_plan(
            plan_id,
            target=ref,
            local_path=local_path,
            workstream=(workstream or None),
            remote_dest=(remote_dest or None),
            allow_secrets=bool(allow_secrets),
            mode=mode,
            methodology=methodology or self._methodology,
            cloud_target=cloud_target or self._cloud,
        )
        return self._require(plan_id)

    # -- transcript (engine-driven) ----------------------------------------

    def append_turn(self, plan_id: str, content: str) -> PlanTurn:
        """Drive the planner engine for one operator turn and return the persisted
        planner reply. Fully consumes the engine's token stream (the non-streaming
        path); use :meth:`stream_turn` for token-by-token SSE."""
        done = None
        for event in self.stream_turn(plan_id, content):
            if isinstance(event, DoneEvent):
                done = event
        assert done is not None, "engine did not emit a terminal event"
        return done.turn

    def stream_turn(self, plan_id: str, content: str):
        """Drive one operator turn, yielding the engine's events (token / stage /
        done) as they occur — the SSE token feed. Guards plan existence and the
        finalized lock before touching the engine."""
        plan = self._require(plan_id)
        if plan.status == STATUS_FINALIZED:
            raise PlanConflict("plan is finalized — no more turns")
        return self._engine.run_turn(plan_id, content)

    def record_operator_turn(self, plan_id: str, content: str) -> PlanTurn:
        """Append JUST the operator's turn (no reply yet) — the streaming-UI flow posts
        the turn, then streams the reply over a separate SSE connection. Guards
        existence + the finalized lock."""
        plan = self._require(plan_id)
        if plan.status == STATUS_FINALIZED:
            raise PlanConflict("plan is finalized — no more turns")
        return self._store.append_turn(plan_id, ROLE_OPERATOR, content)

    def stream_reply(self, plan_id: str):
        """Generate + stream the planner's reply to the latest (already-recorded)
        operator turn — the UI's SSE reply feed. A no-op if already answered."""
        self._require(plan_id)
        return self._engine.reply_for_latest(plan_id)

    # -- finalize (readiness-gated) ----------------------------------------

    def finalize(self, plan_id: str) -> PlanRow:
        """Lock the plan. Succeeds ONLY when EVERY readiness criterion is met
        (greenfield: the always-execute INCEPTION stages are laid down; brownfield: the
        requirements-readiness gate — scope, components, acceptance, well-formed units).
        An already-finalized plan is a no-op."""
        plan = self._require(plan_id)
        if plan.status == STATUS_FINALIZED:
            return plan
        report = self.readiness(plan_id)
        if not aidlc.is_ready(report):
            raise PlanNotReady(aidlc.unmet_summary(report))
        self._store.set_status(plan_id, STATUS_FINALIZED)
        # Land the finalized plan on the remote — the authoritative INCEPTION artifact
        # the build (and any other host) reads. Must reach the remote (raises on push
        # failure); a no-op when there's no git sync wired or no portable target.
        if self._docs_sync is not None:
            self._docs_sync(plan_id)
        return self._require(plan_id)

    # -- rebuild the cache from git (a fresh host doesn't start from scratch) ---

    def load_from_git(self, target: str) -> PlanRow:
        """Reconstruct a plan for ``target`` from its committed git plan docs, GIT
        AUTHORITATIVE: acquire (clone/fetch), read ``aidlc-docs/inception/``, and
        reconcile the Postgres cache to it. On a fresh host (empty Postgres) this fully
        rebuilds the plan from the repo — no re-running of INCEPTION. Raises
        :class:`PlanNotFound` when the repo carries no plan docs."""
        plan_id = plan_docs.load_from_repo(
            self._store, target, cache_root=self._cache_root,
            methodology=self._methodology, cloud_target=self._cloud,
            plan_id_factory=lambda: f"plan-{uuid4().hex}",
        )
        if plan_id is None:
            raise PlanNotFound(f"no plan documents found in the repo for target {target!r}")
        return self._require(plan_id)

    def readiness(self, plan_id: str):
        """The plan's explicit finalize criteria, each flagged met/unmet — surfaced in
        ``GET /plans/{id}`` so the UI can show what is still blocking finalize.

        A v2 target's gate = every applicable ``kind=="plan"`` stage laid down + a
        non-empty, well-formed work-list (reusing the shared readiness machinery); any
        other target keeps the built-in greenfield/brownfield rule, unchanged."""
        plan = self._require(plan_id)
        units = self._store.list_units(plan_id)
        catalog = _v2_catalog_for(plan)
        if catalog is not None:
            completed = {u.stage_slug for u in units
                         if u.phase == aidlc.Phase.INCEPTION.value and u.stage_slug}
            build = [u for u in units
                     if u.stage_slug and u.phase != aidlc.Phase.INCEPTION.value]
            return v2plan.readiness(catalog, mode=plan.mode, scope=None,
                                    completed_slugs=completed, units=build)
        requirements = self._store.list_requirements(plan_id)
        inception = [u.title for u in units if u.phase == aidlc.Phase.INCEPTION.value]
        return aidlc.readiness_report(
            plan.mode, inception_stages=inception, requirements=requirements, units=units)

    # -- queries -----------------------------------------------------------

    def get_plan(self, plan_id: str) -> PlanRow:
        return self._require(plan_id)

    def aggregate(self, plan_id: str):
        """The full plan: ``(row, turns, requirements, units, readiness)``."""
        plan = self._require(plan_id)
        return (
            plan,
            self._store.list_turns(plan_id),
            self._store.list_requirements(plan_id),
            self._store.list_units(plan_id),
            self.readiness(plan_id),
        )

    def list_plans(
        self,
        *,
        status: Optional[str] = None,
        mode: Optional[str] = None,
        target: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        order: str = "desc",
    ) -> tuple[list[PlanRow], int]:
        filt = {"status": status, "mode": mode, "target": target}
        rows = self._store.list_plans(filt, limit=limit, offset=offset, order=order)
        total = self._store.count_plans(filt)
        return rows, total

    # -- helpers -----------------------------------------------------------

    def _require(self, plan_id: str) -> PlanRow:
        plan = self._store.get_plan(plan_id)
        if plan is None:
            raise PlanNotFound(plan_id)
        return plan
