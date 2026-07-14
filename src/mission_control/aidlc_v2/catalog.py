"""Adapter: turn the vendored AI-DLC **v2** methodology into a machine model.

Pure and offline — the only I/O is reading the vendored stage files under
``methodology/aidlc-common/stages/``. Each stage's YAML frontmatter becomes a
:class:`StageSpec`; :func:`load_catalog` returns the whole set in a dependency-valid
order.

The *methodology* is upstream content; the **classification** of each stage into
Mission Control's own execution kinds (``plan`` / ``sim`` / ``burn``) is MC-owned and
lives in one obvious editable block below (:data:`_PHASE_DEFAULT` +
:data:`_STAGE_OVERRIDE`). MC reads v2 as content and runs its own orchestration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# Target modes (AI-DLC vocabulary — mirrors mission_control.aidlc, kept local so this
# adapter has no dependency on the v1 module).
MODE_GREENFIELD = "greenfield"
MODE_BROWNFIELD = "brownfield"

# The five v2 phases, in methodology order. Used as the topological-sort tie-breaker
# and as the key for the classification default table.
PHASE_ORDER = ("initialization", "ideation", "inception", "construction", "operation")
_PHASE_INDEX = {p: i for i, p in enumerate(PHASE_ORDER)}


# -- MC-owned classification (the one obvious editable block) ----------------
#
# kind ∈ {"plan","sim","burn"}; deferred = "not run in v1 (needs cloud creds)".
#   plan — interactive, artifact-only planning (no code side effects).
#   sim  — read-only analysis/design over the target.
#   burn — mutates the target; gated by go/no-go.
# Lookup: a per-stage override keyed by (phase, slug) wins; otherwise the phase default.

_PHASE_DEFAULT: dict[str, tuple[str, bool]] = {
    "initialization": ("plan", False),
    "ideation": ("plan", False),
    "inception": ("plan", False),
    "construction": ("burn", False),  # default; specific stages overridden below
    "operation": ("burn", True),      # deferred in v1 — needs cloud credentials
}

_STAGE_OVERRIDE: dict[tuple[str, str], tuple[str, bool]] = {
    # construction design work is read-only analysis → sim
    ("construction", "functional-design"): ("sim", False),
    ("construction", "nfr-requirements"): ("sim", False),
    ("construction", "nfr-design"): ("sim", False),
    # construction stages that mutate the target → burn
    ("construction", "code-generation"): ("burn", False),
    ("construction", "build-and-test"): ("burn", False),
    ("construction", "ci-pipeline"): ("burn", False),
    ("construction", "infrastructure-design"): ("burn", False),
}


def classify(phase: str, slug: str) -> tuple[str, bool]:
    """Return ``(kind, deferred)`` for a stage — per-stage override then phase default."""
    if (phase, slug) in _STAGE_OVERRIDE:
        return _STAGE_OVERRIDE[(phase, slug)]
    return _PHASE_DEFAULT.get(phase, ("plan", False))


# -- the machine model -------------------------------------------------------


@dataclass(frozen=True)
class Consumed:
    """An artifact a stage consumes, and under what terms."""

    artifact: str
    required: bool = False
    conditional_on: str | None = None


@dataclass(frozen=True)
class StageSpec:
    """A single vendored v2 stage, normalized for the runtime."""

    slug: str
    phase: str
    title: str
    path: Path
    execution: str  # "ALWAYS" | "CONDITIONAL" | "" (tolerant of omission)
    condition: str
    lead_agent: str | None
    support_agents: list[str]
    reviewer: str | None
    requires_stage: list[str]
    produces: list[str]
    consumes: list[Consumed]
    sensors: list[str]
    scopes: list[str]
    kind: str  # derived (MC-owned): "plan" | "sim" | "burn"
    deferred: bool  # derived (MC-owned)


# -- parsing -----------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Return ``(frontmatter_dict, body)``. Empty dict if there is no ``---`` head."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    data = yaml.safe_load(m.group(1)) or {}
    if not isinstance(data, dict):
        data = {}
    return data, text[m.end():]


def _as_list(value) -> list:
    """Coerce a scalar/None/list frontmatter value to a list (YAML gives us the right
    shape already; this just tolerates a bare scalar or an omitted key)."""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _str_list(value) -> list[str]:
    return [str(v) for v in _as_list(value)]


def _parse_consumes(value) -> list[Consumed]:
    """Parse the ``consumes:`` block — a list of nested objects
    ``{artifact, required, conditional_on}`` (NOT bare strings like ``produces:``).
    Tolerates a bare string entry by treating it as an optional artifact."""
    out: list[Consumed] = []
    for item in _as_list(value):
        if isinstance(item, dict):
            artifact = item.get("artifact")
            if artifact is None:
                continue
            out.append(
                Consumed(
                    artifact=str(artifact),
                    required=bool(item.get("required", False)),
                    conditional_on=(
                        str(item["conditional_on"])
                        if item.get("conditional_on") is not None
                        else None
                    ),
                )
            )
        else:  # tolerate a bare string
            out.append(Consumed(artifact=str(item), required=False))
    return out


def _title_from_body(body: str, slug: str) -> str:
    """The stage title is the first ``# H1`` in the body; fall back to the slug."""
    m = _H1_RE.search(body)
    if m:
        return m.group(1).strip()
    return slug.replace("-", " ").title()


