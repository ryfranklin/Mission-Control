"""An explicit ``runs`` table: the operator-facing index of runs.

This is a SMALL, first-class table living in the SAME Postgres as the LangGraph
checkpoint tables — NOT derived from them. The checkpoint tables are LangGraph's
private durability substrate (opaque, keyed by thread + checkpoint blobs); this
table is our own denormalized status/cost ledger, one row per run, cheap to list
and filter for a control surface.

Schema is managed the same idempotent way as the checkpointer's own tables
(``PostgresSaver.setup()``): a ``CREATE TABLE IF NOT EXISTS`` run over the shared
connection pool. All writes are **upserts keyed by run_id**, so the node-boundary
recovery model holds — a re-run node re-applies its transition without ever
duplicating the row, and once-only stamps (``started_at`` / ``ended_at``) are
guarded with ``COALESCE`` so a replay can't move them.

``run_id`` is the LangGraph ``thread_id`` — the identity that survives a crash and
resume — so the row a resumed run updates is the same one it created.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# -- lifecycle statuses (functional labels; sim/burn metaphor stays in roles) --
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_AWAITING_GATE = "awaiting_gate"
STATUS_APPLIED = "applied"  # burn approved, merged, and pushed (trunk or workstream)
STATUS_PUSH_REJECTED = "push_rejected"  # approved, but the push was non-fast-forward
STATUS_MERGE_CONFLICT = "merge_conflict"  # approved, but integrating the remote conflicted
STATUS_BLOCKED_SECRETS = "blocked_secrets"  # egress blocked: staged content had a secret/PII
STATUS_SCRUBBED = "scrubbed"
STATUS_FAILED = "failed"
STATUS_DONE = "done"

# States after which no more work happens; each stamps ended_at exactly once.
TERMINAL_STATUSES = frozenset(
    {STATUS_APPLIED, STATUS_PUSH_REJECTED, STATUS_MERGE_CONFLICT, STATUS_BLOCKED_SECRETS,
     STATUS_SCRUBBED, STATUS_FAILED, STATUS_DONE}
)

# -- notification kinds (run-lifecycle milestones; functional labels) ----------
# NOTIFICATION-GRAIN events the RunManager appends to the ``notifications`` outbox at
# each run-lifecycle milestone — NOT the per-node firehose. A fleet-wide bridge tails
# this to learn "something happened somewhere" without following every run's SSE.
NOTIFY_RUN_LAUNCHED = "run_launched"
NOTIFY_GATE_AWAITING = "gate_awaiting"
NOTIFY_RUN_TERMINAL = "run_terminal"
# Alert kinds — reserved, populated in S5 (cost threshold breach / quality regression).
NOTIFY_COST_THRESHOLD = "cost_threshold"
NOTIFY_REGRESSION = "regression"

# Kinds that fire at most once per run — deduped by (run_id, kind). Milestones so a
# kill→restart→resume at the same node boundary can't double-append; alerts so a run
# raises at most one cost/quality alert (quiet — a threshold, not per-node spam) and a
# re-run/retry can't double-fire it.
_ONCE_ONLY_KINDS = frozenset(
    {NOTIFY_RUN_LAUNCHED, NOTIFY_GATE_AWAITING, NOTIFY_RUN_TERMINAL,
     NOTIFY_COST_THRESHOLD, NOTIFY_REGRESSION}
)

# One statement per entry: the autocommit pool prepares statements, which forbids
# multiple commands in a single execute().
_DDL = (
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id        TEXT PRIMARY KEY,
        thread_id     TEXT NOT NULL,
        target        TEXT,
        local_path    TEXT,
        task_type     TEXT,
        status        TEXT NOT NULL,
        cost_usd      DOUBLE PRECISION NOT NULL DEFAULT 0,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        started_at    TIMESTAMPTZ,
        ended_at      TIMESTAMPTZ,
        detail        TEXT,
        plan_id       TEXT,
        plan_unit_seq INTEGER
    )
    """,
    # Plan<->run link (a plan owns its child runs; each run carries its plan_id + the
    # unit seq it was built from). Added via ALTER for ledgers created before the link.
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS plan_id TEXT",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS plan_unit_seq INTEGER",
    # target now carries the PORTABLE ref (normalized remote); local_path is the
    # derived machine-local working dir — a separate field, never the identity.
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS local_path TEXT",
    # The changed-files diff a go APPLIED, captured at apply time (the payload
    # worktree.changes() produces). Persisted so an applied/torn-down run can still
    # show what it changed, after the live worktree is gone. Nullable: only a burn
    # that actually changed files gets one; sims and no-change runs stay NULL.
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS changes_json JSONB",
    # A short human description of the task (e.g. the plan unit's title), set at launch
    # so the UI can show what a run is doing while it dispatches — before any worker
    # output or terminal summary. Nullable: standalone/legacy runs may not carry one.
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS subject TEXT",
    # The per-run Slack profile (the selector), validated against the non-secret
    # registry at launch. Nullable: NULL = opt-out (a silent run, no Slack). Stored so
    # every milestone this run emits carries the profile the bridge routes on.
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS slack_profile TEXT",
    "CREATE INDEX IF NOT EXISTS runs_status_idx ON runs (status)",
    "CREATE INDEX IF NOT EXISTS runs_created_at_idx ON runs (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS runs_plan_idx ON runs (plan_id)",
    # Durable per-run event log backing the SSE feed's replay (Last-Event-ID). The
    # in-process channel is the LIVE tail; this table is the durable timeline, so a
    # reconnect after a restart reconstructs the full history — not just the resume
    # leg. `seq` is a global per-run counter (NOT the in-memory index), so numbering
    # continues across process restarts.
    """
    CREATE TABLE IF NOT EXISTS run_events (
        run_id      TEXT NOT NULL,
        seq         INTEGER NOT NULL,
        event_type  TEXT NOT NULL,
        data        JSONB NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (run_id, seq)
    )
    """,
    # The fleet-wide NOTIFICATION outbox: one durable row per run-lifecycle milestone
    # (NOT the per-node firehose — that's run_events). A fleet bridge tails this to
    # learn "something happened somewhere" without following every run's SSE. ``seq``
    # is a GLOBAL monotonic cursor (across all runs), so a consumer advances one durable
    # position; gaps from deduped conflicts are fine (seq is monotonic, not gapless).
    # ``payload`` is metadata-only by construction (see NotificationPayload) — never
    # prompt/code/diff/target contents. ``slack_profile`` is the run's profile (nullable)
    # so the bridge filters/routes without a second lookup.
    """
    CREATE TABLE IF NOT EXISTS notifications (
        seq           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        run_id        TEXT NOT NULL,
        slack_profile TEXT,
        kind          TEXT NOT NULL,
        payload       JSONB NOT NULL,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Once-only milestones dedupe on (run_id, kind): a resume that re-crosses the same
    # node boundary re-appends the same (run_id, kind) → ON CONFLICT DO NOTHING, so no
    # double-emit. Same discipline as the runs ledger and run_events.
    "CREATE UNIQUE INDEX IF NOT EXISTS notifications_run_kind_idx "
    "ON notifications (run_id, kind)",
)


@dataclass
class RunRow:
    """One row of the ``runs`` table."""

    run_id: str
    thread_id: str
    target: Optional[str]  # the PORTABLE identity (normalized remote ref), not a path
    task_type: Optional[str]
    status: str
    cost_usd: float
    created_at: Optional[datetime]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    detail: Optional[str]
    # Plan link (None for standalone runs): the owning plan + the unit seq this run
    # was built from. Defaulted so RunRow(**row) works for pre-link rows / mock stores.
    plan_id: Optional[str] = None
    plan_unit_seq: Optional[int] = None
    # The derived machine-local working dir the run executed in (worktrees carved
    # here). Separate from ``target`` — a portable ref must not be a local path.
    # Defaulted so RunRow(**row) works for rows created before this column.
    local_path: Optional[str] = None
    # The changed-files diff a go applied (branch/message/files/stat/patch), captured
    # at apply time and persisted so it survives worktree teardown. None for sims,
    # no-change burns, and rows created before this column.
    changes_json: Optional[dict] = None
    # A short human description of the task, set at launch (e.g. the plan unit's title),
    # so the UI has a subject to show while the run dispatches. Defaulted for rows /
    # mock stores created before this column.
    subject: Optional[str] = None
    # The per-run Slack profile (the selector), validated at launch against the
    # non-secret registry. None = opt-out (silent run). Defaulted for pre-column rows.
    slack_profile: Optional[str] = None


# Columns whose non-None values narrow a list_runs() query.
_FILTERABLE = ("status", "task_type", "target", "run_id", "plan_id")


class RunStore:
    """Reads/writes the ``runs`` ledger over a shared psycopg connection pool.

    The pool is the one the checkpointer uses (autocommit); this store never opens
    its own — see :func:`mission_control.graph.postgres_checkpointer`.
    """

    def __init__(self, pool) -> None:
        self._pool = pool

    # -- schema ------------------------------------------------------------

    def setup(self) -> None:
        """Create the table if absent. Idempotent (mirrors PostgresSaver.setup())."""
        with self._pool.connection() as conn:
            for statement in _DDL:
                conn.execute(statement)

    # -- transitions (all idempotent upserts keyed by run_id) --------------

    def launch(
        self,
        run_id: str,
        *,
        task_type: Optional[str] = None,
        target: Optional[str] = None,
        local_path: Optional[str] = None,
        plan_id: Optional[str] = None,
        plan_unit_seq: Optional[int] = None,
        subject: Optional[str] = None,
        slack_profile: Optional[str] = None,
    ) -> None:
        """Record a newly submitted run as ``queued``. A no-op if the row already
        exists (so re-submitting or resuming never resets an in-flight run). A run
        built from a plan carries its ``plan_id`` + ``plan_unit_seq`` (the link).
        ``target`` is the portable ref; ``local_path`` the derived working dir.
        ``subject`` is a short human description of the task (e.g. the plan unit's
        title), set at launch so the UI has something to show while the run dispatches —
        before any worker output or terminal summary exists. ``slack_profile`` is the
        validated per-run Slack selector (None = silent run)."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, thread_id, task_type, target, local_path, status,
                                  cost_usd, created_at, plan_id, plan_unit_seq, subject,
                                  slack_profile)
                VALUES (%(run_id)s, %(run_id)s, %(task_type)s, %(target)s, %(local_path)s,
                        %(status)s, 0, now(), %(plan_id)s, %(unit_seq)s, %(subject)s,
                        %(slack_profile)s)
                ON CONFLICT (run_id) DO NOTHING
                """,
                {"run_id": run_id, "task_type": task_type, "target": target,
                 "local_path": local_path, "status": STATUS_QUEUED,
                 "plan_id": plan_id, "unit_seq": plan_unit_seq, "subject": subject,
                 "slack_profile": slack_profile},
            )

    def plan_runs(self, plan_id: str) -> list[RunRow]:
        """A plan's child runs, ordered by the unit seq they were built from — the
        build work-list as it executes."""
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT * FROM runs WHERE plan_id = %s "
                "ORDER BY plan_unit_seq ASC NULLS LAST, created_at ASC",
                (plan_id,),
            )
            return [RunRow(**r) for r in cur.fetchall()]

    def plan_cost(self, plan_id: str) -> float:
        """Rolled-up reconciled cost across a plan's child runs (in-flight runs
        contribute 0 until teardown reconciles them — see cost_summary)."""
        with self._pool.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COALESCE(sum(cost_usd), 0) FROM runs WHERE plan_id = %s",
                        (plan_id,))
            return round(float(cur.fetchone()[0]), 8)

    def mark_running(
        self, run_id: str, *, target: Optional[str] = None, local_path: Optional[str] = None
    ) -> None:
        """Move to ``running`` and stamp ``started_at`` once. Upserts the row if a
        launch insert never happened, so the ledger is robust to a missed launch.
        ``target`` is the portable ref; ``local_path`` the derived working dir."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, thread_id, target, local_path, status, cost_usd,
                                  created_at, started_at)
                VALUES (%(run_id)s, %(run_id)s, %(target)s, %(local_path)s, %(status)s, 0,
                        now(), now())
                ON CONFLICT (run_id) DO UPDATE SET
                    status     = EXCLUDED.status,
                    target     = COALESCE(EXCLUDED.target, runs.target),
                    local_path = COALESCE(EXCLUDED.local_path, runs.local_path),
                    started_at = COALESCE(runs.started_at, EXCLUDED.started_at)
                """,
                {"run_id": run_id, "target": target, "local_path": local_path,
                 "status": STATUS_RUNNING},
            )

    def mark_awaiting_gate(self, run_id: str) -> None:
        """Move to ``awaiting_gate`` (a burn paused at the durable go/no-go gate)."""
        self._set_status(run_id, STATUS_AWAITING_GATE)

    def set_changes(self, run_id: str, changes: dict) -> None:
        """Persist the changed-files diff a go applied for ``run_id`` (the payload
        ``worktree.changes()`` produces). Idempotent: a re-run of the apply node
        re-writes the same payload rather than duplicating. Additive — touches only
        ``changes_json`` and never moves the row's status/stamps."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, thread_id, status, cost_usd, created_at, changes_json)
                VALUES (%(run_id)s, %(run_id)s, %(status)s, 0, now(), %(changes)s)
                ON CONFLICT (run_id) DO UPDATE SET changes_json = EXCLUDED.changes_json
                """,
                {"run_id": run_id, "status": STATUS_RUNNING, "changes": Jsonb(changes)},
            )

    def finish(
        self,
        run_id: str,
        *,
        status: str,
        cost_usd: float,
        detail: Optional[str] = None,
    ) -> None:
        """Record a terminal status with the run's total cost and a short summary,
        stamping ``ended_at`` once. ``cost_usd`` is the absolute running total (the
        sum of the run's priced step events), so a re-run node sets the same value
        rather than double-counting."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, thread_id, status, cost_usd, created_at, ended_at, detail)
                VALUES (%(run_id)s, %(run_id)s, %(status)s, %(cost)s, now(), now(), %(detail)s)
                ON CONFLICT (run_id) DO UPDATE SET
                    status   = EXCLUDED.status,
                    cost_usd = EXCLUDED.cost_usd,
                    detail   = COALESCE(EXCLUDED.detail, runs.detail),
                    ended_at = COALESCE(runs.ended_at, EXCLUDED.ended_at)
                """,
                {"run_id": run_id, "status": status, "cost": cost_usd, "detail": detail},
            )

    def mark_failed(self, run_id: str, detail: str) -> None:
        """Terminal ``failed`` with an error string; stamps ``ended_at`` once.

        Deliberately does NOT touch ``cost_usd`` on an existing row — a failed run may
        still have spent tokens (e.g. a worker that ran out of turns), and that cost is
        recorded separately by :meth:`record_cost` before the failure surfaces here. The
        INSERT's ``0`` only applies to the (rare) case of a never-launched run."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, thread_id, status, cost_usd, created_at, ended_at, detail)
                VALUES (%(run_id)s, %(run_id)s, %(status)s, 0, now(), now(), %(detail)s)
                ON CONFLICT (run_id) DO UPDATE SET
                    status   = EXCLUDED.status,
                    detail   = EXCLUDED.detail,
                    ended_at = COALESCE(runs.ended_at, EXCLUDED.ended_at)
                """,
                {"run_id": run_id, "status": STATUS_FAILED, "detail": detail},
            )

    def record_cost(self, run_id: str, cost_usd: float) -> None:
        """Set a run's cost (absolute, idempotent) without moving its status — used to
        record what a run spent even when it FAILS (e.g. a worker that exhausted its
        turn budget still burned real tokens). Kept separate from the terminal-status
        writers so cost is honest regardless of outcome."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE runs SET cost_usd = %(cost)s WHERE run_id = %(run_id)s",
                {"cost": round(float(cost_usd), 8), "run_id": run_id},
            )

    def _set_status(self, run_id: str, status: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, thread_id, status, cost_usd, created_at)
                VALUES (%(run_id)s, %(run_id)s, %(status)s, 0, now())
                ON CONFLICT (run_id) DO UPDATE SET status = EXCLUDED.status
                """,
                {"run_id": run_id, "status": status},
            )

    # -- queries -----------------------------------------------------------

    def get_run(self, run_id: str) -> Optional[RunRow]:
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute("SELECT * FROM runs WHERE run_id = %s", (run_id,))
            row = cur.fetchone()
        return RunRow(**row) if row else None

    def _where(
        self,
        filter: Optional[dict],
        created_from: Optional[datetime],
        created_to: Optional[datetime],
    ) -> tuple[str, dict]:
        """Build a shared WHERE clause for list/count/summary (parameterized)."""
        clauses, params = [], {}
        for key, value in (filter or {}).items():
            if key not in _FILTERABLE:
                raise ValueError(f"unfilterable column: {key!r} (allowed: {_FILTERABLE})")
            if value is not None:
                clauses.append(f"{key} = %({key})s")
                params[key] = value
        if created_from is not None:
            clauses.append("created_at >= %(_from)s")
            params["_from"] = created_from
        if created_to is not None:
            clauses.append("created_at < %(_to)s")
            params["_to"] = created_to
        return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params

    def list_runs(
        self,
        filter: Optional[dict] = None,
        *,
        limit: int = 100,
        offset: int = 0,
        order: str = "desc",
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
    ) -> list[RunRow]:
        """A page of runs ordered by ``created_at`` (``desc`` = newest-first, default).
        ``filter`` narrows by status/task_type/target/run_id (None values ignored);
        ``created_from``/``created_to`` bound the window (half-open [from, to))."""
        direction = "ASC" if str(order).lower() == "asc" else "DESC"
        where, params = self._where(filter, created_from, created_to)
        params["_limit"], params["_offset"] = limit, offset
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                f"SELECT * FROM runs {where} "
                f"ORDER BY created_at {direction} LIMIT %(_limit)s OFFSET %(_offset)s",
                params,
            )
            rows = cur.fetchall()
        return [RunRow(**r) for r in rows]

    def count_runs(
        self,
        filter: Optional[dict] = None,
        *,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
    ) -> int:
        """Total matching rows (for paging), ignoring limit/offset."""
        where, params = self._where(filter, created_from, created_to)
        with self._pool.connection() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT count(*) FROM runs {where}", params)
            return int(cur.fetchone()[0])

    def cost_summary(
        self,
        filter: Optional[dict] = None,
        *,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
    ) -> dict:
        """Aggregate rollup over the runs registry for a scope (target/window): total
        runs + reconciled cost, priced steps (from the event log), and the sim/burn
        and per-target breakdowns. The transactional counterpart to the DuckDB pass —
        cheap and exact. Cost is reconciled-at-teardown; in-flight runs contribute 0
        (unreconciled, not free — see PHASE5A_FINDINGS Q1)."""
        where, params = self._where(filter, created_from, created_to)
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                f"SELECT count(*) AS runs, COALESCE(sum(cost_usd), 0) AS cost_usd FROM runs {where}",
                params,
            )
            total = cur.fetchone()
            cur.execute(
                f"SELECT task_type, count(*) AS runs, COALESCE(sum(cost_usd), 0) AS cost_usd "
                f"FROM runs {where} GROUP BY task_type ORDER BY task_type",
                params,
            )
            by_task_type = cur.fetchall()
            cur.execute(
                f"SELECT target, count(*) AS runs, COALESCE(sum(cost_usd), 0) AS cost_usd "
                f"FROM runs {where} GROUP BY target ORDER BY cost_usd DESC, target",
                params,
            )
            by_target = cur.fetchall()
            # Priced steps for the scoped runs, from the durable event log.
            cur.execute(
                f"SELECT count(*) FROM run_events WHERE event_type = 'step_metric' "
                f"AND run_id IN (SELECT run_id FROM runs {where})",
                params,
            )
            steps = int(cur.fetchone()["count"])
        return {
            "runs": int(total["runs"]),
            "cost_usd": round(float(total["cost_usd"]), 8),
            "steps": steps,
            "by_task_type": [
                {"task_type": r["task_type"], "runs": int(r["runs"]),
                 "cost_usd": round(float(r["cost_usd"]), 8)}
                for r in by_task_type if r["task_type"]
            ],
            "by_target": [
                {"target": r["target"], "runs": int(r["runs"]),
                 "cost_usd": round(float(r["cost_usd"]), 8)}
                for r in by_target if r["target"]
            ],
        }

    def profile_digest(
        self, slack_profile: str, *, created_from: Optional[datetime] = None, top_n: int = 5
    ) -> dict:
        """A metadata-only fleet digest scoped to ONE Slack profile: the runs that named
        it (a run with no profile appears in no digest). Returns total runs + reconciled
        cost, the status breakdown (applied/scrubbed/failed/…), and the top targets by
        cost. ``created_from`` bounds the window (e.g. the last day)."""
        clause = "slack_profile = %(profile)s"
        params: dict = {"profile": slack_profile, "top_n": top_n}
        if created_from is not None:
            clause += " AND created_at >= %(from)s"
            params["from"] = created_from
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                f"SELECT count(*) AS runs, COALESCE(sum(cost_usd), 0) AS cost_usd "
                f"FROM runs WHERE {clause}", params)
            total = cur.fetchone()
            cur.execute(
                f"SELECT status, count(*) AS runs FROM runs WHERE {clause} "
                f"GROUP BY status", params)
            by_status = {r["status"]: int(r["runs"]) for r in cur.fetchall()}
            cur.execute(
                f"SELECT target, count(*) AS runs, COALESCE(sum(cost_usd), 0) AS cost_usd "
                f"FROM runs WHERE {clause} AND target IS NOT NULL "
                f"GROUP BY target ORDER BY cost_usd DESC, target LIMIT %(top_n)s", params)
            top_targets = cur.fetchall()
        return {
            "profile": slack_profile,
            "runs": int(total["runs"]),
            "cost_usd": round(float(total["cost_usd"]), 8),
            "by_status": by_status,
            "top_targets": [
                {"target": r["target"], "runs": int(r["runs"]),
                 "cost_usd": round(float(r["cost_usd"]), 8)}
                for r in top_targets
            ],
        }

    def list_targets(self) -> list[str]:
        """Distinct non-null targets the registry has seen, alphabetical — the
        set the UI's launch selector offers."""
        with self._pool.connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT target FROM runs WHERE target IS NOT NULL ORDER BY target"
            )
            return [r[0] for r in cur.fetchall()]

    # -- durable event log (backs the SSE replay) --------------------------

    def append_event(self, run_id: str, seq: int, event_type: str, data: dict) -> None:
        """Persist one feed event at a global per-run ``seq``. Idempotent: a replayed
        node that re-emits the same (run_id, seq) is a no-op."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO run_events (run_id, seq, event_type, data)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id, seq) DO NOTHING
                """,
                (run_id, seq, event_type, Jsonb(data)),
            )

    def read_events(self, run_id: str, *, after_seq: Optional[int] = None) -> list[dict]:
        """The durable timeline for a run, in order; ``after_seq`` replays only the
        tail past a client's Last-Event-ID."""
        clause = "AND seq > %(after)s" if after_seq is not None else ""
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                f"SELECT seq, event_type, data FROM run_events "
                f"WHERE run_id = %(run_id)s {clause} ORDER BY seq ASC",
                {"run_id": run_id, "after": after_seq},
            )
            return [
                {"seq": r["seq"], "event": r["event_type"], "data": r["data"]}
                for r in cur.fetchall()
            ]

    def max_event_seq(self, run_id: str) -> int:
        """Highest persisted seq for a run, or -1 if none (seeds the live counter so
        numbering continues across a restart)."""
        with self._pool.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT max(seq) FROM run_events WHERE run_id = %s", (run_id,))
            value = cur.fetchone()[0]
        return -1 if value is None else int(value)

    # -- notification outbox (the fleet-wide milestone feed) ---------------

    def append_notification(
        self,
        run_id: str,
        kind: str,
        *,
        slack_profile: Optional[str] = None,
        payload: dict,
    ) -> bool:
        """Append one NOTIFICATION-GRAIN milestone to the outbox, assigning the next
        GLOBAL ``seq``. Once-only milestones (launch/gate/terminal) dedupe on
        (run_id, kind): a resume that re-crosses the boundary is a no-op, so a
        kill→restart→resume never double-emits. ``payload`` must be metadata-only.

        Returns ``True`` when a row was inserted, ``False`` when a once-only milestone
        was deduped away."""
        with self._pool.connection() as conn:
            conflict = (
                "ON CONFLICT (run_id, kind) DO NOTHING"
                if kind in _ONCE_ONLY_KINDS else ""
            )
            cur = conn.execute(
                f"""
                INSERT INTO notifications (run_id, slack_profile, kind, payload)
                VALUES (%s, %s, %s, %s)
                {conflict}
                """,
                (run_id, slack_profile, kind, Jsonb(payload)),
            )
            return cur.rowcount > 0

    def read_notifications(
        self, *, after_seq: int = 0, limit: int = 100
    ) -> list[dict]:
        """The outbox tail with ``seq > after_seq``, oldest-first (ascending seq) so a
        bridge processes milestones in order and advances its durable cursor to the
        last seq it saw. ``limit`` bounds one pull."""
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                """
                SELECT seq, run_id, slack_profile, kind, payload, created_at
                FROM notifications WHERE seq > %(after)s
                ORDER BY seq ASC LIMIT %(limit)s
                """,
                {"after": after_seq, "limit": limit},
            )
            return [dict(r) for r in cur.fetchall()]

    def notifications_summary(self) -> dict:
        """The outbox's total row count + highest global ``seq`` (0 if empty) — so a
        consumer knows how far behind its cursor is and how far it can advance."""
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT count(*) AS total, COALESCE(max(seq), 0) AS last_seq "
                "FROM notifications"
            )
            row = cur.fetchone()
        return {"total": int(row["total"]), "last_seq": int(row["last_seq"])}
