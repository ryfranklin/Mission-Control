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
import subprocess
from pathlib import Path

from .. import aidlc, roles, worktree
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
                 docs_sync=None) -> None:
        self._plans = plan_store
        self._runs = run_manager
        # Persists a unit's ``done`` mark to git (write flight-plan.yaml + commit +
        # push) — the portable progress record. None → no git sync (offline / tests).
        self._docs_sync = docs_sync
        # SCRATCH area for a not-yet-created greenfield repo (one dir per plan). This is
        # NOT a durable identity — it never becomes the plan's portable ``target`` (it's
        # a machine-local path, the very thing portability retired). A greenfield build
        # gets a real remote in a later slice; until then it runs locally off this
        # scratch clone with no portable ref (docs sync is a no-op without a target).
        self._workspaces = Path(
            workspaces_dir or os.environ.get("MC_PLAN_WORKSPACES", "plan-workspaces")
        )

    # -- kickoff (called from the finalize endpoint, on the loop) ----------

    async def start_build(self, plan_id: str) -> None:
        """Move the plan to ``building`` and dispatch its deps-free units. Async so it
        runs on the event loop (the launch path spawns background drives there).

        A greenfield build needs a **git repo** to host worktrees — being a directory
        is not enough. So if the target isn't a usable repo we make it one before
        dispatching: an existing (e.g. empty) directory is ``git init``-ed in place (the
        operator pointed the build there); a missing / absent target gets a fresh
        scaffolded workspace. This closes the failure where an empty non-git target
        passed the old "is it a dir?" check and every run then failed at worktree
        creation."""
        plan = self._plans.get_plan(plan_id)
        if plan is None:
            return
        if plan.mode == aidlc.MODE_GREENFIELD and not _is_git_repo(plan.working_path):
            base = (
                Path(plan.working_path) if _is_dir(plan.working_path)
                else self._workspaces / plan_id
            )
            repo = self._ensure_repo(base)
            # Record ONLY the derived working dir (scratch). Deliberately NOT set as the
            # plan's ``target`` — an anonymous local scaffold is a machine-local path,
            # not a portable identity. Greenfield gets a real remote (and thus a target
            # + docs push) in a later slice.
            self._plans.set_local_path(plan_id, repo)
        self._plans.set_status(plan_id, STATUS_BUILDING)
        self._advance(plan_id)

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


    # -- greenfield workspace: init-in-place or scaffold -------------------

    def _ensure_repo(self, repo: Path) -> str:
        """Make ``repo`` a git repo with at least one commit (worktree add needs a
        HEAD), idempotently, and return its path. Works both for an operator-named
        directory (init in place) and a fresh scaffold path."""
        repo = repo.expanduser()
        repo.mkdir(parents=True, exist_ok=True)
        if not _is_git_repo(repo):
            for args in (
                ["init", "-b", "main"],
                ["config", "user.email", "planner@mission-control.local"],
                ["config", "user.name", "Mission Control Planner"],
            ):
                subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        if _no_head(repo):  # need an initial commit before a worktree can branch off it
            readme = repo / "README.md"
            if not readme.exists():
                readme.write_text(f"# {repo.name}\n\nWorkspace for a Mission Control build.\n")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-m", "init"],
                           check=True, capture_output=True)
        return str(repo)


def _is_dir(target) -> bool:
    return bool(target) and Path(target).expanduser().is_dir()


def _is_git_repo(target) -> bool:
    """True only when ``target`` is its OWN git root — delegates to the shared,
    safety-critical check in :mod:`mission_control.worktree` (never a parent repo)."""
    return worktree.is_git_repo(target)


def _no_head(repo: Path) -> bool:
    """True if the repo has no commits yet (``git worktree add`` needs a HEAD)."""
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", "HEAD"],
                       capture_output=True)
    return r.returncode != 0


def _prompt_for(unit) -> str:
    """The instruction handed to the worker for a unit's run."""
    if unit.task_type == roles.SIM:
        return f"Validate the planned INCEPTION work: {unit.title}"
    return f"Implement the planned change: {unit.title}"
