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
