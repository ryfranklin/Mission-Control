"""Smoke test: the package imports. Real tests arrive with real logic."""

import mission_control


def test_package_imports():
    assert mission_control.__version__
