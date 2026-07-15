"""The PLAN store: a running instance's own operational memory.

A plan is the interactive, durable record of *what MC is about to build* for a
target — the planning-side counterpart to the runs ledger. It lives in the SAME
Postgres as the LangGraph checkpointer, the runs ledger, and ``run_events`` (one
substrate for all of the instance's durable state), and follows the same idempotent
discipline: schema via ``CREATE TABLE IF NOT EXISTS``, stage-boundary writes as
upserts keyed by natural identity, so a replay re-applies without duplicating.

Four tables, one aggregate:

* ``plans`` — the header: target, mode, methodology/cloud target, stage, status.
* ``plan_turns`` — the interactive transcript (operator ↔ planner), ordered by seq.
* ``plan_requirements`` — the accreting requirements (key → value/state), upserted.
* ``plan_units`` — the CONSTRUCTION work-list MC will execute; each unit's
  ``task_type`` is DERIVED from its phase via :func:`aidlc.task_type_for_phase`,
  never stored by hand (the sim/burn metaphor stays sourced from ``roles``).

This is a store + seam only — it adds NO orchestration to ``graph.py``. It is a
client of the existing runtime that happens to share its connection pool.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .aidlc import Phase, task_type_for_phase

# -- plan lifecycle statuses (functional labels) ---------------------------
STATUS_DRAFTING = "drafting"
STATUS_READY = "ready"
STATUS_FINALIZED = "finalized"
STATUS_BUILDING = "building"
STATUS_DONE = "done"

# -- transcript roles (planner domain, not the MC metaphor) ----------------
ROLE_OPERATOR = "operator"
ROLE_PLANNER = "planner"

# -- unit statuses (the work-list's per-unit execution state) --------------
# These travel in flight-plan.yaml, so they are the PORTABLE per-unit progress record:
# a ``done`` unit is not re-run on another machine. Not-``done`` units are (re-)runnable.
UNIT_PENDING = "pending"
UNIT_DONE = "done"
# A unit recorded in the work-list but deliberately NOT dispatched (an AI-DLC v2
# ``operation`` stage — deferred in v1, needs cloud creds). It travels in the plan and
# counts as resolved for plan completion, but the builder never launches it.
UNIT_DEFERRED = "deferred"
# A stage that ran but produced none of its artifacts — CAPCOM's verification failed it.
# Its dependents are held (never deployed onto missing inputs); surfaced for the operator
# rather than silently counted done. Resolved for plan completion (it won't re-run here).
UNIT_BLOCKED = "blocked"

# One statement per entry: the autocommit pool prepares statements, which forbids
# multiple commands in a single execute() (same constraint as the runs ledger).
_DDL = (
    """
    CREATE TABLE IF NOT EXISTS plans (
        id           TEXT PRIMARY KEY,
        target       TEXT,
        local_path   TEXT,
        workstream   TEXT,
        remote_dest  TEXT,
        allow_secrets BOOLEAN NOT NULL DEFAULT false,
        mode         TEXT NOT NULL,
        methodology  TEXT NOT NULL DEFAULT 'aidlc',
        cloud_target TEXT NOT NULL DEFAULT 'aws',
        stage        TEXT,
        status       TEXT NOT NULL DEFAULT 'drafting',
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS plans_status_idx ON plans (status)",
    "CREATE INDEX IF NOT EXISTS plans_created_at_idx ON plans (created_at DESC)",
    # target now carries the PORTABLE ref (normalized remote); local_path is the
    # derived machine-local working dir — a separate field, never the identity.
    "ALTER TABLE plans ADD COLUMN IF NOT EXISTS local_path TEXT",
    # Optional workstream: the plan's build reconciles through the mc/ws/<name> branch.
    "ALTER TABLE plans ADD COLUMN IF NOT EXISTS workstream TEXT",
    # Greenfield only: the operator-supplied remote destination bootstrap creates+pushes
    # to. Consumed once at build start (then ``target`` holds the resulting portable ref).
    "ALTER TABLE plans ADD COLUMN IF NOT EXISTS remote_dest TEXT",
    # Explicit operator override of the egress content guard for this plan's commits
    # (audited). Default false → a secret/PII in pushed content blocks the commit.
    "ALTER TABLE plans ADD COLUMN IF NOT EXISTS allow_secrets BOOLEAN NOT NULL DEFAULT false",
    # The interactive transcript: operator turns and the planner's replies, in order.
    # seq is a per-plan counter (continues across process restarts).
    """
    CREATE TABLE IF NOT EXISTS plan_turns (
        plan_id    TEXT NOT NULL,
        seq        INTEGER NOT NULL,
        role       TEXT NOT NULL,
        content    TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (plan_id, seq)
    )
    """,
    # The accreting requirements — upserted by (plan_id, key) so a re-run of the
    # capturing stage updates in place rather than duplicating.
    """
    CREATE TABLE IF NOT EXISTS plan_requirements (
        plan_id    TEXT NOT NULL,
        key        TEXT NOT NULL,
        value      TEXT,
        state      TEXT NOT NULL DEFAULT 'open',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (plan_id, key)
    )
    """,
    # The CONSTRUCTION work-list — upserted by (plan_id, seq). task_type is derived
    # from phase at write time; depends_on is a JSONB list of prerequisite unit seqs.
    """
    CREATE TABLE IF NOT EXISTS plan_units (
        plan_id    TEXT NOT NULL,
        seq        INTEGER NOT NULL,
        title      TEXT NOT NULL,
        phase      TEXT NOT NULL,
        task_type  TEXT NOT NULL,
        depends_on JSONB NOT NULL DEFAULT '[]'::jsonb,
        status     TEXT NOT NULL DEFAULT 'pending',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (plan_id, seq)
    )
    """,
    # The v2 stage this unit was derived from (its catalog slug), so a worker can find
    # the right stage file. NULL for built-in (v1) plans — backward-compatible.
    "ALTER TABLE plan_units ADD COLUMN IF NOT EXISTS stage_slug TEXT",
    # Whether this unit halts for a human go/no-go (a code-writing v2 stage) vs. writes
    # and auto-applies (a design/doc stage). Default true = the historical behavior (a
    # side-effectful unit gates). Backward-compatible.
    "ALTER TABLE plan_units ADD COLUMN IF NOT EXISTS gated BOOLEAN NOT NULL DEFAULT true",
    # How many times CAPCOM has dispatched this unit. Drives the bounded re-run loop:
    # a stage that produces nothing is retried (with escalated instruction) up to a cap,
    # then held. Not portable build state — resets on a fresh-host rebuild.
    "ALTER TABLE plan_units ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0",
)


