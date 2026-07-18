"""Egress content guard: secrets/PII in content headed for the remote are blocked at the
commit boundary (naming file + rule); clean spec content flows through; an explicit
operator override allows it and is recorded for audit; the bootstrap seeds a .gitignore.

Uses a local bare-repo remote + a fake run manager (deterministic) where a full run is
overkill; the pure scanner is tested directly."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from mission_control import content_guard, plan_docs, project_ref, repo_source, roles, worktree
from mission_control.content_guard import GuardViolation
from mission_control.graph import (
    PUSH_BLOCKED,
    _Deps,
    _apply_burn,
    _dispatch,
    _run_worker,
    _teardown,
    build_plans_store,
    postgres_checkpointer,
)
from mission_control.runs_store import STATUS_APPLIED, STATUS_BLOCKED_SECRETS
from mission_control.tasks import Task
from mission_control.worker import WorkerResult


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


# -- the pure scanner: precision over recall -------------------------------

_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"[:16]          # AKIA + 16 → AKIA-shaped
_PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc...\n-----END RSA PRIVATE KEY-----"


@pytest.mark.parametrize("text,rule", [
    (f"aws_key = {_AWS_KEY}", "aws-access-key-id"),
    (_PRIVATE_KEY, "private-key"),
    ("PASSWORD=hunter2secret", "secret-assignment"),
    ("api_key: sk-abcdef123456", "secret-assignment"),
    ("db = postgres://user:s3cr3tpw@db.internal:5432/app", "connection-string-password"),
    ("token xoxb-123456789012-abcdefABCDEF", "slack-token"),
    ("ssn 123-45-6789 on file", "pii-ssn"),
])
def test_scan_flags_high_signal_secrets(text, rule):
    findings = content_guard.scan_text(text, file="f")
    assert any(f.rule == rule for f in findings), f"expected {rule} in {[f.rule for f in findings]}"
    # The raw secret is never reproduced in the finding.
    assert all("hunter2secret" not in f.excerpt and "s3cr3tpw" not in f.excerpt for f in findings)


@pytest.mark.parametrize("text", [
    "This unit adds a password reset flow for users.",     # prose mentioning 'password'
    "## Requirements\n\n- scope: bound to the ingestion path",
    "title: Configure the api_key rotation policy",         # 'api_key' as prose, no assignment
    "See docs/architecture.md for the component map.",
    "phase: CONSTRUCTION\ndepends_on: [1]\nstatus: pending",
])
def test_scan_passes_clean_spec_prose(text):
    assert content_guard.scan_text(text, file="f") == []


@pytest.mark.parametrize("text", [
    "const secret = process.env.JWT_SECRET",              # env read (the secure pattern)
    "  clientSecret: config.get('client_secret'),",       # config read + call
    "  password: string;",                                # a TS type annotation, not a value
    "const secret = deriveKey(salt, iterations)",         # a computed value (call), not a literal
    "DATABASE_URL=postgres://strata:strata@localhost:5432/strata",   # local DSN, pw == user
    'url = "postgres://user:password@db:5432/app"',       # dummy password + container host
    "redis://user:pass@127.0.0.1:6379",                   # loopback dev DSN
])
def test_scan_skips_generated_code_false_positives(text):
    """The tuned guard does not flag real application source: secrets read from env/config,
    type annotations, computed values, or local/dev/example connection strings."""
    assert content_guard.scan_text(text, file="f") == []


@pytest.mark.parametrize("text,rule", [
    ('const KEY = "sk-live-abcdef123456"', "secret-assignment"),          # hardcoded literal
    ("db = postgres://admin:Xk9zP2qW@prod-db.corp.net:5432/app",          # real remote DSN
     "connection-string-password"),
])
def test_scan_still_flags_real_hardcoded_secrets(text, rule):
    """Tuning must not blind the guard: a hardcoded credential literal or a real remote
    connection string is still caught."""
    findings = content_guard.scan_text(text, file="f")
    assert any(f.rule == rule for f in findings), [f.rule for f in findings]


# -- burn output blocked at the commit boundary (distinct terminal state) ---

class _SecretWorker:
    """A worker that writes a file containing a planted secret into the worktree."""

    def __init__(self, content: str, filename: str = "config.txt") -> None:
        self._content = content
        self._filename = filename

    def investigate(self, task: Task, workdir: Path) -> WorkerResult:
        (Path(workdir) / self._filename).write_text(self._content)
        return WorkerResult(summary="wrote config", made_changes=True, steps=[])


@pytest.fixture
def remote(tmp_path):
    work = tmp_path / "seed"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    (work / "README.md").write_text("# seed\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "trunk")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(work), str(bare)], check=True, capture_output=True)
    _git(bare, "remote", "remove", "origin")
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    return bare


def _burn_state(rid="burn-guard"):
    return {"run_id": rid, "task_id": rid, "task_type": roles.BURN,
            "prompt": "change", "decision": roles.GO}


def test_burn_with_secret_is_blocked_and_nothing_pushed(remote, tmp_path, mem_store):
    cache = tmp_path / "cache"
    deps = _Deps(None, _SecretWorker(f"AWS_KEY={_AWS_KEY}\n"), target_ref=str(remote),
                 cache_root=cache, runs_store=mem_store)
    st = _burn_state()
    st.update(_dispatch(deps, st))
    st.update(_run_worker(deps, st))
    before = _git(remote, "rev-parse", "refs/heads/main").strip()

    st.update(_apply_burn(deps, st))
    assert st["push_status"] == PUSH_BLOCKED
    assert "config.txt" in st["push_detail"] and "aws-access-key-id" in st["push_detail"]
    assert st.get("applied") is False
    assert _git(remote, "rev-parse", "refs/heads/main").strip() == before   # nothing pushed

    st.update(_teardown(deps, st))
    assert mem_store.get_run("burn-guard").status == STATUS_BLOCKED_SECRETS
    local = project_ref.cache_dir_for(str(remote), root=cache).resolve()
    assert len(worktree.list_worktrees(local)) == 1        # no leak


def test_clean_burn_passes_through(remote, tmp_path, mem_store):
    cache = tmp_path / "cache"
    deps = _Deps(None, _SecretWorker("just some ordinary config: level=info\n"),
                 target_ref=str(remote), cache_root=cache, runs_store=mem_store)
    st = _burn_state("burn-clean")
    st.update(_dispatch(deps, st))
    st.update(_run_worker(deps, st))
    st.update(_apply_burn(deps, st))
    assert st["push_status"] == "pushed" and st["applied"] is True
    st.update(_teardown(deps, st))
    assert mem_store.get_run("burn-clean").status == STATUS_APPLIED


def test_override_allows_push_and_records_audit(remote, tmp_path, mem_store):
    cache = tmp_path / "cache"
    deps = _Deps(None, _SecretWorker(f"AWS_KEY={_AWS_KEY}\n"), target_ref=str(remote),
                 cache_root=cache, runs_store=mem_store)
    st = _burn_state("burn-override")
    st["allow_secrets"] = True                              # explicit operator ack
    st.update(_dispatch(deps, st))
    st.update(_run_worker(deps, st))
    st.update(_apply_burn(deps, st))

    assert st["push_status"] == "pushed" and st["applied"] is True   # override let it through
    assert "OVERRIDDEN" in st["guard_override"]              # audit note recorded on the run
    st.update(_teardown(deps, st))
    row = mem_store.get_run("burn-override")
    assert row.status == STATUS_APPLIED
    assert "OVERRIDDEN" in (row.detail or "")               # auditable in the ledger


# -- plan docs commit is guarded too ---------------------------------------

@pytest.fixture
def pg_store():
    try:
        _cp, pool = postgres_checkpointer(setup=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable (run `docker compose up -d`): {e}")
    plan_store = build_plans_store(pool, setup=True)
    try:
        yield plan_store
    finally:
        pool.close()


def test_plan_docs_with_secret_requirement_is_blocked(pg_store, remote, tmp_path):
    ref, _ = project_ref.resolve_target(str(remote))
    pid = f"plan-{uuid4().hex}"
    pg_store.open_plan(pid, target=ref, local_path=None, mode="brownfield",
                       methodology="aidlc", cloud_target="aws")
    # A requirement value carrying a planted secret → the docs commit must be blocked.
    pg_store.upsert_requirement(pid, "api_key", value=f"api_key={_AWS_KEY}", state="ready")
    with pytest.raises(GuardViolation):
        plan_docs.sync_to_repo(pg_store, pid, cache_root=tmp_path / "cache")
    # Nothing was pushed: the remote has no aidlc-docs yet.
    tree = _git(remote, "ls-tree", "-r", "--name-only", "refs/heads/main").split()
    assert not any(t.startswith("aidlc-docs/") for t in tree)


# -- bootstrap seeds a .gitignore for secret files -------------------------

def test_bootstrap_seeds_gitignore_for_secret_files(tmp_path):
    dest = tmp_path / "created-remote.git"
    scratch = tmp_path / "scratch"
    repo_source.bootstrap_remote(str(dest), scratch)
    fresh = tmp_path / "clone"
    subprocess.run(["git", "clone", str(dest), str(fresh)], check=True, capture_output=True)
    gitignore = (fresh / ".gitignore").read_text()
    for shape in (".env", "*.pem", "credentials"):
        assert shape in gitignore
