"""AI-DLC steering: multi-location probe, false-positive guard, phase mapping,
and prompt composition."""

from __future__ import annotations

from pathlib import Path

from mission_control import Phase, Task, TaskType, aidlc, probe, task_type_for_phase
from mission_control.aidlc import (
    FLAVOR_AIDLC_V2,
    FLAVOR_CLAUDE,
    FLAVOR_GENERIC,
    FLAVOR_KIRO,
)
from mission_control.sdk_worker import SdkWorker, _system_prompt

# A realistic assembled steering file with the AI-DLC content signature.
SIGNED = (
    "# AI-DLC Core Workflow\n\n"
    "## INCEPTION\nClarify what and why before building.\n\n"
    "## CONSTRUCTION\nImplement behind review gates.\n"
)

# A generic CLAUDE.md that has nothing to do with the methodology.
DECOY = "# CLAUDE.md\n\nRun `pytest` before committing. Use 2-space indents.\n"


def _write(root: Path, rel: str, text: str = SIGNED) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# -- (a) dir-based install matches (self-identifying, no signature needed) ----

def test_dir_based_install_matches(tmp_path):
    # Kiro: dir-based flavor, core + a separate detail-rules dir.
    _write(
        tmp_path,
        ".kiro/steering/aws-aidlc-rules/core-workflow.md",
        "workflow steps without any phase words",  # intentionally unsigned
    )
    (tmp_path / ".kiro/aws-aidlc-rule-details").mkdir(parents=True)

    steering = probe(tmp_path)
    assert steering is not None
    assert steering.flavor == FLAVOR_KIRO
    assert steering.detail_rules_dir == tmp_path / ".kiro/aws-aidlc-rule-details"
    assert "workflow steps" in steering.core_rules_text


# -- (b) assembled AGENTS.md WITH signature matches --------------------------

def test_assembled_agents_md_with_signature_matches(tmp_path):
    _write(tmp_path, "AGENTS.md", SIGNED)  # no .aidlc-rule-details dir

    steering = probe(tmp_path)
    assert steering is not None
    assert steering.flavor == FLAVOR_GENERIC
    assert steering.detail_rules_dir is None  # bundled / no sibling detail dir
    assert "CONSTRUCTION" in steering.core_rules_text


# -- (c) decoy generic CLAUDE.md with NO signature must NOT match ------------

def test_decoy_claude_md_without_signature_does_not_match(tmp_path):
    _write(tmp_path, "CLAUDE.md", DECOY)
    assert probe(tmp_path) is None


def test_signature_guard_only_on_shared_filenames():
    assert aidlc.has_aidlc_signature(SIGNED) is True
    assert aidlc.has_aidlc_signature(DECOY) is False


def test_priority_first_hit_wins(tmp_path):
    # Both a generic AGENTS.md (priority 1) and a Claude CLAUDE.md (priority 2/3)
    # carry the signature; the generic one wins.
    _write(tmp_path, "AGENTS.md", SIGNED)
    _write(tmp_path, "CLAUDE.md", SIGNED)
    assert probe(tmp_path).flavor == FLAVOR_GENERIC


def test_claude_dir_variant_matches(tmp_path):
    _write(tmp_path, ".claude/CLAUDE.md", SIGNED)
    (tmp_path / ".aidlc-rule-details").mkdir()
    steering = probe(tmp_path)
    assert steering.flavor == FLAVOR_CLAUDE
    assert steering.detail_rules_dir == tmp_path / ".aidlc-rule-details"


def test_no_install_runs_plain(tmp_path):
    (tmp_path / "README.md").write_text("# just a repo\n")
    assert probe(tmp_path) is None


# -- phase → task-type mapping (INCEPTION→sim, CONSTRUCTION→burn) -------------

def test_phase_maps_to_task_type():
    assert task_type_for_phase(Phase.INCEPTION) is TaskType.READ_ONLY
    assert task_type_for_phase(Phase.CONSTRUCTION) is TaskType.SIDE_EFFECTFUL


# -- prompt composition ------------------------------------------------------

def test_steering_folds_into_system_prompt(tmp_path):
    _write(tmp_path, "AGENTS.md", SIGNED)
    steering = probe(tmp_path)
    task = Task("t", TaskType.READ_ONLY, "look around")
    prompt = _system_prompt(task, steering)
    assert "AI-DLC" in prompt
    assert "CONSTRUCTION" in prompt  # the target's own rules are embedded
    assert steering.flavor in prompt


def test_no_steering_leaves_base_prompt(tmp_path):
    task = Task("t", TaskType.READ_ONLY, "look around")
    prompt = _system_prompt(task, None)
    assert "AI-DLC RULES" not in prompt


def test_greenfield_opener_only_when_greenfield():
    assert aidlc.apply_invocation("build a service", greenfield=True) == (
        "Using AI-DLC, build a service"
    )
    assert aidlc.apply_invocation("fix a bug", greenfield=False) == "fix a bug"


# -- v2 detection (the directory-tree flavor) --------------------------------
#
# The v2 layout is a ``.aidlc/`` catalog root holding ``aidlc-common/stages/<phase>/
# *.md``. A single minimal stage file is enough to exercise the detection rule
# (independent of the real vendored tree — that path is covered in test_aidlc_v2_install).


def _write_v2_layout(root: Path) -> Path:
    """Lay down the minimal v2 catalog layout probe() keys on; return the catalog root."""
    catalog_root = root / ".aidlc"
    _write(
        catalog_root,
        "aidlc-common/stages/inception/requirements-analysis.md",
        "---\nslug: requirements-analysis\nphase: inception\n---\n# Requirements\n",
    )
    return catalog_root


def test_v2_layout_detected(tmp_path):
    catalog_root = _write_v2_layout(tmp_path)
    steering = probe(tmp_path)
    assert steering is not None
    assert steering.flavor == FLAVOR_AIDLC_V2
    # steering carries a resolvable catalog root M3 hands to load_catalog()
    assert steering.catalog_root == catalog_root
    assert steering.catalog_root.is_dir()


def test_v2_wins_over_legacy(tmp_path):
    """When both a legacy install AND a v2 tree are present, v2 wins."""
    _write(tmp_path, "AGENTS.md", SIGNED)        # a valid legacy (generic) hit
    _write(tmp_path, ".claude/CLAUDE.md", SIGNED)  # another legacy hit
    _write_v2_layout(tmp_path)
    steering = probe(tmp_path)
    assert steering.flavor == FLAVOR_AIDLC_V2


def test_legacy_flavor_unchanged_without_v2(tmp_path):
    """No v2 tree → the legacy probe order is untouched and carries no catalog_root."""
    _write(tmp_path, "AGENTS.md", SIGNED)
    steering = probe(tmp_path)
    assert steering.flavor == FLAVOR_GENERIC
    assert steering.catalog_root is None


def test_empty_v2_dir_does_not_match(tmp_path):
    """A ``.aidlc/`` with no ``aidlc-common/stages/<phase>/*.md`` is not a v2 hit —
    and with no legacy install either, probe() still returns None."""
    (tmp_path / ".aidlc" / "aidlc-common" / "stages").mkdir(parents=True)
    assert probe(tmp_path) is None
