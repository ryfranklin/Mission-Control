"""LLM-as-judge for the non-deterministic (rubric) half of a golden task.

Only invoked when a task has a non-empty ``judge_rubric`` — tasks pinned entirely
by deterministic asserts never call the judge, so we don't pay for it.

The judge runs through the same Claude Agent SDK path as the worker (so it reuses
the worker's auth) but on a **stronger, configurable model** than the worker, with
``setting_sources=[]`` and no tools — a pure scoring call. It scores the worker's
output against each rubric criterion (0..1), and the weighted mean becomes the
task's ``quality_judge``. The judge's own token usage is returned as a
:class:`~mission_control.telemetry.StepUsage` so the caller can price it via the
telemetry module — the judge is not free and its cost must be visible.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from time import perf_counter

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from .telemetry import StepUsage

# The judge defaults to a STRONGER tier than the worker (worker default is
# Haiku 4.5). Opus 4.8 is the strongest generally-available Opus — verified
# against the Claude models reference. Configurable per judge.
DEFAULT_JUDGE_MODEL = "claude-opus-4-8"

# A judge scores from provided text only — it needs no tools. Deny them all so it
# can't wander into the filesystem or the web.
_NO_TOOLS = [
    "Read", "Write", "Edit", "NotebookEdit", "Bash", "KillShell",
    "Glob", "Grep", "WebFetch", "WebSearch", "Task",
]

_JUDGE_SYSTEM = (
    "You are a strict, calibrated evaluator of coding-agent output. You are given "
    "a TASK, the agent's OUTPUT, and a RUBRIC of weighted criteria. Score EACH "
    "criterion from 0.0 (not met) to 1.0 (fully met), judging ONLY from the "
    "OUTPUT. Give partial credit for partial fulfillment; be conservative when "
    "the output does not clearly satisfy a criterion.\n"
    "Respond with ONLY a JSON object, no prose and no code fences:\n"
    '{"criteria": [{"index": <int>, "score": <0..1>, "reason": "<short>"}], '
    '"rationale": "<one paragraph>"}'
)


class JudgeError(RuntimeError):
    """The judge failed to run or returned unparseable output."""


@dataclass
class JudgeResult:
    """The judge's verdict for one task."""

    score: float  # weighted mean over the rubric, 0..1
    rationale: str
    usage: StepUsage  # the judge's own token/latency, for telemetry/pricing
    per_criterion: list[dict] = field(default_factory=list)


class LlmJudge:
    """Scores worker output against a task's rubric with a stronger model."""

    def __init__(self, model: str = DEFAULT_JUDGE_MODEL, max_turns: int = 2) -> None:
        self.model = model
        self.max_turns = max_turns

    def score(
        self, *, task_prompt: str, worker_output: str, rubric: list[dict]
    ) -> JudgeResult:
        if not rubric:
            raise JudgeError("score() called with an empty rubric — caller should skip the judge")
        import asyncio

        return asyncio.run(self._score(task_prompt, worker_output, rubric))

    # -- internals ---------------------------------------------------------

    def _prompt(self, task_prompt: str, worker_output: str, rubric: list[dict]) -> str:
        lines = []
        for i, item in enumerate(rubric, start=1):
            weight = item.get("weight", 1)
            lines.append(f"{i} (weight {weight}): {item['criterion'].strip()}")
        return (
            f"TASK:\n{task_prompt.strip()}\n\n"
            f"AGENT OUTPUT:\n{worker_output.strip()}\n\n"
            f"RUBRIC (score each numbered criterion 0..1):\n" + "\n".join(lines)
        )

    async def _score(
        self, task_prompt: str, worker_output: str, rubric: list[dict]
    ) -> JudgeResult:
        workdir = tempfile.mkdtemp(prefix="mc-judge-")
        options = ClaudeAgentOptions(
            model=self.model,
            setting_sources=[],
            system_prompt=_JUDGE_SYSTEM,
            cwd=workdir,
            max_turns=self.max_turns,
            permission_mode="bypassPermissions",
            disallowed_tools=list(_NO_TOOLS),
        )
        texts: list[str] = []
        result: ResultMessage | None = None
        model = self.model
        start = perf_counter()
        try:
            async for message in query(
                prompt=self._prompt(task_prompt, worker_output, rubric), options=options
            ):
                if isinstance(message, AssistantMessage):
                    if message.error:
                        raise JudgeError(f"judge model error: {message.error}")
                    model = message.model or model
                    texts.extend(b.text for b in message.content if isinstance(b, TextBlock))
                elif isinstance(message, ResultMessage):
                    result = message
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        latency_ms = int((perf_counter() - start) * 1000)

        if result is not None and result.is_error:
            raise JudgeError(f"judge run failed: {result.result or result.errors}")

        raw = (result.result if result and result.result else "\n".join(texts)).strip()
        parsed = _parse_json(raw)
        score, per_criterion = _weighted_score(parsed, rubric)
        usage = _usage_from_result(result, model, latency_ms)
        return JudgeResult(
            score=score,
            rationale=str(parsed.get("rationale", "")).strip(),
            usage=usage,
            per_criterion=per_criterion,
        )


# -- helpers ---------------------------------------------------------------

def _parse_json(text: str) -> dict:
    """Extract the JSON object from the judge's reply, tolerating fences/prose."""
    t = text.strip()
    if "{" in t and "}" in t:
        t = t[t.find("{") : t.rfind("}") + 1]
    try:
        obj = json.loads(t)
    except json.JSONDecodeError as e:
        raise JudgeError(f"judge output was not valid JSON: {e}: {text[:200]!r}")
    if not isinstance(obj, dict):
        raise JudgeError(f"judge output was not a JSON object: {text[:200]!r}")
    return obj


def _clamp(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def _weighted_score(parsed: dict, rubric: list[dict]) -> tuple[float, list[dict]]:
    """Weighted mean of per-criterion scores; missing criteria count as 0."""
    by_index = {}
    for c in parsed.get("criteria") or []:
        try:
            by_index[int(c.get("index"))] = c
        except (TypeError, ValueError):
            continue
    total_w = 0.0
    acc = 0.0
    per_criterion: list[dict] = []
    for i, item in enumerate(rubric, start=1):
        weight = float(item.get("weight", 1))
        c = by_index.get(i, {})
        s = _clamp(c.get("score"))
        acc += weight * s
        total_w += weight
        per_criterion.append(
            {"index": i, "weight": weight, "score": s, "reason": str(c.get("reason", ""))}
        )
    score = round(acc / total_w, 4) if total_w else 0.0
    return score, per_criterion


def _usage_from_result(result, model: str, latency_ms: int) -> StepUsage:
    """Aggregate the judge call's usage into a single StepUsage."""
    usage = (result.usage if result else None) or {}
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
