"""AI-DLC steering — detect a target repo's AI-DLC install and compose it in.

AI-DLC is a methodology the runtime applies to *target* repos at run time (per the
AI-DLC config-placement spec). Mission Control itself is not built under AI-DLC. AI-DLC installs into agent-specific locations inside the target
repo, so detection is a **multi-location probe** in priority order that takes the
first hit and normalizes it to ``(core_rules_text, detail_rules_dir | None,
flavor)``.

Because a worktree is a checkout of the target repo, its rules already travel
with it — the runtime seeds nothing; it reads what is already there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .tasks import TaskType

# Install flavors (functional identifiers, not Mission Control metaphor).
FLAVOR_GENERIC = "generic"
FLAVOR_CLAUDE = "claude"
FLAVOR_KIRO = "kiro"
FLAVOR_AMAZON_Q = "amazon-q"
FLAVOR_CURSOR = "cursor"
FLAVOR_CLINE = "cline"
FLAVOR_COPILOT = "copilot"

# The greenfield task-prompt opener.
AIDLC_OPENER = "Using AI-DLC, "


class Phase(str, Enum):
    """AI-DLC methodology phases (external vocabulary, not our metaphor)."""

    INCEPTION = "INCEPTION"  # what / why — read-only, produces aidlc-docs/
    CONSTRUCTION = "CONSTRUCTION"  # how — mutates code, gated


def task_type_for_phase(phase: Phase) -> TaskType:
    """INCEPTION → read-only (sim); CONSTRUCTION → side-effectful (burn)."""
    return (
        TaskType.READ_ONLY
        if phase is Phase.INCEPTION
        else TaskType.SIDE_EFFECTFUL
    )


@dataclass(frozen=True)
class AidlcSteering:
    """A normalized AI-DLC install detected in a target worktree."""

    core_rules_text: str
    detail_rules_dir: Path | None  # None for bundled single-file installs
    flavor: str


@dataclass(frozen=True)
class _Candidate:
    flavor: str
    core_rel: str  # core steering file, relative to the worktree root
    detail_rel: str | None  # detail-rules dir, relative; None if bundled
    needs_signature: bool  # shared filenames must prove AI-DLC content


# Priority order (first hit wins). Shared filenames require a content signature;
# dir-based / AI-DLC-named paths are self-identifying.
_PROBE_ORDER: tuple[_Candidate, ...] = (
    _Candidate(FLAVOR_GENERIC, "AGENTS.md", ".aidlc-rule-details", True),
    _Candidate(FLAVOR_CLAUDE, ".claude/CLAUDE.md", ".aidlc-rule-details", True),
    _Candidate(FLAVOR_CLAUDE, "CLAUDE.md", ".aidlc-rule-details", True),
    _Candidate(
        FLAVOR_KIRO,
        ".kiro/steering/aws-aidlc-rules/core-workflow.md",
        ".kiro/aws-aidlc-rule-details",
        False,
    ),
    _Candidate(
        FLAVOR_AMAZON_Q,
        ".amazonq/rules/aws-aidlc-rules/core-workflow.md",
        ".amazonq/aws-aidlc-rule-details",
        False,
    ),
    _Candidate(FLAVOR_CURSOR, ".cursor/rules/ai-dlc-workflow.mdc", None, False),
    _Candidate(FLAVOR_CLINE, ".clinerules/core-workflow.md", ".aidlc-rule-details", False),
    _Candidate(FLAVOR_COPILOT, ".github/copilot-instructions.md", None, True),
)

# AI-DLC content signature: phase markers, or core-workflow / AI-DLC structure.
_SIGNATURE = re.compile(
    r"\bINCEPTION\b|\bCONSTRUCTION\b|(?i:\bai[- ]?dlc\b)|(?i:core[- ]workflow)"
)


def has_aidlc_signature(text: str) -> bool:
    """True if the text looks like AI-DLC steering (guards shared filenames)."""
    return bool(_SIGNATURE.search(text))


def probe(worktree_root: Path) -> AidlcSteering | None:
    """Probe known AI-DLC install paths in priority order; return the first hit
    normalized, or ``None`` to run plain (AI-DLC is opt-in per target)."""
    root = Path(worktree_root)
    for c in _PROBE_ORDER:
        core = root / c.core_rel
        if not core.is_file():
            continue
        text = core.read_text(encoding="utf-8", errors="ignore")
        # False-positive guard: a shared filename only counts with a signature.
        if c.needs_signature and not has_aidlc_signature(text):
            continue
        detail_dir: Path | None = None
        if c.detail_rel is not None:
            candidate = root / c.detail_rel
            if candidate.is_dir():
                detail_dir = candidate
        return AidlcSteering(
            core_rules_text=text,
            detail_rules_dir=detail_dir,
            flavor=c.flavor,
        )
    return None


def compose_system_prompt(base_prompt: str, steering: AidlcSteering) -> str:
    """Fold the target's own AI-DLC rules into the worker's system prompt."""
    parts = [
        base_prompt,
        "",
        f"This target project follows AI-DLC (install flavor: {steering.flavor}). "
        "Its own rules below are authoritative — follow them:",
        "",
        "----- BEGIN AI-DLC RULES -----",
        steering.core_rules_text.strip(),
        "----- END AI-DLC RULES -----",
    ]
    if steering.detail_rules_dir is not None:
        parts.append(
            f"\nDetailed rules live in '{steering.detail_rules_dir}'. "
            "Consult them on demand as the rules direct."
        )
    return "\n".join(parts)


def apply_invocation(prompt: str, *, greenfield: bool) -> str:
    """Greenfield tasks open with 'Using AI-DLC, …'; brownfield skip the opener."""
    return f"{AIDLC_OPENER}{prompt}" if greenfield else prompt
