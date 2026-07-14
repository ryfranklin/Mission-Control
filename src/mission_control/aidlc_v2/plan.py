"""Turn a v2 catalog into a Mission Control plan: the planning walk + the work-list.

Pure functions over a loaded catalog (``list[StageSpec]``) — no I/O, no probing. The
caller (the planner engine / manager) probes the target, loads the catalog, and hands
it here. This keeps the metaphor rule and the "v2 is content" boundary intact: MC's own
classification (``kind`` → ``plan``/``sim``/``burn``) lives in :mod:`.catalog`; the
mapping into MC's plan units / readiness lives here.

* the **INCEPTION walk** = the applicable ``kind=="plan"`` stages, in dependency order;
* the **work-list** = one MC unit per applicable ``kind in {"sim","burn"}`` stage, with
  ``task_type`` from the kind (``sim``→sim, ``burn``→burn), ``phase`` the stage's v2
  phase, and dependencies from the stage ``requires_stage`` / ``consumes`` DAG. Deferred
  (``operation``) stages are RECORDED as units but flagged so the builder never
  dispatches them (v1: no cloud creds).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..aidlc import ReadinessCriterion
from ..roles import BURN, SIM
from .catalog import StageSpec, applicable

# The v2 kinds that become dispatchable MC work (vs. ``plan``, the interactive walk).
_WORK_KINDS = ("sim", "burn")


@dataclass(frozen=True)
class PlannedUnit:
    """One MC work-list unit derived from a non-plan v2 stage."""

    stage_slug: str
    phase: str          # the stage's v2 phase (e.g. "construction", "operation")
    task_type: str      # SIM | BURN — from the stage kind, not the phase
    title: str
    deferred: bool       # recorded but never dispatched (v1: operation needs cloud creds)
    requires: tuple[str, ...]  # non-plan stage slugs this unit depends on


def plan_stages(catalog: list[StageSpec], *, mode: str, scope: str | None = None
                ) -> list[StageSpec]:
    """The interactive INCEPTION walk: applicable ``kind=="plan"`` stages, in the
    catalog's dependency order."""
    return [
        s for s in catalog
        if s.kind == "plan" and applicable(s, mode=mode, scope=scope)
    ]


def unit_stages(catalog: list[StageSpec], *, mode: str, scope: str | None = None
                ) -> list[StageSpec]:
    """The non-plan stages that become MC units, in dependency order. Deferred stages
    are INCLUDED (recorded) — ``enable_deferred=True`` — but carry ``deferred=True`` so
    the caller can mark them not-to-dispatch."""
    return [
        s for s in catalog
        if s.kind in _WORK_KINDS
        and applicable(s, mode=mode, scope=scope, enable_deferred=True)
    ]


def build_units(catalog: list[StageSpec], *, mode: str, scope: str | None = None
                ) -> list[PlannedUnit]:
    """One :class:`PlannedUnit` per applicable non-plan stage, in dependency order, with
    ``requires`` built from the stage ``requires_stage``/``consumes`` DAG (restricted to
    the other units — dependencies on already-completed plan stages are dropped)."""
    stages = unit_stages(catalog, mode=mode, scope=scope)
    slugs = {s.slug for s in stages}
    # Which unit produces each artifact (for consumes-based edges); last writer wins.
    produced_by = {art: s.slug for s in stages for art in s.produces}

    units: list[PlannedUnit] = []
    for s in stages:
        requires: list[str] = []
        seen: set[str] = set()
        for r in s.requires_stage:               # explicit stage dependencies
            if r in slugs and r != s.slug and r not in seen:
                requires.append(r)
                seen.add(r)
        for c in s.consumes:                     # implicit: producer of a consumed artifact
            prod = produced_by.get(c.artifact)
            if prod and prod in slugs and prod != s.slug and prod not in seen:
                requires.append(prod)
                seen.add(prod)
        units.append(PlannedUnit(
            stage_slug=s.slug,
            phase=s.phase,
            task_type=BURN if s.kind == "burn" else SIM,
            title=s.title,
            deferred=s.deferred,
            requires=tuple(requires),
        ))
    return units


def _unit_wellformed(unit) -> bool:
    """A v2 build unit is well-formed when it names its stage, carries a sim/burn
    task_type, and has a list ``depends_on``."""
    return bool(
        getattr(unit, "stage_slug", None)
        and unit.task_type in (SIM, BURN)
        and isinstance(unit.depends_on, list)
    )


def readiness(
    catalog: list[StageSpec],
    *,
    mode: str,
    scope: str | None = None,
    completed_slugs,
    units,
) -> list[ReadinessCriterion]:
    """The v2 finalize gate, reusing the shared :class:`ReadinessCriterion` machinery:
    one criterion per applicable ``kind=="plan"`` stage (met once laid down) PLUS a
    non-empty, well-formed work-list criterion. ``units`` are the plan's build units
    (the non-plan units — plan-stage INCEPTION units are excluded by the caller)."""
    done = set(completed_slugs)
    crits = [
        ReadinessCriterion(
            f"stage:{s.slug}", f"{s.title} in place", s.slug in done,
            "" if s.slug in done else "stage not yet completed",
        )
        for s in plan_stages(catalog, mode=mode, scope=scope)
    ]
    build = list(units)
    malformed = [u for u in build if not _unit_wellformed(u)]
    if not build:
        detail = "no work-list units yet"
    elif malformed:
        detail = f"{len(malformed)} malformed unit(s)"
    else:
        detail = ""
    crits.append(ReadinessCriterion(
        "units", "Work-list is ready", bool(build) and not malformed, detail))
    return crits


def stage_question(stage: StageSpec) -> str:
    """Render the current plan stage's clarifying question, sourced from the stage
    file's frontmatter guidance (its ``condition`` and what it must ``produce``)."""
    from ..aidlc import StageQuestion, format_question_block

    guidance = stage.condition.strip() or f"Work the {stage.title} stage."
    if stage.produces:
        guidance += " This stage should produce: " + ", ".join(stage.produces) + "."
    return format_question_block(stage.title, (StageQuestion(guidance),))
