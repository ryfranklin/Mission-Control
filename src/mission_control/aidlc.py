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
# The vendored AWS AI-DLC v2 methodology, installed by mission_control.aidlc_v2.
# Distinct from the legacy single-file/detail-dir flavors above: v2 is a directory
# tree MC reads as a catalog (see aidlc_v2.catalog), and it wins over any legacy hit.
FLAVOR_AIDLC_V2 = "aidlc-v2"

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


# -- planning: target modes, requirement states, and the finalize readiness rule --

# Target modes (AI-DLC vocabulary, not our metaphor): a fresh build vs. an existing
# codebase. Greenfield opens with "Using AI-DLC, …" (see apply_invocation); brownfield
# reverse-engineers first.
MODE_GREENFIELD = "greenfield"
MODE_BROWNFIELD = "brownfield"
MODES = (MODE_GREENFIELD, MODE_BROWNFIELD)

# Requirement lifecycle states (the accreting requirements' readiness). An ``open``
# requirement is captured but unresolved and blocks a brownfield finalize; ``ready``
# is resolved/agreed and counts toward the gate.
REQ_OPEN = "open"
REQ_READY = "ready"

# The always-execute INCEPTION stages a greenfield plan must lay down before it can be
# finalized (per the AI-DLC INCEPTION phase; the conditional stages — reverse
# engineering, user stories, application/units — are excluded here).
REQUIRED_INCEPTION_STAGES = (
    "Workspace Detection",
    "Requirements Analysis",
    "Workflow Planning",
)

# The requirement keys the brownfield readiness gate checks — the accreting, checkable
# requirements that make a plan "clear enough for Mission Control to operate".
REQ_KEY_SCOPE = "scope"
REQ_KEY_COMPONENTS = "affected_components"
REQ_KEY_ACCEPTANCE = "acceptance_criteria"

# Requirement keys for the reverse-engineering artifacts folded back from the sim run.
REQ_KEY_RE_SUMMARY = "reverse_engineering:summary"
REQ_KEY_RE_RUN = "reverse_engineering:run"


@dataclass(frozen=True)
class ReadinessCriterion:
    """One checkable finalize criterion, with whether it is currently met. The unmet
    ones are surfaced in ``GET /plans/{id}`` so the UI can show what is still blocking."""

    key: str
    label: str
    met: bool
    detail: str = ""


@dataclass(frozen=True)
class BrownfieldCriterion:
    """A brownfield readiness criterion: the requirement key that satisfies it (``None``
    for the units criterion) and the clarifying question that gathers it."""

    key: str
    label: str
    req_key: str | None
    question: "StageQuestion"


def _unit_wellformed(unit) -> tuple[bool, str]:
    """A unit is well-formed when it has a title, a valid phase, a ``task_type``
    consistent with that phase, and a list ``depends_on``."""
    if not (getattr(unit, "title", "") and str(unit.title).strip()):
        return False, "missing title"
    try:
        phase = Phase(unit.phase)
    except (ValueError, TypeError):
        return False, f"invalid phase {unit.phase!r}"
    if unit.task_type != task_type_for_phase(phase).value:
        return False, "task_type inconsistent with phase"
    if not isinstance(unit.depends_on, list):
        return False, "depends_on is not a list"
    return True, ""


def _worklist_criterion(
    units, *, key: str = "units", label: str = "CONSTRUCTION work-list is ready"
) -> ReadinessCriterion:
    """A plan is only executable once it has a non-empty, well-formed CONSTRUCTION
    work-list. Shared by both modes so neither can be finalized with nothing to build."""
    construction = [u for u in units if u.phase == Phase.CONSTRUCTION.value]
    malformed = [f"unit {u.seq}: {_unit_wellformed(u)[1]}"
                 for u in units if not _unit_wellformed(u)[0]]
    if not construction:
        detail = "no CONSTRUCTION units yet"
    elif malformed:
        detail = "; ".join(malformed)
    else:
        detail = ""
    return ReadinessCriterion(key, label, bool(construction) and not malformed, detail)


