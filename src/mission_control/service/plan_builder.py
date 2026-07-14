"""The plan builder — hands a finalized Flight Plan to Mission Control.

On finalize (readiness met), a plan's ``plan_units`` are translated into runs and
dispatched on the EXISTING launch path — this adds NO new orchestration:

* INCEPTION / validation units (``task_type == sim``) → **sim** runs (read-only);
* CONSTRUCTION / change units (``task_type == burn``) → **burn** runs behind the
  normal go/no-go gate.

``task_type`` is already on each unit (via :func:`aidlc.task_type_for_phase`), so the
builder just reads it.

**Committed unit status is the authority for what to dispatch** (the portable-progress
record): a unit is runnable only if its status != ``done`` and every unit in its
``depends_on`` is ``done``. That status lives in flight-plan.yaml (Prompt 4), reconciled
into Postgres on load — so a build resumed on a DIFFERENT machine (empty Postgres, git
reconciled) dispatches exactly the not-done, dependency-satisfied units, never re-running
completed ones. The host-local in-flight LangGraph checkpoint is deliberately NOT
portable: the worst case on a host swap is that one mid-flight unit re-runs from the
start (a re-run of one unit never cascades into rebuilding completed ones — those are
``done`` in git).

It is edge-triggered: :meth:`start_build` (and :meth:`resume_builds` on restart)
dispatch the runnable units; each time a child run reaches a terminal state the
RunManager calls :meth:`on_run_terminal` (on the loop), which — on SUCCESS — marks the
unit ``done`` and pushes that to git, then dispatches any newly-unblocked units and
rolls the plan's status up (``building`` → ``done``). A no-go/failed run does NOT advance
status; its dependents stay blocked, but the plan itself continues.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .. import aidlc, plan_docs, repo_source, roles, worktree
from ..plans_store import (
    STATUS_BUILDING,
    STATUS_DONE,
    UNIT_DONE,
    PlanStore,
)
from ..runs_store import (
    STATUS_APPLIED,
    STATUS_DONE as RUN_STATUS_DONE,
    STATUS_FAILED,
    STATUS_MERGE_CONFLICT,
    STATUS_PUSH_REJECTED,
    STATUS_SCRUBBED,
)

# A dependency counts as satisfied only when its run SUCCEEDED (a sim done, a burn
# applied on a go AND pushed). A scrubbed / failed / push-rejected dep blocks its
# dependents — a push that didn't land means the change isn't on the trunk the next
# unit would build off, so building on it would diverge (full conflict handling later).
_SUCCESS = frozenset({RUN_STATUS_DONE, STATUS_APPLIED})
_FAILED = frozenset({STATUS_SCRUBBED, STATUS_FAILED, STATUS_PUSH_REJECTED, STATUS_MERGE_CONFLICT})


class PlanBuilder:
    """Schedules a plan's units onto the run launch path, in dependency order."""

    def __init__(self, plan_store: PlanStore, run_manager, *, workspaces_dir=None,
                 docs_sync=None, cache_root=None) -> None:
        self._plans = plan_store
        self._runs = run_manager
        # Persists a unit's ``done`` mark to git (write flight-plan.yaml + commit +
        # push) — the portable progress record. None → no git sync (offline / tests).
        self._docs_sync = docs_sync
        # Where remotes are cached/acquired (ensure_local). None → project_ref default.
        # Kept consistent with docs_sync's cache root so both hit the same clone.
        self._cache_root = Path(cache_root) if cache_root else None
        # EPHEMERAL scratch for greenfield bootstrap ONLY — the initial commit is staged
        # here then pushed to the created remote and the dir is discarded. It is NEVER the
        # durable target: after bootstrap the plan's ``target`` is the pushed remote's ref
        # and the working copy is the acquired cache clone.
        self._workspaces = Path(
            workspaces_dir or os.environ.get("MC_PLAN_WORKSPACES", "plan-workspaces")
        )

    # -- kickoff (called from the finalize endpoint, on the loop) ----------

    async def start_build(self, plan_id: str) -> None:
        """Move the plan to ``building`` and dispatch its runnable units. Async so it
        runs on the event loop (the launch path spawns background drives there).

        A greenfield plan (no target yet) is BOOTSTRAPPED first: an operator-supplied
        remote destination is created + seeded (initial commit with aidlc-docs/) + pushed,
        the plan's ``target`` is set to the resulting portable ref, and the remote is
        acquired locally. After that there is NO greenfield/brownfield difference — both
        are "a repo with a remote" and run the same acquire → worktree → gate → push path.
        Greenfield with no destination fails loudly (never an anonymous local-only dir)."""
        plan = self._plans.get_plan(plan_id)
        if plan is None:
            return
        if plan.mode == aidlc.MODE_GREENFIELD and not plan.target:
            self._bootstrap_greenfield(plan_id, plan)
        self._plans.set_status(plan_id, STATUS_BUILDING)
        self._advance(plan_id)

    def _bootstrap_greenfield(self, plan_id: str, plan) -> None:
        """Create the project's remote so identity + durability exist from unit 1. The
        operator's destination comes from the plan (``remote_dest``) or the injected
        ``MC_GREENFIELD_REMOTE`` env — never a hardcoded host/org. Seeds the initial
        commit with the current plan docs, pushes, then records the portable ref as the
        plan's ``target`` and acquires the remote locally as the build's working copy."""
        dest = plan.remote_dest or os.environ.get("MC_GREENFIELD_REMOTE")
        if not dest:
            raise repo_source.BootstrapError(
                "greenfield build requires a remote destination (plan remote_dest / "
                "MC_GREENFIELD_REMOTE); refusing a non-portable local-only workspace")
        scratch = self._workspaces / plan_id
        try:
            self._seed_scratch(plan_id, scratch)          # README + aidlc-docs/ (the plan)
            ref = repo_source.bootstrap_remote(dest, scratch,
                                               allow_secrets=bool(plan.allow_secrets))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)    # scratch is ephemeral
        self._plans.set_target(plan_id, ref)              # the portable identity now exists
        local = repo_source.ensure_local(ref, root=self._cache_root)  # acquire the remote
        self._plans.set_local_path(plan_id, str(local))

    def _seed_scratch(self, plan_id: str, scratch: Path) -> None:
        """Populate the ephemeral bootstrap scratch with the initial commit's content:
        the plan docs (so the created remote carries aidlc-docs/inception/ from birth)."""
        scratch.mkdir(parents=True, exist_ok=True)
        plan_docs.dump_plan(plan_docs.plan_doc_from_store(self._plans, plan_id),
                            plan_docs.docs_dir(scratch))

    # -- restart durability: resume in-flight builds -----------------------

    async def resume_builds(self) -> None:
        """Re-advance every plan left ``building`` by a previous process. The plan
        store is durable operational memory, so on restart the units + their child runs
        survive; this re-dispatches any unit whose dependencies have since (durably)
        succeeded but that wasn't dispatched before the crash. Burns paused at the gate
        resume normally on approve. Called once on service startup (on the loop)."""
        for plan in self._plans.list_plans({"status": STATUS_BUILDING}, limit=1000):
            self._advance(plan.id)

    # -- edge trigger: a child run terminated ------------------------------

    def on_run_terminal(self, run_id: str, plan_id: str) -> None:
        """A plan-child run reached a terminal state. On SUCCESS, mark its unit ``done``
        and push that to git (the portable progress record) BEFORE dispatching more, so
        the durable record is updated before anything builds on it. Then dispatch
        newly-unblocked units and roll the plan up. Registered as the run observer."""
        run = self._runs.get_run(run_id)
        if run is not None and run.plan_unit_seq is not None and run.status in _SUCCESS:
            self._mark_unit_done(plan_id, run.plan_unit_seq)
        self._advance(plan_id)

    def _mark_unit_done(self, plan_id: str, seq: int) -> None:
        """Mark a unit ``done`` and persist that to git (write flight-plan.yaml + commit
        + push). Idempotent: re-marking an already-``done`` unit (e.g. a crash between the
        merge/push and the status write) is a no-op — no redundant commit. The status
        write + commit + push is one logical step (:func:`plan_docs.sync_to_repo`); a
        rejected push surfaces from there rather than leaving git silently behind."""
        unit = next((u for u in self._plans.list_units(plan_id) if u.seq == seq), None)
        if unit is None or unit.status == UNIT_DONE:
            return  # unknown, or already recorded done → nothing to do
        self._plans.set_unit_status(plan_id, seq, UNIT_DONE)
        if self._docs_sync is not None:
            self._docs_sync(plan_id)

    # -- the scheduler -----------------------------------------------------

    def _advance(self, plan_id: str) -> None:
        plan = self._plans.get_plan(plan_id)
        if plan is None or plan.status == STATUS_DONE:
            return
        units = self._plans.list_units(plan_id)

        # No runnable git repo to host worktrees (a brownfield target that vanished, or
        # a scaffold that failed) → nothing to dispatch onto; the build is trivially
        # complete. Guarding on "is it a repo?" (not just "is it a dir?") stops a
        # non-git target from cascading into a pile of failed runs.
        target = plan.working_path  # the LOCAL working dir worktrees are carved from
        if not _is_git_repo(target):
            self._plans.set_status(plan_id, STATUS_DONE)
            return

        # COMMITTED unit status is the authority for "done" (portable across machines);
        # the in-session child runs only tell us what is already dispatched / has failed
        # THIS session (so we don't re-dispatch an in-flight unit or one that will never
        # finish). done_seqs comes from git-reconciled status, not from local runs.
        by_seq = {r.plan_unit_seq: r for r in self._runs.child_runs(plan_id)}
        done: set = set()
        for unit in units:
            if unit.status == UNIT_DONE:
                done.add(unit.seq)
            elif (r := by_seq.get(unit.seq)) is not None and r.status in _SUCCESS:
                # A successful run whose done-mark was lost (crash between the run and the
                # status write): heal it — mark done + push. Idempotent on re-entry.
                self._mark_unit_done(plan_id, unit.seq)
                done.add(unit.seq)
        dead = self._dead_seqs(units, by_seq)  # own run failed, or a dep is dead

        for unit in units:  # units come back in seq order
            if unit.seq in done or unit.seq in by_seq or unit.seq in dead:
                continue  # already done (git) / dispatched this session / will never run
            if not all(dep in done for dep in (unit.depends_on or [])):
                continue  # a dependency isn't done yet → not (yet) dispatchable
            self._dispatch(plan_id, target, unit)
            by_seq = {r.plan_unit_seq: r for r in self._runs.child_runs(plan_id)}

        if self._all_resolved(units, done, dead):
            self._plans.set_status(plan_id, STATUS_DONE)

    def _dispatch(self, plan_id: str, target: str, unit) -> None:
        plan = self._plans.get_plan(plan_id)
        self._runs.launch(
            target=target, task_type=unit.task_type, prompt=_prompt_for(unit),
            plan_id=plan_id, plan_unit_seq=unit.seq,
            workstream=plan.workstream if plan else None,
            allow_secrets=bool(plan.allow_secrets) if plan else False,
        )

    # -- dependency logic --------------------------------------------------

    @staticmethod
    def _all_resolved(units, done: set, dead: set) -> bool:
        """The plan is done when every unit is resolved: it is committed ``done``, or it
        is dead (its own run failed, or it's transitively blocked so it will never run).
        A not-done, not-dead unit means there is still work in flight or to dispatch."""
        return all(u.seq in done or u.seq in dead for u in units)

    @staticmethod
    def _dead_seqs(units, by_seq) -> set:
        """Units that will never reach ``done``: seeded by a unit whose OWN run failed
        this session (a no-go/scrubbed/failed/push-rejected run), then propagated to
        every dependent. (A committed-``done`` unit is never dead — its run succeeded.)"""
        dead: set = {u.seq for u in units
                     if (r := by_seq.get(u.seq)) is not None and r.status in _FAILED}
        changed = True
        while changed:
            changed = False
            for unit in units:
                if unit.seq in dead:
                    continue
                if any(dep in dead for dep in (unit.depends_on or [])):
                    dead.add(unit.seq)
                    changed = True
        return dead


def _is_git_repo(target) -> bool:
    """True only when ``target`` is its OWN git root — delegates to the shared,
    safety-critical check in :mod:`mission_control.worktree` (never a parent repo)."""
    return worktree.is_git_repo(target)


def _prompt_for(unit) -> str:
    """The instruction handed to the worker for a unit's run."""
    if unit.task_type == roles.SIM:
        return f"Validate the planned INCEPTION work: {unit.title}"
    return f"Implement the planned change: {unit.title}"
