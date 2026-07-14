"""Task model — what a worker is asked to do.

Functional names only. The two task types map to metaphor vocabulary (``sim`` /
``burn``) whose *string* values are sourced from :mod:`.roles`, so a metaphor
swap stays a one-file change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from . import roles


class TaskType(Enum):
    """Read-only vs side-effectful work.

    Member *names* are functional; member *values* come from :mod:`.roles` so the
    metaphor terms live in exactly one place.
    """

    READ_ONLY = roles.SIM  # investigate without mutating the target
    SIDE_EFFECTFUL = roles.BURN  # produces changes gated behind approval

    @property
    def is_side_effectful(self) -> bool:
        return self is TaskType.SIDE_EFFECTFUL


@dataclass(frozen=True)
class Task:
    """A unit of work handed to a worker."""

    task_id: str
    task_type: TaskType
    prompt: str
    # Greenfield targets get the "Using AI-DLC, …" prompt opener when steering
    # is detected; brownfield targets skip it.
    greenfield: bool = False
    # Optional workstream name → the long-lived mc/ws/<name> branch this task builds on
    # and reconciles into (None → work directly on trunk, the single-line default).
    workstream: Optional[str] = None
    # Explicit operator override of the egress content guard (recorded for audit). Default
    # False → a secret/PII in the pushed content blocks the run.
    allow_secrets: bool = False
