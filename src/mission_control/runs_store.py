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

# -- lifecycle statuses (functional labels; sim/burn metaphor stays in roles) --
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_AWAITING_GATE = "awaiting_gate"
STATUS_APPLIED = "applied"
STATUS_SCRUBBED = "scrubbed"
STATUS_FAILED = "failed"
STATUS_DONE = "done"

# States after which no more work happens; each stamps ended_at exactly once.
TERMINAL_STATUSES = frozenset(
    {STATUS_APPLIED, STATUS_SCRUBBED, STATUS_FAILED, STATUS_DONE}
)

# One statement per entry: the autocommit pool prepares statements, which forbids
# multiple commands in a single execute().
_DDL = (
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id      TEXT PRIMARY KEY,
        thread_id   TEXT NOT NULL,
        target      TEXT,
        task_type   TEXT,
        status      TEXT NOT NULL,
        cost_usd    DOUBLE PRECISION NOT NULL DEFAULT 0,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        started_at  TIMESTAMPTZ,
        ended_at    TIMESTAMPTZ,
        detail      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS runs_status_idx ON runs (status)",
    "CREATE INDEX IF NOT EXISTS runs_created_at_idx ON runs (created_at DESC)",
)


@dataclass
class RunRow:
    """One row of the ``runs`` table."""

    run_id: str
    thread_id: str
    target: Optional[str]
    task_type: Optional[str]
    status: str
    cost_usd: float
    created_at: Optional[datetime]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    detail: Optional[str]


# Columns whose non-None values narrow a list_runs() query.
_FILTERABLE = ("status", "task_type", "target", "run_id")


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
    ) -> None:
        """Record a newly submitted run as ``queued``. A no-op if the row already
        exists (so re-submitting or resuming never resets an in-flight run)."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, thread_id, task_type, target, status, cost_usd, created_at)
                VALUES (%(run_id)s, %(run_id)s, %(task_type)s, %(target)s, %(status)s, 0, now())
                ON CONFLICT (run_id) DO NOTHING
                """,
                {"run_id": run_id, "task_type": task_type, "target": target, "status": STATUS_QUEUED},
            )

    def mark_running(self, run_id: str, *, target: Optional[str] = None) -> None:
        """Move to ``running`` and stamp ``started_at`` once. Upserts the row if a
        launch insert never happened, so the ledger is robust to a missed launch."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, thread_id, target, status, cost_usd, created_at, started_at)
                VALUES (%(run_id)s, %(run_id)s, %(target)s, %(status)s, 0, now(), now())
                ON CONFLICT (run_id) DO UPDATE SET
                    status     = EXCLUDED.status,
                    target     = COALESCE(EXCLUDED.target, runs.target),
                    started_at = COALESCE(runs.started_at, EXCLUDED.started_at)
                """,
                {"run_id": run_id, "target": target, "status": STATUS_RUNNING},
            )

    def mark_awaiting_gate(self, run_id: str) -> None:
        """Move to ``awaiting_gate`` (a burn paused at the durable go/no-go gate)."""
        self._set_status(run_id, STATUS_AWAITING_GATE)

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
        """Terminal ``failed`` with an error string; stamps ``ended_at`` once."""
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

    def list_runs(self, filter: Optional[dict] = None, *, limit: int = 100) -> list[RunRow]:
        """Runs newest-first. ``filter`` narrows by any of status/task_type/target/
        run_id (keys with None values are ignored); unknown keys are rejected."""
        clauses, params = [], {}
        for key, value in (filter or {}).items():
            if key not in _FILTERABLE:
                raise ValueError(f"unfilterable column: {key!r} (allowed: {_FILTERABLE})")
            if value is not None:
                clauses.append(f"{key} = %({key})s")
                params[key] = value
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params["_limit"] = limit
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                f"SELECT * FROM runs {where} ORDER BY created_at DESC LIMIT %(_limit)s",
                params,
            )
            rows = cur.fetchall()
        return [RunRow(**r) for r in rows]
