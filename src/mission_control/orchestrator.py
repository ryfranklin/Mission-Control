"""Orchestration entry point.

An orchestrator (``roles.ORCHESTRATOR``) dispatches a worker (``roles.WORKER``)
to run a task inside an isolated git worktree, gates any side effects behind an
approval decision (``roles.GO`` / ``roles.NO_GO``), and guarantees teardown. It
can also terminate a task early (``roles.SCRUB``) with no leaks.

Metaphor terms appear ONLY as ``roles.*`` constants, used for human-facing
labels — never spelled out as identifiers or literals. Everything else here is
functionally named, so a metaphor swap stays a one-file change in ``roles.py``.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import repo_source, roles, worktree
from .tasks import Task, TaskType
from .telemetry import RunTelemetry, TelemetrySink, events_from_steps
from .worker import StubWorker, Worker, WorkerResult

# Default location for per-run JSONL telemetry files.
DEFAULT_TELEMETRY_DIR = Path("telemetry")

# An approval gate: inspects the pending run and returns True for go, False for
# no-go. None means "no approver wired up" → default to no-go (changes blocked).
Approval = Callable[["TaskRun"], bool]

# Functional outcome labels (not metaphor vocabulary).
OUTCOME_COMPLETED = "completed"  # sim finished, or burn approved + applied
OUTCOME_BLOCKED = "blocked"  # burn produced changes but approval was no-go
OUTCOME_TERMINATED = "terminated"  # task killed early (scrub)

# Push outcome for an applied burn (mirrors graph.py; the write hop to the remote).
PUSH_PUSHED = "pushed"       # merged locally AND pushed to origin
PUSH_REJECTED = "rejected"   # push refused as non-fast-forward
PUSH_CONFLICT = "conflict"   # integrating the remote advance produced a merge conflict
PUSH_ERROR = "error"         # push failed on creds / network (surfaced loudly)
PUSH_SKIPPED = "skipped"     # no remote to push to (a remote-less local target)


@dataclass
class TaskRun:
    """A dispatched task and its live isolation. Handle for gating / termination."""

    task: Task
    worktree: worktree.Worktree
    live: bool = True


@dataclass
class RunResult:
    """The record of a completed run, including its per-step telemetry."""

    task: Task
    worker_result: Optional[WorkerResult]
    applied: bool  # were the worker's changes merged into the target repo?
    decision: Optional[str]  # roles.GO / roles.NO_GO, or None for read-only
    outcome: str  # one of the OUTCOME_* labels
    telemetry: Optional[RunTelemetry] = None  # None only for terminate (no run)
    # Push outcome for an applied burn (one of the PUSH_* labels); None when no merge
    # happened (sim / no-go / burn no-op), so "applied + pushed" vs "applied locally,
    # push rejected" is legible to callers.
    push_status: Optional[str] = None


class Orchestrator:
    """Dispatches workers into isolated worktrees and gates their side effects."""

    def __init__(
        self,
        target_repo: Path,
        worker: Optional[Worker] = None,
        telemetry_dir: Optional[Path] = None,
    ) -> None:
        self.target_repo = Path(target_repo).resolve()
        self.worker: Worker = worker if worker is not None else StubWorker()
        self.telemetry_dir = Path(
            telemetry_dir if telemetry_dir is not None else DEFAULT_TELEMETRY_DIR
        )
        # Live runs by task id, so a task can be terminated by id.
        self._active: dict[str, TaskRun] = {}

    # -- lifecycle ---------------------------------------------------------

    def dispatch(self, task: Task) -> TaskRun:
        """Create an isolated worktree for a task and register it as live.

        (``roles.ORCHESTRATOR`` sends a ``roles.WORKER`` to its station.)
        """
        wt = worktree.create_worktree(self.target_repo, task.task_id)
        run = TaskRun(task=task, worktree=wt)
        self._active[task.task_id] = run
        return run

    def run_task(self, task: Task, approval: Optional[Approval] = None) -> RunResult:
        """Full lifecycle: dispatch → run worker → gate side effects → teardown.

        Read-only tasks never apply changes. Side-effectful tasks apply their
        changes only on a ``roles.GO`` decision; a ``roles.NO_GO`` (or absent
        approver) blocks them.
        Teardown always runs, even on error.
        """
        run = self.dispatch(task)
        try:
            worker_result = self.worker.investigate(task, run.worktree.path)
            applied, decision, outcome, push_status = self._gate(run, worker_result, approval)
            telemetry = self._write_telemetry(task, worker_result, outcome)
            return RunResult(
                task=task,
                worker_result=worker_result,
                applied=applied,
                decision=decision,
                outcome=outcome,
                telemetry=telemetry,
                push_status=push_status,
            )
        finally:
            self._teardown(run)

    def terminate(self, run: TaskRun) -> RunResult:
        """Kill a task and tear down its worktree, discarding any work.

        (Implements ``roles.SCRUB``.)
        """
        self._teardown(run)
        return RunResult(
            task=run.task,
            worker_result=None,
            applied=False,
            decision=None,
            outcome=OUTCOME_TERMINATED,
        )

    # -- gating ------------------------------------------------------------

    def _gate(
        self,
        run: TaskRun,
        worker_result: WorkerResult,
        approval: Optional[Approval],
    ) -> tuple[bool, Optional[str], str, Optional[str]]:
        """Decide whether the worker's changes are applied. Returns
        (applied, decision, outcome, push_status)."""
        if run.task.task_type is not TaskType.SIDE_EFFECTFUL:
            # Read-only work never applies changes; nothing to gate, nothing to push.
            return False, None, OUTCOME_COMPLETED, None

        # Side-effectful: commit the worker's changes onto the task branch so the
        # decision has a concrete diff to accept or reject.
        committed = worktree.commit_changes(
            run.worktree, f"task {run.task.task_id}: {run.task.prompt}"
        )
        if not committed:
            # A burn that produced no changes is a completed no-op — nothing to push.
            return False, None, OUTCOME_COMPLETED, None

        approved = approval is not None and approval(run)
        if approved:
            worktree.merge_into_target(
                run.worktree, f"apply task {run.task.task_id}"
            )
            # The write hop: push the merged result to the remote so it leaves the host.
            # Fires ONLY on an affirmative gate, after the merge — never on sim/no-go/no-op.
            return True, roles.GO, OUTCOME_COMPLETED, self._push()
        # No approver, or explicit no-go → changes stay quarantined on the branch
        # and are discarded at teardown.
        return False, roles.NO_GO, OUTCOME_BLOCKED, None

    def _push(self) -> str:
        """Push the just-merged branch to origin. Returns a PUSH_* label; a rejection or
        creds/network failure is recorded (not raised) so it's legible in the result and
        never force-pushed. A remote-less target has nothing to push."""
        if not repo_source.has_origin(self.target_repo):
            return PUSH_SKIPPED
        branch = repo_source.current_branch(self.target_repo)
        try:
            repo_source.push_to_remote(self.target_repo, branch)
            return PUSH_PUSHED
        except repo_source.MergeConflict:
            return PUSH_CONFLICT
        except repo_source.PushRejected:
            return PUSH_REJECTED
        except repo_source.PushError:
            return PUSH_ERROR

    # -- telemetry ---------------------------------------------------------

    def _write_telemetry(
        self, task: Task, worker_result: WorkerResult, outcome: str
    ) -> RunTelemetry:
        """Enrich the worker's raw per-step usage into priced JSONL events —
        one file per run, one line per step."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = self.telemetry_dir / f"run-{task.task_id}-{stamp}-{uuid.uuid4().hex[:6]}.jsonl"
        events = events_from_steps(
            worker_result.steps,
            task_id=task.task_id,
            task_type=task.task_type.value,  # metaphor string, from roles
            outcome=outcome,
        )
        with TelemetrySink(path) as sink:
            for event in events:
                sink.record(event)
            return sink.telemetry

    # -- teardown ----------------------------------------------------------

    def _teardown(self, run: TaskRun) -> None:
        if not run.live:
            return
        worktree.remove_worktree(run.worktree)
        run.live = False
        self._active.pop(run.task.task_id, None)