@dataclass
class PlanRow:
    """One row of the ``plans`` header table."""

    id: str
    target: Optional[str]  # the PORTABLE identity (normalized remote ref), not a path
    mode: str
    methodology: str
    cloud_target: str
    stage: Optional[str]
    status: str
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    # The derived machine-local working dir (worktrees / probes run here). Separate
    # from ``target`` — a portable ref must not be a local path. Defaulted so
    # PlanRow(**row) works for rows created before this column.
    local_path: Optional[str] = None
    # Optional workstream: the plan's build reconciles through the mc/ws/<name> branch
    # rather than directly onto trunk. Defaulted for rows created before this column.
    workstream: Optional[str] = None
    # Greenfield only: the operator-supplied remote destination to bootstrap (create +
    # push) at build start. Defaulted for rows created before this column.
    remote_dest: Optional[str] = None
    # Explicit operator override of the egress content guard for this plan (audited).
    allow_secrets: bool = False

    @property
    def working_path(self) -> Optional[str]:
        """The machine-local working dir to operate in. Prefers ``local_path`` (the
        derived field); falls back to ``target`` for rows written before the split,
        where ``target`` still held the resolved path."""
        return self.local_path or self.target


@dataclass
class PlanTurn:
    """One entry of the interactive transcript."""

    plan_id: str
    seq: int
    role: str
    content: str
    created_at: Optional[datetime]


