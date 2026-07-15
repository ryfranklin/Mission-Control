"""A v2 stage-unit worker steers from that ONE stage's definition + its lead-agent
knowledge (not the whole methodology, not the generic prompt), with the sim/burn tool
block preserved, setting_sources=[], and an explicit ban on v2's .ts tools/hooks and
subagents."""

from __future__ import annotations

import pytest

from mission_control.aidlc import FLAVOR_AIDLC_V2, probe
from mission_control.aidlc_v2 import catalog as v2catalog
from mission_control.aidlc_v2 import install as install_v2
from mission_control.aidlc_v2 import steering as v2steering
from mission_control.sdk_worker import SdkWorker, _MUTATING_TOOLS, _resolve_system_prompt
from mission_control.tasks import Task, TaskType


@pytest.fixture(scope="module")
def catalog():
    return v2catalog.load_catalog(v2catalog.default_methodology_root())


def _stage(catalog, slug):
    return next(s for s in catalog if s.slug == slug)


# -- compose_stage_prompt over the real vendored files ---------------------

def test_prompt_embeds_stage_body_and_lead_agent_knowledge(catalog):
    root = v2catalog.default_methodology_root()
    stage = _stage(catalog, "functional-design")   # a sim design stage
    assert stage.kind == "sim" and stage.lead_agent == "aidlc-architect-agent"

    prompt = v2steering.compose_stage_prompt(stage, root)

    # the stage's own protocol body is embedded verbatim
    assert "### Stage protocol" in prompt
    assert v2steering._read_body(stage.path) in prompt

    # the lead agent's definition + EVERY one of its knowledge files are embedded
    assert "### Lead agent: aidlc-architect-agent" in prompt
    assert "### Agent knowledge (aidlc-architect-agent)" in prompt
    kfiles = sorted((root / "knowledge" / "aidlc-architect-agent").glob("*.md"))
    assert kfiles  # sanity: the agent has knowledge
    assert prompt.count("--- knowledge/aidlc-architect-agent/") == len(kfiles)
    for kf in kfiles:
        assert v2steering._read_body(kf) in prompt


def test_prompt_is_one_stage_not_the_whole_methodology(catalog):
    root = v2catalog.default_methodology_root()
    prompt = v2steering.compose_stage_prompt(_stage(catalog, "functional-design"), root)
    # exactly one stage protocol section; a different stage's body is NOT dumped in
    assert prompt.count("### Stage protocol") == 1
    other_body = v2steering._read_body(_stage(catalog, "code-generation").path)
    assert other_body not in prompt


def test_prompt_names_produces_and_consumes(catalog):
    root = v2catalog.default_methodology_root()
    stage = _stage(catalog, "code-generation")      # a burn code stage
    prompt = v2steering.compose_stage_prompt(stage, root)
    for artifact in stage.produces:
        assert artifact in prompt
    assert "Read ONLY these input artifacts" in prompt


def test_burn_stage_instructs_writing_real_code_not_only_docs(catalog):
    """Regression: a burn (code) stage's PRIMARY deliverable is source in the working
    directory — not just its `produces:` design docs under aidlc-docs/. Without this the
    code-generation stage produced a plan.md and no code."""
    root = v2catalog.default_methodology_root()
    stage = _stage(catalog, "code-generation")
    assert stage.kind == "burn"
    prompt = v2steering.compose_stage_prompt(stage, root)
    assert "CODE/CHANGE stage" in prompt
    assert "working directory" in prompt
    # it must NOT be caged into docs-only output (the bug we fixed)
    assert "produce nothing else" not in prompt
    assert "confine your output to `aidlc-docs/`" in prompt
    # ...and it must forbid secret-shaped values (the egress guard blocks them otherwise)
    assert "NEVER write real or realistic secrets" in prompt
    assert "placeholders" in prompt


def test_design_stage_writes_docs_but_not_source(catalog):
    """A design/doc stage (ungated) is told to WRITE its artifacts under aidlc-docs/ —
    it is a producer, not a read-only validator — but must not touch application source
    (that's the code stages' gated job)."""
    root = v2catalog.default_methodology_root()
    stage = _stage(catalog, "functional-design")
    assert not v2catalog.gates(stage.slug)                   # ungated design stage
    prompt = v2steering.compose_stage_prompt(stage, root)
    assert "DESIGN/DOC stage" in prompt
    assert "WRITE your artifacts" in prompt
    assert "Actually create the files" in prompt
    assert "do not modify application SOURCE" in prompt.lower() \
        or "Do NOT modify application SOURCE" in prompt
    assert "CODE/CHANGE stage" not in prompt


def test_prompt_forbids_ts_tools_hooks_and_subagents(catalog):
    """The vendored bodies themselves reference .ts tools; MC's override must explicitly
    disable them (and v2 subagents), collapsing the reviewer into the go/no-go gate."""
    root = v2catalog.default_methodology_root()
    prompt = v2steering.compose_stage_prompt(_stage(catalog, "code-generation"), root)
    assert "aidlc-*.ts" in prompt                        # explicitly named + forbidden
    assert "bun .claude/tools/*" in prompt
    assert "IGNORE every instruction" in prompt          # override the embedded material
    assert "Do NOT spawn subagents" in prompt
    assert "go/no-go gate" in prompt                     # reviewer collapses into the gate


# -- sdk_worker integration: stage steering + tool block + setting_sources --

def _worker_options(tmp_path, slug, task_type):
    install_v2(tmp_path)                        # target now carries AI-DLC v2
    steering = probe(tmp_path)
    assert steering is not None and steering.flavor == FLAVOR_AIDLC_V2
    task = Task(task_id="t", task_type=task_type, prompt="do the stage",
                stage_slug=slug)
    system_prompt = _resolve_system_prompt(task, steering)
    options = SdkWorker()._options(task, tmp_path, system_prompt)
    return system_prompt, options


def test_sim_stage_worker_blocks_mutation_and_keeps_setting_sources(tmp_path):
    prompt, options = _worker_options(tmp_path, "functional-design", TaskType.READ_ONLY)
    # steered from the stage (not the generic prompt)
    assert "AI-DLC v2 stage: Functional Design (functional-design)" in prompt
    assert "### Agent knowledge (aidlc-architect-agent)" in prompt
    # explicit context only — no filesystem settings / hooks auto-loaded
    assert options.setting_sources == []
    # a sim genuinely cannot write
    for tool in _MUTATING_TOOLS:
        assert tool in options.disallowed_tools


def test_burn_stage_worker_may_write(tmp_path):
    prompt, options = _worker_options(tmp_path, "code-generation", TaskType.SIDE_EFFECTFUL)
    assert "AI-DLC v2 stage: Code Generation (code-generation)" in prompt
    assert options.setting_sources == []
    assert options.disallowed_tools == []               # burn may write


def test_non_stage_run_uses_generic_prompt(tmp_path):
    """No stage_slug → the generic worker prompt, even with v2 installed."""
    install_v2(tmp_path)
    steering = probe(tmp_path)
    task = Task(task_id="t", task_type=TaskType.READ_ONLY, prompt="look around")
    prompt = _resolve_system_prompt(task, steering)
    assert "AI-DLC v2 stage:" not in prompt
    assert "autonomous engineering worker" in prompt     # the generic base
