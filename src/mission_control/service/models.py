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
    slack_profile: Optional[str] = Field(
        None,
        description="Optional per-run Slack profile name (from GET /slack/profiles). "
                    "None (default) = no Slack (a silent run); an unknown name is "
                    "rejected at launch.",
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
    # A short human description of the task, available from launch (dispatch) onward —
    # so the UI shows what a run is doing before any worker output / terminal summary.
    subject: Optional[str] = None
    # The per-run Slack profile selected at launch (None = a silent run).
    slack_profile: Optional[str] = None

    @classmethod
    def from_row(cls, row: RunRow) -> "RunDetail":
        return cls(**row.__dict__)


class RunList(BaseModel):
    """A page of runs (``GET /runs``). ``total`` is the full match count (ignoring
    limit/offset) so a client can page."""

    runs: list[RunDetail]
    total: int
    limit: int
    offset: int


class TargetList(BaseModel):
    """Response for ``GET /targets`` — distinct targets the registry has seen."""

    targets: list[str]


class DecisionResponse(BaseModel):
    """Response for approve / reject / scrub / cancel — the transition was accepted;
    the run resolves asynchronously (poll ``GET /runs/{id}`` or the SSE feed)."""

    run_id: str
    status: str
    accepted: bool = True


class SlackProfile(BaseModel):
    """One NON-SECRET Slack profile (``GET /slack/profiles``) — name + channel only.
    No token value or env-var name crosses the wire."""

    name: str
    channel: Optional[str] = None


class SlackProfileList(BaseModel):
    """Response for ``GET /slack/profiles``: the selectable profiles plus the canonical
    opt-out. ``none`` names the no-Slack choice (a null ``slack_profile`` at launch), so
    a client can render an explicit "None" option in its selector."""

    profiles: list[SlackProfile]
    none: str = Field(..., description="Label for the opt-out (a null slack_profile).")


class NotificationPayload(BaseModel):
    """The METADATA-ONLY body of a milestone or alert. By construction it has NO field
    for prompt/code/diff/target contents — only names, types, status, cost, node,
    timestamps, and (for alerts) threshold/budget/axis metadata — so the outbox can
    never leak run content. Every field is a name or a number/bool."""

    target: Optional[str] = None          # the portable ref (a name), not target data
    task_type: Optional[str] = None       # "sim"/"burn"
    status: Optional[str] = None
    cost_usd: Optional[float] = None
    node: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    # -- cost_threshold alert metadata --
    reason: Optional[str] = None          # "per_run" | "window"
    threshold: Optional[float] = None
    budget: Optional[float] = None
    window_total: Optional[float] = None
    window_hours: Optional[float] = None
    # -- regression alert metadata (regressed axes: metric + baseline band vs observed) --
    axes: Optional[list[dict]] = None
    k: Optional[float] = None
    n: Optional[int] = None


class Notification(BaseModel):
    """One outbox row: a global monotonic ``seq``, the run it concerns, the run's Slack
    profile (nullable — the bridge routes on it), the milestone ``kind``, and a
    metadata-only ``payload``."""

    seq: int
    run_id: str
    slack_profile: Optional[str] = None
    kind: str
    payload: NotificationPayload
    created_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: dict) -> "Notification":
        return cls(
            seq=row["seq"], run_id=row["run_id"], slack_profile=row.get("slack_profile"),
            kind=row["kind"], payload=NotificationPayload(**(row.get("payload") or {})),
            created_at=row.get("created_at"),
        )


class NotificationList(BaseModel):
    """Response for ``GET /notifications?after=&limit=`` — the bridge's at-least-once
    pull feed. ``notifications`` carry ``seq > after`` (oldest-first). ``last_seq`` is
    the outbox's global high-water mark and ``total`` its full count, so a consumer can
    advance a durable cursor and see how far behind it is."""

    notifications: list[Notification]
    total: int
    last_seq: int


class ProfileDigest(BaseModel):
    """A metadata-only fleet digest for ONE Slack profile (``GET /notifications/digest``):
    the runs that named it, counted by status, with total cost and top targets by cost.
    A run with no profile appears in no digest. Names + numbers only — no run content."""

    profile: str
    runs: int
    cost_usd: float
    by_status: dict = Field(default_factory=dict)
    top_targets: list[dict] = Field(default_factory=list)
    window_hours: Optional[float] = None


class MetricsResponse(BaseModel):
    """Cross-run cost/quality summary. The ``per_run``/``by_task_type``/… fields are
    the global DuckDB analytics rollup. ``runs_summary`` is an exact aggregate over
    the runs registry, narrowed to ``scope`` (target + time window) when given."""

    per_run: list[dict]
    by_task_type: list[dict]
    worker_vs_judge: dict
    quality_trend: list[dict]
    telemetry_rollup: dict
    scope: Optional[dict] = None
    runs_summary: dict = Field(default_factory=dict)
