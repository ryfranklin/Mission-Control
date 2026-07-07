"""Offline tests for the SDK worker's construction invariants.

These don't make a live call — they assert the load-bearing contract:
fully-explicit context (``setting_sources=[]``), a configurable cheap-tier
model, isolation via ``cwd``, and read-only enforcement for ``sim`` tasks.
The end-to-end live run lives in ``mission_control.demo_sdk``.
"""

from __future__ import annotations

from pathlib import Path

from mission_control import SdkWorker
from mission_control.sdk_worker import DEFAULT_MODEL, _MUTATING_TOOLS
from mission_control.tasks import Task, TaskType


def _task(task_type: TaskType) -> Task:
    return Task(task_id="t1", task_type=task_type, prompt="do the thing")


def test_default_model_is_cheap_tier():
    assert SdkWorker().model == DEFAULT_MODEL == "claude-haiku-4-5"


def test_model_is_configurable():
    assert SdkWorker(model="claude-sonnet-4-6").model == "claude-sonnet-4-6"


def test_context_is_fully_explicit_no_filesystem_settings():
    # CRITICAL: setting_sources=[] means NO CLAUDE.md / settings are auto-loaded.
    opts = SdkWorker()._options(_task(TaskType.READ_ONLY), Path("/tmp/wt"))
    assert opts.setting_sources == []


def test_worker_runs_in_the_provided_worktree():
    opts = SdkWorker()._options(_task(TaskType.READ_ONLY), Path("/tmp/wt"))
    assert opts.cwd == "/tmp/wt"
    assert opts.model == DEFAULT_MODEL


def test_read_only_task_hard_blocks_mutating_tools():
    opts = SdkWorker()._options(_task(TaskType.READ_ONLY), Path("/tmp/wt"))
    assert opts.disallowed_tools == list(_MUTATING_TOOLS)
    assert "Write" in opts.disallowed_tools and "Bash" in opts.disallowed_tools


def test_side_effectful_task_may_mutate():
    opts = SdkWorker()._options(_task(TaskType.SIDE_EFFECTFUL), Path("/tmp/wt"))
    assert opts.disallowed_tools == []


def test_system_prompt_reflects_task_type():
    ro = SdkWorker()._options(_task(TaskType.READ_ONLY), Path("/tmp/wt")).system_prompt
    se = SdkWorker()._options(_task(TaskType.SIDE_EFFECTFUL), Path("/tmp/wt")).system_prompt
    assert "not modify" in ro.lower() or "do not modify" in ro.lower()
    assert "edit files" in se.lower()
