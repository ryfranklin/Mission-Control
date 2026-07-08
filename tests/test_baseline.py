"""Baseline + regression-check statistics (offline, StubWorker)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mission_control import StubWorker, roles
from mission_control.baseline import (
    Baseline,
    MetricStats,
    Stat,
    build_baseline,
    check_regression,
    check_run,
)
from mission_control.evals import EvalResult, EvalRun


# -- Stat math -------------------------------------------------------------

def test_stat_from_samples():
    s = Stat.from_samples([1, 2, 3])
    assert s.mean == 2.0
    assert s.stddev == pytest.approx(1.0)  # sample stddev
    assert (s.min, s.max, s.n) == (1.0, 3.0, 3)


def test_stat_single_sample_has_zero_stddev():
    s = Stat.from_samples([0.5])
    assert s.stddev == 0.0 and s.mean == 0.5


# -- synthetic baseline with real spread -----------------------------------

def _synth_baseline() -> Baseline:
    # quality mean 0.90, stddev ~0.038 → band lower ~0.824 at k=2
    q = Stat.from_samples([0.90, 0.85, 0.95, 0.88, 0.92])
    # cost mean 0.30, stddev ~0.0158 → band upper ~0.332 at k=2
    c = Stat.from_samples([0.30, 0.31, 0.29, 0.32, 0.28])
    return Baseline(
        n=5, k=2.0, tasks=["t1"],
        aggregate=MetricStats(q, c),
        per_task={"t1": MetricStats(q, c)},
        created="test",
    )


def test_within_noise_run_passes():
    bl = _synth_baseline()
    rep = check_regression(bl, current_quality=0.87, current_cost=0.31)
    assert rep.passed
    assert not rep.quality.regressed and not rep.cost.regressed


def test_out_of_band_quality_drop_flags_quality_axis_only():
    bl = _synth_baseline()
    rep = check_regression(bl, current_quality=0.50, current_cost=0.30)
    assert rep.quality.regressed is True
    assert rep.cost.regressed is False
    assert not rep.passed
    assert "REGRESSION" in rep.report()


def test_out_of_band_cost_spike_flags_cost_axis_only():
    bl = _synth_baseline()
    rep = check_regression(bl, current_quality=0.90, current_cost=0.50)
    assert rep.cost.regressed is True
    assert rep.quality.regressed is False
    assert not rep.passed


def test_k_widens_the_band():
    bl = _synth_baseline()
    # A drop that trips at k=2 should be tolerated at a large k.
    assert check_regression(bl, current_quality=0.80, current_cost=0.30, k=2.0).quality.regressed
    assert not check_regression(bl, current_quality=0.80, current_cost=0.30, k=10.0).quality.regressed


def test_per_task_regression_detected():
    bl = _synth_baseline()
    rep = check_regression(
        bl, current_quality=0.90, current_cost=0.30,
        per_task_current={"t1": (0.40, 0.30)},  # task quality tanks
    )
    assert any(a.regressed and a.metric == "quality_total" and a.scope == "t1" for a in rep.per_task)
    assert not rep.passed


# -- integration: build over the golden path with StubWorker ---------------

def _sandbox(tmp_path: Path) -> Path:
    s = tmp_path / "sandbox"
    s.mkdir()
    (s / "README.md").write_text("# sandbox\n")
    return s


def _spec(tmp_path: Path, name: str) -> Path:
    p = tmp_path / f"{name}.yaml"
    p.write_text(yaml.safe_dump({
        "id": name,
        "task_type": roles.SIM,
        "prompt": "inspect",
        "known_good": {
            "deterministic": {"outcome": "completed", "no_changes": True,
                              "output_contains": ["stub"]},
            "judge_rubric": [],
        },
    }))
    return p


def test_build_baseline_persists_and_round_trips(tmp_path):
    specs = [_spec(tmp_path, "a"), _spec(tmp_path, "b")]
    bpath = tmp_path / "baseline.json"
    bl = build_baseline(
        specs, sandbox_src=_sandbox(tmp_path), worker=StubWorker(),
        n=3, out_dir=tmp_path / "out", baseline_path=bpath,
    )
    assert bpath.exists()
    assert bl.n == 3 and bl.tasks == ["a", "b"]
    # StubWorker is deterministic → zero spread.
    assert bl.aggregate.quality_total.stddev == 0.0
    assert bl.aggregate.cost_usd.stddev == 0.0

    loaded = Baseline.load(bpath)
    assert loaded.tasks == bl.tasks
    assert loaded.aggregate.cost_usd.mean == bl.aggregate.cost_usd.mean
    assert set(loaded.per_task) == {"a", "b"}


def test_fresh_stub_run_passes_against_its_baseline(tmp_path):
    specs = [_spec(tmp_path, "a")]
    sandbox = _sandbox(tmp_path)
    bl = build_baseline(specs, sandbox_src=sandbox, worker=StubWorker(),
                        n=3, out_dir=tmp_path / "out")
    from mission_control.evals import run_eval
    fresh = run_eval(specs, sandbox_src=sandbox, worker=StubWorker(), out_dir=tmp_path / "out2")
    rep = check_run(bl, fresh)
    assert rep.passed  # identical to baseline → within (zero) band


def test_degraded_run_is_flagged(tmp_path):
    specs = [_spec(tmp_path, "a")]
    bl = build_baseline(specs, sandbox_src=_sandbox(tmp_path), worker=StubWorker(),
                        n=3, out_dir=tmp_path / "out")
    degraded = EvalRun(
        path=Path("x"),
        results=[EvalResult(
            task_id="a", task_type="sim", asserts_passed=0, asserts_failed=2,
            quality_deterministic=0.0, quality_judge=None, quality_total=0.0,
            cost_usd=bl.aggregate.cost_usd.mean, total_tokens=100, latency_ms=10,
        )],
    )
    rep = check_run(bl, degraded)
    assert rep.quality.regressed and not rep.cost.regressed