def parse_stage(path: Path) -> StageSpec:
    """Parse one ``<phase>/<stage>.md`` file into a :class:`StageSpec`. Tolerant of any
    missing optional key; ``slug``/``phase`` fall back to the file path if omitted."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(text)

    slug = str(fm.get("slug") or path.stem)
    phase = str(fm.get("phase") or path.parent.name)
    kind, deferred = classify(phase, slug)

    return StageSpec(
        slug=slug,
        phase=phase,
        title=_title_from_body(body, slug),
        path=path,
        execution=str(fm.get("execution") or ""),
        condition=str(fm.get("condition") or ""),
        lead_agent=(str(fm["lead_agent"]) if fm.get("lead_agent") else None),
        support_agents=_str_list(fm.get("support_agents")),
        reviewer=(str(fm["reviewer"]) if fm.get("reviewer") else None),
        requires_stage=_str_list(fm.get("requires_stage")),
        produces=_str_list(fm.get("produces")),
        consumes=_parse_consumes(fm.get("consumes")),
        sensors=_str_list(fm.get("sensors")),
        scopes=_str_list(fm.get("scopes")),
        kind=kind,
        deferred=deferred,
    )


# -- loading + ordering ------------------------------------------------------


def _base_priority(stage: StageSpec) -> tuple[int, str]:
    """Fallback ordering key: phase order, then declared (slug) order within a phase."""
    return (_PHASE_INDEX.get(stage.phase, len(PHASE_ORDER)), stage.slug)


def _toposort(stages: list[StageSpec]) -> list[StageSpec]:
    """Order stages so none precedes a stage it ``requires_stage``. Kahn's algorithm
    with the phase/declared order as the tie-breaker among ready nodes, so the result
    stays as close to methodology order as the dependencies allow. Edges to unknown
    slugs are ignored; any residual cycle is appended in base order (never dropped)."""
    by_slug = {s.slug: s for s in stages}
    # deps[s] = required stages that exist in this set (dedup, ignore self + unknowns)
    deps: dict[str, set[str]] = {
        s.slug: {r for r in s.requires_stage if r in by_slug and r != s.slug}
        for s in stages
    }
    # StageSpec is unhashable (it holds lists), so track readiness by slug and keep
    # `ready` a slug list sorted by the phase/declared-order tie-breaker.
    priority = {s.slug: _base_priority(s) for s in stages}
    placed: set[str] = set()
    ordered: list[StageSpec] = []

    def _ready_slugs() -> list[str]:
        return sorted(
            (slug for slug in by_slug if slug not in placed and deps[slug] <= placed),
            key=lambda slug: priority[slug],
        )

    while (ready := _ready_slugs()):
        slug = ready[0]
        ordered.append(by_slug[slug])
        placed.add(slug)
    # Append anything left (a dependency cycle) in base order — never drop a stage.
    if len(ordered) != len(stages):
        leftover = sorted(
            (s for s in stages if s.slug not in placed), key=_base_priority
        )
        ordered.extend(leftover)
    return ordered


def default_methodology_root() -> Path:
    """The vendored methodology root shipped inside this package."""
    return Path(__file__).resolve().parent / "methodology"


def stages_dir(root: Path) -> Path:
    """The vendored stages directory under a methodology root."""
    return Path(root) / "aidlc-common" / "stages"


def load_catalog(root: Path) -> list[StageSpec]:
    """Parse every ``aidlc-common/stages/<phase>/<stage>.md`` under ``root`` and return
    the stages in a dependency-valid (topologically sorted) order."""
    base = stages_dir(root)
    stages = [parse_stage(p) for p in sorted(base.glob("*/*.md"))]
    return _toposort(stages)


# -- applicability -----------------------------------------------------------


def applicable(
    stage: StageSpec,
    *,
    mode: str,
    scope: str | None = None,
    enable_deferred: bool = False,
) -> bool:
    """Whether a stage should run for a given ``mode``/``scope``.

    Heuristics (the free-text ``condition`` is advisory — the engine decides the rest
    at run time):
      * ``deferred`` stages are skipped unless ``enable_deferred`` is set.
      * ``reverse-engineering`` is brownfield-only.
      * a ``scope`` outside the stage's declared ``scopes`` excludes it.
      * ``ALWAYS`` stages otherwise run; ``CONDITIONAL`` default to eligible.
    """
    if stage.deferred and not enable_deferred:
        return False
    if scope is not None and stage.scopes and scope not in stage.scopes:
        return False
    if stage.slug == "reverse-engineering":
        return mode == MODE_BROWNFIELD
    # ALWAYS → run; CONDITIONAL → eligible (runtime evaluates the specific condition).
    return True
