"""Git worktree isolation — one throwaway worktree per task, no leaks.

Each task runs in its own linked worktree on a dedicated branch, created from a
configurable target repo. Teardown removes the worktree, deletes the branch, and
prunes git's bookkeeping so ``git worktree list`` shows only the main worktree.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Functional prefixes for the branch/worktree namespace (not metaphor terms).
_BRANCH_PREFIX = "mc/task"
_WORKTREE_TMP_PREFIX = "mc-worktree-"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def is_git_repo(target) -> bool:
    """True ONLY when ``target`` is its OWN git work-tree root — never merely a folder
    nested inside an ancestor repository.

    Load-bearing for safety: every worktree we carve is created with
    ``git -C <target> worktree add``, which resolves UP to an ancestor repo if
    ``target`` isn't its own root — so a task pointed at a subdir of a parent repo
    would create branches/worktrees in that PARENT. Requiring ``--show-toplevel`` to
    equal ``target`` guarantees we only ever touch a repo we own."""
    try:
        root = Path(target).expanduser().resolve()
    except (OSError, TypeError):
        return False
    if not root.is_dir():
        return False
    r = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    try:
        return Path(r.stdout.strip()).resolve() == root
    except OSError:
        return False


@dataclass
class Worktree:
    """A live, isolated working copy for a single task."""

    path: Path  # the worktree's working directory
    branch: str  # the dedicated branch checked out there
    target_repo: Path  # the repo this worktree was carved from
    _holder: Path  # temp dir that contains ``path`` (cleaned up on teardown)


def create_worktree(target_repo: Path, name: str) -> Worktree:
    """Carve an isolated worktree + branch off ``target_repo`` at its current HEAD."""
    target_repo = Path(target_repo).resolve()
    branch = f"{_BRANCH_PREFIX}/{name}"

    # Give git a path that does not yet exist (it creates the leaf directory),
    # but keep it inside a temp holder we fully control for cleanup.
    holder = Path(tempfile.mkdtemp(prefix=_WORKTREE_TMP_PREFIX))
    path = holder / name

    _git(target_repo, "worktree", "add", "-b", branch, str(path), "HEAD")
    return Worktree(path=path, branch=branch, target_repo=target_repo, _holder=holder)


def remove_worktree(worktree: Worktree) -> None:
    """Tear down a worktree with no leaks: drop the worktree, delete its branch,
    prune bookkeeping, and remove the temp holder. Idempotent-ish and forgiving —
    teardown must never mask the original outcome."""
    target = worktree.target_repo

    # Remove the linked worktree (force: it may hold uncommitted/committed work).
    subprocess.run(
        ["git", "-C", str(target), "worktree", "remove", "--force", str(worktree.path)],
        check=False,
        capture_output=True,
        text=True,
    )
    # Delete the dedicated branch (force: it may be unmerged on a no-go).
    subprocess.run(
        ["git", "-C", str(target), "branch", "-D", worktree.branch],
        check=False,
        capture_output=True,
        text=True,
    )
    # Prune any stale administrative entries, then drop the temp holder.
    subprocess.run(
        ["git", "-C", str(target), "worktree", "prune"],
        check=False,
        capture_output=True,
        text=True,
    )
    shutil.rmtree(worktree._holder, ignore_errors=True)


def list_worktrees(target_repo: Path) -> list[Path]:
    """Absolute paths of all live worktrees (main + linked). For leak checks."""
    out = _git(Path(target_repo), "worktree", "list", "--porcelain").stdout
    paths: list[Path] = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            paths.append(Path(line[len("worktree ") :]).resolve())
    return paths


def has_changes(worktree: Worktree) -> bool:
    """True if the worker mutated the worktree relative to its HEAD."""
    out = _git(worktree.path, "status", "--porcelain").stdout
    return bool(out.strip())


def commit_changes(worktree: Worktree, message: str) -> bool:
    """Stage + commit everything in the worktree. Returns False if nothing changed."""
    if not has_changes(worktree):
        return False
    _git(worktree.path, "add", "-A")
    _git(worktree.path, "commit", "-m", message)
    return True


def merge_into_target(worktree: Worktree, message: str) -> None:
    """Apply the worktree's committed work back onto the target repo's HEAD branch."""
    _git(
        worktree.target_repo,
        "merge",
        "--no-ff",
        "--no-edit",
        "-m",
        message,
        worktree.branch,
    )


# Cap the patch we surface to a reviewer (a huge burn shouldn't blow up the page).
_MAX_PATCH_BYTES = 200_000


def changes(target_repo, branch: str, worktree_path=None) -> dict:
    """The changes a **go** would apply, against the branch's merge-base with the
    target's current HEAD.

    A burn's work may be committed on ``branch`` (real workers commit) OR left
    uncommitted in the worktree (e.g. the stub), and apply commits any uncommitted work
    before merging — so a faithful review must show BOTH. When ``worktree_path`` is
    given, we diff the worktree's FULL pending state (committed + staged + unstaged +
    untracked) vs base, computed in a throwaway index so the real worktree is untouched.
    Falls back to the committed-only ``base..branch`` diff otherwise.

    Returns a JSON-friendly dict for the go/no-go review UI. Raises on git errors
    (caller handles)."""
    target = Path(target_repo)
    base = _git(target, "merge-base", "HEAD", branch).stdout.strip()
    wt = Path(worktree_path) if worktree_path else None

    if wt is not None and wt.is_dir():
        tmp = tempfile.mkdtemp(prefix="mc-review-")   # race-free throwaway index dir
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(tmp, "index")}

        def g(*args: str) -> str:
            return subprocess.run(["git", "-C", str(wt), *args], env=env,
                                  capture_output=True, text=True, check=True).stdout
        try:
            g("read-tree", base)             # throwaway index := base tree
            g("add", "-A")                   # stage the worktree's full current state
            stat = g("diff", "--cached", "--stat", base).rstrip()
            numstat = g("diff", "--cached", "--numstat", base)
            patch = g("diff", "--cached", base)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        rng = f"{base}..{branch}"
        stat = _git(target, "diff", "--stat", rng).stdout.rstrip()
        numstat = _git(target, "diff", "--numstat", rng).stdout
        patch = _git(target, "diff", rng).stdout

    message = _git(target, "log", "-1", "--format=%B", branch).stdout.strip()
    files: list[dict] = []
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            added, removed, path = parts
            files.append({"path": path, "added": added, "removed": removed})

    truncated = len(patch) > _MAX_PATCH_BYTES
    return {
        "branch": branch,
        "message": message,
        "files": files,
        "file_count": len(files),
        "stat": stat,
        "patch": patch[:_MAX_PATCH_BYTES],
        "truncated": truncated,
    }
