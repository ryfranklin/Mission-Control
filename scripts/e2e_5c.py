"""Phase 5c end-to-end harness — the full flow against the REAL service + REAL bridge.

What is real here: the service (FastAPI + RunManager + graph + StubWorker), the durable
Postgres notification outbox (global IDENTITY seq), the bridge's poll/route/cursor loop,
the durable cursor file, per-profile authorization + the one-shot gate, and alert
emission. The bridge's ServiceClient talks to the app over an in-process ASGI transport
(real HTTP endpoints). ONLY the Slack egress is faked — no org Slack app / tokens are
available in this environment, so the bot Web client + Socket Mode handler are recording
fakes. That boundary is called out honestly in PHASE5C_FINDINGS.md.

Two profiles stand in for two workspaces (config from a registry file + env token-var
names): A = "acme-eng" (#eng, approver U_ALICE), B = "acme-ops" (#ops, approver U_BOB).

Run:  .venv/bin/python scripts/e2e_5c.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import psycopg

# -- clean, isolated Postgres DB (never touch the operator's working DB) ------
_ADMIN = "postgresql://mc:mc@localhost:5432/postgres?sslmode=disable"
_DB = "mission_control_e2e5c"
with psycopg.connect(_ADMIN, autocommit=True) as _c:
    _c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (_DB,))
    _c.execute(f'DROP DATABASE IF EXISTS "{_DB}"')
    _c.execute(f'CREATE DATABASE "{_DB}"')
os.environ["MC_POSTGRES_URL"] = f"postgresql://mc:mc@localhost:5432/{_DB}?sslmode=disable"

from mission_control import roles  # noqa: E402
from mission_control.graph import build_runs_store, postgres_checkpointer  # noqa: E402
from mission_control.service import RunManager, create_app  # noqa: E402
from mission_control.service.alerts import CostAlertConfig  # noqa: E402
from mission_control.slack.bridge import ServiceClient, SlackBridge, resolve_active_profiles  # noqa: E402
from mission_control.slack.cursor import CursorStore  # noqa: E402
from mission_control.slack_registry import SlackRegistry  # noqa: E402
from mission_control.worker import StubWorker  # noqa: E402

# -- capture the bridge's log lines (skips + AUDIT) ---------------------------
LOG_LINES: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record):
        LOG_LINES.append(record.getMessage())


logging.getLogger("mission_control.slack.bridge").addHandler(_Capture())
logging.getLogger("mission_control.slack.bridge").setLevel(logging.INFO)


# -- fake Slack egress (records what WOULD be posted) -------------------------
class FakeBot:
    def __init__(self, token=None):
        self.token = token
        self.posts: list[dict] = []
        self.updates: list[dict] = []

    async def chat_postMessage(self, **kw):
        ts = f"ts-{len(self.posts) + 1}"
        rec = dict(kw)
        rec["ts"] = ts
        self.posts.append(rec)
        return {"ok": True, "ts": ts}

    async def chat_update(self, **kw):
        self.updates.append(kw)
        return {"ok": True}


class FakeApp:
    def __init__(self, bot_token=None):
        self.actions: list[str] = []
        self.commands: list[str] = []

    def action(self, aid):
        self.actions.append(aid)
        return lambda fn: fn

    def command(self, name):
        self.commands.append(name)
        return lambda fn: fn


class FakeHandler:
    def __init__(self, app, app_token):
        self.app, self.app_token = app, app_token
        self.connected = False

    async def connect_async(self):
        self.connected = True

    async def close_async(self):
        self.connected = False


class Recorder:
    def __init__(self):
        self.msgs: list = []

    async def __call__(self, text=None, **kw):
        self.msgs.append(text if text is not None else kw)

    @property
    def text(self):
        return " | ".join(str(m) for m in self.msgs).lower()


def git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    g = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)  # noqa
    g("init", "-b", "main"); g("config", "user.email", "e@x.com"); g("config", "user.name", "E")
    (path / "README.md").write_text("# t\n"); g("add", "-A"); g("commit", "-m", "init")
    return path


def posts_of(bot: FakeBot) -> list[str]:
    """The notification KIND rendered in each post (from the header/text)."""
    out = []
    for p in bot.posts:
        blob = json.dumps(p)
        if "left the pad" in blob: out.append("run_launched")
        elif "holding at go/no-go" in blob: out.append("gate_awaiting")
        elif "Cost alert" in blob: out.append("cost_threshold")
        elif "regression" in blob.lower(): out.append("regression")
        elif "Fleet digest" in blob: out.append("digest")
        else: out.append("run_terminal")
    return out


def find_gate_post(bot: FakeBot):
    for p in bot.posts:
        if "mc_go" in json.dumps(p):
            return p
    return None


SUMMARY: dict = {}


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="e2e5c-"))
    target_a = git_repo(tmp / "target-a")
    target_b = git_repo(tmp / "target-b")

    # --- registry (config) + env token var NAMES (values are dummy; egress faked) ---
    registry_path = tmp / "slack_registry.json"
    registry_path.write_text(json.dumps({"profiles": [
        {"name": "acme-eng", "channel": "#eng", "approvers": ["U_ALICE"],
         "token_env": "TOK_ENG_BOT", "app_token_env": "TOK_ENG_APP"},
        {"name": "acme-ops", "channel": "#ops", "approvers": ["U_BOB"],
         "token_env": "TOK_OPS_BOT", "app_token_env": "TOK_OPS_APP"},
    ]}))
    full_env = {"TOK_ENG_BOT": "xoxb-eng", "TOK_ENG_APP": "xapp-eng",
                "TOK_OPS_BOT": "xoxb-ops", "TOK_OPS_APP": "xapp-ops"}
    registry = SlackRegistry.from_path(registry_path)
    A, B = "acme-eng", "acme-ops"

    # --- the durable service (Postgres outbox + checkpointer) ---
    checkpointer, pool = postgres_checkpointer(setup=True)
    store = build_runs_store(pool, setup=True)

    def build_service():
        mgr = RunManager(checkpointer=checkpointer, runs_store=store,
                         worker_factory=lambda: StubWorker(),
                         slack_registry=registry,
                         cost_alerts=CostAlertConfig(per_run=1e-9))  # tiny → any run crosses
        return mgr, create_app(mgr)

    manager, app = build_service()
    cursor_path = tmp / "cursor"

    async def _noop_validate(bot_client, app_token):  # fake tokens never hit real Slack
        return None

    async def build_bridge(app_obj, env=full_env):
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app_obj), base_url="http://svc")
        profiles = await resolve_active_profiles(registry, env=env,
                                                 client_factory=lambda t: FakeBot(t),
                                                 validate=_noop_validate)
        br = SlackBridge(profiles=profiles, service=ServiceClient("http://svc", http),
                         cursor=CursorStore(cursor_path),
                         app_factory=lambda bot: FakeApp(bot),
                         socket_factory=lambda a, t: FakeHandler(a, t))
        return br, http

    bridge, http = await build_bridge(app)
    await bridge.connect()
    SUMMARY["profiles_active"] = sorted(bridge.active_profiles)
    SUMMARY["buttons_registered"] = sorted(bridge.active_profiles[A].app.actions)
    SUMMARY["slash_registered"] = bridge.active_profiles[A].app.commands
    SUMMARY["handlers_connected"] = all(p.handler.connected for p in bridge.active_profiles.values())

    async def launch(target, task_type, profile):
        r = await http.post("/runs", json={"target": str(target), "task_type": task_type,
                                           **({"slack_profile": profile} if profile else {})})
        r.raise_for_status()
        return r.json()["run_id"]

    async def gj(path, **params):
        r = await http.get(path, params=params or None)
        r.raise_for_status()
        return r.json()

    async def wait_status(run_id, wanted, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = (await http.get(f"/runs/{run_id}")).json()["status"]
            if s in wanted:
                return s
            await asyncio.sleep(0.03)
        raise SystemExit(f"{run_id} stuck; last={s}")

    bot_a = bridge.active_profiles[A].bot_client
    bot_b = bridge.active_profiles[B].bot_client

    # ============ (0) OPT-IN DEFAULT: no profile → silent ============
    t0 = time.time()
    silent = await launch(target_a, roles.SIM, None)
    await wait_status(silent, {"done"})
    await bridge.poll_once()
    SUMMARY["null_profile_posts_A"] = len(bot_a.posts)
    SUMMARY["null_profile_posts_B"] = len(bot_b.posts)  # both must be 0

    # ============ (A) FULL BURN on profile A ============
    burn_a = await launch(target_a, roles.BURN, A)
    await wait_status(burn_a, {"awaiting_gate"})
    t_gate = time.time()
    await bridge.poll_once()                     # → run_launched + gate_awaiting to A
    SUMMARY["A_after_gate_poll"] = posts_of(bot_a)
    SUMMARY["A_pull_latency_s"] = round(time.time() - t_gate, 3)
    gate_post = find_gate_post(bot_a)
    SUMMARY["A_gate_has_buttons"] = gate_post is not None
    SUMMARY["A_gate_threaded_root"] = bot_a.posts[0]["ts"]  # launch is root
    chan, ts = gate_post["channel"], gate_post["ts"]

    # --- BRIDGE RESTART mid-flight (run holding at the gate) ---
    cur_before = CursorStore(cursor_path).get()
    await bridge.aclose()
    await http.aclose()
    bridge, http = await build_bridge(app)       # fresh process: same cursor + store
    await bridge.connect()
    reopened = sorted(bridge.active_profiles)
    n_new = await bridge.poll_once()             # nothing new past the cursor
    bot_a = bridge.active_profiles[A].bot_client  # fresh recorder
    bot_b = bridge.active_profiles[B].bot_client
    SUMMARY["bridge_restart"] = {"cursor": cur_before, "reopened": reopened,
                                 "new_posts_after_restart": n_new,
                                 "A_reposts": len(bot_a.posts)}

    # --- authorization negatives (run still at the gate) ---
    r_bad = await bridge.handle_gate_action(profile_name=A, decision=roles.GO, run_id=burn_a,
                                            user_id="U_RANDO", respond=Recorder(),
                                            client=bot_a, channel=chan, message_ts=ts)
    runs_after_bad = (await http.get(f"/runs/{burn_a}")).json()["status"]
    r_cross = await bridge.handle_gate_action(profile_name=B, decision=roles.GO, run_id=burn_a,
                                              user_id="U_BOB", respond=Recorder(),
                                              client=bot_b, channel="#ops", message_ts="x")
    runs_after_cross = (await http.get(f"/runs/{burn_a}")).json()["status"]

    # --- allowlisted GO resolves once ---
    r_go = await bridge.handle_gate_action(profile_name=A, decision=roles.GO, run_id=burn_a,
                                           user_id="U_ALICE", respond=Recorder(),
                                           client=bot_a, channel=chan, message_ts=ts)
    applied = await wait_status(burn_a, {"applied", "push_rejected", "done"})
    # --- double-click AFTER resolution → one-shot conflict ---
    conflict_respond = Recorder()
    r_dbl = await bridge.handle_gate_action(profile_name=A, decision=roles.NO_GO, run_id=burn_a,
                                            user_id="U_ALICE", respond=conflict_respond,
                                            client=bot_a, channel=chan, message_ts=ts)
    SUMMARY["A_gate_authz"] = {
        "non_allowlisted": r_bad, "status_unchanged_after_bad": runs_after_bad,
        "cross_profile_B_on_A_run": r_cross, "status_unchanged_after_cross": runs_after_cross,
        "allowlisted_go": r_go, "terminal_status": applied,
        "double_click": r_dbl, "double_click_reply": conflict_respond.text[:60],
        "gate_message_updated": len(bot_a.updates) >= 1,
    }

    # --- terminal + cost alert + regression to A ---
    await bridge.poll_once()                     # → run_terminal + cost_threshold
    reg = {"passed": False, "k": 2, "n": 3, "axes": {
        "quality_total": {"current": 0.61, "baseline_mean": 0.80, "baseline_stddev": 0.05,
                          "threshold": 0.70, "higher_is_worse": False, "regressed": True},
        "cost_usd": {"current": 0.3, "baseline_mean": 0.28, "baseline_stddev": 0.02,
                     "threshold": 0.32, "higher_is_worse": True, "regressed": False}},
        "runs": [{"eval_output": "EVAL-CONTENT-must-not-leak"}]}
    emitted = (await http.post(f"/runs/{burn_a}/alerts/regression", json=reg)).json()["emitted"]
    await bridge.poll_once()                     # → regression to A
    SUMMARY["A_all_posts"] = posts_of(bot_a)
    SUMMARY["A_regression_emitted"] = emitted
    SUMMARY["B_posts_during_A_flow"] = len(bot_b.posts)  # must be 0 (isolation)

    # ============ (B) SMALLER FLOW on profile B ============
    burn_b = await launch(target_b, roles.BURN, B)
    await wait_status(burn_b, {"awaiting_gate"})
    await bridge.poll_once()
    gate_b = find_gate_post(bot_b)
    # B's allowlist governs B: A's approver may NOT resolve a B-run on B's connection.
    r_a_on_b = await bridge.handle_gate_action(profile_name=B, decision=roles.GO, run_id=burn_b,
                                               user_id="U_ALICE", respond=Recorder(),
                                               client=bot_b, channel=gate_b["channel"],
                                               message_ts=gate_b["ts"])
    r_b_ok = await bridge.handle_gate_action(profile_name=B, decision=roles.GO, run_id=burn_b,
                                             user_id="U_BOB", respond=Recorder(),
                                             client=bot_b, channel=gate_b["channel"],
                                             message_ts=gate_b["ts"])
    await wait_status(burn_b, {"applied", "push_rejected", "done"})
    await bridge.poll_once()
    SUMMARY["B_flow"] = {"posts": posts_of(bot_b),
                         "A_approver_on_B_run": r_a_on_b, "B_approver_go": r_b_ok}
    # Isolation: A's recorder is unchanged by anything in B's flow.
    SUMMARY["B_isolation_A_untouched"] = posts_of(bot_a) == SUMMARY["A_all_posts"]

    # ============ per-profile DIGEST ============
    posted = await bridge.post_digests(window_hours=24)
    dig_a = await gj("/notifications/digest", profile=A)
    dig_b = await gj("/notifications/digest", profile=B)
    SUMMARY["digest"] = {"posted": posted, "A_runs": dig_a["runs"], "B_runs": dig_b["runs"],
                         "A_by_status": dig_a["by_status"]}

    # ============ metadata-only audit (every payload on the wire) ============
    all_notes = (await gj("/notifications", limit=500))["notifications"]
    allowed = {"target", "task_type", "status", "cost_usd", "node", "created_at", "started_at",
               "ended_at", "reason", "threshold", "budget", "window_total", "window_hours",
               "axes", "k", "n"}
    axis_allowed = {"metric", "current", "baseline_mean", "baseline_stddev", "threshold",
                    "higher_is_worse", "regressed"}
    violations = []
    for n in all_notes:
        extra = set(n["payload"]) - allowed
        if extra:
            violations.append((n["kind"], sorted(extra)))
        for ax in (n["payload"].get("axes") or []):
            if set(ax) - axis_allowed:
                violations.append((n["kind"], "axis:" + str(sorted(set(ax) - axis_allowed))))
    leak = any("EVAL-CONTENT-must-not-leak" in json.dumps(n) for n in all_notes)
    SUMMARY["metadata_audit"] = {"notes": len(all_notes), "field_violations": violations,
                                 "eval_content_leak": leak}
    SUMMARY["outbox_kind_counts"] = _counts([n["kind"] for n in all_notes])

    # ============ SERVICE RESTART (durable outbox + catch-up) ============
    last_seq_before = (await gj("/notifications", after=0, limit=500))["last_seq"]
    await manager.aclose()
    await http.aclose()
    manager, app = build_service()               # new service process over the SAME Postgres
    bridge2, http = await build_bridge(app)
    await bridge2.connect()
    # A brand-new run on the restarted service appends to the durable outbox.
    post_restart_run = await launch(target_a, roles.SIM, A)
    await wait_status(post_restart_run, {"done"})
    n_caught = await bridge2.poll_once()          # posts ONLY the new rows, no pre-restart dupes
    bot_a2 = bridge2.active_profiles[A].bot_client
    total_after = (await gj("/notifications", after=0, limit=1))["total"]
    SUMMARY["service_restart"] = {
        "outbox_last_seq_before": last_seq_before,
        "outbox_total_survived": total_after,      # >= the count seen before the restart
        "bridge_caught_up_new_posts": n_caught,    # only the post-restart run's kinds
        "A_posts_after_restart_kinds": posts_of(bot_a2),
    }

    # ============ SINGLE-TOKEN MACHINE (multi-machine posture) ============
    LOG_LINES.clear()
    one_env = {"TOK_ENG_BOT": "xoxb-eng", "TOK_ENG_APP": "xapp-eng"}  # only A's tokens present
    one = await resolve_active_profiles(registry, env=one_env,
                                        client_factory=lambda t: FakeBot(t), validate=_noop_validate)
    skip_lines = [ln for ln in LOG_LINES if "skipped" in ln]
    SUMMARY["single_token_machine"] = {"active": sorted(one),
                                       "skipped_log": skip_lines[:1]}

    await bridge2.aclose(); await http.aclose(); pool.close()
    print(json.dumps(SUMMARY, indent=2, default=str))


def _counts(items):
    out: dict = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out


if __name__ == "__main__":
    asyncio.run(main())
