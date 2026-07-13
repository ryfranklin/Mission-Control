"""The plan builder — hands a finalized Flight Plan to Mission Control.

On finalize (readiness met), a plan's ``plan_units`` are translated into runs and
dispatched on the EXISTING launch path — this adds NO new orchestration:

* INCEPTION / validation units (``task_type == sim``) → **sim** runs (read-only);
* CONSTRUCTION / change units (``task_type == burn``) → **burn** runs behind the
  normal go/no-go gate.

``task_type`` is already on each unit (via :func:`aidlc.task_type_for_phase`), so the
builder just reads it. It respects ``depends_on`` ordering: a unit is dispatched only
once every unit it depends on has a SUCCESSFUL terminal run. Mission Control's own
durability / gate / teardown govern each run unchanged; the builder only *schedules*.

It is edge-triggered: :meth:`start_build` dispatches the deps-free units, and each
time a child run reaches a terminal state the RunManager calls :meth:`on_run_terminal`
(on the loop), which dispatches any newly-unblocked units and rolls the plan's status
up (``building`` → ``done``). A rejected gate scrubs just that unit's run — its
dependents are blocked, but the plan itself continues.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .. import aidlc, roles, worktree
from ..plans_store import (
    STATUS_BUILDING,
    STATUS_DONE,
    PlanStore,
)
from ..runs_store import (
    STATUS_APPLIED,
    STATUS_DONE as RUN_STATUS_DONE,
    STATUS_FAILED,
    STATUS_SCRUBBED,
    TERMINAL_STATUSES,
)

# A dependency counts as satisfied only when its run SUCCEEDED (a sim done, a burn
# applied on a go). A scrubbed / failed dep blocks its dependents.
_SUCCESS = frozenset({RUN_STATUS_DONE, STATUS_APPLIED})
_FAILED = frozenset({STATUS_SCRUBBED, STATUS_FAILED})


class PlanBuilder:
    """Schedules a plan's units onto the run launch path, in dependency order."""

    def __init__(self, plan_store: PlanStore, run_manager, *, workspaces_dir=None) -> None:
        self._plans = plan_store
        self._runs = run_manager
        # Where greenfield "new" builds are scaffolded (one git repo per plan).
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
        if plan.mode == aidlc.MODE_GREENFIELD and not _is_git_repo(plan.target):
            base = Path(plan.target) if _is_dir(plan.target) else self._workspaces / plan_id
            self._plans.set_target(plan_id, self._ensure_repo(base))
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
        """A plan-child run reached a terminal state → dispatch newly-unblocked units
        and roll up the plan's status. Registered as the RunManager's run observer."""
        self._advance(plan_id)

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
        target = plan.target
        if not _is_git_repo(target):
            self._plans.set_status(plan_id, STATUS_DONE)
            return

        by_seq = {r.plan_unit_seq: r for r in self._runs.child_runs(plan_id)}
        for unit in units:  # units come back in seq order
            if unit.seq in by_seq:
                continue  # already dispatched (idempotent)
            if not self._deps_succeeded(unit, by_seq):
                continue  # a dep is unfinished or has failed → not (yet) dispatchable
            self._dispatch(plan_id, target, unit)
            by_seq = {r.plan_unit_seq: r for r in self._runs.child_runs(plan_id)}

        if self._all_resolved(units, by_seq):
            self._plans.set_status(plan_id, STATUS_DONE)

    def _dispatch(self, plan_id: str, target: str, unit) -> None:
        self._runs.launch(
            target=target, task_type=unit.task_type, prompt=_prompt_for(unit),
            plan_id=plan_id, plan_unit_seq=unit.seq,
        )

    # -- dependency logic --------------------------------------------------

    @staticmethod
    def _deps_succeeded(unit, by_seq) -> bool:
        """Every dependency has a SUCCESSFUL terminal run."""
        for dep in unit.depends_on or []:
            run = by_seq.get(dep)
            if run is None or run.status not in _SUCCESS:
                return False
        return True

    def _all_resolved(self, units, by_seq) -> bool:
        """The plan is done when every unit is resolved: it has a terminal run, or it is
        BLOCKED by a failed/scrubbed dependency (transitively) so it will never run."""
        blocked = self._blocked_seqs(units, by_seq)
        for unit in units:
            run = by_seq.get(unit.seq)
            terminal = run is not None and run.status in TERMINAL_STATUSES
            if not terminal and unit.seq not in blocked:
                return False
        return True

    @staticmethod
    def _blocked_seqs(units, by_seq) -> set:
        """Units transitively blocked by a failed/scrubbed dependency."""
        blocked: set = set()
        changed = True
        while changed:
            changed = False
            for unit in units:
                if unit.seq in blocked:
                    continue
                for dep in unit.depends_on or []:
                    run = by_seq.get(dep)
                    if (run is not None and run.status in _FAILED) or dep in blocked:
                        blocked.add(unit.seq)
                        changed = True
                        break
        return blocked


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
