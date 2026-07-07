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
