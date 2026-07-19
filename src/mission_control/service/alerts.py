"""Alert-class notifications: the "operator not watching" value.

These stay METADATA-ONLY and are routed (by the outbox's ``slack_profile``) to the
run's profile like every other notification. This module holds the thresholds config
and the PURE helpers that shape alert payloads — the RunManager owns emission (it has
the run row + the store).

* Cost/budget: a run whose reconciled cost crosses a per-run threshold, or whose
  target's rolling-window total crosses a budget. Thresholds from env; default OFF.
* Regression: a CLIENT of the existing Phase-3 eval-gate result — it reads the gate's
  regressed axes (metric, baseline band vs observed) and NOTHING of the eval content.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

# The alert kinds live in runs_store (reserved in S1); re-exported here for callers.
from ..runs_store import NOTIFY_COST_THRESHOLD, NOTIFY_REGRESSION  # noqa: F401

# Env knobs (all optional; unset ⇒ that alert is off).
ENV_PER_RUN = "MC_COST_ALERT_PER_RUN"      # $ per-run reconciled-cost threshold
ENV_BUDGET = "MC_COST_ALERT_BUDGET"        # $ rolling-window budget (per target)
ENV_WINDOW_HOURS = "MC_COST_ALERT_WINDOW_HOURS"  # window length (default 24h)

# The reason a cost alert fired (metadata only).
REASON_PER_RUN = "per_run"
REASON_WINDOW = "window"

# The numeric/bool axis fields we surface from a gate result — a strict whitelist, so
# no eval content (prompts, per-task text, run outputs) can ride along.
_AXIS_FIELDS = ("current", "baseline_mean", "baseline_stddev", "threshold",
                "higher_is_worse", "regressed")


@dataclass(frozen=True)
class CostAlertConfig:
    """Cost/budget thresholds. Both default None ⇒ disabled (cost alerts off)."""

    per_run: Optional[float] = None
    budget: Optional[float] = None
    window_hours: float = 24.0

    @property
    def enabled(self) -> bool:
        return self.per_run is not None or self.budget is not None

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "CostAlertConfig":
        src = env if env is not None else os.environ

        def _f(name: str) -> Optional[float]:
            v = src.get(name)
            return float(v) if v not in (None, "") else None

        return cls(
            per_run=_f(ENV_PER_RUN),
            budget=_f(ENV_BUDGET),
            window_hours=(_f(ENV_WINDOW_HOURS) or 24.0),
        )


def regressed_axes(gate_result: Optional[dict]) -> list[dict]:
    """The regressed axes from a Phase-3 ``GateResult.to_json()`` — each as
    ``{metric, current, baseline_mean, baseline_stddev, threshold, higher_is_worse}``.
    Empty when the gate passed. Whitelist-only: NEVER any eval content."""
    axes = (gate_result or {}).get("axes") or {}
    out: list[dict] = []
    for metric, axis in axes.items():
        if isinstance(axis, dict) and axis.get("regressed"):
            meta = {"metric": metric}
            meta.update({k: axis[k] for k in _AXIS_FIELDS if k in axis})
            out.append(meta)
    return out