@dataclass
class PlanRequirement:
    """One accreting requirement (key → value, with a readiness state)."""

    plan_id: str
    key: str
    value: Optional[str]
    state: str
    created_at: Optional[datetime]
    updated_at: Optional[datetime]


@dataclass
class PlanUnit:
    """One unit of the CONSTRUCTION work-list."""

    plan_id: str
    seq: int
    title: str
    phase: str
    task_type: str
    depends_on: list
    status: str
    created_at: Optional[datetime]
    # The v2 catalog stage this unit derives from (None for built-in/v1 plans).
    # Defaulted so PlanUnit(**row) works for rows created before this column.
    stage_slug: Optional[str] = None
    # Whether a side-effectful unit halts for a human go/no-go (code stage) or writes +
    # auto-applies (design/doc stage). Defaulted (gates) for rows / mocks predating it.
    gated: bool = True
    # How many times CAPCOM has dispatched this unit (the re-run counter). Defaulted for
    # rows / mocks predating it.
    attempts: int = 0


# Columns whose non-None values narrow a list_plans() query.
_FILTERABLE = ("status", "mode", "target", "id")


class PlanStore:
    """Reads/writes the PLAN tables over a shared psycopg connection pool.

    The pool is the one the checkpointer and runs ledger use (autocommit); this
    store never opens its own — see :func:`mission_control.graph.postgres_checkpointer`.
    """

    def __init__(self, pool) -> None:
        self._pool = pool

    # -- schema ------------------------------------------------------------

    def setup(self) -> None:
        """Create the tables if absent. Idempotent (mirrors the runs ledger)."""
        with self._pool.connection() as conn:
            for statement in _DDL:
                conn.execute(statement)

    # -- plans header (idempotent open + stage/status transitions) ---------

    def open_plan(
        self,
        plan_id: str,
        *,
        target: Optional[str],
        mode: str,
        methodology: str,
        cloud_target: str,
        local_path: Optional[str] = None,
        workstream: Optional[str] = None,
        remote_dest: Optional[str] = None,
        allow_secrets: bool = False,
        stage: Optional[str] = None,
        status: str = STATUS_DRAFTING,
    ) -> None:
        """Register a new plan session. A no-op if the row already exists, so a
        re-open never resets an in-progress plan. ``target`` is the portable ref;
        ``local_path`` the derived working dir; ``workstream`` the optional mc/ws line;
        ``remote_dest`` the greenfield bootstrap destination; ``allow_secrets`` the
        explicit content-guard override."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO plans (id, target, local_path, workstream, remote_dest, allow_secrets, mode, methodology, cloud_target, stage, status)
                VALUES (%(id)s, %(target)s, %(local_path)s, %(workstream)s, %(remote_dest)s, %(allow_secrets)s, %(mode)s, %(methodology)s, %(cloud)s, %(stage)s, %(status)s)
                ON CONFLICT (id) DO NOTHING
                """,
                {
                    "id": plan_id, "target": target, "local_path": local_path,
                    "workstream": workstream, "remote_dest": remote_dest,
                    "allow_secrets": allow_secrets,
                    "mode": mode, "methodology": methodology, "cloud": cloud_target,
                    "stage": stage, "status": status,
                },
            )

    def set_stage(self, plan_id: str, stage: str) -> None:
        """Move the plan to a new stage (idempotent; bumps updated_at)."""
        self._update(plan_id, "stage", stage)

    def set_mode(self, plan_id: str, mode: str) -> None:
        """Set the plan's mode (workspace detection may derive it; bumps updated_at)."""
        self._update(plan_id, "mode", mode)

    def set_target(self, plan_id: str, target: str) -> None:
        """Record the build target's portable identity (a normalized remote ref)."""
        self._update(plan_id, "target", target)

    def set_local_path(self, plan_id: str, local_path: str) -> None:
        """Record the derived machine-local working dir (e.g. a scaffolded workspace
        for a greenfield plan). Separate from the identity in ``target``."""
        self._update(plan_id, "local_path", local_path)

    def set_status(self, plan_id: str, status: str) -> None:
        """Move the plan to a new status (idempotent; bumps updated_at)."""
        self._update(plan_id, "status", status)

    def _update(self, plan_id: str, column: str, value) -> None:
        # column is a fixed internal literal, never user input (no injection surface).
        with self._pool.connection() as conn:
            conn.execute(
                f"UPDATE plans SET {column} = %(value)s, updated_at = now() WHERE id = %(id)s",
                {"value": value, "id": plan_id},
            )

    def get_plan(self, plan_id: str) -> Optional[PlanRow]:
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute("SELECT * FROM plans WHERE id = %s", (plan_id,))
            row = cur.fetchone()
        return PlanRow(**row) if row else None

    def _where(self, filter: Optional[dict]) -> tuple[str, dict]:
        clauses, params = [], {}
        for key, value in (filter or {}).items():
            if key not in _FILTERABLE:
                raise ValueError(f"unfilterable column: {key!r} (allowed: {_FILTERABLE})")
            if value is not None:
                clauses.append(f"{key} = %({key})s")
                params[key] = value
        return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params

    def list_plans(
        self,
        filter: Optional[dict] = None,
        *,
        limit: int = 100,
        offset: int = 0,
        order: str = "desc",
    ) -> list[PlanRow]:
        """A page of plans ordered by ``created_at`` (``desc`` = newest-first)."""
        direction = "ASC" if str(order).lower() == "asc" else "DESC"
        where, params = self._where(filter)
        params["_limit"], params["_offset"] = limit, offset
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                f"SELECT * FROM plans {where} "
                f"ORDER BY created_at {direction} LIMIT %(_limit)s OFFSET %(_offset)s",
                params,
            )
            rows = cur.fetchall()
        return [PlanRow(**r) for r in rows]

    def count_plans(self, filter: Optional[dict] = None) -> int:
        """Total matching plans (for paging), ignoring limit/offset."""
        where, params = self._where(filter)
        with self._pool.connection() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT count(*) FROM plans {where}", params)
            return int(cur.fetchone()[0])

    # -- transcript (append-in-order) --------------------------------------

    def append_turn(self, plan_id: str, role: str, content: str) -> PlanTurn:
        """Append one transcript turn at the next per-plan seq. The seq is computed
        and inserted in a single statement, so concurrent appends stay ordered and
        gap-free without an explicit lock."""
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                """
                INSERT INTO plan_turns (plan_id, seq, role, content)
                VALUES (
                    %(pid)s,
                    COALESCE((SELECT max(seq) FROM plan_turns WHERE plan_id = %(pid)s), -1) + 1,
                    %(role)s, %(content)s
                )
                RETURNING plan_id, seq, role, content, created_at
                """,
                {"pid": plan_id, "role": role, "content": content},
            )
            row = cur.fetchone()
        return PlanTurn(**row)

    def list_turns(self, plan_id: str) -> list[PlanTurn]:
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT * FROM plan_turns WHERE plan_id = %s ORDER BY seq ASC", (plan_id,)
            )
            return [PlanTurn(**r) for r in cur.fetchall()]

    # -- requirements (accreting; upsert by key) ---------------------------

    def upsert_requirement(
        self, plan_id: str, key: str, *, value: Optional[str], state: str
    ) -> None:
        """Record or update one requirement, keyed by (plan_id, key). Re-capturing a
        key updates its value/state in place (bumps updated_at) — no duplicate."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO plan_requirements (plan_id, key, value, state)
                VALUES (%(pid)s, %(key)s, %(value)s, %(state)s)
                ON CONFLICT (plan_id, key) DO UPDATE SET
                    value      = EXCLUDED.value,
                    state      = EXCLUDED.state,
                    updated_at = now()
                """,
                {"pid": plan_id, "key": key, "value": value, "state": state},
            )

    def list_requirements(self, plan_id: str) -> list[PlanRequirement]:
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT * FROM plan_requirements WHERE plan_id = %s ORDER BY key ASC",
                (plan_id,),
            )
            return [PlanRequirement(**r) for r in cur.fetchall()]

    # -- units (the CONSTRUCTION work-list; upsert by seq) -----------------

    def upsert_unit(
        self,
        plan_id: str,
        seq: int,
        *,
        title: str,
        phase: Phase | str,
        depends_on: Optional[list] = None,
        status: str = UNIT_PENDING,
        task_type: Optional[str] = None,
        stage_slug: Optional[str] = None,
        gated: bool = True,
    ) -> PlanUnit:
        """Record or update one work-list unit, keyed by (plan_id, seq). Idempotent at
        the stage boundary that lays the work-list down.

        For built-in (v1) plans ``task_type`` is left None and DERIVED from ``phase`` via
        :func:`aidlc.task_type_for_phase`, so the sim/burn mapping stays sourced from the
        methodology. AI-DLC v2 units pass ``task_type`` explicitly (from the stage
        ``kind``) — because a v2 phase (e.g. a design stage in ``construction``) does not
        determine sim vs. burn — plus the ``stage_slug`` that identifies the stage."""
        phase_str = phase.value if isinstance(phase, Phase) else str(phase)
        # Only coerce through the v1 Phase enum when we must derive task_type from it;
        # v2 phases ("construction"/"operation") are stored verbatim.
        if task_type is None:
            task_type = task_type_for_phase(Phase(phase_str)).value
        deps = list(depends_on or [])
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                """
                INSERT INTO plan_units (plan_id, seq, title, phase, task_type, depends_on, status, stage_slug, gated)
                VALUES (%(pid)s, %(seq)s, %(title)s, %(phase)s, %(tt)s, %(deps)s, %(status)s, %(slug)s, %(gated)s)
                ON CONFLICT (plan_id, seq) DO UPDATE SET
                    title      = EXCLUDED.title,
                    phase      = EXCLUDED.phase,
                    task_type  = EXCLUDED.task_type,
                    depends_on = EXCLUDED.depends_on,
                    status     = EXCLUDED.status,
                    stage_slug = EXCLUDED.stage_slug,
                    gated      = EXCLUDED.gated
                RETURNING plan_id, seq, title, phase, task_type, depends_on, status, created_at, stage_slug, gated, attempts
                """,
                {
                    "pid": plan_id, "seq": seq, "title": title, "phase": phase_str,
                    "tt": task_type, "deps": Jsonb(deps), "status": status,
                    "slug": stage_slug, "gated": gated,
                },
            )
            row = cur.fetchone()
        return PlanUnit(**row)

    def list_units(self, plan_id: str) -> list[PlanUnit]:
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT * FROM plan_units WHERE plan_id = %s ORDER BY seq ASC", (plan_id,)
            )
            return [PlanUnit(**r) for r in cur.fetchall()]

    def set_unit_status(self, plan_id: str, seq: int, status: str) -> None:
        """Update one unit's execution status (idempotent). Setting it to
        :data:`UNIT_DONE` is the durable, portable progress mark — it is then written to
        flight-plan.yaml and pushed, so another machine won't re-run that unit."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE plan_units SET status = %s WHERE plan_id = %s AND seq = %s",
                (status, plan_id, seq),
            )

    def bump_unit_attempts(self, plan_id: str, seq: int) -> int:
        """Increment a unit's dispatch counter and return the new value (the CAPCOM
        re-run loop uses it to cap retries). Atomic increment-and-return."""
        with self._pool.connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE plan_units SET attempts = attempts + 1 "
                "WHERE plan_id = %s AND seq = %s RETURNING attempts",
                (plan_id, seq),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0

    # -- cache reconciliation (git is authoritative; Postgres is a rebuildable cache) --

    def clear_units(self, plan_id: str) -> None:
        """Drop a plan's cached units. Used when reconciling the Postgres cache to the
        git source of truth: units are cleared then re-inserted from the on-disk plan,
        so any local divergence (extra/changed units) resolves to the git version."""
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM plan_units WHERE plan_id = %s", (plan_id,))

    def clear_requirements(self, plan_id: str) -> None:
        """Drop a plan's cached requirements — the requirements counterpart to
        :meth:`clear_units` for git-authoritative reconciliation."""
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM plan_requirements WHERE plan_id = %s", (plan_id,))
