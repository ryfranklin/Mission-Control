"""Render v2's own ``aidlc-state.md`` from Mission Control's plan state.

MC owns orchestration, so it keeps v2's state file coherent instead of running
``aidlc-state.ts``: the file is DERIVED from the plan (git is the source of truth, the
Postgres plan store is a rebuildable cache), scaffolded from the vendored
``knowledge/aidlc-shared/state-template.md`` so the format stays faithful to what a real
v2 harness expects.

Scope is deliberately narrow (NOT a reimplementation of v2's state machine): MC lists
the compiled stages under their phase headings and marks each ``[x]`` (completed) or
``[ ]`` (pending). It does not reproduce v2's ``[-] [?] [R] [S]`` transitions — a stage
is either done in the plan or not.
"""

from __future__ import annotations

from pathlib import Path

from .catalog import PHASE_ORDER, applicable

STATE_FILENAME = "aidlc-state.md"

_LEGEND = ("<!-- Checkbox states: [ ] pending, [-] in-progress, [?] awaiting approval, "
           "[R] revising, [x] completed, [S] skipped -->")

# Used only if the vendored template is somehow absent — keeps rendering robust.
_FALLBACK_TEMPLATE = (
    "# AI-DLC State Tracking\n\n"
    "## Project Information\n- **Project Type**: [Greenfield/Brownfield]\n\n"
    "## Stage Progress\n\n## Current Status\n- **Status**: [Running/Completed]\n"
)


def state_file_path(record_root: Path) -> Path:
    """``aidlc-state.md`` lives at the record root (``<target>/aidlc-docs/``), matching
    v2's ``<record>/aidlc-state.md``."""
    return Path(record_root) / STATE_FILENAME


def _template(catalog_root: Path) -> str:
    tmpl = Path(catalog_root) / "knowledge" / "aidlc-shared" / "state-template.md"
    if tmpl.is_file():
        return tmpl.read_text(encoding="utf-8", errors="ignore")
    return _FALLBACK_TEMPLATE


def _stage_progress(catalog, *, mode: str, scope: str | None, completed_slugs) -> str:
    """The generated ``## Stage Progress`` rows: one ``### <PHASE> PHASE`` heading per
    compiled phase, then one checkbox row per applicable stage (``[x]`` if completed)."""
    done = set(completed_slugs)
    lines: list[str] = [_LEGEND, ""]
    for phase in PHASE_ORDER:
        stages = [
            s for s in catalog
            if s.phase == phase
            and applicable(s, mode=mode, scope=scope, enable_deferred=True)
        ]
        if not stages:
            continue
        lines.append(f"### {phase.upper()} PHASE")
        for s in stages:
            mark = "x" if s.slug in done else " "
            lines.append(f"- [{mark}] {s.slug} — EXECUTE")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_state_file(
    catalog, *, catalog_root: Path, mode: str, completed_slugs, scope: str | None = None
) -> str:
    """Build ``aidlc-state.md`` from the vendored template, with the Stage Progress
    section filled from the plan state. Deterministic (no timestamps) so diffs stay
    clean and the content guard sees stable metadata."""
    template = _template(catalog_root)
    template = template.replace("[Greenfield/Brownfield]", mode.capitalize())

    section = "## Stage Progress\n" + _stage_progress(
        catalog, mode=mode, scope=scope, completed_slugs=completed_slugs)

    marker = "## Stage Progress"
    idx = template.find(marker)
    if idx == -1:  # template has no such section → append one
        return template.rstrip() + "\n\n" + section + "\n"
    after = template.find("\n## ", idx + len(marker))
    head = template[:idx].rstrip()
    tail = template[after:].lstrip("\n") if after != -1 else ""
    parts = [head, "", section]
    if tail:
        parts += ["", tail]
    return "\n".join(parts).rstrip() + "\n"
