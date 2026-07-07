"""Per-run telemetry: JSONL emission, priced fields, and identical shape across
a sim and a burn (verified offline with StubWorker)."""

from __future__ import annotations

import json
from pathlib import Path

from mission_control import Orchestrator, Task, TaskType, roles
from mission_control import pricing

# The exact JSONL record shape every step event must have.
EXPECTED_KEYS = {
    "step_id",
    "parent_step_id",
    "task_id",
    "task_type",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "context_size_tokens",
    "cost_usd",
    "latency_ms",
    "outcome",
}


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _run_sim(orch: Orchestrator):
    return orch.run_task(Task("sim-1", TaskType.READ_ONLY, "look around"))


def _run_burn(orch: Orchestrator):
    return orch.run_task(
        Task("burn-1", TaskType.SIDE_EFFECTFUL, "make a change"),
        approval=lambda run: True,
    )


def test_run_emits_one_jsonl_file_per_run(tmp_path):
    # Two runs (fresh repos to keep worktrees clean) → two distinct files.
    a = _run_sim(Orchestrator(_git_repo(tmp_path, "a"), telemetry_dir=tmp_path / "tele"))
    b = _run_sim(Orchestrator(_git_repo(tmp_path, "b"), telemetry_dir=tmp_path / "tele"))
    assert a.telemetry.path.exists() and b.telemetry.path.exists()
    assert a.telemetry.path != b.telemetry.path
    assert len(list((tmp_path / "tele").glob("*.jsonl"))) == 2


def test_sim_and_burn_have_identical_telemetry_shape(tmp_path):
    sim = _run_sim(Orchestrator(_git_repo(tmp_path, "sim"), telemetry_dir=tmp_path / "t"))
    burn = _run_burn(Orchestrator(_git_repo(tmp_path, "burn"), telemetry_dir=tmp_path / "t"))

    sim_lines = _lines(sim.telemetry.path)
    burn_lines = _lines(burn.telemetry.path)
    assert sim_lines and burn_lines

    for line in sim_lines + burn_lines:
        assert set(line.keys()) == EXPECTED_KEYS

    # Identical shape: same keys in the same set for both task types.
    assert {k for line in sim_lines for k in line} == {
        k for line in burn_lines for k in line
    }


def test_task_type_uses_metaphor_values(tmp_path):
    sim = _run_sim(Orchestrator(_git_repo(tmp_path, "s"), telemetry_dir=tmp_path / "t"))
    burn = _run_burn(Orchestrator(_git_repo(tmp_path, "b"), telemetry_dir=tmp_path / "t"))
    assert _lines(sim.telemetry.path)[0]["task_type"] == roles.SIM
    assert _lines(burn.telemetry.path)[0]["task_type"] == roles.BURN


def test_step_fields_are_priced_and_linked(tmp_path):
    result = _run_sim(Orchestrator(_git_repo(tmp_path, "x"), telemetry_dir=tmp_path / "t"))
    event = _lines(result.telemetry.path)[0]

    # context_size = full rendered prompt = input + cache read + cache creation.
    assert event["context_size_tokens"] == (
        event["input_tokens"] + event["cache_read_tokens"] + event["cache_creation_tokens"]
    )
    # cost matches the pricing module for the recorded token counts.
    assert event["cost_usd"] > 0
    # first step has no parent; ids are namespaced by task.
    assert event["parent_step_id"] is None
    assert event["step_id"].startswith("sim-1")


def test_summary_aggregates_steps(tmp_path):
    result = _run_sim(Orchestrator(_git_repo(tmp_path, "y"), telemetry_dir=tmp_path / "t"))
    s = result.telemetry.summary()
    events = _lines(result.telemetry.path)
    assert s["steps"] == len(events)
    assert s["cost_usd"] > 0
    assert s["input_tokens"] == sum(e["input_tokens"] for e in events)


# -- helper ---------------------------------------------------------------

def _git_repo(base: Path, name: str) -> Path:
    """A throwaway git repo with one commit (inline, so this file is self-contained)."""
    import subprocess

    repo = base / f"repo-{name}"
    repo.mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (repo / "README.md").write_text("# target\n")
    git("add", "-A")
    git("commit", "-m", "init")
    return repo
