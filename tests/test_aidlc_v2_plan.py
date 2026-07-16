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


def test_plan_stages_are_intent_gathering_only(catalog):
    stages = v2plan.plan_stages(catalog, mode=MODE)
    assert stages
    # the walk covers initialization + ideation (intent); inception onward is produced
    # by CAPCOM at build, never walked (so a stage is never both a record and a unit).
    assert {s.phase for s in stages} <= {"initialization", "ideation"}
    walk = {s.slug for s in stages}
    build = {u.stage_slug for u in v2plan.build_units(catalog, mode=MODE)}
    assert walk.isdisjoint(build)                    # no stage duplicated across walk/build


def test_reverse_engineering_is_a_brownfield_build_unit(catalog):
    # RE moved from the walk into the producing pipeline: a build unit, brownfield-only.
    assert "reverse-engineering" not in {s.slug for s in v2plan.plan_stages(catalog, mode="brownfield")}
    bf = {u.stage_slug for u in v2plan.build_units(catalog, mode="brownfield")}
    gf = {u.stage_slug for u in v2plan.build_units(catalog, mode=MODE)}
    assert "reverse-engineering" in bf and "reverse-engineering" not in gf


# -- the work-list -----------------------------------------------------------


def test_units_are_the_producing_stages_inception_through_operation(catalog):
    units = v2plan.build_units(catalog, mode=MODE)
    # CAPCOM produces the whole chain: every applicable inception/construction/operation
    # stage is a build unit (initialization/ideation are the walk's intent, not units).
    expected = {s.slug for s in catalog
                if s.phase in ("inception", "construction", "operation")
                and v2catalog.applicable(s, mode=MODE)}
    # operation is deferred but still recorded → present in the set
    expected |= {s.slug for s in catalog if s.phase == "operation"}
    assert {u.stage_slug for u in units} == expected
    # inception producers are included so construction has real inputs
    assert {"requirements-analysis", "application-design", "units-generation"} \
        <= {u.stage_slug for u in units}


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


def test_code_generation_depends_on_its_full_producing_chain(catalog):
    by_slug = {u.stage_slug: u for u in v2plan.build_units(catalog, mode=MODE)}
    cg = by_slug["code-generation"]
    # units-generation is now a produced INCEPTION unit → it IS a real dependency (the
    # chain is complete; code-generation waits for its unit-of-work + designs).
    assert {"units-generation", "functional-design", "nfr-requirements", "nfr-design",
            "infrastructure-design"} <= set(cg.requires)
    # requires only reference other build units (no dangling ideation/initialization deps)
    unit_slugs = set(by_slug)
    assert set(cg.requires) <= unit_slugs


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


def _required_consumes(catalog, slug):
    return {c.artifact for c in next(s for s in catalog if s.slug == slug).consumes
            if c.required}


def test_missing_inputs_ignores_optional_consumes(catalog):
    """Optional inputs never count as missing — their absence must not block/regenerate."""
    slug = "code-generation"
    required = _required_consumes(catalog, slug)
    optional = {c.artifact for c in next(s for s in catalog if s.slug == slug).consumes
                if not c.required}
    assert optional                                      # this stage HAS optional inputs
    # nothing available at all → only the REQUIRED inputs are reported
    missing = v2plan.missing_inputs(catalog, slug, producer_done={}, producer_files={},
                                    on_disk=set())
    assert set(missing) == required
    assert optional.isdisjoint(missing)


def test_missing_inputs_layer1_producer_not_done(catalog):
    """A required input whose producer UNIT isn't done is missing (outcome-based)."""
    slug = "code-generation"
    # unit-of-work's producer is units-generation. Mark it NOT done → unit-of-work missing.
    missing = v2plan.missing_inputs(
        catalog, slug,
        producer_done={"units-generation": False, "requirements-analysis": True},
        producer_files={"requirements-analysis": {"requirements"}},
        on_disk=set())
    assert "unit-of-work" in missing                     # producer not done
    assert "requirements" not in missing                 # producer done + wrote it


def test_missing_inputs_layer2_producer_done_but_omitted_artifact(catalog):
    """Layer 2: a producer that is DONE but did not write the artifact → still missing
    (this is the case a filename-only check on the whole tree could miss)."""
    slug = "code-generation"
    # units-generation is done but its written files DON'T include unit-of-work.
    missing = v2plan.missing_inputs(
        catalog, slug,
        producer_done={"units-generation": True, "requirements-analysis": True},
        producer_files={"units-generation": {"unit-of-work-story-map"},   # omitted unit-of-work
                        "requirements-analysis": {"requirements"}},
        on_disk=set())
    assert "unit-of-work" in missing                     # done, but not written → missing
    assert "requirements" not in missing                 # done + written → present


def test_missing_inputs_present_when_producer_done_and_wrote_it(catalog):
    """No false 'missing' (the original gap): producer done + wrote the file → present,
    regardless of where else on disk it might or might not appear."""
    slug = "code-generation"
    req = _required_consumes(catalog, slug)
    missing = v2plan.missing_inputs(
        catalog, slug,
        producer_done={a_producer: True for a_producer in
                       {s.slug for s in catalog for art in s.produces if art in req}},
        producer_files={s.slug: set(s.produces) for s in catalog},  # each producer wrote all
        on_disk=set())
    assert missing == []                                 # everything accounted for → no regen


def test_missing_inputs_disk_fallback_for_no_producer_artifact(catalog, tmp_path):
    """An artifact with NO producer unit (walk-produced/external) falls back to disk."""
    # requirements-analysis (brownfield) requires business-overview etc.; use a stage whose
    # required input has no producer unit here → only the on-disk set decides it.
    slug = "code-generation"
    req = _required_consumes(catalog, slug)
    # no producer units at all → every required input decided by on_disk
    on_disk = set(req)                                   # all present on disk
    assert v2plan.missing_inputs(catalog, slug, producer_done={}, producer_files={},
                                 on_disk=on_disk) == []
    # drop one from disk → it's the only thing reported missing
    one = sorted(req)[0]
    partial = v2plan.missing_inputs(catalog, slug, producer_done={}, producer_files={},
                                    on_disk=set(req) - {one})
    assert set(partial) == {one}


def test_readiness_flags_malformed_units(catalog):
    plan_slugs = {s.slug for s in v2plan.plan_stages(catalog, mode=MODE)}
    bad = [_FakeUnit("", "construction", "nonsense", [])]  # no slug, bad task_type
    r = v2plan.readiness(catalog, mode=MODE, completed_slugs=plan_slugs, units=bad)
    assert not is_ready(r)
    assert any(c.key == "units" and not c.met for c in r)
