#!/usr/bin/env python
"""Reproducibly vendor the AWS AI-DLC **v2** methodology *content* into this repo.

Mission Control reads AI-DLC v2 as **text only** — its stage definitions, protocols,
agent definitions, and knowledge. We never execute v2's TypeScript hooks or tools; MC
substitutes its own orchestration, go/no-go gate, and state. This script therefore
copies only the content subtrees and explicitly EXCLUDES ``hooks/`` and ``tools/``
(all ``.ts``) plus any binary.

Pinned to an exact upstream revision so the vendored tree is reproducible. Re-running
overwrites the vendored tree in place and refreshes ``VENDOR.json``.

Usage::

    python scripts/vendor_aidlc_v2.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# -- the pin (exact upstream revision) -------------------------------------
REPO = "https://github.com/awslabs/aidlc-workflows"
REF = "v2"
COMMIT = "d4fc34dd2e548b43fb781ff6177662d6bf54e6f8"

# Content subtrees to copy, relative to ``dist/claude/.claude/`` in the upstream repo.
SOURCE_BASE = "dist/claude/.claude"
SOURCE_PATHS = (
    "aidlc-common/stages",
    "aidlc-common/protocols",
    "agents",
    "knowledge",
)

# Never vendor these — MC runs its own orchestration, not v2's runtime.
EXCLUDED = ("hooks", "tools")

# Destination inside this repo.
REPO_ROOT = Path(__file__).resolve().parent.parent
DEST = REPO_ROOT / "src" / "mission_control" / "aidlc_v2" / "methodology"

# Extensions we refuse to vendor (executable runtime + binaries).
_BLOCKED_SUFFIXES = {".ts", ".tsx", ".js", ".mjs", ".cjs"}
_TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ""}


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(cmd, cwd=cwd, check=True)


def _shallow_clone(dest: Path) -> None:
    """Shallow-clone the pinned commit into ``dest`` (init + fetch-by-sha, so a
    shallow fetch works even though the commit isn't a branch tip)."""
    _run(["git", "init", "--quiet", str(dest)])
    _run(["git", "remote", "add", "origin", REPO], cwd=dest)
    _run(["git", "fetch", "--quiet", "--depth", "1", "origin", COMMIT], cwd=dest)
    _run(["git", "checkout", "--quiet", "FETCH_HEAD"], cwd=dest)


def _is_binary(path: Path) -> bool:
    """A file is binary if it isn't a known-text extension AND contains a NUL byte."""
    if path.suffix.lower() in _TEXT_SUFFIXES:
        return False
    return b"\x00" in path.read_bytes()[:8192]


def _should_skip(path: Path) -> str | None:
    """Return a reason to skip ``path``, or ``None`` to vendor it."""
    if path.suffix.lower() in _BLOCKED_SUFFIXES:
        return f"blocked suffix {path.suffix}"
    if _is_binary(path):
        return "binary"
    return None


def _copy_subtree(src_root: Path, rel: str, skipped: list[str]) -> int:
    """Copy ``src_root/rel`` into ``DEST/rel``, filtering blocked/binary files.
    Returns the number of files copied."""
    src = src_root / SOURCE_BASE / rel
    if not src.is_dir():
        raise SystemExit(f"expected source subtree missing upstream: {rel}")
    copied = 0
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        reason = _should_skip(path)
        rel_to_sub = path.relative_to(src)
        if reason is not None:
            skipped.append(f"{rel}/{rel_to_sub} ({reason})")
            continue
        target = DEST / rel / rel_to_sub
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    return copied


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="aidlc-v2-") as tmp:
        clone = Path(tmp) / "aidlc-workflows"
        print(f"cloning {REPO} @ {COMMIT[:12]} …")
        _shallow_clone(clone)

        # Fresh vendor: clear any prior tree, keep the dir.
        if DEST.exists():
            shutil.rmtree(DEST)
        DEST.mkdir(parents=True)

        skipped: list[str] = []
        total = 0
        for rel in SOURCE_PATHS:
            n = _copy_subtree(clone, rel, skipped)
            print(f"  vendored {n:>3} files from {rel}")
            total += n

        vendor = {
            "repo": REPO,
            "ref": REF,
            "commit": COMMIT,
            "vendored_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "source_base": SOURCE_BASE,
            "source_paths": list(SOURCE_PATHS),
            "excluded": list(EXCLUDED),
            "file_count": total,
        }
        (DEST / "VENDOR.json").write_text(
            json.dumps(vendor, indent=2) + "\n", encoding="utf-8"
        )

    print(f"vendored {total} files → {DEST.relative_to(REPO_ROOT)}")
    if skipped:
        print(f"skipped {len(skipped)} non-content files:")
        for s in skipped:
            print(f"  - {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
