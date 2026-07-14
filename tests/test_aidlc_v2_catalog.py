"""Catalog adapter over the REAL vendored AI-DLC v2 methodology.

These run against the vendored files (not fixtures) so they double as a check that the
vendored tree is intact and that the MC-owned classification stays honest.
"""

from __future__ import annotations

import pytest

from mission_control.aidlc_v2 import catalog
from mission_control.aidlc_v2.catalog import (
    MODE_BROWNFIELD,
    MODE_GREENFIELD,
    PHASE_ORDER,
    Consumed,
    StageSpec,
    applicable,
    classify,
    load_catalog,
    parse_stage,
)

ROOT = catalog.default_methodology_root()


@pytest.fixture(scope="module")
def stages() -> list[StageSpec]:
    return load_catalog(ROOT)


@pytest.fixture(scope="module")
def by_slug(stages) -> dict[str, StageSpec]:
    return {s.slug: s for s in stages}


# -- coverage: 32 stages across the 5 phases ---------------------------------


def test_loads_all_32_stages(stages):
    assert len(stages) == 32


def test_five_phases_present(stages):
    phases = {s.phase for s in stages}
    assert phases == set(PHASE_ORDER)
    # every stage lands in a known phase
    assert all(s.phase in PHASE_ORDER for s in stages)


def test_slugs_unique(stages):
    slugs = [s.slug for s in stages]
    assert len(slugs) == len(set(slugs))


# -- parsing fidelity --------------------------------------------------------


def test_requirements_analysis_frontmatter(by_slug):
    ra = by_slug["requirements-analysis"]
    assert ra.phase == "inception"
    assert ra.title == "Requirements Analysis"
    assert ra.lead_agent == "aidlc-product-agent"
    assert ra.reviewer == "aidlc-product-lead-agent"
    assert ra.execution == "ALWAYS"
    assert "requirements" in ra.produces


def test_consumes_parsed_as_nested_objects(by_slug):
    """The likely snag: consumes: is a list of {artifact, required, conditional_on}
    objects, NOT bare strings like produces:."""
    ra = by_slug["requirements-analysis"]
    assert ra.consumes  # non-empty
    assert all(isinstance(c, Consumed) for c in ra.consumes)
    by_artifact = {c.artifact: c for c in ra.consumes}
    # a brownfield-conditional consume from the real file
    assert by_artifact["architecture"].conditional_on == "brownfield"
    assert by_artifact["architecture"].required is False
    # produces stays a flat list of strings
    assert all(isinstance(p, str) for p in ra.produces)


def test_required_consume_flag(by_slug):
    cg = by_slug["code-generation"]
    by_artifact = {c.artifact: c for c in cg.consumes}
    assert by_artifact["unit-of-work"].required is True
    assert by_artifact["requirements"].required is True


def test_parse_stage_directly_matches_load(by_slug):
    p = ROOT / "aidlc-common" / "stages" / "inception" / "requirements-analysis.md"
    direct = parse_stage(p)
    assert direct.slug == by_slug["requirements-analysis"].slug
    assert direct.consumes == by_slug["requirements-analysis"].consumes


# -- topological order -------------------------------------------------------


def test_topological_order_respects_requires_stage(stages):
    position = {s.slug: i for i, s in enumerate(stages)}
    for s in stages:
        for req in s.requires_stage:
            if req in position:  # ignore any dangling reference
                assert position[req] < position[s.slug], (
                    f"{s.slug} placed before its dependency {req}"
                )


def test_order_falls_back_to_phase_order(stages):
    """With no cross-dependency forcing otherwise, phases stay in methodology order:
    the first stage of each phase appears in PHASE_ORDER sequence."""
    first_seen = {}
    for i, s in enumerate(stages):
        first_seen.setdefault(s.phase, i)
    seq = [first_seen[p] for p in PHASE_ORDER]
    assert seq == sorted(seq)


# -- classification (the MC-owned table) -------------------------------------


def test_classification_matches_table(by_slug):
    assert by_slug["code-generation"].kind == "burn"
    assert by_slug["requirements-analysis"].kind == "plan"
    assert by_slug["functional-design"].kind == "sim"
    assert by_slug["nfr-requirements"].kind == "sim"
    assert by_slug["nfr-design"].kind == "sim"
    for slug in ("build-and-test", "ci-pipeline", "infrastructure-design"):
        assert by_slug[slug].kind == "burn"


def test_operation_stages_deferred(stages):
    op = [s for s in stages if s.phase == "operation"]
    assert op  # the phase exists
    assert all(s.deferred and s.kind == "burn" for s in op)


def test_non_operation_not_deferred(stages):
    assert all(not s.deferred for s in stages if s.phase != "operation")


def test_plan_phases_are_plan(stages):
    for s in stages:
        if s.phase in ("initialization", "ideation", "inception"):
            assert s.kind == "plan"


def test_classify_is_pure_lookup():
    assert classify("construction", "code-generation") == ("burn", False)
    assert classify("operation", "deployment-execution") == ("burn", True)
    assert classify("inception", "requirements-analysis") == ("plan", False)
    # unknown phase → safe default
    assert classify("mystery", "whatever") == ("plan", False)


# -- applicability -----------------------------------------------------------


def test_reverse_engineering_is_brownfield_only(by_slug):
    re_stage = by_slug["reverse-engineering"]
    assert applicable(re_stage, mode=MODE_BROWNFIELD, scope=None)
    assert not applicable(re_stage, mode=MODE_GREENFIELD, scope=None)


def test_deferred_skipped_unless_enabled(by_slug):
    dep = by_slug["deployment-execution"]
    assert not applicable(dep, mode=MODE_GREENFIELD, scope=None)
    assert applicable(dep, mode=MODE_GREENFIELD, scope=None, enable_deferred=True)


def test_scope_filter(by_slug):
    ug = by_slug["units-generation"]
    assert "poc" not in ug.scopes  # per the real file
    assert not applicable(ug, mode=MODE_GREENFIELD, scope="poc")
    assert applicable(ug, mode=MODE_GREENFIELD, scope="feature")


def test_always_stage_applicable_by_default(by_slug):
    ra = by_slug["requirements-analysis"]
    assert applicable(ra, mode=MODE_GREENFIELD, scope=None)
