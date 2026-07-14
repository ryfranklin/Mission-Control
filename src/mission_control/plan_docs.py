"""Plan documents — the Planner's INCEPTION output as committed git artifacts.

The plan (mode, the CONSTRUCTION work-list with deps + status, plan-level status, and
the readiness-gated requirements) is the source of truth for a project. It must travel
WITH the project, not sit only in one host's Postgres — so it lives in the target repo
under ``aidlc-docs/inception/``, committed and pushed. Postgres becomes a *rebuildable
cache* of this git truth.

Reconciliation rule (authoritative): **git wins.** On load for a known project ref we
``ensure_local`` + fetch, read the on-disk plan, and overwrite the Postgres cache to
match it — clearing and re-inserting units/requirements so any local divergence (a
stale or extra cached row) resolves to the git version. A fresh host with an empty
Postgres therefore reconstructs the whole plan from the repo (the "doesn't start from
scratch" property).

On-disk layout under ``<target>/aidlc-docs/inception/``:

* ``flight-plan.yaml`` — the MACHINE-AUTHORITATIVE artifact: mode, plan status, the
  units (seq, title, phase, task_type, depends_on, status), and the readiness-gated
  requirements. Deterministic key ordering (sorted keys, units by seq, requirements by
  key) so diffs stay clean.
* ``requirements.md`` — a rendered, human-readable view of the same requirements (for
  reviewers reading the repo). NOT parsed back — ``flight-plan.yaml`` is the source
  read by :func:`load_plan`, because requirement *values* are free prose (a
  reverse-engineering summary may itself contain markdown headings) and must not be at
  the mercy of a markdown parser.
* ``turns/`` — optional per-checkpoint decision summaries; off by default. Metadata /
  spec only — never secrets or PII (these are committed and pushed).

``task_type`` is written for readability but DERIVED from ``phase`` on load
(:func:`aidlc.task_type_for_phase`) so the sim/burn mapping stays sourced from the
methodology, never trusted from the file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from . import aidlc, project_ref, repo_source, worktree
from .aidlc import Phase

# Location of the plan docs inside a target worktree.
DOCS_SUBDIR = Path("aidlc-docs") / "inception"
FLIGHT_PLAN_FILE = "flight-plan.yaml"
REQUIREMENTS_FILE = "requirements.md"

# Schema marker so a future format change is detectable in-repo.
_SCHEMA = 1


@dataclass
class UnitDoc:
    """One CONSTRUCTION/INCEPTION unit as it appears on disk."""

    seq: int
    title: str
    phase: str
    depends_on: list
    status: str

    @property
    def task_type(self) -> str:
        """Derived from phase (never trusted from the file)."""
        return aidlc.task_type_for_phase(Phase(self.phase)).value


@dataclass
class RequirementDoc:
    """One readiness-gated requirement as it appears on disk."""

    key: str
    value: str
    state: str


@dataclass
class PlanDoc:
    """A plan's portable, on-disk form. Round-trips through :func:`dump_plan` /
    :func:`load_plan`. ``task_type`` is derived, so equality compares the authoritative
    fields (mode, status, units-by-phase, requirements)."""

    mode: str
    status: str
    units: list = field(default_factory=list)          # list[UnitDoc]
    requirements: list = field(default_factory=list)   # list[RequirementDoc]


# -- serialization ---------------------------------------------------------

def docs_dir(local_repo) -> Path:
    """The plan-docs directory inside a target worktree."""
    return Path(local_repo) / DOCS_SUBDIR


def dump_plan(plan: PlanDoc, dir) -> None:
    """Write ``plan`` to ``dir`` as ``flight-plan.yaml`` (+ a rendered
    ``requirements.md``). Deterministic ordering so diffs are clean."""
    dir = Path(dir)
    dir.mkdir(parents=True, exist_ok=True)

    data = {
        "schema": _SCHEMA,
        "mode": plan.mode,
        "status": plan.status,
        "units": [
            {
                "seq": u.seq,
                "title": u.title,
                "phase": u.phase,
                "task_type": u.task_type,          # rendered; derived from phase on load
                "depends_on": list(u.depends_on),
                "status": u.status,
            }
            for u in sorted(plan.units, key=lambda u: u.seq)
        ],
        "requirements": [
            {"key": r.key, "value": r.value, "state": r.state}
            for r in sorted(plan.requirements, key=lambda r: r.key)
        ],
    }
    (dir / FLIGHT_PLAN_FILE).write_text(
        yaml.safe_dump(data, sort_keys=True, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    (dir / REQUIREMENTS_FILE).write_text(_render_requirements_md(plan), encoding="utf-8")


def load_plan(dir) -> PlanDoc:
    """Read a :class:`PlanDoc` back from ``flight-plan.yaml`` in ``dir``. Raises
    :class:`FileNotFoundError` if there is no plan on disk."""
    path = Path(dir) / FLIGHT_PLAN_FILE
    if not path.is_file():
        raise FileNotFoundError(f"no plan document at {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    units = [
        UnitDoc(
            seq=int(u["seq"]),
            title=str(u["title"]),
            phase=str(u["phase"]),
            depends_on=[int(d) for d in (u.get("depends_on") or [])],
            status=str(u.get("status", "pending")),
        )
        for u in (data.get("units") or [])
    ]
    requirements = [
        RequirementDoc(key=str(r["key"]), value=str(r.get("value", "")),
                       state=str(r.get("state", aidlc.REQ_OPEN)))
        for r in (data.get("requirements") or [])
    ]
    return PlanDoc(
        mode=str(data.get("mode", aidlc.MODE_GREENFIELD)),
        status=str(data.get("status", "")),
        units=units,
        requirements=requirements,
    )


def _render_requirements_md(plan: PlanDoc) -> str:
    """A human-readable rendering of the requirements (NOT parsed back)."""
    lines = [
        "# Requirements",
        "",
        "Readiness-gated requirements captured during AI-DLC INCEPTION. Machine-managed "
        "by Mission Control — the authoritative copy is `flight-plan.yaml`; edit via the "
        "planner, not by hand. Metadata/spec only (no secrets).",
        "",
    ]
    if not plan.requirements:
        lines.append("_None captured yet._")
        return "\n".join(lines) + "\n"
    for r in sorted(plan.requirements, key=lambda r: r.key):
        lines.append(f"## {r.key}  ({r.state})")
        lines.append("")
        lines.append(r.value.strip() or "_(no value)_")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# -- store <-> doc ---------------------------------------------------------

def plan_doc_from_store(store, plan_id: str) -> PlanDoc:
    """Build a :class:`PlanDoc` from the current Postgres plan state (the write side —
    what gets serialized to git at a checkpoint)."""
    plan = store.get_plan(plan_id)
    if plan is None:
        raise KeyError(plan_id)
    units = [
        UnitDoc(seq=u.seq, title=u.title, phase=u.phase,
                depends_on=list(u.depends_on or []), status=u.status)
        for u in store.list_units(plan_id)
    ]
    requirements = [
        RequirementDoc(key=r.key, value=r.value or "", state=r.state)
        for r in store.list_requirements(plan_id)
    ]
    return PlanDoc(mode=plan.mode, status=plan.status, units=units, requirements=requirements)


def reconcile_into_store(store, plan_id: str, plan: PlanDoc) -> None:
    """Overwrite the Postgres cache for ``plan_id`` to match the git ``plan`` (git
    wins). Sets mode/status, then CLEARS and re-inserts units + requirements so extra or
    diverged cached rows resolve to the on-disk version. The plan header must already
    exist (the caller opens it for the ref)."""
    store.set_mode(plan_id, plan.mode)
    if plan.status:
        store.set_status(plan_id, plan.status)
    store.clear_units(plan_id)
    for u in sorted(plan.units, key=lambda u: u.seq):
        store.upsert_unit(plan_id, u.seq, title=u.title, phase=Phase(u.phase),
                          depends_on=list(u.depends_on), status=u.status)
    store.clear_requirements(plan_id)
    for r in plan.requirements:
        store.upsert_requirement(plan_id, r.key, value=r.value, state=r.state)


# -- git sync (write direction: commit + push the plan docs) ---------------

def sync_to_repo(store, plan_id: str, *, cache_root: Optional[Path] = None) -> bool:
    """Write the plan docs into the target worktree, commit, and push — the durable
    handoff of an INCEPTION checkpoint / finalize to the remote.

    Acquires the target from its portable ref (``ensure_local``: clone-if-absent /
    fetch-if-present), so the docs always land on an up-to-date working copy, then
    records that derived path as the plan's ``local_path``. INCEPTION docs mutate no
    code, so there is no go/no-go gate — but they must reach the remote: the push reuses
    the gated :func:`repo_source.push_to_remote` (loud on failure, never force).

    Returns True if docs were written; False when there is no portable target yet (a
    greenfield plan whose remote is not created — a later slice). A push failure raises
    (``repo_source.PushError`` / ``PushRejected``) — the docs must land."""
    plan = store.get_plan(plan_id)
    if plan is None or not plan.target:
        return False  # no portable identity to acquire/push to (e.g. greenfield scratch)

    local = repo_source.ensure_local(plan.target, root=cache_root)
    if str(local) != (plan.local_path or ""):
        store.set_local_path(plan_id, str(local))

    dump_plan(plan_doc_from_store(store, plan_id), docs_dir(local))

    # Commit the docs directly on the working copy's branch (no task worktree — these
    # are read-only-to-code artifacts). Serialized with other shared-repo mutations.
    rel = str(DOCS_SUBDIR)
    with worktree._repo_lock(local):
        worktree._git(local, "add", rel)
        status = worktree._git(local, "status", "--porcelain", rel).stdout
        if status.strip():
            worktree._git(local, "commit", "-m", f"aidlc-docs: plan {plan_id} checkpoint")
    if repo_source.has_origin(local):
        repo_source.push_to_remote(local, repo_source.current_branch(local))
    return True


def load_from_repo(store, target: str, *, cache_root: Optional[Path] = None,
                   methodology: str, cloud_target: str, plan_id_factory) -> Optional[str]:
    """Rebuild the Postgres cache for ``target`` from its git plan docs and return the
    plan id (or ``None`` if the repo has no plan docs). GIT WINS.

    Acquires the target (``ensure_local`` + fetch), loads ``flight-plan.yaml``, finds an
    existing cached plan for this ref (else opens a fresh header via ``plan_id_factory``),
    and reconciles the cache to the on-disk plan. On a fresh host (empty Postgres) this
    fully reconstructs the plan from the repo — no re-running of INCEPTION."""
    ref, _ = project_ref.resolve_target(target)
    local = repo_source.ensure_local(ref, root=cache_root)
    try:
        doc = load_plan(docs_dir(local))
    except FileNotFoundError:
        return None

    existing = store.list_plans({"target": ref}, limit=1)
    if existing:
        plan_id = existing[0].id
        store.set_local_path(plan_id, str(local))
    else:
        plan_id = plan_id_factory()
        # Header opened with the default status; reconcile_into_store sets the real
        # status (and mode/units/requirements) from the git plan next.
        store.open_plan(plan_id, target=ref, local_path=str(local), mode=doc.mode,
                        methodology=methodology, cloud_target=cloud_target)
    reconcile_into_store(store, plan_id, doc)
    return plan_id
