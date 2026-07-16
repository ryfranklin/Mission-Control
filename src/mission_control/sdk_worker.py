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
import os
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
# (Haiku 4.5: $1/$5 per 1M tokens, 200K context). Configurable per worker, or via
# MC_WORKER_MODEL for a whole service (a stronger model finishes a task in fewer turns).
# NB: this is the LOW-LEVEL default (used by the eval harness, whose judge must stay a
# STRONGER tier than the worker). The SERVICE defaults the build worker to Opus — see
# SERVICE_WORKER_MODEL in mission_control.service.
DEFAULT_MODEL = "claude-haiku-4-5"

# Bound a single task so a runaway worker can't loop forever. 20 is fine for small
# changes but too low for a feature-sized CONSTRUCTION unit; raise it per service with
# MC_WORKER_MAX_TURNS when the plan's units are coarse-grained.
DEFAULT_MAX_TURNS = 20

# Tools that mutate the filesystem. A read-only task hard-blocks these so a
# `sim` genuinely cannot write, even though its worktree is disposable anyway.
_MUTATING_TOOLS = ["Write", "Edit", "NotebookEdit", "Bash", "KillShell"]

# Fallback seam DB (docker-compose default) when MC_POSTGRES_URL is unset — only
# used to *derive* the isolated worker URL below, never connected to here.
_DEFAULT_PG_URL = "postgresql://mc:mc@localhost:5432/mission_control"


def _worker_pg_url() -> str:
    """Isolated Postgres URL handed to the worker subprocess so a self-targeting
    build's tests never touch the live seam DB. Explicit override via
    ``MC_WORKER_POSTGRES_URL``; otherwise the seam URL with its database name
    suffixed ``_worker`` (a separate DB on the same server)."""
    explicit = os.environ.get("MC_WORKER_POSTGRES_URL")
    if explicit:
        return explicit
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(os.environ.get("MC_POSTGRES_URL") or _DEFAULT_PG_URL)
    db = parts.path.lstrip("/") or "mission_control"
    return urlunsplit(parts._replace(path="/" + db + "_worker"))


class WorkerError(RuntimeError):
    """The SDK worker failed to complete a task (auth, API, or a hard error).

    Carries any priced ``steps`` the worker managed to consume before failing — a run
    that errors (e.g. exhausted its turn budget) has still spent real tokens, and that
    cost must be recorded rather than silently dropped."""

    def __init__(self, *args, steps=None) -> None:
        super().__init__(*args)
        self.steps = list(steps or [])


class SdkWorker:
    """Runs a task through the Claude Agent SDK in an isolated working directory."""

    def __init__(
        self,
        model: str | None = None,
        max_turns: int | None = None,
        eval_gate_mcp: bool = False,
    ) -> None:
        # Explicit arg wins; else the service-wide env override; else the default.
        self.model = model or os.environ.get("MC_WORKER_MODEL") or DEFAULT_MODEL
        self.max_turns = (
            max_turns if max_turns is not None
            else int(os.environ.get("MC_WORKER_MAX_TURNS") or DEFAULT_MAX_TURNS)
        )
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
        system_prompt: str | None = None,
    ) -> ClaudeAgentOptions:
        read_only = task.task_type is not TaskType.SIDE_EFFECTFUL
        if system_prompt is None:  # default: the generic (non-stage) worker prompt
            system_prompt = _system_prompt(task)
        return ClaudeAgentOptions(
            model=self.model,
            # CRITICAL: no filesystem settings / CLAUDE.md — context is only
            # what we compose (base prompt + stage/AI-DLC steering). Preserved for
            # v2 stage runs too: v2 hooks/tools never auto-load.
            setting_sources=[],
            system_prompt=system_prompt,
            cwd=str(workdir),
            # Worker DB isolation: the worker runs inside a disposable worktree and
            # may execute the target repo's own test suite. Point any Postgres it
            # touches at a dedicated database so a *self-targeting* build never
            # reads/writes/migrates the live seam DB (MC_POSTGRES_URL) — that once
            # broke the running seam's cached query plan mid-build. Explicit worker
            # context per CLAUDE.md; override via MC_WORKER_POSTGRES_URL.
            env={**os.environ, "MC_POSTGRES_URL": _worker_pg_url()},
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
        system_prompt = _resolve_system_prompt(task, steering)
        options = self._options(task, workdir, system_prompt)
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
            # The error result still carries the usage consumed up to the failure (e.g.
            # a run that hit the turn cap). Attach the priced steps so the cost is
            # recorded on the failed run instead of being dropped as $0.
            raise WorkerError(
                f"worker error on task {task.task_id}: {detail}",
                steps=_steps_from_result(result, model, turn_latencies_ms, total_latency_ms),
            )

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


def _base_prompt(task: Task) -> str:
    """The worker framing + the sim/burn read-only-vs-write constraint (the tool block
    enforces it too; this states it in prose)."""
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
    return (
        "You are an autonomous engineering worker operating inside an isolated "
        "git worktree. Your entire context is provided here — there is no "
        "project configuration to consult.\n"
        f"{constraint}\n"
        "Work directly and report a concise result."
    )


def _resolve_system_prompt(task: Task, steering: AidlcSteering | None) -> str:
    """Choose the worker's system prompt for this run.

    A v2 stage-unit run (``task.stage_slug`` set + a v2 install detected) steers from
    that single stage's definition + lead-agent knowledge (see
    :func:`aidlc_v2.steering.compose_stage_prompt`) — not the whole methodology and not
    the generic prompt. Every other run uses :func:`_system_prompt`.
    """
    if (
        task.stage_slug
        and steering is not None
        and steering.flavor == aidlc.FLAVOR_AIDLC_V2
        and steering.catalog_root is not None
    ):
        from .aidlc_v2 import catalog as v2catalog
        from .aidlc_v2 import steering as v2steering

        stage = next(
            (s for s in v2catalog.load_catalog(steering.catalog_root)
             if s.slug == task.stage_slug),
            None,
        )
        if stage is not None:
            return _base_prompt(task) + "\n\n" + v2steering.compose_stage_prompt(
                stage, steering.catalog_root)
    return _system_prompt(task, steering)


def _system_prompt(task: Task, steering: AidlcSteering | None = None) -> str:
    """Compose the worker's entire context. Explicit by design — nothing is
    auto-loaded from the filesystem.

    When the target worktree carries an AI-DLC install, its own rules are folded
    in via :func:`aidlc.compose_system_prompt`; otherwise the worker runs plain.
    """
    base = _base_prompt(task)
    if steering is not None:
        return aidlc.compose_system_prompt(base, steering)
    return base
