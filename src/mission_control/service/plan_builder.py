"""The plan builder — hands a finalized Flight Plan to Mission Control.

On finalize (readiness met), a plan's ``plan_units`` are translated into runs and
dispatched on the EXISTING launch path — this adds NO new orchestration:

* INCEPTION / validation units (``task_type == sim``) → **sim** runs (read-only);
* CONSTRUCTION / change units (``task_type == burn``) → **burn** runs behind the
  normal go/no-go gate.

``task_type`` is already on each unit (via :func:`aidlc.task_type_for_phase`), so the
builder just reads it.

**Committed unit status is the authority for what to dispatch** (the portable-progress
record): a unit is runnable only if its status != ``done`` and every unit in its
``depends_on`` is ``done``. That status lives in flight-plan.yaml (Prompt 4), reconciled
into Postgres on load — so a build resumed on a DIFFERENT machine (empty Postgres, git
reconciled) dispatches exactly the not-done, dependency-satisfied units, never re-running
completed ones. The host-local in-flight LangGraph checkpoint is deliberately NOT
portable: the worst case on a host swap is that one mid-flight unit re-runs from the
start (a re-run of one unit never cascades into rebuilding completed ones — those are
``done`` in git).

It is edge-triggered: :meth:`start_build` (and :meth:`resume_builds` on restart)
dispatch the runnable units; each time a child run reaches a terminal state the
RunManager calls :meth:`on_run_terminal` (on the loop), which — on SUCCESS — marks the
unit ``done`` and pushes that to git, then dispatches any newly-unblocked units and
rolls the plan's status up (``building`` → ``done``). A no-go/failed run does NOT advance
status; its dependents stay blocked, but the plan itself continues.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from .. import aidlc, plan_docs, repo_source, roles, worktree
from ..plans_store import (
    STATUS_BUILDING,
    STATUS_DONE,
    UNIT_BLOCKED,
    UNIT_DEFERRED,
    UNIT_DONE,
    UNIT_PENDING,
    PlanStore,
)
from ..runs_store import (
    STATUS_APPLIED,
    STATUS_AWAITING_GATE,
    STATUS_BLOCKED_SECRETS,
    STATUS_DONE as RUN_STATUS_DONE,
    STATUS_FAILED,
    STATUS_MERGE_CONFLICT,
    STATUS_PUSH_REJECTED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SCRUBBED,
)

# A dependency counts as satisfied only when its run SUCCEEDED (a sim done, a burn
# applied on a go AND pushed). A scrubbed / failed / push-rejected dep blocks its
# dependents — a push that didn't land means the change isn't on the trunk the next
# unit would build off, so building on it would diverge (full conflict handling later).
_SUCCESS = frozenset({RUN_STATUS_DONE, STATUS_APPLIED})
_FAILED = frozenset({STATUS_SCRUBBED, STATUS_FAILED, STATUS_PUSH_REJECTED, STATUS_MERGE_CONFLICT})
# A run still in progress (not terminal) — its unit is not (yet) re-dispatchable.
_INFLIGHT = frozenset({STATUS_QUEUED, STATUS_RUNNING, STATUS_AWAITING_GATE})

# CAPCOM's bounded re-run loop: a stage that produces nothing is re-dispatched (with
# escalated instruction) up to this many total attempts, then held. Bounded so a stuck
# stage can't loop forever (the operator's "no unnecessary loops"). Overridable per run.
MAX_STAGE_ATTEMPTS = int(os.environ.get("MC_STAGE_MAX_ATTEMPTS", "3"))

# Appended to a retry's prompt when CAPCOM can't diagnose a specific missing input.
_RETRY_NOTE = (
    "\n\nCAPCOM RETRY — your previous attempt at this stage wrote NO files. You MUST "
    "actually create this stage's artifacts on disk now (write the files; do not merely "
    "describe them or report that inputs are missing). Use the inputs already present in "
    "the working directory."
)


def _escalation_note(missing: list) -> str:
    """CAPCOM's diagnostic re-run instruction. When specific consumed inputs are absent
    on disk, name them so the worker adapts (proceed on what's present, note what's not);
    otherwise fall back to the generic retry nudge."""
    if not missing:
        return _RETRY_NOTE
    return (
        "\n\nCAPCOM DIAGNOSIS — your previous attempt wrote NO files. These inputs you "
        "consume are NOT present on disk: " + ", ".join(missing) + ". Proceed using the "
        "inputs that ARE available and produce your artifacts from those. If a missing "
        "input is strictly required, still write your best-effort output and note exactly "
        "what was missing at the top of the file. You MUST write your files this time."
    )


def _secrets_note(detail: str) -> str:
    """Feed the egress guard's findings back to the worker so its re-run fixes the CAUSE:
    name what tripped the guard and require placeholders / env references — never real or
    realistic secret values. (The previous attempt was blocked; nothing was applied.)"""
    findings = detail.split("egress —", 1)[-1].strip() if "egress" in (detail or "") else ""
    where = f" It flagged: {findings}." if findings else ""
    return (
        "\n\nCAPCOM DIAGNOSIS — your previous attempt was BLOCKED by the egress secret "
        "guard and NOTHING was applied." + where + " Do NOT commit real or realistic "
        "credentials, connection strings with real passwords, or hardcoded "
        "`secret = \"...\"` string literals. Read secrets from environment variables "
        "(e.g. process.env.X / os.environ); keep .env.example and README values as obvious "
        "placeholders (CHANGEME); point example connection strings at localhost."
    )


class PlanBuilder:
    """Schedules a plan's units onto the run launch path, in dependency order."""

    def __init__(self, plan_store: PlanStore, run_manager, *, workspaces_dir=None,
                 docs_sync=None, cache_root=None) -> None:
        self._plans = plan_store
        self._runs = run_manager
        # Persists a unit's ``done`` mark to git (write flight-plan.yaml + commit +
        # push) — the portable progress record. None → no git sync (offline / tests).
        self._docs_sync = docs_sync
        # Where remotes are cached/acquired (ensure_local). None → project_ref default.
        # Kept consistent with docs_sync's cache root so both hit the same clone.
        self._cache_root = Path(cache_root) if cache_root else None
        # EPHEMERAL scratch for greenfield bootstrap ONLY — the initial commit is staged
        # here then pushed to the created remote and the dir is discarded. It is NEVER the
        # durable target: after bootstrap the plan's ``target`` is the pushed remote's ref
        # and the working copy is the acquired cache clone.
        self._workspaces = Path(
            workspaces_dir or os.environ.get("MC_PLAN_WORKSPACES", "plan-workspaces")
        )

    # -- kickoff (called from the finalize endpoint, on the loop) ----------

    async def start_build(self, plan_id: str) -> None:
        """Move the plan to ``building`` and dispatch its runnable units. Async so it
        runs on the event loop (the launch path spawns background drives there).

        A greenfield plan (no target yet) is BOOTSTRAPPED first: an operator-supplied
        remote destination is created + seeded (initial commit with aidlc-docs/) + pushed,
        the plan's ``target`` is set to the resulting portable ref, and the remote is
        acquired locally. After that there is NO greenfield/brownfield difference — both
        are "a repo with a remote" and run the same acquire → worktree → gate → push path.
        Greenfield with no destination fails loudly (never an anonymous local-only dir)."""
        plan = self._plans.get_plan(plan_id)
        if plan is None:
            return
        if plan.mode == aidlc.MODE_GREENFIELD and not plan.target:
            self._bootstrap_greenfield(plan_id, plan)
        self._plans.set_status(plan_id, STATUS_BUILDING)
        self._advance(plan_id)

    def _bootstrap_greenfield(self, plan_id: str, plan) -> None:
        """Create the project's remote so identity + durability exist from unit 1. The
        operator's destination comes from the plan (``remote_dest``) or the injected
        ``MC_GREENFIELD_REMOTE`` env — never a hardcoded host/org. Seeds the initial
        commit with the current plan docs, pushes, then records the portable ref as the
        plan's ``target`` and acquires the remote locally as the build's working copy."""
        dest = plan.remote_dest or os.environ.get("MC_GREENFIELD_REMOTE")
        if not dest:
            raise repo_source.BootstrapError(
                "greenfield build requires a remote destination (plan remote_dest / "
                "MC_GREENFIELD_REMOTE); refusing a non-portable local-only workspace")
        scratch = self._workspaces / plan_id
        try:
            self._seed_scratch(plan_id, scratch)          # README + aidlc-docs/ (the plan)
            ref = repo_source.bootstrap_remote(dest, scratch,
                                               allow_secrets=bool(plan.allow_secrets))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)    # scratch is ephemeral
        self._plans.set_target(plan_id, ref)              # the portable identity now exists
        local = repo_source.ensure_local(ref, root=self._cache_root)  # acquire the remote
        self._plans.set_local_path(plan_id, str(local))

    def _seed_scratch(self, plan_id: str, scratch: Path) -> None:
        """Populate the ephemeral bootstrap scratch with the initial commit's content:
        the plan docs (so the created remote carries aidlc-docs/inception/ from birth)."""
        scratch.mkdir(parents=True, exist_ok=True)
        plan_docs.dump_plan(plan_docs.plan_doc_from_store(self._plans, plan_id),
                            plan_docs.docs_dir(scratch))

    # -- restart durability: resume in-flight builds -----------------------

    async def resume_builds(self) -> None:
        """Re-advance every plan left ``building`` by a previous process. The plan
        store is durable operational memory, so on restart the units + their child runs
        survive; this re-dispatches any unit whose dependencies have since (durably)
        succeeded but that wasn't dispatched before the crash. Burns paused at the gate
        resume normally on approve. Called once on service startup (on the loop)."""
        for plan in self._plans.list_plans({"status": STATUS_BUILDING}, limit=1000):
            self._advance(plan.id)

    # -- edge trigger: a child run terminated ------------------------------

    def on_run_terminal(self, run_id: str, plan_id: str) -> None:
        """A plan-child run reached a terminal state. On SUCCESS (a sim done / a burn
        applied on a GO), mark its unit ``done`` and push that to git (the portable
        progress record) BEFORE dispatching more. On a NO-GO scrub, record the stage's
        'request changes' — the unit stays not-done (its stage stays incomplete) and
        only that unit is scrubbed; the plan is not failed. On a HARD worker error (a
        transient API 5xx / network blip), retry the stage — bounded by the same attempts
        cap — rather than killing it dead. Then dispatch newly-unblocked units and roll
        the plan up. Registered as the run observer."""
        run = self._runs.get_run(run_id)
        if run is not None and run.plan_unit_seq is not None:
            if run.status in _SUCCESS:
                # CAPCOM's verification gate: mark done ONLY if the stage actually
                # produced its artifacts. A produced-nothing stage is re-run (bounded),
                # then held — its dependents never deploy onto missing inputs.
                if self._produced(plan_id, run.plan_unit_seq, run):
                    self._mark_unit_done(plan_id, run.plan_unit_seq)
                else:
                    self._handle_no_output(plan_id, run.plan_unit_seq)
            elif run.status == STATUS_SCRUBBED:  # a NO-GO at the gate
                self._request_changes(plan_id, run.plan_unit_seq,
                                      getattr(run, "detail", None))
            elif run.status == STATUS_FAILED:  # a hard worker error, not a rejection
                self._handle_failure(plan_id, run.plan_unit_seq)
            elif run.status == STATUS_BLOCKED_SECRETS:  # egress guard blocked the output
                self._handle_blocked_secrets(plan_id, run.plan_unit_seq)
        self._advance(plan_id)

    def _unit(self, plan_id: str, seq: int):
        return next((u for u in self._plans.list_units(plan_id) if u.seq == seq), None)

    def _produced(self, plan_id: str, seq: int, run) -> bool:
        """CAPCOM's completeness check (pure): did this stage produce its artifacts?
        True when it wrote files or isn't a v2 stage unit; False when a producing v2 stage
        SUCCEEDED but WROTE NOTHING (it almost certainly ran without its inputs)."""
        unit = self._unit(plan_id, seq)
        if unit is None or not getattr(unit, "stage_slug", None):
            return True
        return bool(getattr(run, "changes_json", None))

    def _handle_no_output(self, plan_id: str, seq: int) -> None:
        """CAPCOM's negotiation on a produced-nothing stage:

        1. If a missing consumed input has a PRODUCER unit in this plan that can still
           run, re-activate that producer (regenerate the upstream artifact) — the
           consumer waits on it and re-runs once it exists. This is the back-and-forth:
           CAPCOM fixes the cause upstream, not just nudges the consumer.
        2. Otherwise re-run the consumer itself (with the input diagnosis) up to the cap.
        3. When neither can make progress (cap reached / no producer), HOLD it + surface.
        Bounded throughout by each unit's ``attempts`` cap, so it cannot churn forever."""
        unit = self._unit(plan_id, seq)
        if unit is None or not getattr(unit, "stage_slug", None):
            return
        plan = self._plans.get_plan(plan_id)
        target = plan.working_path if plan else None
        if not _is_git_repo(target):
            self._hold(plan_id, unit)
            return
        missing = self._missing_inputs(plan_id, target, unit.stage_slug)
        if unit.attempts >= MAX_STAGE_ATTEMPTS:
            self._hold(plan_id, unit, missing)
            return

        producers = self._rerunnable_producers(target, plan_id, missing)
        if producers:
            # Regenerate the upstream artifacts: re-activate the producers (they re-run
            # first); the consumer stays pending and re-runs once its inputs exist.
            for p in producers:
                self._plans.set_unit_status(plan_id, p.seq, UNIT_PENDING)
            names = ", ".join(sorted({p.stage_slug for p in producers}))
            self._plans.upsert_requirement(
                plan_id, f"{unit.stage_slug}:awaiting-inputs",
                value=f"missing {', '.join(missing)} — CAPCOM re-running producer(s): {names}",
                state=aidlc.REQ_OPEN)
            # consumer is left PENDING; _advance dispatches the producers, then it re-runs.
        else:
            # No upstream to regenerate → re-run the consumer itself (via _advance, which
            # re-dispatches a pending unit whose last run is terminal, with the diagnosis).
            detail = ("re-running; inputs missing on disk: " + ", ".join(missing)) \
                if missing else "re-running (attempt produced nothing)"
            self._plans.upsert_requirement(
                plan_id, f"{unit.stage_slug}:retry", value=detail, state=aidlc.REQ_OPEN)
            # consumer left PENDING with a terminal last run → _advance re-dispatches it.

    def _handle_failure(self, plan_id: str, seq: int) -> None:
        """A stage's worker HARD-ERRORED (a transient API 5xx / network blip / SDK crash),
        distinct from a stage that ran cleanly but produced nothing. Retry it, bounded by
        the SAME attempts cap, and HOLD only once the cap is spent. A transient error must
        not permanently kill a stage — and everything downstream — while attempts remain;
        that is the difference between a blip and a real dead-end. Left PENDING under the
        cap, so ``_advance`` re-dispatches it (an under-cap failure is no longer treated as
        dead by :meth:`_dead_seqs`)."""
        unit = self._unit(plan_id, seq)
        if unit is None:
            return
        if unit.attempts >= MAX_STAGE_ATTEMPTS:
            self._hold_failed(plan_id, unit)
            return
        key = getattr(unit, "stage_slug", None) or f"unit-{seq}"
        self._plans.upsert_requirement(
            plan_id, f"{key}:retry",
            value=f"worker errored (attempt {unit.attempts}/{MAX_STAGE_ATTEMPTS}) — "
                  "CAPCOM re-running",
            state=aidlc.REQ_OPEN)

    def _hold_failed(self, plan_id: str, unit) -> None:
        """Block a stage whose worker kept hard-erroring through its attempts cap — its
        dependents are held (never deployed onto a stage that never ran) and the failure
        is surfaced, not silently dead."""
        key = getattr(unit, "stage_slug", None) or f"unit-{unit.seq}"
        self._plans.set_unit_status(plan_id, unit.seq, UNIT_BLOCKED)
        self._plans.upsert_requirement(
            plan_id, f"{key}:failed",
            value=f"worker errored on every attempt ({unit.attempts}/{MAX_STAGE_ATTEMPTS}) — "
                  "held by CAPCOM; its dependents will not run until it succeeds",
            state=aidlc.REQ_OPEN)
        if self._docs_sync is not None:
            self._docs_sync(plan_id)

    def _handle_blocked_secrets(self, plan_id: str, seq: int) -> None:
        """The egress guard BLOCKED this stage's output (it staged secret-shaped content),
        so NOTHING was applied. Retry it — bounded by the SAME attempts cap — and on the
        re-run feed the guard's findings back to the worker (see :func:`_secrets_note`) so
        it fixes the cause (placeholders / env references) rather than reproducing them.
        HOLD (blocked + surfaced) once the cap is spent, so a stage that keeps emitting
        secrets cannot churn forever (unbounded, blind re-dispatch was the bug)."""
        unit = self._unit(plan_id, seq)
        if unit is None:
            return
        if unit.attempts >= MAX_STAGE_ATTEMPTS:
            self._hold_secrets(plan_id, unit)
            return
        key = getattr(unit, "stage_slug", None) or f"unit-{seq}"
        self._plans.upsert_requirement(
            plan_id, f"{key}:secrets",
            value=f"egress guard blocked secret-shaped content "
                  f"(attempt {unit.attempts}/{MAX_STAGE_ATTEMPTS}) — CAPCOM re-running with "
                  "a placeholder / env-var directive",
            state=aidlc.REQ_OPEN)
        # unit left PENDING → _advance re-dispatches it, with the secrets diagnosis in the
        # prompt (see _dispatch); an under-cap blocked_secrets is not treated as dead.

    def _hold_secrets(self, plan_id: str, unit) -> None:
        """Block a stage whose output kept tripping the egress guard through its attempts
        cap — dependents are held and the gap is surfaced, not silently churned."""
        key = getattr(unit, "stage_slug", None) or f"unit-{unit.seq}"
        self._plans.set_unit_status(plan_id, unit.seq, UNIT_BLOCKED)
        self._plans.upsert_requirement(
            plan_id, f"{key}:secrets-blocked",
            value=f"egress guard blocked secret-shaped content on every attempt "
                  f"({unit.attempts}/{MAX_STAGE_ATTEMPTS}) — held by CAPCOM; remove the "
                  "secret-shaped values (placeholders / env vars) before re-running",
            state=aidlc.REQ_OPEN)
        if self._docs_sync is not None:
            self._docs_sync(plan_id)

    def _latest_run(self, plan_id: str, seq: int):
        """The most recent run for a unit (child_runs is created_at-ordered → last wins)."""
        runs = [r for r in self._runs.child_runs(plan_id) if r.plan_unit_seq == seq]
        return runs[-1] if runs else None

    def _missing_inputs(self, plan_id: str, target, stage_slug: str) -> list:
        """CAPCOM's diagnosis: which REQUIRED consumed artifacts are missing, judged by
        the PRODUCER's outcome + the files it actually wrote (not a filename guess), with
        an on-disk fallback for artifacts no build unit produces. Empty for a non-v2
        target."""
        catalog = self._catalog(target)
        if catalog is None:
            return []
        from ..aidlc_v2 import plan as v2plan
        # Only BUILD units (not the INCEPTION walk records) are artifact producers with
        # runs; a producer's written files come from its latest run's applied diff.
        build = [u for u in self._plans.list_units(plan_id)
                 if getattr(u, "stage_slug", None) and u.phase != aidlc.Phase.INCEPTION.value]
        runs_by_seq = {r.plan_unit_seq: r for r in self._runs.child_runs(plan_id)}
        producer_done = {u.stage_slug: (u.status == UNIT_DONE) for u in build}
        producer_files = {u.stage_slug: self._written_stems(runs_by_seq.get(u.seq))
                          for u in build}
        record_root = Path(target) / "aidlc-docs"
        on_disk = {p.stem for p in record_root.rglob("*.md")} if record_root.is_dir() else set()
        return v2plan.missing_inputs(
            catalog, stage_slug, producer_done=producer_done,
            producer_files=producer_files, on_disk=on_disk)

    @staticmethod
    def _written_stems(run) -> set:
        """The filename stems a run committed — its produced-artifact manifest, read from
        the applied diff (``changes_json``)."""
        cj = getattr(run, "changes_json", None) or {}
        out = set()
        for f in (cj.get("files") or []):
            path = f.get("path") if isinstance(f, dict) else f
            if path:
                out.add(Path(path).stem)
        return out

    def _catalog(self, target):
        """The target's v2 catalog, or None (probe is read-only)."""
        if not target:
            return None
        steering = aidlc.probe(Path(target))
        if steering is None or steering.flavor != aidlc.FLAVOR_AIDLC_V2 \
                or steering.catalog_root is None:
            return None
        from ..aidlc_v2 import catalog as v2catalog
        return v2catalog.load_catalog(steering.catalog_root)

    def _rerunnable_producers(self, target, plan_id: str, missing: list) -> list:
        """The build units that PRODUCE a missing artifact and can still run (their last
        run is terminal — done/blocked — and they're under their attempts cap). These are
        the upstream stages CAPCOM re-runs to regenerate what a consumer needs."""
        catalog = self._catalog(target)
        if not missing or catalog is None:
            return []
        produced_by = {art: s.slug for s in catalog for art in s.produces}
        want = {produced_by[a] for a in missing if a in produced_by}
        out = []
        for u in self._plans.list_units(plan_id):
            if (getattr(u, "stage_slug", None) in want
                    and u.status in (UNIT_DONE, UNIT_BLOCKED)
                    and u.attempts < MAX_STAGE_ATTEMPTS):
                out.append(u)
        return out

    def _hold(self, plan_id: str, unit, missing=()) -> None:
        """Block a stage that produced nothing after its retries — dependents are held
        (never deployed onto missing inputs) and the gap (incl. the diagnosed missing
        inputs) is surfaced, not silently done."""
        why = (" (missing inputs: " + ", ".join(missing) + ")") if missing else ""
        self._plans.set_unit_status(plan_id, unit.seq, UNIT_BLOCKED)
        self._plans.upsert_requirement(
            plan_id, f"{unit.stage_slug}:no-output",
            value=f"stage produced no artifacts after {unit.attempts} attempt(s){why} — "
                  "held by CAPCOM; its dependents will not run until its outputs exist",
            state=aidlc.REQ_OPEN)
        if self._docs_sync is not None:
            self._docs_sync(plan_id)

    def _request_changes(self, plan_id: str, seq: int, feedback: Optional[str]) -> None:
        """Record a NO-GO'd stage's gate feedback as a 'request changes' — the v2
        ``[?]``→revising signal, collapsed into MC's gate. The unit is NOT marked done
        (its stage stays ``[ ]`` in aidlc-state.md); the recorded requirement travels in
        the plan docs so the next attempt (or a reviewer) can read the feedback. This is
        MC's stand-in for ``aidlc-state.ts reject`` — MC never shells out to that tool."""
        unit = next((u for u in self._plans.list_units(plan_id) if u.seq == seq), None)
        if unit is None or not getattr(unit, "stage_slug", None):
            return  # not a v2 stage unit → nothing v2-specific to record
        note = feedback or f"changes requested at the go/no-go gate for {unit.stage_slug}"
        self._plans.upsert_requirement(
            plan_id, f"{unit.stage_slug}:changes-requested", value=note,
            state=aidlc.REQ_OPEN)
        # Re-sync so the incomplete stage + the request-changes note land in git.
        if self._docs_sync is not None:
            self._docs_sync(plan_id)

    def _mark_unit_done(self, plan_id: str, seq: int) -> None:
        """Mark a unit ``done`` and persist that to git (write flight-plan.yaml + commit
        + push). Idempotent: re-marking an already-``done`` unit (e.g. a crash between the
        merge/push and the status write) is a no-op — no redundant commit. The status
        write + commit + push is one logical step (:func:`plan_docs.sync_to_repo`); a
        rejected push surfaces from there rather than leaving git silently behind."""
        unit = next((u for u in self._plans.list_units(plan_id) if u.seq == seq), None)
        if unit is None or unit.status == UNIT_DONE:
            return  # unknown, or already recorded done → nothing to do
        self._plans.set_unit_status(plan_id, seq, UNIT_DONE)
        if self._docs_sync is not None:
            self._docs_sync(plan_id)

    # -- the scheduler -----------------------------------------------------

    def _advance(self, plan_id: str) -> None:
        # This is MC's own advance: it dispatches the next dependency-satisfied unit(s)
        # itself. For a v2 plan it REPLACES v2's `aidlc-orchestrate.ts report` auto-
        # advance — MC owns sequencing and never shells out to v2's .ts tools.
        plan = self._plans.get_plan(plan_id)
        if plan is None or plan.status == STATUS_DONE:
            return
        units = self._plans.list_units(plan_id)

        # No runnable git repo to host worktrees (a brownfield target that vanished, or
        # a scaffold that failed) → nothing to dispatch onto; the build is trivially
        # complete. Guarding on "is it a repo?" (not just "is it a dir?") stops a
        # non-git target from cascading into a pile of failed runs.
        target = plan.working_path  # the LOCAL working dir worktrees are carved from
        if not _is_git_repo(target):
            self._plans.set_status(plan_id, STATUS_DONE)
            return

        # _advance is a PURE dispatcher: it never marks done / holds / verifies (that is
        # on_run_terminal's job, run once per terminal run). Committed unit STATUS is the
        # authority — ``done`` skips, ``blocked`` (CAPCOM-held) is dead, the rest dispatch
        # when ready. The in-session child runs only tell us what is in-flight (don't
        # re-dispatch a running unit) — the LATEST run per seq wins (child_runs is
        # created_at-ordered), so a re-run's fresh in-flight run is what we see.
        by_seq = {r.plan_unit_seq: r for r in self._runs.child_runs(plan_id)}
        done = {u.seq for u in units if u.status == UNIT_DONE}
        # own run failed / a held (produced-nothing) unit → dependents held too
        dead = self._dead_seqs(units, by_seq)
        # Deferred units (AI-DLC v2 ``operation`` stages) are RECORDED in the plan but
        # never dispatched in v1 (they need cloud creds). They count as resolved so the
        # plan can still complete; they are never launched and never block a dependent.
        deferred = {u.seq for u in units if u.status == UNIT_DEFERRED}

        for unit in units:  # units come back in seq order
            if unit.seq in done or unit.seq in dead or unit.seq in deferred:
                continue  # done / dead / held / deferred-never-dispatched
            r = by_seq.get(unit.seq)
            if r is not None and r.status in _INFLIGHT:
                continue  # currently running / at the gate → wait for it
            if unit.status != UNIT_PENDING:
                continue  # only PENDING units dispatch (a re-run resets status to pending)
            if not all(dep in done for dep in (unit.depends_on or [])):
                continue  # a dependency isn't done yet → not (yet) dispatchable
            # Dispatch a fresh unit OR re-dispatch a PENDING unit whose last run is
            # terminal — the latter is CAPCOM's retry / upstream-regeneration re-run.
            self._dispatch(plan_id, target, unit)
            by_seq = {r.plan_unit_seq: r for r in self._runs.child_runs(plan_id)}

        if self._all_resolved(units, done | deferred, dead):
            self._plans.set_status(plan_id, STATUS_DONE)

    def _dispatch(self, plan_id: str, target: str, unit) -> None:
        plan = self._plans.get_plan(plan_id)
        # Count this dispatch (the re-run cap reads it). A RE-run (attempt > 1) carries
        # CAPCOM's diagnostic note, computed from the unit's own state: if IT is missing
        # inputs → name them; else (it has inputs but wrote nothing, incl. a regenerated
        # producer) → "you MUST write your artifacts".
        last = self._latest_run(plan_id, unit.seq)  # the terminal run this re-run follows
        attempt = self._plans.bump_unit_attempts(plan_id, unit.seq)
        note = ""
        if attempt > 1 and getattr(unit, "stage_slug", None):
            if last is not None and getattr(last, "status", None) == STATUS_BLOCKED_SECRETS:
                # The egress guard blocked the last attempt → tell the worker WHAT tripped
                # it and to use placeholders / env refs, not "write more artifacts".
                note = _secrets_note(getattr(last, "detail", "") or "")
            else:
                note = _escalation_note(self._missing_inputs(plan_id, target, unit.stage_slug))
        prompt = _prompt_for(unit) + note
        self._runs.launch(
            target=target, task_type=unit.task_type, prompt=prompt,
            plan_id=plan_id, plan_unit_seq=unit.seq,
            workstream=plan.workstream if plan else None,
            allow_secrets=bool(plan.allow_secrets) if plan else False,
            # A v2 unit carries its stage slug → the worker steers from that stage's
            # definition + lead-agent knowledge (see aidlc_v2.steering). None for v1.
            stage_slug=getattr(unit, "stage_slug", None),
            # The unit title is the run's subject — shown while it dispatches so the
            # operator sees WHAT is running, not a blank card, for the minutes it takes.
            subject=unit.title,
            # A design/doc stage writes + auto-applies (gated=False); a code stage halts
            # for a human GO (gated=True). Non-v2 units default to gated.
            gated=getattr(unit, "gated", True),
        )

    # -- dependency logic --------------------------------------------------

    @staticmethod
    def _all_resolved(units, done: set, dead: set) -> bool:
        """The plan is done when every unit is resolved: it is committed ``done``, or it
        is dead (its own run failed, or it's transitively blocked so it will never run).
        A not-done, not-dead unit means there is still work in flight or to dispatch."""
        return all(u.seq in done or u.seq in dead for u in units)

    @staticmethod
    def _dead_seqs(units, by_seq, extra_dead=frozenset()) -> set:
        """Units that will never reach ``done``: seeded by a unit whose OWN run failed
        this session (a no-go/scrubbed/push-rejected run), a unit CAPCOM blocked for
        producing nothing (committed ``blocked`` status, or freshly blocked this pass via
        ``extra_dead``), then propagated to every dependent. (A committed-``done`` unit is
        never dead — its run succeeded and produced.)

        A HARD worker error (``failed``) is the one terminal that is NOT automatically
        dead: while the unit is under its attempts cap it is a RETRY candidate (a transient
        API 5xx must not kill the stage), so ``_advance`` re-dispatches it. It only counts
        as dead once its attempts are spent (by then CAPCOM has held it ``blocked``)."""
        dead: set = set(extra_dead) | {
            u.seq for u in units
            if u.status == UNIT_BLOCKED
            or ((r := by_seq.get(u.seq)) is not None
                and r.status in _FAILED
                and not (r.status == STATUS_FAILED and u.attempts < MAX_STAGE_ATTEMPTS))
        }
        changed = True
        while changed:
            changed = False
            for unit in units:
                if unit.seq in dead:
                    continue
                if any(dep in dead for dep in (unit.depends_on or [])):
                    dead.add(unit.seq)
                    changed = True
        return dead


def _is_git_repo(target) -> bool:
    """True only when ``target`` is its OWN git root — delegates to the shared,
    safety-critical check in :mod:`mission_control.worktree` (never a parent repo)."""
    return worktree.is_git_repo(target)


def _prompt_for(unit) -> str:
    """The instruction handed to the worker for a unit's run."""
    if unit.task_type == roles.SIM:
        return f"Validate the planned INCEPTION work: {unit.title}"
    return f"Implement the planned change: {unit.title}"
