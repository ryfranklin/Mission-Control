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
import threading
from dataclasses import dataclass
from pathlib import Path

# Functional prefixes for the branch/worktree namespace (not metaphor terms).
_BRANCH_PREFIX = "mc/task"
_WORKTREE_TMP_PREFIX = "mc-worktree-"

# Per-repo locks, keyed by resolved repo path. See _repo_lock for the rationale.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _repo_lock(repo_path) -> threading.Lock:
    """The lock serializing mutations of ONE shared repo, keyed by its resolved path.

    With portable identity, many runs against the same ref now share ONE cache clone
    (``repo_source.ensure_local``), so their git mutations — the acquisition
    clone/fetch, ``worktree add``, ``merge``, ``worktree remove`` — all contend on the
    same ``.git`` admin (refs, HEAD, the worktree list). ONE per-repo lock is the
    natural granularity: a fetch-only lock would still let a fetch race a
    worktree-add's ref reads.

    Every caller acquires this lock for a single operation and releases it before the
    next one runs (dispatch does ``ensure_local`` then ``create_worktree``
    sequentially), so the lock is NEVER held across another lock acquisition — no
    self-deadlock (the flock risk the architecture review flagged). It is in-process
    only; cross-process locking is deliberately out of scope for this slice.

    IMPORTANT (workstreams): this serializes ONLY within a single host — it is not
    cross-host mutual exclusion. Two hosts (or processes) building the same project
    can race. That is safe because cross-host correctness comes from GIT, not this
    lock: a lost race surfaces as a non-fast-forward push or a recoverable merge
    conflict at the gate/promote (see repo_source.push_to_remote / promote), never
    silent corruption or a forced overwrite."""
    key = str(Path(repo_path).resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


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


def create_worktree(target_repo: Path, name: str, *, base: str = "HEAD") -> Worktree:
    """Carve an isolated worktree + branch off ``target_repo`` at ``base``.

    ``base`` is any commit-ish the new branch starts from. It defaults to ``HEAD``
    (the repo's current checkout). The run graph passes ``origin/<trunk>`` (via
    :func:`repo_source.default_base`) so a fetched cache clone builds on the remote's
    default branch rather than whatever the shared clone's HEAD happens to point at;
    callers can pass any explicit base (the seam for later workstream branches). The
    branch namespace and teardown are unchanged."""
    target_repo = Path(target_repo).resolve()
    branch = f"{_BRANCH_PREFIX}/{name}"

    # Give git a path that does not yet exist (it creates the leaf directory),
    # but keep it inside a temp holder we fully control for cleanup.
    holder = Path(tempfile.mkdtemp(prefix=_WORKTREE_TMP_PREFIX))
    path = holder / name

    # Serialize branch/worktree creation on the shared repo (see _repo_lock).
    with _repo_lock(target_repo):
        _git(target_repo, "worktree", "add", "-b", branch, str(path), base)
    return Worktree(path=path, branch=branch, target_repo=target_repo, _holder=holder)


def remove_worktree(worktree: Worktree) -> None:
    """Tear down a worktree with no leaks: drop the worktree, delete its branch,
    prune bookkeeping, and remove the temp holder. Idempotent-ish and forgiving —
    teardown must never mask the original outcome."""
    target = worktree.target_repo

    # Serialize teardown of the shared repo against concurrent add/merge (see _repo_lock).
    with _repo_lock(target):
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
    """Apply the worktree's committed work back onto the target repo's HEAD branch.

    Still the READ-side of a run in this slice: the merge lands on the LOCAL clone's
    HEAD branch only — nothing is pushed to the remote (push lands gated later)."""
    # Serialize the merge on the shared repo against concurrent add/remove (see _repo_lock).
    with _repo_lock(worktree.target_repo):
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
