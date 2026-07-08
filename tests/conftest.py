"""Shared fixtures: a throwaway git repo to act as the orchestrator's target."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def target_repo(tmp_path: Path) -> Path:
    """A fresh git repo with one commit on ``main`` — the target for worktrees."""
    repo = tmp_path / "target"
    repo.mkdir()
    _run(repo, "init", "-b", "main")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# target\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "init")
    return repo


class FakeJudge:
    """Offline stand-in for LlmJudge — canned score + priced (Opus) usage, no LLM."""

    model = "claude-opus-4-8"

    def score(self, *, task_prompt, worker_output, rubric):
        from mission_control.judge import JudgeResult
        from mission_control.telemetry import StepUsage

        return JudgeResult(
            score=0.8,
            rationale="fake judge",
            usage=StepUsage(
                model="claude-opus-4-8",
                input_tokens=100,
                output_tokens=50,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                latency_ms=10,
            ),
            per_criterion=[],
        )


@pytest.fixture
def fake_judge() -> FakeJudge:
    return FakeJudge()
