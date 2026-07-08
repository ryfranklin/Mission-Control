"""Eval gate CLI contract (offline).

Exit code is the contract: 0 on pass, nonzero on a quality OR total-cost
regression. The decision step (`evaluate_gate`) is tested with a synthetic
baseline; the full path (`run_gate` / `main`) is tested with StubWorker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from mission_control import StubWorker, roles
from mission_control.baseline import Baseline, MetricStats, Stat, build_baseline
from mission_control.eval_gate import evaluate_gate, run_gate


# -- fixtures ---------------------------------------------------------------

def _synth_baseline() -> Baseline:
    q = Stat.from_samples([0.90, 0.85, 0.95, 0.88, 0.92])   # ~0.90 ± 0.038 → lower ~0.824
    c = Stat.from_samples([0.30, 0.31, 0.29, 0.32, 0.28])   # ~0.30 ± 0.016 → upper ~0.332
    return Baseline(n=5, k=2.0, tasks=["t"], aggregate=MetricStats(q, c),
                    per_task={"t": MetricStats(q, c)}, created="test")


def _sandbox(tmp_path: Path) -> Path:
    s = tmp_path / "sandbox"
    s.mkdir()
    (s / "README.md").write_text("# sandbox\n")
    return s


def _tasks_dir(tmp_path: Path) -> Path:
    d = tmp_path / "tasks"
    d.mkdir()
    (d / "sim-a.yaml").write_text(yaml.safe_dump({
        "id": "sim-a", "task_type": roles.SIM, "prompt": "inspect",
        "known_good": {
            "deterministic": {"outcome": "completed", "no_changes": True,
                              "output_contains": ["stub"]},
            "judge_rubric": [],
        },
    }))
    return d


# -- decision step: exit code per axis --------------------------------------

def test_within_band_exits_zero():
    r = evaluate_gate(_synth_baseline(), current_quality=0.88, current_cost=0.31)
    assert r.passed and r.exit_code == 0


def test_quality_drop_exits_nonzero_on_quality_axis():
    r = evaluate_gate(_synth_baseline(), current_quality=0.50, current_cost=0.30)
    assert r.exit_code != 0
    assert r.report.quality.regressed is True
    assert r.report.cost.regressed is False


def test_cost_spike_exits_nonzero_on_cost_axis():
    r = evaluate_gate(_synth_baseline(), current_quality=0.90, current_cost=0.50)
    assert r.exit_code != 0
    assert r.report.cost.regressed is True
    assert r.report.quality.regressed is False


def test_k_is_configurable():
    bl = _synth_baseline()
    assert evaluate_gate(bl, current_quality=0.80, current_cost=0.30, k=2.0).exit_code != 0
    assert evaluate_gate(bl, current_quality=0.80, current_cost=0.30, k=10.0).exit_code == 0


def test_result_emits_json_and_human_report():
    r = evaluate_gate(_synth_baseline(), current_quality=0.50, current_cost=0.30, n=3)
    j = r.to_json()
    assert j["exit_code"] == 1 and j["k"] == 2.0 and j["n"] == 3
    assert set(j["axes"]) == {"quality_total", "cost_usd"}
    assert j["axes"]["quality_total"]["regressed"] is True
    text = r.human_report()
    assert "REGRESSION" in text and "exit code: 1" in text
    assert "worker + judge" in text  # cost axis is documented as total


# -- full path: clean run exits 0 -------------------------------------------

def test_run_gate_clean_exits_zero(tmp_path):
    sandbox = _sandbox(tmp_path)
    tasks = sorted((_tasks_dir(tmp_path)).glob("*.yaml"))
    bpath = tmp_path / "baseline.json"
    build_baseline(tasks, sandbox_src=sandbox, worker=StubWorker(), n=3,
                   out_dir=tmp_path / "b", baseline_path=bpath)
    result = run_gate(baseline_path=bpath, tasks=tasks, sandbox_src=sandbox,
                      worker=StubWorker(), n=1, out_dir=tmp_path / "g")
    assert result.passed and result.exit_code == 0
    assert result.runs and "quality_total" in result.runs[0]


_REPO = Path(__file__).resolve().parents[1]


def _demo_gate(tmp_path, baseline_name):
    import mission_control.eval_gate as g

    with pytest.raises(SystemExit) as ei:
        g.main([
            "--demo",
            "--tasks", str(_REPO / "ci/demo/tasks"),
            "--sandbox", str(_REPO / "ci/demo/sandbox"),
            "--baseline", str(_REPO / "ci/demo" / baseline_name),
            "--json", str(tmp_path / "r.json"),
            "--out-dir", str(tmp_path / "o"),
        ])
    return ei.value.code


def test_demo_gate_clean_baseline_exits_zero(tmp_path):
    # The shipped --demo path against the committed pass baseline → green.
    assert _demo_gate(tmp_path, "baseline.pass.json") == 0


def test_demo_gate_regressed_baseline_exits_nonzero(tmp_path):
    # Same run, committed regressed baseline → the gate reddens (blocks promote).
    assert _demo_gate(tmp_path, "baseline.regressed.json") != 0


def test_main_clean_run_exits_zero_and_writes_json(tmp_path, monkeypatch):
    sandbox = _sandbox(tmp_path)
    tasks_dir = _tasks_dir(tmp_path)
    bpath = tmp_path / "baseline.json"
    build_baseline(sorted(tasks_dir.glob("*.yaml")), sandbox_src=sandbox,
                   worker=StubWorker(), n=3, out_dir=tmp_path / "b", baseline_path=bpath)

    import mission_control.eval_gate as g
    monkeypatch.setattr(g, "SdkWorker", lambda *a, **k: StubWorker())
    monkeypatch.setattr(g, "LlmJudge", lambda *a, **k: None)  # empty rubric → judge unused

    out_json = tmp_path / "gate.json"
    with pytest.raises(SystemExit) as ei:
        g.main([
            "--baseline", str(bpath), "--tasks", str(tasks_dir), "--sandbox", str(sandbox),
            "--n", "1", "--json", str(out_json), "--out-dir", str(tmp_path / "g"),
        ])
    assert ei.value.code == 0
    payload = json.loads(out_json.read_text())
    assert payload["passed"] is True and payload["exit_code"] == 0
