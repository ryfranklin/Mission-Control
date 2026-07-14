"""Acquisition layer — bring a project's target into the per-host cache.

Portable identity (``project_ref``) says *which* project a run is for; this module
turns that identity into a concrete local working copy on THIS machine, fetched from
the remote, so a fresh host can pick up an existing project. It is the read
direction only — **clone + fetch, never push** (push lands gated in a later slice).

* :func:`ensure_local` — ref → local working path (clone if absent, fetch if present).
* :func:`trunk_of` — the remote's default branch (never hardcodes ``main``).
* :func:`default_base` — the commit-ish a fresh worktree should branch from
  (``origin/<trunk>`` when a remote-tracking trunk exists, else ``HEAD``).

The local path this returns is the DERIVED value from ``project_ref`` — never the
identity. Failures (bad ref, no network, no creds) raise :class:`RepoAcquireError`
loudly; we never silently fall back to a stale or empty local copy.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

from . import project_ref, worktree


class RepoAcquireError(RuntimeError):
    """Raised when cloning/fetching a target fails — surfaced, never swallowed."""


class BootstrapError(RuntimeError):
    """Raised when a greenfield build has no remote destination to create+push to, or
    the create/push fails. Greenfield must NOT silently fall back to a non-portable
    local-only dir — the whole point is identity + durability from unit 1."""


class PushRejected(RuntimeError):
    """The remote refused the push as non-fast-forward (a race we lost that wasn't a
    content conflict). A legible, expected outcome — the caller records a distinct
    terminal state and NEVER force-pushes."""


class PushError(RuntimeError):
    """The push failed for an environmental reason (missing/invalid credentials, no
    network, …). Surfaced loudly, never swallowed."""


class MergeConflict(RuntimeError):
    """Integrating a remote advance (workstream→branch, or promote→trunk) produced a
    real content conflict. We do NOT auto-resolve and NEVER force-push: the merge is
    aborted to a clean tree and this is raised carrying the conflicting files, so the
    gate/control room can surface a distinct 'blocked: merge conflict' state for an
    operator to resolve. Two workstreams touching the same files converge here — the
    intended behavior, not corruption."""

    def __init__(self, branch: str, files: list) -> None:
        self.branch = branch
        self.files = list(files)
        listed = ", ".join(self.files) if self.files else "(unknown files)"
        super().__init__(f"merge conflict integrating into {branch!r}: {listed}")


# Long-lived workstream branches live under this prefix on the remote — the ONLY
# workstream naming convention (no team/account hardcoding), a sibling of mc/task.
WS_BRANCH_PREFIX = "mc/ws"


def workstream_branch(name: str) -> str:
    """The long-lived branch a workstream reconciles through: ``mc/ws/<name>``."""
    return f"{WS_BRANCH_PREFIX}/{name}"


def ensure_local(ref: str, *, root: Optional[Path] = None) -> Path:
    """Resolve a normalized ``ref`` to a local working path, acquiring it if needed.

    * The ref already names a usable local working repo (an operator's own checkout,
      or the remote-less fallback identity from :func:`project_ref.resolve_target`) →
      operate there in place; nothing to acquire.
    * Otherwise the ref is acquired into :func:`project_ref.cache_dir_for`: a first-time
      target is ``git clone``d; an existing cache clone is refreshed with
      ``git fetch origin --prune``.

    Returns the local working path (the derived value — never the identity). Raises
    :class:`RepoAcquireError` on any clone/fetch failure."""
    if not ref:
        raise RepoAcquireError("no target ref to acquire")

    # In-place: an existing non-bare working repo needs no acquisition (this is every
    # legacy/local target, and the remote-less fallback identity). Keeps behavior
    # identical to before portability for those targets.
    if worktree.is_git_repo(ref):
        return Path(ref).expanduser().resolve()

    root = Path(root) if root is not None else project_ref.DEFAULT_CACHE_ROOT
    cache = project_ref.cache_dir_for(ref, root=root).resolve()

    # ONE per-repo lock guards the shared cache clone (see worktree._repo_lock): many
    # runs against the same ref now share this clone, so clone/fetch here and the
    # later worktree-add contend on the same .git admin. Acquired for clone/fetch and
    # released before create_worktree takes it — never nested, so no self-deadlock.
    with worktree._repo_lock(cache):
        if _is_git_dir(cache):
            _fetch(cache)
        else:
            _clone(_clone_url_for(ref), cache)
    return cache


def trunk_of(local_repo) -> str:
    """The remote's default branch name (e.g. ``main`` / ``master`` / anything else).

    Discovered from ``refs/remotes/origin/HEAD`` (set by ``git clone``); falls back to
    probing ``origin/main`` then ``origin/master``. Never hardcodes a name. Raises
    :class:`LookupError` when there is no remote-tracking trunk to discover."""
    repo = Path(local_repo)
    head = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True,
    )
    prefix = "refs/remotes/origin/"
    if head.returncode == 0 and head.stdout.strip().startswith(prefix):
        return head.stdout.strip()[len(prefix):]

    for candidate in ("main", "master"):
        probe = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
             f"refs/remotes/origin/{candidate}"],
            capture_output=True, text=True,
        )
        if probe.returncode == 0:
            return candidate

    raise LookupError(f"no remote-tracking trunk (origin/HEAD, origin/main, origin/master) in {repo}")


def default_base(local_repo) -> str:
    """The commit-ish a fresh worktree branches from: ``origin/<trunk>`` when a
    remote-tracking trunk exists (a fetched cache clone builds on the remote's default
    branch, not the shared clone's arbitrary HEAD), else ``HEAD`` for a repo with no
    remote (e.g. a local-only or freshly-scaffolded target)."""
    try:
        return f"origin/{trunk_of(local_repo)}"
    except LookupError:
        return "HEAD"


# -- write direction: push an approved, merged result back to the remote ---

def has_origin(local_repo) -> bool:
    """True if the repo has an ``origin`` remote to push to. False for a remote-less
    local target (nothing to push — the merge lives only on this host)."""
    result = subprocess.run(
        ["git", "-C", str(local_repo), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def current_branch(local_repo) -> str:
    """The branch the local clone currently has checked out — the branch a merge
    landed on and therefore the one to push (the trunk, or later a workstream branch)."""
    return subprocess.run(
        ["git", "-C", str(local_repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def push_to_remote(work_dir, branch: str, *, lock_repo=None) -> None:
    """Integrate a remote advance on ``origin/<branch>`` then push HEAD there — the
    write hop that lets an approved change leave the host. Only ever called AFTER an
    affirmative gate.

    Works whether ``work_dir`` is the clone on its trunk (trunk runs) or an isolated
    task worktree on its task branch (workstream runs): it integrates ``origin/<branch>``
    into the current HEAD and pushes ``HEAD:<branch>``. First fetches + merges so a
    simple non-conflicting push doesn't fail on a stale ref.

    Idempotent for node-boundary recovery: an already-pushed HEAD integrates as a no-op
    and ``git push`` reports *up-to-date* (exit 0).

    Auth is ambient; the remote is always the acquired clone's ``origin`` (never
    hardcoded). Raises :class:`MergeConflict` (with conflicting files) on a real content
    conflict during integrate, :class:`PushRejected` on a non-fast-forward push, and
    :class:`PushError` on an environmental failure — NEVER force-pushes.

    ``lock_repo`` is the repo whose per-repo lock serializes this against other
    shared-clone mutations; it defaults to ``work_dir`` but a worktree must pass its
    owning clone so worktree and clone ops on the same ``.git`` serialize together. NOTE
    that this lock only serializes WITHIN a host (see worktree._repo_lock); cross-host
    safety comes from git itself — a lost race is a non-fast-forward or a recoverable
    merge conflict here, never corruption."""
    repo = Path(work_dir)
    remote_ref = f"origin/{branch}"

    with worktree._repo_lock(lock_repo if lock_repo is not None else repo):
        fetched = subprocess.run(
            ["git", "-C", str(repo), "fetch", "origin"], capture_output=True, text=True,
        )
        if fetched.returncode != 0:
            raise PushError(f"fetch before push of {branch!r} failed: {fetched.stderr.strip()}")

        # Integrate a remote advance for the simple, non-conflicting case. A real
        # conflict is aborted (leaving a clean tree) and surfaced with its files — no
        # force, no auto-resolution, no half-merged state.
        has_remote_branch = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", remote_ref],
            capture_output=True, text=True,
        ).returncode == 0
        if has_remote_branch:
            merged = subprocess.run(
                ["git", "-C", str(repo), "merge", "--no-edit", remote_ref],
                capture_output=True, text=True,
            )
            if merged.returncode != 0:
                files = _conflicted_files(repo)
                subprocess.run(["git", "-C", str(repo), "merge", "--abort"],
                               capture_output=True, text=True)
                raise MergeConflict(branch, files)

        pushed = subprocess.run(
            ["git", "-C", str(repo), "push", "origin", f"HEAD:{branch}"],
            capture_output=True, text=True,
        )
    if pushed.returncode == 0:
        return  # includes the already-pushed 'Everything up-to-date' no-op
    stderr = pushed.stderr.strip()
    low = stderr.lower()
    if any(m in low for m in ("non-fast-forward", "fetch first", "[rejected]",
                              "failed to push some refs")):
        raise PushRejected(f"push of {branch!r} rejected (non-fast-forward): {stderr}")
    raise PushError(f"push of {branch!r} failed: {stderr}")


def _conflicted_files(repo: Path) -> list:
    """The files left with conflict markers by a failed merge (for operator detail)."""
    out = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", "--diff-filter=U"],
        capture_output=True, text=True,
    ).stdout
    return [f for f in out.splitlines() if f.strip()]


# -- workstreams: many teams, one project, reconciling through the gate -----

def ensure_workstream_branch(local_repo, name: str) -> str:
    """Ensure the long-lived ``mc/ws/<name>`` branch exists on the remote and return it.

    Fetches; if ``origin/mc/ws/<name>`` is absent it is created from ``origin/<trunk>``
    and pushed (a fast-forward create — never a force). If present, the fetch just brings
    it up to date. This is the per-workstream line a team's task worktrees branch off of.
    Raises :class:`RepoAcquireError` on fetch failure, :class:`PushError` on a failed
    create-push."""
    repo = Path(local_repo)
    branch = workstream_branch(name)
    remote_ref = f"origin/{branch}"
    # Within-host serialization only (see worktree._repo_lock); cross-host races resolve
    # as a merge conflict or non-fast-forward at the gate, never corruption.
    with worktree._repo_lock(repo):
        fetched = subprocess.run(
            ["git", "-C", str(repo), "fetch", "origin", "--prune"],
            capture_output=True, text=True,
        )
        if fetched.returncode != 0:
            raise RepoAcquireError(
                f"fetch before ensuring workstream {name!r} failed: {fetched.stderr.strip()}")
        exists = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", remote_ref],
            capture_output=True, text=True,
        ).returncode == 0
        if not exists:
            trunk = trunk_of(repo)
            created = subprocess.run(
                ["git", "-C", str(repo), "push", "origin",
                 f"origin/{trunk}:refs/heads/{branch}"],
                capture_output=True, text=True,
            )
            if created.returncode != 0:
                raise PushError(
                    f"could not create workstream branch {branch!r}: {created.stderr.strip()}")
            subprocess.run(["git", "-C", str(repo), "fetch", "origin", "--prune"],
                           capture_output=True, text=True)
    return branch


def promote(local_repo, name: str, *, message: Optional[str] = None) -> None:
    """Reconcile a workstream branch into trunk — the explicit, separately-gated step
    that advances trunk (a per-task run never does). Merges ``origin/mc/ws/<name>`` into
    a fresh worktree off ``origin/<trunk>`` and pushes trunk.

    A conflict is aborted to a clean tree and raised as :class:`MergeConflict` with the
    files (two workstreams touching the same code surface here) — never auto-resolved,
    never force-pushed. Raises :class:`PushRejected` / :class:`PushError` like any push."""
    repo = Path(local_repo)
    branch = workstream_branch(name)
    with worktree._repo_lock(repo):
        fetched = subprocess.run(
            ["git", "-C", str(repo), "fetch", "origin", "--prune"],
            capture_output=True, text=True,
        )
        if fetched.returncode != 0:
            raise RepoAcquireError(f"fetch before promoting {name!r} failed: {fetched.stderr.strip()}")
        trunk = trunk_of(repo)

    wt = worktree.create_worktree(repo, f"promote-{name}", base=f"origin/{trunk}")
    try:
        with worktree._repo_lock(repo):
            merged = subprocess.run(
                ["git", "-C", str(wt.path), "merge", "--no-edit",
                 "-m", message or f"promote {branch} into {trunk}", f"origin/{branch}"],
                capture_output=True, text=True,
            )
            if merged.returncode != 0:
                files = _conflicted_files(wt.path)
                subprocess.run(["git", "-C", str(wt.path), "merge", "--abort"],
                               capture_output=True, text=True)
                raise MergeConflict(trunk, files)
        # Integrate any trunk advance and push HEAD (trunk + workstream) to trunk.
        push_to_remote(wt.path, trunk, lock_repo=repo)
    finally:
        worktree.remove_worktree(wt)


# -- greenfield bootstrap: create the remote so identity exists from unit 1 --

def bootstrap_remote(dest: str, scratch_dir, *, allow_secrets: bool = False) -> str:
    """Create a project's remote from an operator-supplied destination and return its
    normalized :mod:`project_ref` id — so a greenfield build has a stable, portable
    identity (and a durable remote) BEFORE unit 1, exactly like a brownfield target.

    The seed commit is scanned by :mod:`content_guard` before it is pushed; a secret
    blocks the bootstrap unless ``allow_secrets`` (an explicit operator override). A
    ``.gitignore`` for common secret files is seeded as defense-in-depth.

    ``dest`` is the operator's remote destination (a URL, or a local path standing in as
    the remote — never a hardcoded host/org/account). ``scratch_dir`` is EPHEMERAL: the
    initial commit is staged there and pushed; the durable copy is the remote, re-acquired
    via :func:`ensure_local` afterwards. The caller seeds ``scratch_dir`` (e.g. the plan
    docs) before calling; a minimal README is added if none is present.

    For a LOCAL destination that doesn't exist yet it is created as an empty bare repo, so
    the operator needn't hand-create it. For a URL the remote endpoint is assumed to exist
    (creating one over the wire needs a host API — out of scope for an agnostic runtime).
    Reuses :func:`push_to_remote`. Raises :class:`BootstrapError` when no destination is
    given (never a silent local-only fallback) or the create/push fails."""
    if not dest or not str(dest).strip():
        raise BootstrapError(
            "greenfield build requires a remote destination (arg / env / API field); "
            "refusing to fall back to a non-portable local-only workspace")
    dest = str(dest).strip()
    scratch = Path(scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)
    if not (scratch / "README.md").exists():
        (scratch / "README.md").write_text(
            "# Project\n\nBootstrapped by Mission Control.\n", encoding="utf-8")
    # Defense-in-depth: seed a .gitignore for common secret files so they never get
    # staged in the first place (the content guard is the enforcement; this is the first
    # line). Repo-agnostic — filename shapes only, no account/host names.
    gitignore = scratch / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_SECRET_GITIGNORE, encoding="utf-8")

    # A local destination that isn't a repo yet is created as an empty bare remote.
    if not project_ref.is_remote_url(dest):
        dpath = Path(dest).expanduser()
        if not _is_git_dir(dpath):
            dpath.parent.mkdir(parents=True, exist_ok=True)
            created = subprocess.run(["git", "init", "--bare", str(dpath)],
                                     capture_output=True, text=True)
            if created.returncode != 0:
                raise BootstrapError(f"could not create remote at {dest!r}: {created.stderr.strip()}")
            subprocess.run(["git", "-C", str(dpath), "symbolic-ref", "HEAD", "refs/heads/main"],
                           capture_output=True, text=True)

    for args in (["init", "-b", "main"],
                 ["config", "user.email", "planner@mission-control.local"],
                 ["config", "user.name", "Mission Control Planner"],
                 ["add", "-A"]):
        done = subprocess.run(["git", "-C", str(scratch), *args], capture_output=True, text=True)
        if done.returncode != 0:
            raise BootstrapError(f"bootstrap stage failed ({args[0]}): {done.stderr.strip()}")
    # EGRESS GUARD: the seed is about to be committed + pushed — scan it first.
    from . import content_guard
    content_guard.enforce_staged(scratch, allow=allow_secrets)
    committed = subprocess.run(["git", "-C", str(scratch), "commit", "-m",
                                "bootstrap: initialize project"], capture_output=True, text=True)
    if committed.returncode != 0:
        raise BootstrapError(f"bootstrap commit failed: {committed.stderr.strip()}")
    subprocess.run(["git", "-C", str(scratch), "remote", "add", "origin", dest],
                   check=True, capture_output=True)
    # Push the seed onto the (empty) remote's trunk — creates it; loud on failure.
    push_to_remote(scratch, "main")
    return project_ref.normalize_remote(dest)


# Common secret-file shapes to keep OUT of a committed/pushed repo (first line of
# defense; the content guard is the enforcement). No account/host specifics.
_SECRET_GITIGNORE = "\n".join([
    "# Mission Control: keep secrets out of the shared remote.",
    ".env", ".env.*", "*.env",
    "*.pem", "*.key", "*_rsa", "*_dsa", "*_ed25519", "id_rsa*",
    "*.p12", "*.pfx", "*.keystore",
    "credentials", "credentials.*", ".netrc", ".npmrc", ".pypirc",
    "*.secret", "secrets.*", ".aws/", ".ssh/",
    "",
])


# -- internals -------------------------------------------------------------

def _clone_url_for(ref: str) -> str:
    """The URL git should clone from, derived from the stored identity.

    * a full URL (scheme or scp form) → used as-is;
    * an existing local path (a bare repo used as origin, or a local fixture) → that path;
    * a scheme-less normalized ref (``host/org/repo``) → ``https://<ref>`` — a sensible
      default acquisition scheme. Git's ``url.<base>.insteadOf`` config can rewrite it
      to ssh for private/ssh-only remotes, so no remote is hardcoded here."""
    if project_ref.is_remote_url(ref):
        return ref
    # A filesystem path — existing, or clearly path-shaped (absolute / home / explicitly
    # relative) — is cloned directly (covers a bare-repo origin and local fixtures). Only
    # a bare scheme-less remote id (``host/org/repo``) gets an https scheme applied.
    if Path(ref).expanduser().exists() or ref.startswith(("/", "~", "./", "../")):
        return str(Path(ref).expanduser())
    return f"https://{ref}"


def _is_git_dir(path: Path) -> bool:
    """True if ``path`` is an existing git repository (a populated cache clone)."""
    if not path.exists():
        return False
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-dir"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _clone(url: str, cache: Path) -> None:
    """First-time acquisition. On failure, remove any half-written cache dir so a later
    call can't mistake a partial clone for a present one, then raise loudly."""
    cache.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", url, str(cache)], capture_output=True, text=True,
    )
    if result.returncode != 0:
        shutil.rmtree(cache, ignore_errors=True)
        raise RepoAcquireError(f"clone failed for {url!r}: {result.stderr.strip()}")


def _fetch(cache: Path) -> None:
    """Refresh an existing cache clone. Loud on failure — we never silently serve a
    stale copy (the caller asked for the current remote state)."""
    result = subprocess.run(
        ["git", "-C", str(cache), "fetch", "origin", "--prune"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RepoAcquireError(f"fetch failed for {cache}: {result.stderr.strip()}")
