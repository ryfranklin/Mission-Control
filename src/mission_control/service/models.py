"""Typed request/response models for the HTTP service seam.

Deliberately thin: they mirror the runs-ledger row and the analytics rollup. No
orchestration lives here — the models only shape what crosses the wire.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from ..runs_store import RunRow
from ..tasks import TaskType

# task_type wire values come from the metaphor (roles.SIM / roles.BURN) via the
# TaskType enum — never hardcoded here, so the metaphor stays in roles.py.
_TASK_TYPE_VALUES = tuple(t.value for t in TaskType)


class LaunchRequest(BaseModel):
    """Body for ``POST /runs``."""

    target: str = Field(..., description="Path to the target git repository.")
    task_type: str = Field(..., description=f"One of {_TASK_TYPE_VALUES}.")
    prompt: str = Field(
        "Investigate the target repository and report your findings.",
        description="The instruction handed to the worker.",
    )

    @field_validator("task_type")
    @classmethod
    def _known_task_type(cls, v: str) -> str:
        if v not in _TASK_TYPE_VALUES:
            raise ValueError(f"task_type must be one of {_TASK_TYPE_VALUES}")
        return v


class RunDetail(BaseModel):
    """A single runs-ledger row (``GET /runs/{id}``)."""

    run_id: str
    thread_id: str
    target: Optional[str]
    task_type: Optional[str]
    status: str
    cost_usd: float
    created_at: Optional[datetime]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    detail: Optional[str]

    @classmethod
    def from_row(cls, row: RunRow) -> "RunDetail":
        return cls(**row.__dict__)


class RunList(BaseModel):
    """Response for ``GET /runs``."""

    runs: list[RunDetail]


class DecisionResponse(BaseModel):
    """Response for approve / reject / scrub — the transition was accepted; the
    run resolves asynchronously (poll ``GET /runs/{id}`` or the SSE feed)."""

    run_id: str
    status: str
    accepted: bool = True


class MetricsResponse(BaseModel):
    """Cross-run cost/quality summary from the DuckDB analytics pass."""

    per_run: list[dict]
    by_task_type: list[dict]
    worker_vs_judge: dict
    quality_trend: list[dict]
    telemetry_rollup: dict
