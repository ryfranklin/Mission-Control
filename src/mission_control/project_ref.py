"""Portable project identity — a stable id derived from a git remote.

A build's identity must be the SAME on every machine, or the same project on a
different host reads as a brand-new project. A machine-local absolute path can't
be that identity. This module derives a stable identity from a project's one
portable name — its git remote URL — and maps that identity to a local cache
directory with a pure URL→path function.

Four responsibilities, all agnostic (no hardcoded hosts/orgs/accounts):

* :func:`normalize_remote` — canonicalize any git remote URL to one stable id,
  collapsing the ssh and https forms of the same repo together.
* :func:`slug_for` — a filesystem-safe, collision-resistant, length-bounded slug.
* :func:`cache_dir_for` — the pure id→local-cache-path mapping.
* :func:`remote_of` — read a local repo's ``origin`` and normalize it.

:func:`resolve_target` ties them together for callers: it accepts EITHER a local
path or a remote URL and returns ``(ref, local_path)`` — the portable identity
plus the derived working directory. This slice changes identity + storage only;
it does no cloning/fetching, so a URL's ``local_path`` is just its (possibly
not-yet-populated) cache directory.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

# Default root for local checkouts, keyed by portable id. Overridable per call /
# via the caller's env — never hardcoded elsewhere (repo stays host/org agnostic).
DEFAULT_CACHE_ROOT = Path.home() / ".mission-control" / "repos"

# scp-like remote syntax: ``[user@]host:path`` (e.g. ``git@github.com:org/repo.git``).
# Host has no slash or colon; the first colon separates host from path. Only tried
# AFTER ruling out a scheme URL (``https://…`` also contains a colon).
_SCP_RE = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>.+)$")

# The readable part of a slug is bounded; a hash suffix carries the collision
# resistance, so the bound never causes two distinct refs to collide.
_SLUG_MAX_READABLE = 60
_SLUG_HASH_LEN = 12


class NoRemoteError(RuntimeError):
    """Raised when a local repo has no ``origin`` remote to derive an identity from."""


def normalize_remote(url: str) -> str:
    """Canonicalize a git remote URL to one stable identity string.

    Collapses the ssh (``git@host:org/repo.git``), scheme (``https://host/org/repo``,
    ``ssh://git@host/org/repo``), and mixed forms of the SAME repo to a single id
    of the shape ``host/org/repo``:

    * lowercases the host (case-insensitive per DNS),
    * drops any credentials embedded in the URL,
    * strips a trailing ``.git`` and surrounding slashes.

    Path case is preserved (it may be significant on some forges). Idempotent —
    ``normalize_remote(normalize_remote(x)) == normalize_remote(x)``. An
    unrecognizable input is returned trimmed, so this never raises."""
    url = (url or "").strip()
    if not url:
        return ""

    if "://" in url:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()  # hostname excludes userinfo + port
        path = parts.path
    else:
        m = _SCP_RE.match(url)
        if not m:
            return url  # not a recognizable remote; hand back verbatim (trimmed)
        host = m.group("host").lower()
        path = m.group("path")

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.strip("/")

    if not host:
        return path
    return f"{host}/{path}" if path else host


def slug_for(ref: str) -> str:
    """A filesystem-safe, collision-resistant, length-bounded slug for a ref.

    Sanitizes to ``[a-z0-9._-]`` (mirroring worktree naming discipline), lowercases
    for case-insensitive filesystems, bounds the readable head, and appends a short
    hash of the EXACT ref so two refs that sanitize alike still map to distinct
    directories."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", ref or "").strip("-.").lower()
    head = cleaned[:_SLUG_MAX_READABLE].strip("-.")
    digest = hashlib.sha256((ref or "").encode("utf-8")).hexdigest()[:_SLUG_HASH_LEN]
    return f"{head}-{digest}" if head else digest


def cache_dir_for(ref: str, root: Path = DEFAULT_CACHE_ROOT) -> Path:
    """The local cache directory a ref maps to (pure; creates nothing)."""
    return Path(root) / slug_for(ref)


def remote_of(local_repo) -> str:
    """Read ``git remote get-url origin`` for ``local_repo`` and return its
    normalized ref. Raises :class:`NoRemoteError` when there's no ``origin``."""
    result = subprocess.run(
        ["git", "-C", str(local_repo), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise NoRemoteError(f"no 'origin' remote for {local_repo}")
    return normalize_remote(result.stdout.strip())


def is_remote_url(target: str) -> bool:
    """True if ``target`` looks like a git remote URL rather than a local path.

    A scheme URL (``https://…``, ``ssh://…``) or scp-like ``host:path`` — but never
    something that already exists on disk (a real path always wins)."""
    if not target:
        return False
    if "://" in target:
        return True
    if _SCP_RE.match(target):
        # Don't misread a local path that happens to contain a colon.
        return not Path(target).expanduser().exists()
    return False


def resolve_target(target: str, *, root: Path = DEFAULT_CACHE_ROOT) -> tuple[str, Path | None]:
    """Resolve a target (local path OR remote URL) to ``(ref, local_path)``.

    * A **remote URL** → its normalized ref + the derived cache dir (not populated
      in this slice; no clone/fetch).
    * A **local path** → the repo's normalized ``origin`` ref if it has one, else a
      fallback to the resolved absolute path (a non-portable identity, unchanged
      from prior behavior for remote-less repos). ``local_path`` is the resolved
      path.

    ``ref`` is always the identity to store; ``local_path`` is the derived working
    location, never the identity."""
    if is_remote_url(target):
        ref = normalize_remote(target)
        return ref, cache_dir_for(ref, root)

    path = Path(target).expanduser().resolve()
    try:
        return remote_of(path), path
    except NoRemoteError:
        return str(path), path
