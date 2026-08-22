"""Regression: worker commits must not need an ambient git identity.

In the Fargate worker container git has no global/system identity, so `git commit`
(and the `--no-ff` merge commit) failed with "Author identity unknown" (exit 128).
worktree.commit_changes / merge_into_target now inject a Mission Control identity per
command; these tests reproduce the identity-less container and assert both succeed.
"""

import os
import subprocess
from pathlib import Path

from mission_control import worktree


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def test_commit_and_merge_work_without_ambient_git_identity(tmp_path, monkeypatch):
    # Simulate the worker container: no global or system git identity anywhere.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "-A")
    # Seed commit also needs an identity in this identity-less env; give it one explicitly.
    _git(repo, "-c", "user.name=Seed", "-c", "user.email=seed@x", "commit", "-m", "seed")

    wt = worktree.create_worktree(repo, "burn-test")
    (Path(wt.path) / "mctf.py").write_text("def slugify(x):\n    return x.lower()\n")

    # Before the fix this raised CalledProcessError (exit 128).
    assert worktree.commit_changes(wt, "apply task burn-test") is True
    worktree.merge_into_target(wt, "apply task burn-test")

    author = _git(repo, "log", "-1", "--format=%an <%ae>").strip()
    assert author == "Mission Control <bot@mission-control.local>"


def test_identity_is_overridable_via_env(monkeypatch):
    monkeypatch.setattr(worktree, "_AUTHOR_NAME", "Custom Bot")
    monkeypatch.setattr(worktree, "_AUTHOR_EMAIL", "custom@example.test")
    assert worktree._identity_args() == [
        "-c",
        "user.name=Custom Bot",
        "-c",
        "user.email=custom@example.test",
    ]
