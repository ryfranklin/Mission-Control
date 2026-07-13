"""Typed request/response models for the PLAN seam.

Thin, like the runs-ledger models: they mirror the PLAN store rows and shape only
what crosses the wire. The methodology/cloud defaults are resolved at the manager
layer (from env), so they are optional here — an absent field means "use the
instance default", not "aidlc"/"aws" hardcoded at the edge.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from ..aidlc import MODES, ReadinessCriterion
from ..plans_store import PlanRequirement, PlanRow, PlanTurn, PlanUnit


class OpenPlanRequest(BaseModel):
    """Body for ``POST /plans`` — open a planning session for a target."""

    target: Optional[str] = Field(None, description="Path to the target repository.")
    mode: str = Field(..., description=f"One of {MODES}.")
    methodology: Optional[str] = Field(
        None, description="Overrides the instance default (MC_PLANNER_METHODOLOGY)."
    )
    cloud_target: Optional[str] = Field(
        None, description="Overrides the instance default (MC_PLANNER_CLOUD)."
    )

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, v: str) -> str:
        if v not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        return v


class TurnRequest(BaseModel):
    """Body for ``POST /plans/{id}/turns`` — one operator turn."""

    content: str = Field(..., description="The operator's message to the planner.")


class TurnModel(BaseModel):
    """One transcript turn."""

    seq: int
    role: str
    content: str
    created_at: Optional[datetime]

    @classmethod
    def from_row(cls, row: PlanTurn) -> "TurnModel":
        return cls(seq=row.seq, role=row.role, content=row.content, created_at=row.created_at)


class RequirementModel(BaseModel):
    """One accreting requirement."""

    key: str
    value: Optional[str]
    state: str

    @classmethod
    def from_row(cls, row: PlanRequirement) -> "RequirementModel":
        return cls(key=row.key, value=row.value, state=row.state)


class UnitModel(BaseModel):
    """One CONSTRUCTION work-list unit."""

    seq: int
    title: str
    phase: str
    task_type: str
    depends_on: list
    status: str

    @classmethod
    def from_row(cls, row: PlanUnit) -> "UnitModel":
        return cls(
            seq=row.seq, title=row.title, phase=row.phase, task_type=row.task_type,
            depends_on=list(row.depends_on or []), status=row.status,
        )


class PlanSummary(BaseModel):
    """A single plan header (the ``GET /plans`` list row)."""

    id: str
    target: Optional[str]
    mode: str
    methodology: str
    cloud_target: str
    stage: Optional[str]
    status: str
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    @classmethod
    def from_row(cls, row: PlanRow) -> "PlanSummary":
        return cls(**row.__dict__)


class CriterionModel(BaseModel):
    """One finalize-readiness criterion, flagged met/unmet (for the UI to show what is
    still blocking finalize)."""

    key: str
    label: str
    met: bool
    detail: str = ""

    @classmethod
    def from_criterion(cls, c: ReadinessCriterion) -> "CriterionModel":
        return cls(key=c.key, label=c.label, met=c.met, detail=c.detail)


class ChildRunModel(BaseModel):
    """One of a plan's child runs — a unit dispatched onto the launch path, with its
    live status and reconciled cost (links to the run station in the UI)."""

    run_id: str
    unit_seq: Optional[int]
    task_type: Optional[str]
    status: str
    cost_usd: float

    @classmethod
    def from_row(cls, row) -> "ChildRunModel":
        return cls(run_id=row.run_id, unit_seq=row.plan_unit_seq, task_type=row.task_type,
                   status=row.status, cost_usd=row.cost_usd)


class PlanDetail(PlanSummary):
    """The full plan aggregate (``GET /plans/{id}``): header + transcript +
    requirements + the work-list units + the readiness gate + the child runs the
    build dispatched (with rolled-up cost)."""

    turns: list[TurnModel] = Field(default_factory=list)
    requirements: list[RequirementModel] = Field(default_factory=list)
    units: list[UnitModel] = Field(default_factory=list)
    readiness: list[CriterionModel] = Field(default_factory=list)
    ready: bool = False
    child_runs: list[ChildRunModel] = Field(default_factory=list)
    build_cost: float = 0.0

    @classmethod
    def from_aggregate(cls, plan: PlanRow, turns, requirements, units,
                       readiness=(), child_runs=(), build_cost=0.0) -> "PlanDetail":
        criteria = [CriterionModel.from_criterion(c) for c in readiness]
        return cls(
            **plan.__dict__,
            turns=[TurnModel.from_row(t) for t in turns],
            requirements=[RequirementModel.from_row(r) for r in requirements],
            units=[UnitModel.from_row(u) for u in units],
            readiness=criteria,
            ready=all(c.met for c in criteria) if criteria else False,
            child_runs=[ChildRunModel.from_row(r) for r in child_runs],
            build_cost=round(float(build_cost), 8),
        )


class PlanList(BaseModel):
    """A page of plans (``GET /plans``). ``total`` is the full match count."""

    plans: list[PlanSummary]
    total: int
    limit: int
    offset: int


class TurnResponse(BaseModel):
    """Response for ``POST /plans/{id}/turns`` — the planner's reply to the turn."""

    plan_id: str
    reply: TurnModel
