"""Worker interface + a canned stub.

A worker runs a task inside an already-isolated working directory (a git
worktree). It never manages its own isolation or approval — the orchestrator
owns that. Phase 0 ships only :class:`StubWorker` (no LLM); a real Claude Agent
SDK worker slots in behind the same :class:`Worker` interface later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .tasks import Task, TaskType
from .telemetry import StepUsage

# Canned artifact a stub burn writes, so the orchestrator has real changes to gate.
_STUB_BURN_FILENAME = "STUB_BURN.txt"

# Model a stub reports for its synthetic step, so pricing/telemetry works offline.
_STUB_MODEL = "claude-haiku-4-5"


@dataclass
class WorkerResult:
    """What a worker reports back after running a task."""

    summary: str
    made_changes: bool  # True only when the worker mutated its working directory
    # Per-model-request usage the worker observed, in order. The orchestrator
    # turns these into priced telemetry events.
    steps: list[StepUsage] = field(default_factory=list)


@runtime_checkable
class Worker(Protocol):
    """A worker runs a task in ``workdir`` and reports a :class:`WorkerResult`.

    Implementations mutate ``workdir`` directly for side-effectful tasks; the
    orchestrator detects and gates those changes via git.
    """

    def investigate(self, task: Task, workdir: Path) -> WorkerResult: ...


class StubWorker:
    """Canned worker — no LLM. Read-only tasks touch nothing; side-effectful
    tasks write a single marker file so approval gating has something to gate."""

    def investigate(self, task: Task, workdir: Path) -> WorkerResult:
        workdir = Path(workdir)
        # One canned step so telemetry has identical shape to a real worker.
        step = StepUsage(
            model=_STUB_MODEL,
            input_tokens=1200,
            output_tokens=340,
            cache_read_tokens=800,
            cache_creation_tokens=200,
            cache_creation_5m_tokens=200,
            cache_creation_1h_tokens=0,
            latency_ms=1234,
        )
        if task.task_type is TaskType.SIDE_EFFECTFUL:
            marker = workdir / _STUB_BURN_FILENAME
            marker.write_text(f"stub change for task {task.task_id}: {task.prompt}\n")
            return WorkerResult(
                summary=f"[stub] applied change for task {task.task_id}",
                made_changes=True,
                steps=[step],
            )
        return WorkerResult(
            summary=f"[stub] investigated (read-only) task {task.task_id}",
            made_changes=False,
            steps=[step],
        )
