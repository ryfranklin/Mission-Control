"""Deterministic eval runner (offline, StubWorker).

The runner is exercised with the StubWorker so tests are deterministic and make
no live calls. StubWorker: a sim reports "[stub] investigated …" and changes
nothing; a burn writes STUB_BURN.txt; both emit one synthetic telemetry step.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from mission_control import StubWorker, roles
from mission_control.evals import EvalResult, evaluate_task, run_eval

STUB_BURN_FILE = "STUB_BURN.txt"


def _sandbox(tmp_path: Path) -> Path:
    """Minimal sandbox fixture (no git — the runner initializes it)."""
    s = tmp_path / "sandbox"
    s.mkdir()
    (s / "README.md").write_text("# sandbox\n")
    return s


def _write_spec(tmp_path: Path, name: str, spec: dict) -> Path:
    p = tmp_path / f"{name}.yaml"
    p.write_text(yaml.safe_dump(spec, sort_keys=False))
    return p


def _sim_spec(task_id: str, deterministic: dict) -> dict:
    return {
        "id": task_id,
        "task_type": roles.SIM,
        "prompt": "inspect the repo",
        "known_good": {"deterministic": deterministic, "judge_rubric": []},
    }


def _burn_spec(task_id: str, approval: str, deterministic: dict) -> dict:
    return {
        "id": task_id,
        "task_type": roles.BURN,
        "approval": approval,
        "prompt": "make a change",
        "known_good": {"deterministic": deterministic, "judge_rubric": []},
    }


# -- a task whose asserts pass scores 1.0 ------------------------------------

# -- cost_usd is the TOTAL (worker + judge) -----------------------------------

def test_no_rubric_has_zero_judge_cost(tmp_path):
    spec = _write_spec(tmp_path, "sim-nojudge", _sim_spec("sim-nojudge", {"outcome": "completed"}))
    run = run_eval([spec], sandbox_src=_sandbox(tmp_path), worker=StubWorker(), out_dir=tmp_path / "out")
    (r,) = run.results
    assert r.cost_judge == 0.0
    assert r.cost_usd == r.cost_worker      # total == worker when judge skipped
    assert r.cost_worker > 0


def test_cost_usd_folds_in_judge(tmp_path, fake_judge):
    spec = _write_spec(tmp_path, "sim-judged", {
        "id": "sim-judged",
        "task_type": roles.SIM,
        "prompt": "inspect the repo",
        "known_good": {
            "deterministic": {"outcome": "completed"},
            "judge_rubric": [{"criterion": "good analysis", "weight": 1}],
        },
    })
    run = run_eval(
        [spec], sandbox_src=_sandbox(tmp_path), worker=StubWorker(),
        judge=fake_judge, out_dir=tmp_path / "out",
    )
    (r,) = run.results
    assert r.cost_judge > 0                                   # judge ran and cost something
    assert r.cost_usd == round(r.cost_worker + r.cost_judge, 8)  # gated axis = total
    assert r.cost_usd > r.cost_worker                          # judge is folded in
    # run-level aggregate keeps the breakdown visible and totals correctly.
    s = run.summary()
    assert s["cost_usd"] == round(s["cost_worker"] + s["cost_judge"], 6)


def test_passing_task_scores_one(tmp_path):
    spec = _write_spec(
        tmp_path,
        "sim-pass",
        _sim_spec(
            "sim-pass",
            {
                "outcome": "completed",
                "applied": False,
                "no_changes": True,
                "tests": "ignore",
                "output_contains": ["stub", "investigated"],
            },
        ),
    )
    run = run_eval([spec], sandbox_src=_sandbox(tmp_path), worker=StubWorker(), out_dir=tmp_path / "out")
    (r,) = run.results
    assert r.quality_deterministic == 1.0
    assert r.asserts_failed == 0
    assert r.asserts_passed >= 4  # outcome, applied, no_changes, 2x output_contains


# -- a deliberately-broken expectation fails cleanly ------------------------

def test_broken_expectation_fails_cleanly(tmp_path):
    spec = _write_spec(
        tmp_path,
        "sim-broken",
        _sim_spec(
            "sim-broken",
            {
                "outcome": "completed",             # passes
                "no_changes": True,                 # passes
                "tests": "ignore",
                "output_contains": ["THIS_STRING_NEVER_APPEARS"],  # fails
            },
        ),
    )
    # Must not raise — a failing assert is a datapoint, not a crash.
    run = run_eval([spec], sandbox_src=_sandbox(tmp_path), worker=StubWorker(), out_dir=tmp_path / "out")
    (r,) = run.results
    assert r.asserts_failed == 1
    assert r.asserts_passed == 2
    assert 0.0 < r.quality_deterministic < 1.0


# -- telemetry is captured per task -----------------------------------------

def test_telemetry_captured_per_task(tmp_path):
    spec = _write_spec(tmp_path, "sim-tele", _sim_spec("sim-tele", {"outcome": "completed"}))
    run = run_eval([spec], sandbox_src=_sandbox(tmp_path), worker=StubWorker(), out_dir=tmp_path / "out")
    (r,) = run.results
    assert isinstance(r, EvalResult)
    assert r.cost_usd > 0
    assert r.total_tokens > 0          # stub step: 1200+340+800+200
    assert r.latency_ms >= 0


# -- go burn: changed files + gate applied ----------------------------------

def test_burn_go_detects_applied_change(tmp_path):
    spec = _write_spec(
        tmp_path,
        "burn-go",
        _burn_spec(
            "burn-go",
            roles.GO,
            {
                "outcome": "completed",
                "applied": True,
                "decision": roles.GO,
                "files_touched": [STUB_BURN_FILE],
                "tests": "ignore",
            },
        ),
    )
    run = run_eval([spec], sandbox_src=_sandbox(tmp_path), worker=StubWorker(), out_dir=tmp_path / "out")
    (r,) = run.results
    assert r.quality_deterministic == 1.0


# -- no-go burn: blocked, target unchanged ----------------------------------

def test_burn_nogo_blocks_and_leaves_target_clean(tmp_path):
    spec = _write_spec(
        tmp_path,
        "burn-nogo",
        _burn_spec(
            "burn-nogo",
            roles.NO_GO,
            {
                "outcome": "blocked",
                "applied": False,
                "decision": roles.NO_GO,
                "no_changes": True,
                "tests": "ignore",
            },
        ),
    )
    run = run_eval([spec], sandbox_src=_sandbox(tmp_path), worker=StubWorker(), out_dir=tmp_path / "out")
    (r,) = run.results
    assert r.quality_deterministic == 1.0


# -- run-level: JSONL file (one per run) + aggregate -------------------------

def test_run_emits_one_jsonl_with_aggregate(tmp_path):
    good = _write_spec(tmp_path, "g", _sim_spec("g", {"outcome": "completed", "no_changes": True}))
    bad = _write_spec(tmp_path, "b", _sim_spec("b", {"output_contains": ["NOPE"]}))
    run = run_eval([good, bad], sandbox_src=_sandbox(tmp_path), worker=StubWorker(), out_dir=tmp_path / "out")

    lines = [json.loads(x) for x in run.path.read_text().splitlines()]
    assert len(lines) == 2                       # one line per task
    assert {l["task_id"] for l in lines} == {"g", "b"}
    assert all("checks" in l for l in lines)     # per-assert breakdown is emitted

    s = run.summary()
    assert s["tasks"] == 2
    assert s["tasks_fully_passed"] == 1
    assert 0.0 < s["mean_quality_deterministic"] < 1.0
    assert s["total_tokens"] > 0
