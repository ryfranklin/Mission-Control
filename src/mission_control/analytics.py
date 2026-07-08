"""DuckDB analytics over the JSONL spine — the mini-medallion read layer.

The telemetry + eval JSONL files are the raw/bronze spine (the transactional
write path is Postgres, kept SEPARATE — nothing is copied there). DuckDB queries
the JSONL in place via ``read_json_auto`` (zero ETL) as the columnar read layer,
and reports cross-run cost/quality: cost per run, per task_type, the worker-vs-
judge split, and the quality trend across runs.

``union_by_name`` tolerates schema drift across accumulated files (e.g. runs
recorded before the total-cost fold-in have no ``cost_worker``/``cost_judge`` —
they read as NULL and are coalesced to 0).

    python -m mission_control.analytics
"""

from __future__ import annotations

import glob
import json
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

DEFAULT_EVAL_GLOB = "evals/eval-*.jsonl"
DEFAULT_TELEMETRY_GLOB = "telemetry/*.jsonl"


def _files_literal(files) -> str:
    """A DuckDB list literal of file paths (single quotes escaped)."""
    return "[" + ", ".join("'" + str(f).replace("'", "''") + "'" for f in files) + "]"


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@dataclass
class Analytics:
    per_run: list[dict] = field(default_factory=list)
    by_task_type: list[dict] = field(default_factory=list)
    worker_vs_judge: dict = field(default_factory=dict)
    quality_trend: list[dict] = field(default_factory=list)
    telemetry_rollup: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "per_run": self.per_run,
            "by_task_type": self.by_task_type,
            "worker_vs_judge": self.worker_vs_judge,
            "quality_trend": self.quality_trend,
            "telemetry_rollup": self.telemetry_rollup,
        }

    def print_report(self) -> None:
        print("== cost / quality per run ==")
        print(f"  {'run':22} {'tasks':>5} {'cost':>10} {'worker':>9} {'judge':>9} {'quality':>8}")
        for r in self.per_run:
            print(f"  {r['run'][:22]:22} {r['tasks']:>5} ${r['cost_usd']:>9.6f} "
                  f"${r['cost_worker']:>8.6f} ${r['cost_judge']:>8.6f} {r['quality_total']:>8.3f}")
        print("\n== by task_type ==")
        for r in self.by_task_type:
            print(f"  {r['task_type']:6} n={r['n']:<4} avg_cost=${r['avg_cost']:.6f} "
                  f"avg_quality={r['avg_quality']:.3f}")
        w = self.worker_vs_judge
        if w:
            print("\n== worker vs judge (runs that recorded the split) ==")
            print(f"  worker ${w['worker']:.6f}  judge ${w['judge']:.6f}  "
                  f"judge_share {w['judge_share']:.0%}")
        if self.telemetry_rollup:
            t = self.telemetry_rollup
            print("\n== telemetry spine ==")
            print(f"  steps={t['steps']} tokens={t['tokens']} cost=${t['cost_usd']:.6f}")
        print("\n== quality trend across runs ==")
        spark = " ".join(f"{r['quality_total']:.2f}" for r in self.quality_trend)
        print(f"  {spark}")


def analyze(
    eval_glob: str = DEFAULT_EVAL_GLOB,
    telemetry_glob: str = DEFAULT_TELEMETRY_GLOB,
    *,
    rollup_path: Path | None = None,
) -> Analytics:
    con = duckdb.connect()
    result = Analytics()

    eval_files = sorted(glob.glob(eval_glob))
    if eval_files:
        con.execute(
            f"create or replace view evals as "
            f"select *, regexp_replace(filename, '.*/', '') as run "
            f"from read_json_auto({_files_literal(eval_files)}, filename=true, union_by_name=true)"
        )
        result.per_run = _rows(con, """
            select run, count(*) as tasks,
                   round(sum(cost_usd), 6) as cost_usd,
                   round(coalesce(sum(cost_worker), 0), 6) as cost_worker,
                   round(coalesce(sum(cost_judge), 0), 6) as cost_judge,
                   round(avg(quality_total), 4) as quality_total
            from evals group by run order by run
        """)
        result.by_task_type = _rows(con, """
            select task_type, count(*) as n,
                   round(avg(cost_usd), 6) as avg_cost,
                   round(avg(quality_total), 4) as avg_quality
            from evals group by task_type order by task_type
        """)
        wvj = _rows(con, """
            select round(coalesce(sum(cost_worker), 0), 6) as worker,
                   round(coalesce(sum(cost_judge), 0), 6) as judge
            from evals
        """)[0]
        total = wvj["worker"] + wvj["judge"]
        wvj["judge_share"] = round(wvj["judge"] / total, 4) if total else 0.0
        result.worker_vs_judge = wvj
        result.quality_trend = [
            {"run": r["run"], "quality_total": r["quality_total"]} for r in result.per_run
        ]

    tele_files = sorted(glob.glob(telemetry_glob))
    if tele_files:
        con.execute(
            f"create or replace view telem as "
            f"select * from read_json_auto({_files_literal(tele_files)}, union_by_name=true)"
        )
        result.telemetry_rollup = _rows(con, """
            select count(*) as steps,
                   coalesce(sum(input_tokens + output_tokens
                                + cache_read_tokens + cache_creation_tokens), 0) as tokens,
                   round(coalesce(sum(cost_usd), 0), 6) as cost_usd
            from telem
        """)[0]

    if rollup_path is not None:
        Path(rollup_path).parent.mkdir(parents=True, exist_ok=True)
        Path(rollup_path).write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True))

    con.close()
    return result


def main() -> None:  # pragma: no cover - convenience entry point
    result = analyze(rollup_path=Path("analytics/rollup.json"))
    if not result.per_run:
        print("no eval JSONL found (run some evals first, e.g. the golden set).")
        return
    result.print_report()
    print("\nrollup persisted → analytics/rollup.json")


if __name__ == "__main__":  # pragma: no cover
    main()
