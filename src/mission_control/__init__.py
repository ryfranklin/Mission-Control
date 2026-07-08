"""Mission Control — an agent-orchestration runtime.

Spawns workers, isolates them in git worktrees, supervises them, records
telemetry, and gates their side effects.
"""

from .aidlc import AidlcSteering, Phase, probe, task_type_for_phase
from .judge import DEFAULT_JUDGE_MODEL, JudgeResult, LlmJudge
from .orchestrator import Orchestrator, RunResult, TaskRun
from .sdk_worker import SdkWorker, WorkerError
from .tasks import Task, TaskType
from .telemetry import RunTelemetry, StepEvent, StepUsage, TelemetrySink
from .worker import StubWorker, Worker, WorkerResult

__version__ = "0.0.0"

__all__ = [
    "Orchestrator",
    "RunResult",
    "TaskRun",
    "Task",
    "TaskType",
    "Worker",
    "StubWorker",
    "SdkWorker",
    "WorkerError",
    "WorkerResult",
    "StepUsage",
    "StepEvent",
    "RunTelemetry",
    "TelemetrySink",
    "AidlcSteering",
    "Phase",
    "probe",
    "task_type_for_phase",
    "LlmJudge",
    "JudgeResult",
    "DEFAULT_JUDGE_MODEL",
]
