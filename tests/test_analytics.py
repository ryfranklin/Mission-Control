"""DuckDB analytics over JSONL — aggregates match a hand-computed fixture.

Offline (DuckDB is local; no Postgres, nothing copied anywhere)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mission_control.analytics import analyze

# Two runs with the full cost breakdown + one older run WITHOUT cost_worker/judge
# (schema drift — must read as NULL → coalesced to 0).
RUN_A = [
    {"task_id": "t1", "task_type": "sim", "quality_total": 1.0, "cost_usd": 0.03, "cost_worker": 0.01, "cost_judge": 0.02},
    {"task_id": "t2", "task_type": "burn", "quality_total": 0.8, "cost_usd": 0.05, "cost_worker": 0.02, "cost_judge": 0.03},
]
RUN_B = [
    {"task_id": "t1", "task_type": "sim", "quality_total": 0.9, "cost_usd": 0.04, "cost_worker": 0.01, "cost_judge": 0.03},
    {"task_id": "t2", "task_type": "burn", "quality_total": 0.7, "cost_usd": 0.06, "cost_worker": 0.02, "cost_judge": 0.04},
]
RUN_C = [  # older schema: no worker/judge split
    {"task_id": "t1", "task_type": "sim", "quality_total": 0.5, "cost_usd": 0.02},
]


def _write_run(d: Path, stamp: str, rows: list[dict]) -> None:
    (d / f"eval-{stamp}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


@pytest.fixture
def evaldir(tmp_path: Path) -> Path:
    d = tmp_path / "evals"
    d.mkdir()
    _write_run(d, "20260101-000001-aaa", RUN_A)
    _write_run(d, "20260101-000002-bbb", RUN_B)
    _write_run(d, "20260101-000003-ccc", RUN_C)
    return d


def _by_run(per_run: list[dict]) -> dict[str, dict]:
    return {r["run"]: r for r in per_run}


def test_per_run_aggregates(evaldir):
    a = analyze(eval_glob=str(evaldir / "eval-*.jsonl"), telemetry_glob=str(evaldir / "none-*.jsonl"))
    runs = _by_run(a.per_run)
    ra = runs["eval-20260101-000001-aaa.jsonl"]
    assert ra["tasks"] == 2
    assert ra["cost_usd"] == pytest.approx(0.08)
    assert ra["cost_worker"] == pytest.approx(0.03)
    assert ra["cost_judge"] == pytest.approx(0.05)
    assert ra["quality_total"] == pytest.approx(0.9)
    rc = runs["eval-20260101-000003-ccc.jsonl"]
    assert rc["cost_usd"] == pytest.approx(0.02)
    assert rc["cost_worker"] == 0 and rc["cost_judge"] == 0   # schema drift → 0
    assert rc["quality_total"] == pytest.approx(0.5)


def test_by_task_type(evaldir):
    a = analyze(eval_glob=str(evaldir / "eval-*.jsonl"), telemetry_glob=str(evaldir / "none-*.jsonl"))
    bt = {r["task_type"]: r for r in a.by_task_type}
    assert bt["sim"]["n"] == 3
    assert bt["sim"]["avg_cost"] == pytest.approx(0.03)      # (0.03+0.04+0.02)/3
    assert bt["sim"]["avg_quality"] == pytest.approx(0.8)    # (1.0+0.9+0.5)/3
    assert bt["burn"]["n"] == 2
    assert bt["burn"]["avg_cost"] == pytest.approx(0.055)
    assert bt["burn"]["avg_quality"] == pytest.approx(0.75)


def test_worker_vs_judge_split(evaldir):
    a = analyze(eval_glob=str(evaldir / "eval-*.jsonl"), telemetry_glob=str(evaldir / "none-*.jsonl"))
    w = a.worker_vs_judge
    assert w["worker"] == pytest.approx(0.06)                # run C contributes 0
    assert w["judge"] == pytest.approx(0.12)
    assert w["judge_share"] == pytest.approx(0.12 / 0.18, abs=1e-4)  # stored rounded to 4dp


def test_quality_trend_ordered(evaldir):
    a = analyze(eval_glob=str(evaldir / "eval-*.jsonl"), telemetry_glob=str(evaldir / "none-*.jsonl"))
    assert [round(r["quality_total"], 3) for r in a.quality_trend] == [0.9, 0.8, 0.5]


def test_rollup_persisted(evaldir, tmp_path):
    out = tmp_path / "roll" / "rollup.json"
    analyze(eval_glob=str(evaldir / "eval-*.jsonl"),
            telemetry_glob=str(evaldir / "none-*.jsonl"), rollup_path=out)
    assert out.exists()
    doc = json.loads(out.read_text())
    assert len(doc["per_run"]) == 3 and "worker_vs_judge" in doc


def test_telemetry_rollup(tmp_path):
    d = tmp_path / "telem"
    d.mkdir()
    steps = [
        {"step_id": "s0", "input_tokens": 100, "output_tokens": 50,
         "cache_read_tokens": 10, "cache_creation_tokens": 5, "cost_usd": 0.001},
        {"step_id": "s1", "input_tokens": 200, "output_tokens": 60,
         "cache_read_tokens": 0, "cache_creation_tokens": 0, "cost_usd": 0.002},
    ]
    (d / "run-x.jsonl").write_text("\n".join(json.dumps(s) for s in steps) + "\n")
    a = analyze(eval_glob=str(tmp_path / "none-*.jsonl"), telemetry_glob=str(d / "*.jsonl"))
    t = a.telemetry_rollup
    assert t["steps"] == 2
    assert t["tokens"] == 100 + 50 + 10 + 5 + 200 + 60      # 425
    assert t["cost_usd"] == pytest.approx(0.003)