def readiness_report(
    mode: str, *, inception_stages=(), requirements=(), units=()
) -> list[ReadinessCriterion]:
    """The explicit, checkable finalize criteria for a plan, each flagged met/unmet.

    * **greenfield** — one criterion per always-execute INCEPTION stage, PLUS a
      non-empty, well-formed CONSTRUCTION work-list (so a plan can't be handed off with
      nothing to build — readiness flips green at Workflow Planning otherwise).
    * **brownfield** — the requirements-readiness gate: scope bounded, affected
      components identified, acceptance criteria stated, and every CONSTRUCTION unit
      well-formed. ("Clear enough for Mission Control to operate.")
    """
    if mode == MODE_BROWNFIELD:
        state_by_key = {r.key: r.state for r in requirements}
        crits: list[ReadinessCriterion] = []
        for c in BROWNFIELD_CRITERIA:
            if c.req_key is not None:
                met = state_by_key.get(c.req_key) == REQ_READY
                crits.append(ReadinessCriterion(
                    c.key, c.label, met, "" if met else "not yet captured"))
            else:  # the units criterion
                crits.append(_worklist_criterion(units, key=c.key, label=c.label))
        return crits
    # greenfield (the default mode)
    present = set(inception_stages)
    crits = [
        ReadinessCriterion(f"stage:{s}", f"{s} in place", s in present,
                           "" if s in present else "stage not yet completed")
        for s in REQUIRED_INCEPTION_STAGES
    ]
    crits.append(_worklist_criterion(units))
    return crits


def is_ready(criteria) -> bool:
    """A plan is finalizable when every readiness criterion is met."""
    return all(c.met for c in criteria)


def unmet_summary(criteria) -> str:
    """A short human summary of the unmet criteria (the finalize-refusal reason)."""
    return "; ".join(c.label for c in criteria if not c.met)


# -- the INCEPTION stage walk (drives the interactive planner) --------------
#
# The ordered AI-DLC INCEPTION stages the planner walks with the operator. Each is
# an INCEPTION-phase (read-only, ``sim``) activity; completing one lays it down as a
# ``plan_unit`` titled by its ``title`` (which is why the titles here MUST match
# REQUIRED_INCEPTION_STAGES for the required ones — the readiness gate looks them up
# by title). ``units_generation`` is the terminal stage that additionally emits the
# CONSTRUCTION (``burn``) work-list.


@dataclass(frozen=True)
class StageQuestion:
    """One clarifying question, with optional multiple-choice options (A, B, C…)."""

    text: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class InceptionStage:
    """One INCEPTION stage in the walk."""

    key: str
    title: str
    conditional: bool  # user stories run only if warranted; the rest always run
    questions: tuple[StageQuestion, ...]


INCEPTION_STAGES: tuple[InceptionStage, ...] = (
    InceptionStage(
        "workspace_detection", "Workspace Detection", False,
        (
            StageQuestion(
                "Is this a fresh build or an existing codebase?",
                ("Greenfield — a new project", "Brownfield — an existing repository"),
            ),
        ),
    ),
    InceptionStage(
        "requirements_analysis", "Requirements Analysis", False,
        (
            StageQuestion("What is the core problem this must solve?"),
            StageQuestion(
                "Which non-functional needs are in scope?",
                ("Performance", "Security", "Scalability", "None material yet"),
            ),
        ),
    ),
    InceptionStage(
        "user_stories", "User Stories", True,
        (
            StageQuestion("Who are the primary user personas, and what are their goals?"),
        ),
    ),
    InceptionStage(
        "workflow_planning", "Workflow Planning", False,
        (
            StageQuestion(
                "How should the build be sequenced?",
                ("Thin end-to-end slice first", "Backend then frontend", "Risk-first"),
            ),
        ),
    ),
    InceptionStage(
        "units_generation", "Units Generation", False,
        (
            StageQuestion(
                "Ready to decompose into a CONSTRUCTION work-list?",
                ("Yes — generate the units", "Not yet — refine first"),
            ),
        ),
    ),
)

INCEPTION_STAGE_BY_KEY = {s.key: s for s in INCEPTION_STAGES}
INCEPTION_STAGE_BY_TITLE = {s.title: s for s in INCEPTION_STAGES}

# The reverse-engineering stage title (a brownfield-only INCEPTION activity, run as a
# read-only sim; laid down as a unit like the other stages).
REVERSE_ENGINEERING_TITLE = "Reverse Engineering"

# The brownfield requirements-readiness gate, in the order the planner gathers them.
# Each criterion carries the clarifying question that captures it; the terminal
# ``units`` criterion is satisfied by the CONSTRUCTION work-list, not a requirement.
BROWNFIELD_CRITERIA: tuple[BrownfieldCriterion, ...] = (
    BrownfieldCriterion(
        "scope", "Scope is bounded", REQ_KEY_SCOPE,
        StageQuestion("What is the bounded scope of this change, and what is explicitly "
                      "OUT of scope?"),
    ),
    BrownfieldCriterion(
        "components", "Affected components are identified", REQ_KEY_COMPONENTS,
        StageQuestion("Which components / modules of the existing codebase will this "
                      "change touch?"),
    ),
    BrownfieldCriterion(
        "acceptance", "Acceptance criteria are stated", REQ_KEY_ACCEPTANCE,
        StageQuestion("What are the acceptance criteria — how will we know it is done "
                      "and correct?"),
    ),
    BrownfieldCriterion(
        "units", "Every CONSTRUCTION unit is well-formed", None,
        StageQuestion("Ready to decompose the change into a CONSTRUCTION work-list?",
                      ("Yes — generate the units", "Not yet — refine first")),
    ),
)

