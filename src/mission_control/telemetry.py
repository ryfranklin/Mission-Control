"""Per-step telemetry: raw usage from workers, enriched step events, JSONL sink.

A *step* is one model request. A worker reports the raw token/latency usage of
each step it made (:class:`StepUsage`); the orchestrator enriches those into
:class:`StepEvent` records (ids, task context, cost, outcome) and streams them to
a JSONL file — one file per run.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import pricing


@dataclass
class StepUsage:
    """Raw usage a worker observed for one model request. No pricing/ids yet."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    # Breakdown of cache_creation_tokens by TTL, for accurate write pricing.
    cache_creation_5m_tokens: int = 0
    cache_creation_1h_tokens: int = 0
    latency_ms: int = 0


@dataclass
class StepEvent:
    """One enriched, priced telemetry record — the JSONL line shape."""

    step_id: str
    parent_step_id: str | None
    task_id: str
    task_type: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    context_size_tokens: int  # the full rendered prompt for this step
    cost_usd: float
    latency_ms: int
    outcome: str

    @classmethod
    def from_usage(
        cls,
        usage: StepUsage,
        *,
        step_id: str,
        parent_step_id: str | None,
        task_id: str,
        task_type: str,
        outcome: str,
    ) -> "StepEvent":
        cost = pricing.cost_usd(
            usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_5m_tokens=usage.cache_creation_5m_tokens,
            cache_creation_1h_tokens=usage.cache_creation_1h_tokens,
        )
        context = (
            usage.input_tokens
            + usage.cache_read_tokens
            + usage.cache_creation_tokens
        )
        return cls(
            step_id=step_id,
            parent_step_id=parent_step_id,
            task_id=task_id,
            task_type=task_type,
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            context_size_tokens=context,
            cost_usd=round(cost, 8),
            latency_ms=usage.latency_ms,
            outcome=outcome,
        )


@dataclass
class RunTelemetry:
    """A run's telemetry file + the events written to it, with a rollup summary."""

    path: Path
    events: list[StepEvent] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "steps": len(self.events),
            "input_tokens": sum(e.input_tokens for e in self.events),
            "output_tokens": sum(e.output_tokens for e in self.events),
            "cache_read_tokens": sum(e.cache_read_tokens for e in self.events),
            "cache_creation_tokens": sum(e.cache_creation_tokens for e in self.events),
            "cost_usd": round(sum(e.cost_usd for e in self.events), 6),
            "latency_ms": sum(e.latency_ms for e in self.events),
        }

    def summary_line(self) -> str:
        s = self.summary()
        return (
            f"telemetry: {s['steps']} step(s), "
            f"in={s['input_tokens']} out={s['output_tokens']} "
            f"cache_r={s['cache_read_tokens']} cache_w={s['cache_creation_tokens']} "
            f"cost=${s['cost_usd']:.6f} latency={s['latency_ms']}ms "
            f"→ {self.path.name}"
        )


class TelemetrySink:
    """Streams :class:`StepEvent` records to a JSONL file, one line per step."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        self.telemetry = RunTelemetry(path=self.path)

    def record(self, event: StepEvent) -> None:
        self._fh.write(json.dumps(asdict(event), sort_keys=True) + "\n")
        self._fh.flush()
        self.telemetry.events.append(event)

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "TelemetrySink":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
