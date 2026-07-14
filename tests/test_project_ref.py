"""Unit tests for the portable project-identity module.

The load-bearing property: the ssh and https forms of the SAME repo collapse to
one stable id, so the same project on a different machine is recognized as the
same project (not a brand-new one)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mission_control import project_ref
from mission_control.project_ref import (
    NoRemoteError,
    cache_dir_for,
    normalize_remote,
    remote_of,
    resolve_target,
    slug_for,
)

# Canonicalization table: every left form of a given repo must collapse to the
# SAME id on the right (ssh vs https, with/without .git, mixed-case host,
# credentialed URL, scheme ssh://).
_CANON = [
    # ssh (scp-like) vs https, with/without .git
    ("git@github.com:org/repo.git", "github.com/org/repo"),
    ("git@github.com:org/repo", "github.com/org/repo"),
    ("https://github.com/org/repo.git", "github.com/org/repo"),
    ("https://github.com/org/repo", "github.com/org/repo"),
    # mixed-case host lowercases; path case preserved
    ("https://GitHub.COM/Org/Repo.git", "github.com/Org/Repo"),
    ("git@GitHub.com:Org/Repo.git", "github.com/Org/Repo"),
    # credentials dropped
    ("https://user:token@github.com/org/repo.git", "github.com/org/repo"),
    ("https://user@github.com/org/repo", "github.com/org/repo"),
    # ssh:// scheme form
    ("ssh://git@github.com/org/repo.git", "github.com/org/repo"),
    # trailing slashes tolerated
    ("https://github.com/org/repo/", "github.com/org/repo"),
    # a non-github host works too (repo stays host-agnostic)
    ("git@gitlab.example.com:team/sub/proj.git", "gitlab.example.com/team/sub/proj"),
]


@pytest.mark.parametrize("url,expected", _CANON)
def test_normalize_remote_canonicalization(url, expected):
    assert normalize_remote(url) == expected


def test_ssh_and_https_forms_collapse_to_one_id():
    """The headline guarantee: the two forms of one repo are one identity."""
    ssh = normalize_remote("git@github.com:acme/widget.git")
    https = normalize_remote("https://github.com/acme/widget")
    assert ssh == https == "github.com/acme/widget"


def test_normalize_remote_is_idempotent():
    for url, _ in _CANON:
        once = normalize_remote(url)
        assert normalize_remote(once) == once


def test_normalize_remote_empty_and_unrecognized():
    assert normalize_remote("") == ""
    assert normalize_remote("   ") == ""
    # An unrecognizable value is handed back trimmed, never raised on.
    assert normalize_remote("  not a url  ") == "not a url"


# -- slug_for --------------------------------------------------------------

def test_slug_for_is_filesystem_safe():
    slug = slug_for("github.com/org/repo")
    assert Path(slug).name == slug  # no path separators leak in
    assert all(c.isalnum() or c in "-._" for c in slug)


def test_slug_for_is_collision_resistant():
    # Two distinct refs that sanitize to the same readable head must NOT collide,
    # because the hash suffix is taken from the exact ref.
    a = slug_for("github.com/org/repo")
    b = slug_for("github.com/org/Repo")
    assert a != b


def test_slug_for_is_length_bounded():
    long_ref = "github.com/" + "verylongsegment/" * 40 + "repo"
    slug = slug_for(long_ref)
    # readable head (<=60) + '-' + 12-char hash
    assert len(slug) <= project_ref._SLUG_MAX_READABLE + 1 + project_ref._SLUG_HASH_LEN


def test_slug_for_is_deterministic():
    assert slug_for("github.com/org/repo") == slug_for("github.com/org/repo")


def test_equal_refs_from_both_forms_share_a_slug():
    """ssh + https of one repo → one ref → one slug → one cache dir."""
    ref_ssh = normalize_remote("git@github.com:org/repo.git")
    ref_https = normalize_remote("https://github.com/org/repo")
    assert slug_for(ref_ssh) == slug_for(ref_https)


# -- cache_dir_for ---------------------------------------------------------

def test_cache_dir_for_is_under_root(tmp_path):
    ref = "github.com/org/repo"
    d = cache_dir_for(ref, root=tmp_path)
    assert d.parent == tmp_path
    assert d.name == slug_for(ref)


def test_cache_dir_for_defaults_to_mission_control_root():
    d = cache_dir_for("github.com/org/repo")
    assert d.parent == project_ref.DEFAULT_CACHE_ROOT


def test_cache_dir_for_creates_nothing(tmp_path):
    cache_dir_for("github.com/org/repo", root=tmp_path)
    assert list(tmp_path.iterdir()) == []


# -- remote_of + resolve_target (touch a real throwaway git repo) ----------

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    (path / "README.md").write_text("# r\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    return path


def test_remote_of_reads_and_normalizes_origin(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "remote", "add", "origin", "git@github.com:acme/widget.git")
    assert remote_of(repo) == "github.com/acme/widget"


def test_remote_of_raises_without_origin(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    with pytest.raises(NoRemoteError):
        remote_of(repo)


def test_resolve_target_local_path_with_remote(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/widget.git")
    ref, local = resolve_target(str(repo))
    assert ref == "github.com/acme/widget"          # portable identity
    assert local == repo.resolve()                   # derived working path


def test_resolve_target_local_path_without_remote_falls_back_to_path(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    ref, local = resolve_target(str(repo))
    assert ref == str(repo.resolve())                # non-portable fallback, unchanged
    assert local == repo.resolve()


def test_resolve_target_accepts_a_remote_url_directly(tmp_path):
    ref, local = resolve_target("git@github.com:acme/widget.git", root=tmp_path)
    assert ref == "github.com/acme/widget"
    assert local == cache_dir_for(ref, root=tmp_path)  # derived cache dir (not cloned)


def test_is_remote_url_distinguishes_paths_from_urls(tmp_path):
    assert project_ref.is_remote_url("git@github.com:org/repo.git")
    assert project_ref.is_remote_url("https://github.com/org/repo")
    assert project_ref.is_remote_url("ssh://git@github.com/org/repo")
    assert not project_ref.is_remote_url(str(tmp_path))   # an existing local path
    assert not project_ref.is_remote_url("/some/local/path")