BROWNFIELD_CRITERION_BY_KEY = {c.key: c for c in BROWNFIELD_CRITERIA}


def next_inception_stage(
    completed_titles, *, user_stories_warranted: bool
) -> InceptionStage | None:
    """The next stage to work, given the stages already laid down. Skips the
    conditional ``user_stories`` stage unless it is warranted. Returns ``None`` when
    the walk is complete."""
    done = set(completed_titles)
    for stage in INCEPTION_STAGES:
        if stage.title in done:
            continue
        if stage.conditional and not user_stories_warranted:
            continue
        return stage
    return None


def format_question_block(title: str, questions) -> str:
    """Render clarifying questions in the AI-DLC question format: numbered questions,
    lettered options, and an ``[Answer]:`` tag the operator fills in."""
    lines = [f"## {title} — please answer:", ""]
    for i, q in enumerate(questions, start=1):
        lines.append(f"{i}. {q.text}")
        for letter, option in zip("ABCDE", q.options):
            lines.append(f"   {letter}) {option}")
        lines.append("   [Answer]: ")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_questions(stage: InceptionStage) -> str:
    """Render an INCEPTION stage's clarifying questions (AI-DLC question format)."""
    return format_question_block(stage.title, stage.questions)


def format_criterion(criterion: BrownfieldCriterion) -> str:
    """Render a brownfield criterion's clarifying question (AI-DLC question format)."""
    return format_question_block(criterion.label, (criterion.question,))


def default_steering(flavor: str = FLAVOR_GENERIC) -> AidlcSteering:
    """A minimal AI-DLC steering used when the target carries no detectable install —
    so a greenfield session still plans *under* AI-DLC (the 'So default' flavor). The
    core text names the INCEPTION phase so composed prompts stay methodology-anchored."""
    core = (
        "AI-DLC methodology (default steering). Work the INCEPTION phase as an "
        "interactive, read-only planning conversation: walk the stages "
        + ", ".join(s.title for s in INCEPTION_STAGES)
        + ". Ask clarifying questions before advancing a stage; never modify the "
        "target. The CONSTRUCTION phase (code generation) is gated and comes later."
    )
    return AidlcSteering(core_rules_text=core, detail_rules_dir=None, flavor=flavor)


@dataclass(frozen=True)
class AidlcSteering:
    """A normalized AI-DLC install detected in a target worktree."""

    core_rules_text: str
    detail_rules_dir: Path | None  # None for bundled single-file installs
    flavor: str
    # For the v2 flavor: the installed methodology root MC reads as a catalog (the dir
    # holding ``aidlc-common/stages/<phase>/*.md``). ``None`` for every legacy flavor.
    catalog_root: Path | None = None


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


# The v2 methodology is installed under this dir (see mission_control.aidlc_v2.install);
# it is the catalog root holding ``aidlc-common/stages/<phase>/*.md``. Kept as a literal
# here so aidlc.py has no import-time dependency on the aidlc_v2 package.
_V2_INSTALL_DIRNAME = ".aidlc"
_V2_STAGES_GLOB = "aidlc-common/stages/*/*.md"


def _probe_v2(root: Path) -> AidlcSteering | None:
    """Detect a v2 install: the ``.aidlc/`` catalog root with at least one
    ``aidlc-common/stages/<phase>/*.md``. Self-identifying (dir-named), so no content
    signature is needed. Returns steering carrying the resolvable ``catalog_root``."""
    catalog_root = root / _V2_INSTALL_DIRNAME
    if not any(catalog_root.glob(_V2_STAGES_GLOB)):
        return None
    core = (
        "AI-DLC v2 methodology is installed in this target. Mission Control drives its "
        "stages (initialization → ideation → inception → construction → operation) via "
        "its own orchestration and go/no-go gate; v2's hooks and tools are not run."
    )
    return AidlcSteering(
        core_rules_text=core,
        detail_rules_dir=None,
        flavor=FLAVOR_AIDLC_V2,
        catalog_root=catalog_root,
    )


def probe(worktree_root: Path) -> AidlcSteering | None:
    """Probe known AI-DLC install paths; return the first hit normalized, or ``None`` to
    run plain (AI-DLC is opt-in per target).

    The v2 catalog layout is checked FIRST and wins over any legacy install; the legacy
    single-file/detail-dir probe order (unchanged) runs only when there is no v2 tree."""
    root = Path(worktree_root)
    v2 = _probe_v2(root)
    if v2 is not None:
        return v2
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
