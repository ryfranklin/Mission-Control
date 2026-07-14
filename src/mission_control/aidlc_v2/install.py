"""Install the vendored AI-DLC **v2** methodology into a target repo.

Canonical install location (pick ONE — this is it): **``<target>/.aidlc/``**. The whole
vendored ``methodology/`` tree maps there verbatim, so after install the target holds::

    <target>/.aidlc/aidlc-common/stages/<phase>/*.md
    <target>/.aidlc/aidlc-common/protocols/*.md
    <target>/.aidlc/agents/*.md
    <target>/.aidlc/knowledge/*.md
    <target>/.aidlc/VENDOR.json

That directory IS the catalog root :func:`mission_control.aidlc_v2.catalog.load_catalog`
consumes, and the layout :func:`mission_control.aidlc.probe` detects as the v2 flavor.

The copy is **staged** (``git add``) so the egress :mod:`content_guard` scans it before
anything reaches a remote — the methodology is spec/metadata only, but we route it
through the same boundary as everything else we push.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .catalog import default_methodology_root

# The one canonical install location inside a target repo.
INSTALL_DIRNAME = ".aidlc"


def install_dir(target_root: Path) -> Path:
    """Where the v2 methodology lives inside ``target_root`` (the catalog root)."""
    return Path(target_root) / INSTALL_DIRNAME


def _stage(target_root: Path, dest: Path) -> None:
    """``git add`` the installed tree so the content guard sees it. Best-effort: a
    non-git target simply isn't staged (nothing to push through the boundary yet)."""
    inside = subprocess.run(
        ["git", "-C", str(target_root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    )
    if inside.returncode != 0:
        return
    subprocess.run(
        ["git", "-C", str(target_root), "add", "--", str(dest)],
        check=True, capture_output=True, text=True,
    )


def install(target_root: Path, *, force: bool = False) -> bool:
    """Copy the vendored ``methodology/`` into ``<target_root>/.aidlc/``.

    Idempotent: if the install already exists and ``force`` is False, does nothing and
    returns ``False``. Otherwise copies the tree (overwriting on ``force``), stages it
    so :mod:`content_guard` applies, and returns ``True``.
    """
    target_root = Path(target_root)
    dest = install_dir(target_root)
    if dest.exists() and not force:
        return False
    source = default_methodology_root()
    shutil.copytree(source, dest, dirs_exist_ok=force)
    _stage(target_root, dest)
    return True
