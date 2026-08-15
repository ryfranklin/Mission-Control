"""Verification node + module (offline: StubWorker, real subprocesses, a FakeJudge).

Covers the pure detection/run/status helpers and the node wired into the run graph:
a green build gates normally, a red build auto-blocks WITHOUT halting a human, an
unverified build still requires the human gate, and plan acceptance criteria are
scored by the judge (priced), advisory by default and enforcing on request.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from mission_control import StubWorker, Task, TaskType, roles, verify
from mission_control.graph import (
    _Deps,
    _gate,
    awaiting_gate,
    build_run_graph,
    resume_gate,
    run_via_graph,
)
from mission_control.worktree import list_worktrees


# -- helpers ----------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def _repo_with_verify(tmp_path: Path, command: str | None) -> Path:
    """A fresh git repo on main. When ``command`` is given, commit a
    ``.mission-control/verify.yml`` declaring one check running that shell command."""
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("# t\n")
    if command is not None:
        cfg = repo / ".mission-control"
        cfg.mkdir()
        (cfg / "verify.yml").write_text(
            f'checks:\n  - name: unit\n    command: "{command}"\n'
        )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def _burn(prompt="change", acceptance=()):
    return Task("burn-1", TaskType.SIDE_EFFECTFUL, prompt, acceptance_criteria=tuple(acceptance))


# -- pure module ------------------------------------------------------------

def test_detect_override_wins(tmp_path):
    root = tmp_path / "r"
    (root / ".mission-control").mkdir(parents=True)
    (root / ".mission-control" / "verify.yml").write_text(
        'checks:\n  - name: t\n    command: pytest -q\n    setup:\n      - pip install -e .\n')
    (root / "package.json").write_text('{"scripts": {"test": "jest"}}')  # would auto-detect
    checks = verify.detect_checks(root)
    assert [c.name for c in checks] == ["t"]  # override wins over auto-detection
    assert checks[0].command == ("pytest", "-q")
    assert checks[0].setup == (("pip", "install", "-e", "."),)


def test_detect_autodetects_common_stacks(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "go.mod").write_text("module x\n")
    names = {c.name for c in verify.detect_checks(root)}
    assert "pytest" in names and "go-test" in names


def test_detect_none_when_no_signals(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    assert verify.detect_checks(root) == []


def test_run_deterministic_pass_fail_unverified(tmp_path):
    root = tmp_path / "r"
    (root / ".mission-control").mkdir(parents=True)

    def write(cmd):
        (root / ".mission-control" / "verify.yml").write_text(
            f'checks:\n  - name: unit\n    command: "{cmd}"\n')

    write("sh -c 'exit 0'")
    assert verify.run_deterministic(root, verify.VerifyPolicy())[0] == verify.VERIFY_PASSED
    write("sh -c 'exit 3'")
    status, report = verify.run_deterministic(root, verify.VerifyPolicy())
    assert status == verify.VERIFY_FAILED and report[0]["exit_code"] == 3

    empty = tmp_path / "e"
    empty.mkdir()
    assert verify.run_deterministic(empty, verify.VerifyPolicy())[0] == verify.VERIFY_UNVERIFIED


def test_overall_status_matrix():
    P = verify.VerifyPolicy()
    assert verify.overall_status(verify.VERIFY_FAILED, None, P) == verify.VERIFY_FAILED
    assert verify.overall_status(verify.VERIFY_UNVERIFIED, None, P) == verify.VERIFY_UNVERIFIED
    assert verify.overall_status(verify.VERIFY_PASSED, None, P) == verify.VERIFY_PASSED
    # acceptance advisory (not enforced) never fails the overall status
    low = {"score": 0.1}
    assert verify.overall_status(verify.VERIFY_PASSED, low, P) == verify.VERIFY_PASSED
    # enforced + below threshold → fail
    E = verify.VerifyPolicy(acceptance_enforced=True, acceptance_threshold=0.7)
    assert verify.overall_status(verify.VERIFY_PASSED, low, E) == verify.VERIFY_FAILED
    assert verify.overall_status(verify.VERIFY_PASSED, {"score": 0.9}, E) == verify.VERIFY_PASSED


def test_acceptance_rubric_shape():
    r = verify.acceptance_rubric(["creates an item", "  ", "returns 404 when missing"])
    assert r == [
        {"criterion": "creates an item", "weight": 1},
        {"criterion": "returns 404 when missing", "weight": 1},
    ]


# -- node wired into the run graph ------------------------------------------

def test_passing_verify_then_go_applies(tmp_path):
    repo = _repo_with_verify(tmp_path, "sh -c 'exit 0'")
    graph = build_run_graph(repo, worker=StubWorker())
    run_via_graph(graph, _burn(), thread_id="burn-1")
    assert awaiting_gate(graph, "burn-1")  # green build still asks the human
    final = resume_gate(graph, "burn-1", roles.GO)
    assert final["verify_status"] == verify.VERIFY_PASSED
    assert final["applied"] is True
    assert len(list_worktrees(repo)) == 1  # only the main tree; the task worktree was reaped


def test_failing_verify_auto_blocks_without_a_human(tmp_path):
    repo = _repo_with_verify(tmp_path, "sh -c 'exit 1'")
    graph = build_run_graph(repo, worker=StubWorker())
    final = run_via_graph(graph, _burn(), thread_id="burn-1")  # runs to END, no interrupt
    assert not awaiting_gate(graph, "burn-1")
    assert final["verify_status"] == verify.VERIFY_FAILED
    assert final["decision"] == roles.NO_GO
    assert final.get("applied") is not True
    assert len(list_worktrees(repo)) == 1  # blocked build left no leaked worktree


def test_unverified_still_requires_the_gate(tmp_path):
    repo = _repo_with_verify(tmp_path, None)  # no checks declared
    graph = build_run_graph(repo, worker=StubWorker())
    run_via_graph(graph, _burn(), thread_id="burn-1")
    assert awaiting_gate(graph, "burn-1")  # unverified → human still decides (default policy)
    final = resume_gate(graph, "burn-1", roles.GO)
    assert final["verify_status"] == verify.VERIFY_UNVERIFIED
    assert final["applied"] is True


def test_acceptance_is_scored_and_priced(tmp_path, fake_judge):
    repo = _repo_with_verify(tmp_path, "sh -c 'exit 0'")
    graph = build_run_graph(repo, worker=StubWorker(), judge_factory=lambda: fake_judge)
    run_via_graph(graph, _burn(acceptance=["supports CRUD on /items"]), thread_id="burn-1")
    final = resume_gate(graph, "burn-1", roles.GO)
    acc = final["verify_report"]["acceptance"]
    assert acc["score"] == 0.8 and acc["enforced"] is False
    # the judge's own token usage is folded into the run's priced steps
    assert any(s.get("model") == "claude-opus-4-8" for s in final["steps"])


def test_enforced_acceptance_below_bar_blocks(tmp_path, fake_judge):
    repo = _repo_with_verify(tmp_path, None)  # no deterministic checks
    policy = verify.VerifyPolicy(acceptance_enforced=True, acceptance_threshold=0.9)
    graph = build_run_graph(repo, worker=StubWorker(),
                            verify_policy=policy, judge_factory=lambda: fake_judge)
    final = run_via_graph(graph, _burn(acceptance=["must be perfect"]), thread_id="burn-1")
    assert final["verify_status"] == verify.VERIFY_FAILED  # 0.8 < 0.9, enforced
    assert final["decision"] == roles.NO_GO


def test_gate_hard_fail_needs_no_worktree_or_interrupt():
    # A unit check of the fail-closed short-circuit: a FAILED verdict → NO_GO directly,
    # never reaching interrupt() (so it's safe to call the node outside a running graph).
    deps = _Deps(target_repo=None, worker=StubWorker())
    out = _gate(deps, {"task_type": roles.BURN, "verify_status": verify.VERIFY_FAILED})
    assert out == {"decision": roles.NO_GO}
