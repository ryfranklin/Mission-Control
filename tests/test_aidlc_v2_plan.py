"""Pure derivation of an MC plan from the v2 catalog: the plan-stage walk, the
work-list (with sim/burn types + dependency order + deferred operation stages), and the
readiness gate — all over the REAL vendored catalog."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from mission_control.aidlc import is_ready
from mission_control.aidlc_v2 import catalog as v2catalog
from mission_control.aidlc_v2 import plan as v2plan
from mission_control.roles import BURN, SIM

MODE = "greenfield"


@pytest.fixture(scope="module")
def catalog():
    return v2catalog.load_catalog(v2catalog.default_methodology_root())


# -- the plan-stage walk -----------------------------------------------------


def test_plan_stages_are_the_plan_kind(catalog):
    stages = v2plan.plan_stages(catalog, mode=MODE)
    assert stages
    assert all(s.kind == "plan" for s in stages)
    # reverse-engineering is a brownfield-only plan stage → absent for greenfield.
    assert "reverse-engineering" not in {s.slug for s in stages}


def test_plan_stages_include_reverse_engineering_for_brownfield(catalog):
    stages = v2plan.plan_stages(catalog, mode="brownfield")
    assert "reverse-engineering" in {s.slug for s in stages}


# -- the work-list -----------------------------------------------------------


def test_units_are_the_applicable_non_plan_stages(catalog):
    units = v2plan.build_units(catalog, mode=MODE)
    # every non-plan (sim/burn) stage becomes exactly one unit
    expected = {s.slug for s in catalog if s.kind in ("sim", "burn")}
    assert {u.stage_slug for u in units} == expected
    assert len(units) == len(expected)


def test_units_are_writable_and_gate_only_code_stages(catalog):
    by_slug = {u.stage_slug: u for u in v2plan.build_units(catalog, mode=MODE)}
    # Every producing stage WRITES its artifacts → all units are side-effectful (BURN).
    assert all(u.task_type == BURN for u in by_slug.values())
    # Only code-writing stages halt for a human GO.
    for slug in ("code-generation", "build-and-test", "ci-pipeline"):
        assert by_slug[slug].gated is True
    # Design/doc stages write + auto-apply (ungated) — no human gate per stage.
    for slug in ("functional-design", "nfr-requirements", "nfr-design",
                 "infrastructure-design"):
        assert by_slug[slug].gated is False
        assert by_slug[slug].phase == "construction"


def test_units_are_in_dependency_valid_order(catalog):
    units = v2plan.build_units(catalog, mode=MODE)
    position = {u.stage_slug: i for i, u in enumerate(units)}
    for u in units:
        for req in u.requires:
            assert position[req] < position[u.stage_slug], (
                f"{u.stage_slug} ordered before its dependency {req}"
            )


def test_requires_drops_plan_stage_dependencies(catalog):
    by_slug = {u.stage_slug: u for u in v2plan.build_units(catalog, mode=MODE)}
    cg = by_slug["code-generation"]
    # units-generation is a plan stage → not a unit → dropped from requires
    assert "units-generation" not in cg.requires
    # its real design/infra prerequisites survive
    assert {"functional-design", "nfr-requirements", "nfr-design",
            "infrastructure-design"} <= set(cg.requires)


def test_operation_stages_recorded_as_deferred(catalog):
    units = v2plan.build_units(catalog, mode=MODE)
    op = [u for u in units if u.phase == "operation"]
    assert op                                   # they ARE recorded
    assert all(u.deferred for u in op)          # ...but flagged deferred
    assert all(u.task_type == BURN for u in op)
    # construction units are not deferred
    assert all(not u.deferred for u in units if u.phase == "construction")


# -- the readiness gate (reuses the shared machinery) ------------------------


@dataclass
class _FakeUnit:
    stage_slug: str
    phase: str
    task_type: str
    depends_on: list


def test_readiness_needs_all_plan_stages_and_a_worklist(catalog):
    plan_slugs = [s.slug for s in v2plan.plan_stages(catalog, mode=MODE)]
    good_units = [_FakeUnit("code-generation", "construction", BURN, [])]

    # nothing done yet → not ready
    r0 = v2plan.readiness(catalog, mode=MODE, completed_slugs=set(), units=[])
    assert not is_ready(r0)

    # all plan stages done but no units → still not ready
    r1 = v2plan.readiness(catalog, mode=MODE, completed_slugs=set(plan_slugs), units=[])
    assert not is_ready(r1)
    assert any(c.key == "units" and not c.met for c in r1)

    # all plan stages done + a well-formed unit → ready
    r2 = v2plan.readiness(catalog, mode=MODE, completed_slugs=set(plan_slugs),
                          units=good_units)
    assert is_ready(r2)


def test_readiness_flags_malformed_units(catalog):
    plan_slugs = {s.slug for s in v2plan.plan_stages(catalog, mode=MODE)}
    bad = [_FakeUnit("", "construction", "nonsense", [])]  # no slug, bad task_type
    r = v2plan.readiness(catalog, mode=MODE, completed_slugs=plan_slugs, units=bad)
    assert not is_ready(r)
    assert any(c.key == "units" and not c.met for c in r)
