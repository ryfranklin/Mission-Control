"""A real worker backed by the Claude Agent SDK.

Slots in behind the same :class:`~mission_control.worker.Worker` interface as
:class:`~mission_control.worker.StubWorker`, so the orchestrator's dispatch path
(``run_task`` → ``investigate``) is unchanged.

Two load-bearing rules from the build spec:

* **Fully explicit context.** The SDK query is constructed with
  ``setting_sources=[]`` so NO filesystem settings or CLAUDE.md are auto-loaded.
  The worker sees only what we compose here (a task-scoped system prompt + the
  task itself). This keeps telemetry honest.
* **Isolation is the orchestrator's job.** The worker runs with ``cwd`` set to
  the caller-provided working directory (a throwaway git worktree) and edits
  freely there; whether those edits are ever applied is decided by the
  orchestrator's approval gate, not the worker.

``investigate`` stays synchronous (matching the interface); it drives the async
``query()`` via :func:`asyncio.run` internally.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from time import perf_counter

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from . import aidlc
from .aidlc import AidlcSteering
from .tasks import Task, TaskType
from .telemetry import StepUsage
from .worker import WorkerResult

# Cheapest current tier — verified against the Claude model catalog
# (Haiku 4.5: $1/$5 per 1M tokens, 200K context). Configurable per worker.
DEFAULT_MODEL = "claude-haiku-4-5"

# Bound a single task so a runaway worker can't loop forever.
DEFAULT_MAX_TURNS = 20

# Tools that mutate the filesystem. A read-only task hard-blocks these so a
# `sim` genuinely cannot write, even though its worktree is disposable anyway.
_MUTATING_TOOLS = ["Write", "Edit", "NotebookEdit", "Bash", "KillShell"]


class WorkerError(RuntimeError):
    """The SDK worker failed to complete a task (auth, API, or a hard error)."""


class SdkWorker:
    """Runs a task through the Claude Agent SDK in an isolated working directory."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_turns: int = DEFAULT_MAX_TURNS,
        eval_gate_mcp: bool = False,
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        # Opt-in: expose the eval-gate to the Controller as a portable MCP tool
        # (consumed over MCP, not hardcoded). Off by default → behavior unchanged.
        self.eval_gate_mcp = eval_gate_mcp

    def _mcp_servers(self) -> dict:
        if not self.eval_gate_mcp:
            return {}
        return {
            "evalgate": {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-m", "mission_control.eval_gate_mcp"],
            }
        }

    # -- Worker interface --------------------------------------------------

    def investigate(self, task: Task, workdir: Path) -> WorkerResult:
        """Run ``task`` in ``workdir`` and report a :class:`WorkerResult`.

        Synchronous by contract; bridges to the async SDK internally.
        """
        return asyncio.run(self._investigate(task, Path(workdir)))

    # -- internals ---------------------------------------------------------

    def _options(
        self,
        task: Task,
        workdir: Path,
        steering: AidlcSteering | None = None,
    ) -> ClaudeAgentOptions:
        read_only = task.task_type is not TaskType.SIDE_EFFECTFUL
        return ClaudeAgentOptions(
            model=self.model,
            # CRITICAL: no filesystem settings / CLAUDE.md — context is only
            # what we compose below (base prompt + any detected AI-DLC rules).
            setting_sources=[],
            system_prompt=_system_prompt(task, steering),
            cwd=str(workdir),
            max_turns=self.max_turns,
            # The worker acts inside a disposable, isolated worktree; the
            # orchestrator's go/no-go gate is the real side-effect boundary, so
            # the worker itself runs non-interactively. A read-only task still
            # hard-blocks mutating tools.
            permission_mode="bypassPermissions",
            disallowed_tools=list(_MUTATING_TOOLS) if read_only else [],
            # Portable tool: the Controller can call the eval-gate over MCP.
            mcp_servers=self._mcp_servers(),
        )

    async def _investigate(self, task: Task, workdir: Path) -> WorkerResult:
        texts: list[str] = []
        result: ResultMessage | None = None
        model = self.model  # updated to the resolved (dated) id once seen
        turn_latencies_ms: list[int] = []

        # A Controller starting in a target worktree probes for an AI-DLC install.
        steering = aidlc.probe(workdir)
        options = self._options(task, workdir, steering)
        prompt = task.prompt
        if steering is not None:
            prompt = aidlc.apply_invocation(prompt, greenfield=task.greenfield)

        start = perf_counter()
        last_mark = start
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                if message.error:
                    raise WorkerError(
                        f"worker error on task {task.task_id}: {message.error}"
                    )
                now = perf_counter()
                turn_latencies_ms.append(int((now - last_mark) * 1000))
                last_mark = now
                model = message.model or model
                texts.extend(b.text for b in message.content if isinstance(b, TextBlock))
            elif isinstance(message, ResultMessage):
                result = message
        total_latency_ms = int((perf_counter() - start) * 1000)

        if result is not None and result.is_error:
            detail = result.result or (result.errors or ["unknown error"])[0]
            raise WorkerError(f"worker error on task {task.task_id}: {detail}")

        summary = (result.result if result and result.result else "\n".join(texts)).strip()
        return WorkerResult(
            summary=summary or f"[sdk] completed task {task.task_id} (no text output)",
            # The worker declares intent; the orchestrator confirms actual
            # changes against git before gating.
            made_changes=task.task_type is TaskType.SIDE_EFFECTFUL,
            steps=_steps_from_result(result, model, turn_latencies_ms, total_latency_ms),
        )


