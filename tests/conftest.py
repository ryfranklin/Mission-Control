"""Shared fixtures: a throwaway git repo target, plus an in-memory runs store and a
service factory for host-runnable (no-Docker) service/CLI tests."""

from __future__ import annotations

import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import pytest


def pytest_configure(config) -> None:
    """Isolate the whole suite onto a dedicated ``*_test`` Postgres database, so running
    ``pytest`` NEVER reads/writes the operator's working DB (the live seam). We derive
    the test DB name from ``MC_POSTGRES_URL`` (suffix ``_test``), create it if absent,
    and point the session's ``MC_POSTGRES_URL`` at it — every store the tests build
    (via ``postgres_checkpointer``) then lands there. Honor an explicit
    ``MC_TEST_POSTGRES_URL`` override. A no-op if Postgres is unreachable (those tests
    skip anyway).

    Also make the suite HERMETIC: the runtime reads deployment config from ``MC_*`` env
    (``MC_API_TOKEN`` gating the mutating endpoints, ``MC_SLACK_REGISTRY`` /
    ``MC_DEFAULT_SLACK_PROFILE`` / ``MC_COST_ALERT_*``, …) via ``*.from_env()``. A
    developer running an operator box has these set in their shell, which would leak into
    the tests — e.g. an ambient ``MC_API_TOKEN`` makes every unauthenticated ``approve``
    a 401, so a gated run never resumes and its SSE feed tails forever (a hang, not a
    failure). Clear every ``MC_*`` var except the Postgres URLs this isolation needs, so
    the suite builds clean services regardless of shell. Tests that WANT config inject it
    explicitly (``create_app(auth_token=…)``, ``RunManager(slack_registry=…)``)."""
    _keep = {"MC_POSTGRES_URL", "MC_TEST_POSTGRES_URL"}
    for var in [k for k in os.environ if k.startswith("MC_") and k not in _keep]:
        del os.environ[var]
    if os.environ.get("MC_TEST_POSTGRES_URL"):
        os.environ["MC_POSTGRES_URL"] = os.environ["MC_TEST_POSTGRES_URL"]
        return
    base = os.environ.get("MC_POSTGRES_URL") or \
        "postgresql://mc:mc@localhost:5432/mission_control?sslmode=disable"
    parts = urlsplit(base)
    db = (parts.path.lstrip("/") or "mission_control")
    if db.endswith("_test"):
        return  # already isolated
    test_db = db + "_test"
    test_url = urlunsplit(parts._replace(path="/" + test_db))
    try:
        import psycopg

        admin = urlunsplit(parts._replace(path="/postgres"))
        with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (test_db,)).fetchone()
            if not exists:
                conn.execute(f'CREATE DATABASE "{test_db}"')
    except Exception:  # noqa: BLE001 — no Postgres → the pg-backed tests skip themselves
        pass
    os.environ["MC_POSTGRES_URL"] = test_url


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def target_repo(tmp_path: Path) -> Path:
    """A fresh git repo with one commit on ``main`` — the target for worktrees."""
    repo = tmp_path / "target"
    repo.mkdir()
    _run(repo, "init", "-b", "main")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# target\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "init")
    return repo


class FakeJudge:
    """Offline stand-in for LlmJudge — canned score + priced (Opus) usage, no LLM."""

    model = "claude-opus-4-8"

    def score(self, *, task_prompt, worker_output, rubric):
        from mission_control.judge import JudgeResult
        from mission_control.telemetry import StepUsage

        return JudgeResult(
            score=0.8,
            rationale="fake judge",
            usage=StepUsage(
                model="claude-opus-4-8",
                input_tokens=100,
                output_tokens=50,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                latency_ms=10,
            ),
            per_criterion=[],
        )


@pytest.fixture
def fake_judge() -> FakeJudge:
    return FakeJudge()


# -- in-memory runs store + service factory (host-runnable, no Docker) --------

