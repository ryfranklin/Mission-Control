"""Install the vendored AI-DLC v2 methodology into a target repo: idempotency, the
canonical ``.aidlc/`` layout, staging (so content_guard sees it), and that probe() +
load_catalog() can consume the result."""

from __future__ import annotations

import subprocess

from mission_control import aidlc, content_guard
from mission_control.aidlc import FLAVOR_AIDLC_V2
from mission_control.aidlc_v2 import install, install_dir
from mission_control.aidlc_v2.catalog import load_catalog


def _staged(repo) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [f for f in out.splitlines() if f.strip()]


def test_install_writes_canonical_layout(target_repo):
    wrote = install(target_repo)
    assert wrote is True
    root = install_dir(target_repo)
    assert root == target_repo / ".aidlc"
    # the whole methodology maps under .aidlc/
    assert (root / "aidlc-common" / "stages").is_dir()
    assert (root / "agents").is_dir()
    assert (root / "knowledge").is_dir()
    assert (root / "VENDOR.json").is_file()
    assert list(root.glob("aidlc-common/stages/*/*.md"))


def test_installed_tree_is_a_loadable_catalog(target_repo):
    install(target_repo)
    stages = load_catalog(install_dir(target_repo))
    assert len(stages) == 32


def test_install_is_idempotent(target_repo):
    assert install(target_repo) is True
    assert install(target_repo) is False  # already present → no write
    # a second call must not corrupt the tree
    assert load_catalog(install_dir(target_repo))


def test_force_reinstalls(target_repo):
    assert install(target_repo) is True
    assert install(target_repo, force=True) is True
    assert len(load_catalog(install_dir(target_repo))) == 32


def test_install_stages_the_copy(target_repo):
    install(target_repo)
    staged = _staged(target_repo)
    assert any(f.startswith(".aidlc/") for f in staged)
    assert ".aidlc/VENDOR.json" in staged


def test_staged_copy_passes_content_guard(target_repo):
    """Methodology is spec/metadata only — the staged install is clean for egress."""
    install(target_repo)
    findings = content_guard.scan_staged(target_repo)
    assert findings == []


def test_install_into_non_git_dir(tmp_path):
    """Best-effort staging: a non-git target still gets the files, just unstaged."""
    assert install(tmp_path) is True
    assert (tmp_path / ".aidlc" / "VENDOR.json").is_file()


def test_probe_detects_v2_after_install(target_repo):
    install(target_repo)
    steering = aidlc.probe(target_repo)
    assert steering is not None
    assert steering.flavor == FLAVOR_AIDLC_V2
    # the steering carries a resolvable catalog root M3 can hand to load_catalog()
    assert steering.catalog_root == target_repo / ".aidlc"
    assert steering.catalog_root.is_dir()
    assert len(load_catalog(steering.catalog_root)) == 32