def _steps_from_result(
    result: ResultMessage | None,
    model: str,
    turn_latencies_ms: list[int],
    total_latency_ms: int,
) -> list[StepUsage]:
    """Build one StepUsage per model request from the authoritative iteration
    breakdown in ``ResultMessage.usage`` (per-AssistantMessage usage is unreliable).

    Falls back to a single aggregate step if no iteration breakdown is present.
    """
    usage = (result.usage if result else None) or {}
    iterations = usage.get("iterations") or []

    if not iterations:
        return [_usage_to_step(usage, model, total_latency_ms)]

    # Prefer measured per-turn latency when it lines up 1:1 with iterations;
    # otherwise apportion the measured total evenly (an honest approximation).
    if len(turn_latencies_ms) == len(iterations):
        latencies = turn_latencies_ms
    else:
        each = total_latency_ms // len(iterations)
        latencies = [each] * len(iterations)

    return [
        _usage_to_step(it, model, latency)
        for it, latency in zip(iterations, latencies)
    ]


def _usage_to_step(usage: dict, model: str, latency_ms: int) -> StepUsage:
    cache_creation = usage.get("cache_creation") or {}
    return StepUsage(
        model=model,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
        cache_creation_5m_tokens=cache_creation.get("ephemeral_5m_input_tokens", 0),
        cache_creation_1h_tokens=cache_creation.get("ephemeral_1h_input_tokens", 0),
        latency_ms=latency_ms,
    )


def _system_prompt(task: Task, steering: AidlcSteering | None = None) -> str:
    """Compose the worker's entire context. Explicit by design — nothing is
    auto-loaded from the filesystem.

    When the target worktree carries an AI-DLC install, its own rules are folded
    in via :func:`aidlc.compose_system_prompt`; otherwise the worker runs plain.
    """
    if task.task_type is TaskType.SIDE_EFFECTFUL:
        constraint = (
            "This is a side-effectful task: you may edit files in the working "
            "directory to accomplish it."
        )
    else:
        constraint = (
            "This is a read-only investigation: inspect the working directory "
            "and report findings. Do not modify any files."
        )
    base = (
        "You are an autonomous engineering worker operating inside an isolated "
        "git worktree. Your entire context is provided here — there is no "
        "project configuration to consult.\n"
        f"{constraint}\n"
        "Work directly and report a concise result."
    )
    if steering is not None:
        return aidlc.compose_system_prompt(base, steering)
    return base