class InMemoryRunStore:
    """A mock of :class:`~mission_control.runs_store.RunStore` — the same method
    surface (idempotent upsert-by-run_id, once-only stamps, absolute cost, a durable
    per-run event log) backed by dicts. Returns real ``RunRow`` objects so the
    service's responses match production. Sharing ONE instance across two managers
    simulates a durable store surviving a service restart."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}
        self._events: dict[str, list[dict]] = {}
        # The fleet-wide notification outbox: a global monotonic seq across ALL runs,
        # deduped on (run_id, kind) for once-only milestones — mirrors the pg table.
        self._notifications: list[dict] = []
        self._notify_seq = 0
        self._lock = threading.Lock()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _ensure(self, run_id: str) -> dict:
        return self._rows.setdefault(run_id, {
            "run_id": run_id, "thread_id": run_id, "target": None, "local_path": None,
            "task_type": None, "status": "queued", "cost_usd": 0.0,
            "created_at": self._now(), "started_at": None, "ended_at": None,
            "detail": None, "plan_id": None, "plan_unit_seq": None,
            "changes_json": None, "slack_profile": None,
        })

    # transitions ---------------------------------------------------------
    def launch(self, run_id, *, task_type=None, target=None, local_path=None,
               plan_id=None, plan_unit_seq=None, subject=None, slack_profile=None):
        with self._lock:
            if run_id not in self._rows:
                row = self._ensure(run_id)
                row["task_type"], row["target"] = task_type, target
                row["local_path"] = local_path
                row["plan_id"], row["plan_unit_seq"] = plan_id, plan_unit_seq
                row["subject"] = subject
                row["slack_profile"] = slack_profile

    def mark_running(self, run_id, *, target=None, local_path=None):
        with self._lock:
            row = self._ensure(run_id)
            row["status"] = "running"
            row["started_at"] = row["started_at"] or self._now()
            row["target"] = target or row["target"]
            row["local_path"] = local_path or row["local_path"]

    def mark_awaiting_gate(self, run_id):
        with self._lock:
            self._ensure(run_id)["status"] = "awaiting_gate"

    def set_changes(self, run_id, changes):
        with self._lock:
            self._ensure(run_id)["changes_json"] = changes

    def finish(self, run_id, *, status, cost_usd, detail=None):
        with self._lock:
            row = self._ensure(run_id)
            row["status"], row["cost_usd"] = status, cost_usd
            row["detail"] = detail or row["detail"]
            row["ended_at"] = row["ended_at"] or self._now()

    def mark_failed(self, run_id, detail):
        with self._lock:
            row = self._ensure(run_id)
            row["status"], row["detail"] = "failed", detail
            row["ended_at"] = row["ended_at"] or self._now()

    # queries -------------------------------------------------------------
    def get_run(self, run_id):
        from mission_control.runs_store import RunRow
        with self._lock:
            row = self._rows.get(run_id)
            return RunRow(**row) if row else None

    def _matches(self, row, filter, created_from, created_to) -> bool:
        for k, v in (filter or {}).items():
            if v is not None and row.get(k) != v:
                return False
        if created_from is not None and row["created_at"] < created_from:
            return False
        if created_to is not None and row["created_at"] >= created_to:
            return False
        return True

    def list_runs(self, filter=None, *, limit=100, offset=0, order="desc",
                  created_from=None, created_to=None):
        from mission_control.runs_store import RunRow
        with self._lock:
            rows = [r for r in self._rows.values()
                    if self._matches(r, filter, created_from, created_to)]
        rows.sort(key=lambda r: r["created_at"], reverse=(str(order).lower() != "asc"))
        return [RunRow(**r) for r in rows[offset:offset + limit]]

    def count_runs(self, filter=None, *, created_from=None, created_to=None):
        with self._lock:
            return sum(1 for r in self._rows.values()
                       if self._matches(r, filter, created_from, created_to))

    def cost_summary(self, filter=None, *, created_from=None, created_to=None):
        with self._lock:
            rows = [r for r in self._rows.values()
                    if self._matches(r, filter, created_from, created_to)]
            run_ids = {r["run_id"] for r in rows}
            steps = sum(1 for rid, log in self._events.items() if rid in run_ids
                        for e in log if e["event"] == "step_metric")
        by_tt, by_tg = {}, {}
        for r in rows:
            tt = by_tt.setdefault(r["task_type"], {"runs": 0, "cost_usd": 0.0})
            tt["runs"] += 1
            tt["cost_usd"] += r["cost_usd"]
            tg = by_tg.setdefault(r["target"], {"runs": 0, "cost_usd": 0.0})
            tg["runs"] += 1
            tg["cost_usd"] += r["cost_usd"]
        return {
            "runs": len(rows),
            "cost_usd": round(sum(r["cost_usd"] for r in rows), 8),
            "steps": steps,
            "by_task_type": [{"task_type": k, "runs": v["runs"], "cost_usd": round(v["cost_usd"], 8)}
                             for k, v in sorted(by_tt.items()) if k],
            "by_target": [{"target": k, "runs": v["runs"], "cost_usd": round(v["cost_usd"], 8)}
                          for k, v in sorted(by_tg.items(), key=lambda kv: -kv[1]["cost_usd"]) if k],
        }

    def list_targets(self):
        with self._lock:
            targets = {r["target"] for r in self._rows.values() if r["target"]}
        return sorted(targets)

    def profile_digest(self, slack_profile, *, created_from=None, top_n=5):
        with self._lock:
            rows = [r for r in self._rows.values()
                    if r.get("slack_profile") == slack_profile
                    and (created_from is None or r["created_at"] >= created_from)]
        by_status, by_target = {}, {}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            if r["target"]:
                tg = by_target.setdefault(r["target"], {"runs": 0, "cost_usd": 0.0})
                tg["runs"] += 1
                tg["cost_usd"] += r["cost_usd"]
        top = sorted(by_target.items(), key=lambda kv: -kv[1]["cost_usd"])[:top_n]
        return {
            "profile": slack_profile,
            "runs": len(rows),
            "cost_usd": round(sum(r["cost_usd"] for r in rows), 8),
            "by_status": by_status,
            "top_targets": [{"target": k, "runs": v["runs"], "cost_usd": round(v["cost_usd"], 8)}
                            for k, v in top],
        }

    # durable event log ---------------------------------------------------
    def append_event(self, run_id, seq, event_type, data):
        with self._lock:
            log = self._events.setdefault(run_id, [])
            if not any(e["seq"] == seq for e in log):
                log.append({"seq": seq, "event": event_type, "data": data})

    def read_events(self, run_id, *, after_seq=None):
        with self._lock:
            log = sorted(self._events.get(run_id, []), key=lambda e: e["seq"])
            return [dict(e) for e in log if after_seq is None or e["seq"] > after_seq]

    def max_event_seq(self, run_id):
        with self._lock:
            log = self._events.get(run_id, [])
            return max((e["seq"] for e in log), default=-1)

    # notification outbox -------------------------------------------------
    def append_notification(self, run_id, kind, *, slack_profile=None, payload):
        from mission_control.runs_store import _ONCE_ONLY_KINDS
        with self._lock:
            if kind in _ONCE_ONLY_KINDS and any(
                n["run_id"] == run_id and n["kind"] == kind for n in self._notifications
            ):
                return False
            self._notify_seq += 1
            self._notifications.append({
                "seq": self._notify_seq, "run_id": run_id,
                "slack_profile": slack_profile, "kind": kind, "payload": payload,
                "created_at": self._now(),
            })
            return True

    def read_notifications(self, *, after_seq=0, limit=100):
        with self._lock:
            rows = sorted(self._notifications, key=lambda n: n["seq"])
            return [dict(n) for n in rows if n["seq"] > after_seq][:limit]

    def notifications_summary(self):
        with self._lock:
            return {"total": len(self._notifications),
                    "last_seq": max((n["seq"] for n in self._notifications), default=0)}


@pytest.fixture
def mem_store() -> InMemoryRunStore:
    return InMemoryRunStore()


@pytest.fixture
def make_service(tmp_path):
    """Factory: build a TestClient over the real service/graph/worker with a given
    (mock) store. Call twice with the SAME store to simulate a restart. Cleans up
    every client it makes."""
    from fastapi.testclient import TestClient
    from langgraph.checkpoint.memory import MemorySaver

    from mission_control.service import RunManager, create_app
    from mission_control.worker import StubWorker

    created = []
    counter = {"n": 0}

    def _make(store, *, worker_factory=None, telemetry_dir=None, slack_registry=None,
              checkpointer=None, cost_alerts=None, default_slack_profile=None,
              auth_token=None):
        counter["n"] += 1
        # A shared checkpointer + shared store = the durable substrate; a fresh manager
        # with empty channels = the restarted process. Pass the SAME checkpointer to two
        # calls to simulate a restart that can still RESUME (not just replay events).
        manager = RunManager(
            checkpointer=checkpointer or MemorySaver(),
            runs_store=store,
            worker_factory=worker_factory or (lambda: StubWorker()),
            telemetry_dir=telemetry_dir or (tmp_path / f"telemetry-{counter['n']}"),
            slack_registry=slack_registry,
            cost_alerts=cost_alerts,
            default_slack_profile=default_slack_profile,
        )
        client = TestClient(create_app(manager, auth_token=auth_token))
        client.__enter__()
        created.append(client)
        return client

    yield _make
    for client in created:
        client.__exit__(None, None, None)
