"""Rendering v2's aidlc-state.md from MC's plan state (pure, over the real catalog +
the vendored state template)."""

from __future__ import annotations

import pytest

from mission_control.aidlc_v2 import catalog as v2catalog
from mission_control.aidlc_v2 import state as v2state

ROOT = v2catalog.default_methodology_root()


@pytest.fixture(scope="module")
def catalog():
    return v2catalog.load_catalog(ROOT)


def test_render_marks_completed_stages_x_and_pending_blank(catalog):
    text = v2state.render_state_file(
        catalog, catalog_root=ROOT, mode="greenfield",
        completed_slugs={"requirements-analysis", "functional-design"})
    assert "- [x] requirements-analysis" in text
    assert "- [x] functional-design" in text
    assert "- [ ] code-generation" in text          # not completed
    # phase headings from the compiled catalog, faithful to the template format
    assert "### CONSTRUCTION PHASE" in text
    assert "### INITIALIZATION PHASE" in text
    # the checkbox legend from the vendored template is preserved
    assert "[x] completed" in text


def test_render_reflects_mode_and_is_deterministic(catalog):
    a = v2state.render_state_file(catalog, catalog_root=ROOT, mode="brownfield",
                                  completed_slugs=set())
    b = v2state.render_state_file(catalog, catalog_root=ROOT, mode="brownfield",
                                  completed_slugs=set())
    assert a == b                                     # no timestamps → clean diffs
    assert "Brownfield" in a                          # Project Type filled from mode
    # brownfield includes reverse-engineering; nothing is completed yet
    assert "- [ ] reverse-engineering" in a


def test_render_greenfield_omits_reverse_engineering(catalog):
    text = v2state.render_state_file(catalog, catalog_root=ROOT, mode="greenfield",
                                     completed_slugs=set())
    assert "reverse-engineering" not in text          # brownfield-only stage
