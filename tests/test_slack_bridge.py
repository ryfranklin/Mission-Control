"""Phase 5c: the Slack bridge — a separate long-running client that routes the global
notification outbox to per-profile Slack channels over Socket Mode.

The Slack clients (bot Web client + Socket Mode handler) and the service's
``/notifications`` feed are mocked; a fixture registry supplies profiles. No network,
no real Slack, no FastAPI app — the bridge is exercised as the standalone process it is.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from mission_control import roles
from mission_control.runs_store import (
    NOTIFY_COST_THRESHOLD,
    NOTIFY_GATE_AWAITING,
    NOTIFY_REGRESSION,
    NOTIFY_RUN_LAUNCHED,
    NOTIFY_RUN_TERMINAL,
)
from mission_control.slack import (
    ActiveProfile,
    BridgeConfigError,
    SlackBridge,
    assemble,
    build_message,
    resolve_active_profiles,
)
from mission_control.slack.bridge import DECISION_CANCEL, GateRelay, SLASH_COMMAND
from mission_control.slack.cursor import CursorStore
from mission_control.slack.message import ACTION_GO, ACTION_NOGO, COST_UNRECONCILED
from mission_control.slack_registry import SlackProfile, SlackRegistry


# -- fakes -----------------------------------------------------------------

class FakeSlackApiError(Exception):
    """Mimics slack_sdk.errors.SlackApiError — carries a ``.response`` with an error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"error": code}


class FakeWebClient:
    """Stands in for slack_sdk's AsyncWebClient. Records posts, and live-validates tokens
    the way resolve_active_profiles calls them: ``auth_test`` (bot) + ``apps_connections_open``
    (app-level). A token containing ``BADBOT`` fails auth_test; an app token containing
    ``BADAPP`` fails apps_connections_open — so tests drive failures via the registry+env
    without hardcoding any real token."""

    def __init__(self, token=None) -> None:
        self.token = token
        self.posts: list[dict] = []
        self.auth_calls = 0
        self.conn_calls = 0

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": f"ts-{len(self.posts)}"}

    async def auth_test(self):
        self.auth_calls += 1
        if self.token and "BADBOT" in self.token:
            raise FakeSlackApiError("invalid_auth")
        return {"ok": True, "user_id": "U_BOT", "team": "T_TEAM"}

    async def apps_connections_open(self, *, app_token):
        self.conn_calls += 1
        if app_token and "BADAPP" in app_token:
            raise FakeSlackApiError("invalid_auth")
        return {"ok": True, "url": "wss://example/socket"}


class FakeApp:
    """Records action/command listener registration; stands in for the Bolt AsyncApp."""

    def __init__(self, bot_token=None) -> None:
        self.bot_token = bot_token
        self.actions: list[str] = []
        self.commands: list[str] = []

    def action(self, action_id):
        self.actions.append(action_id)
        return lambda fn: fn          # the decorator is a no-op here

    def command(self, name):
        self.commands.append(name)
        return lambda fn: fn


class FakeHandler:
    """Records connect/close; stands in for the AsyncSocketModeHandler."""

    def __init__(self, app, app_token) -> None:
        self.app, self.app_token = app, app_token
        self.connected = self.closed = False

    async def connect_async(self):
        self.connected = True

    async def close_async(self):
        self.closed = True


class FakeRespond:
    """Records ephemeral replies (Bolt's ``respond``)."""

    def __init__(self) -> None:
        self.messages: list = []

    async def __call__(self, text=None, **kwargs):
        self.messages.append(text if text is not None else kwargs)

    @property
    def text(self) -> str:
        return " || ".join(str(m) for m in self.messages).lower()


class FakeUpdateClient:
    """Records chat_update calls (Bolt's ``client`` for updating the gate message)."""

    def __init__(self) -> None:
        self.updates: list[dict] = []

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}


class FakeGateService:
    """A stub seam for the privileged path: run lookups + one-shot gate resolution.
    Records get_run / resolve calls so tests can assert 'no service call' and 'exactly
    once'. Models the seam's one-shot guard — the FIRST resolve of a run succeeds, any
    later one returns a 409 conflict (as RunManager._resolve does)."""

    def __init__(self, runs: dict) -> None:
        self.runs = dict(runs)
        self.get_calls: list[str] = []
        self.resolved: list[tuple[str, str]] = []
        self._done: set[str] = set()

    def run_url(self, run_id: str) -> str:
        return f"http://svc/ui/runs/{run_id}"

    async def get_run(self, run_id: str):
        self.get_calls.append(run_id)
        return self.runs.get(run_id)

    async def resolve(self, run_id: str, action: str) -> GateRelay:
        self.resolved.append((run_id, action))
        if run_id in self._done:
            return GateRelay(ok=False, conflict=True, detail="status=applied")
        self._done.add(run_id)
        return GateRelay(ok=True, conflict=False, status="applied")


class FakeService:
    """Stands in for the service seam: serves the outbox tail past ``after`` from a flat
    in-memory list (exactly the pull contract's shape), and builds run-page URLs."""

    def __init__(self, notes: list[dict], *, ignore_after: bool = False, digests=None) -> None:
        self._notes = sorted(notes, key=lambda n: n["seq"])
        self._ignore_after = ignore_after   # replay the WHOLE list every poll (test dedupe)
        self._digests = digests or {}
        self.calls: list[int] = []
        self.digest_calls: list[str] = []

    @property
    def base_url(self) -> str:
        return "http://svc"

    def run_url(self, run_id: str) -> str:
        return f"http://svc/ui/runs/{run_id}"

    async def fetch_notifications(self, *, after: int, limit: int = 200) -> dict:
        self.calls.append(after)
        cut = -1 if self._ignore_after else after
        tail = [n for n in self._notes if n["seq"] > cut][:limit]
        last = self._notes[-1]["seq"] if self._notes else 0
        return {"notifications": tail, "total": len(self._notes), "last_seq": last}

    async def get_digest(self, profile: str, *, hours=None) -> dict:
        self.digest_calls.append(profile)
        return self._digests.get(profile, {"profile": profile, "runs": 0, "cost_usd": 0.0,
                                           "by_status": {}, "top_targets": []})


# -- helpers ---------------------------------------------------------------

def _registry() -> SlackRegistry:
    return SlackRegistry.from_profiles([
        SlackProfile(name="A", channel="#a", token_env="A_BOT", app_token_env="A_APP"),
        SlackProfile(name="B", channel="#b", token_env="B_BOT", app_token_env="B_APP"),
    ])


def _active(name: str, channel: str, approvers=()) -> ActiveProfile:
    return ActiveProfile(name=name, bot_client=FakeWebClient(), channel=channel,
                         approvers=list(approvers), bot_token="xoxb", app_token="xapp")


def _launched(seq: int, profile, *, run_id="run-x") -> dict:
    return {"seq": seq, "run_id": run_id, "slack_profile": profile, "kind": NOTIFY_RUN_LAUNCHED,
            "payload": {"target": "/repo/t", "task_type": "burn", "status": "queued"}}


def _gate(seq: int, profile, *, run_id="run-x") -> dict:
    return {"seq": seq, "run_id": run_id, "slack_profile": profile, "kind": NOTIFY_GATE_AWAITING,
            "payload": {"target": "/repo/t", "task_type": "burn", "status": "awaiting_gate",
                        "cost_usd": 0.0}}  # at the gate: cost NOT reconciled


def _term(seq: int, profile, *, run_id="run-x", status="applied") -> dict:
    return {"seq": seq, "run_id": run_id, "slack_profile": profile, "kind": NOTIFY_RUN_TERMINAL,
            "payload": {"target": "/repo/t", "task_type": "burn", "status": status,
                        "cost_usd": 0.1234, "started_at": "2026-07-17T10:00:00+00:00",
                        "ended_at": "2026-07-17T10:02:00+00:00"}}


def _cost_alert(seq: int, profile, *, run_id="run-x") -> dict:
    return {"seq": seq, "run_id": run_id, "slack_profile": profile, "kind": NOTIFY_COST_THRESHOLD,
            "payload": {"target": "/repo/t", "task_type": "burn", "cost_usd": 1.5,
                        "threshold": 1.0, "reason": "per_run"}}


def _regression(seq: int, profile, *, run_id="run-x") -> dict:
    return {"seq": seq, "run_id": run_id, "slack_profile": profile, "kind": NOTIFY_REGRESSION,
            "payload": {"target": "/repo/t", "axes": [
                {"metric": "quality_total", "current": 0.61, "baseline_mean": 0.80,
                 "baseline_stddev": 0.05, "threshold": 0.70, "higher_is_worse": False}]}}


def _bridge(profiles, service, cursor_path, app_factory=None, socket_factory=None) -> SlackBridge:
    return SlackBridge(
        profiles=profiles, service=service, cursor=CursorStore(cursor_path),
        app_factory=app_factory or (lambda bot: FakeApp(bot)),
        socket_factory=socket_factory or (lambda app, app_token: FakeHandler(app, app_token)),
    )


# -- (1) multi-profile resolution + one connection per resolvable profile ---

def test_resolves_and_connects_per_profile_skips_missing_tokens(tmp_path, caplog):
    # Only A's tokens are present on this "box"; B's are absent → B skipped, not fatal.
    # A's (good) tokens also pass the live auth.test / apps.connections.open check.
    env = {"A_BOT": "xoxb-a", "A_APP": "xapp-a"}
    with caplog.at_level("WARNING"):
        active = asyncio.run(resolve_active_profiles(
            _registry(), env=env, client_factory=lambda t: FakeWebClient(t)))
    assert set(active) == {"A"}                       # B did not resolve
    assert "B" in caplog.text and "skipped" in caplog.text  # logged, not raised
    assert active["A"].bot_client.auth_calls == 1 and active["A"].bot_client.conn_calls == 1

    apps: list[FakeApp] = []
    handlers: list[FakeHandler] = []

    def app_factory(bot):
        app = FakeApp(bot)
        apps.append(app)
        return app

    def socket_factory(app, app_token):
        h = FakeHandler(app, app_token)
        handlers.append(h)
        return h

    bridge = _bridge(active, FakeService([]), tmp_path / "cur", app_factory, socket_factory)
    asyncio.run(bridge.connect())
    assert len(handlers) == 1 and handlers[0].connected    # exactly one socket, for A
    assert set(bridge.active_profiles) == {"A"}
    # The go/no-go buttons + the /mc slash command were registered on A's own app.
    assert len(apps) == 1
    assert set(apps[0].actions) == {ACTION_GO, ACTION_NOGO}
    assert apps[0].commands == [SLASH_COMMAND]


# -- (1b) live token validation during resolution --------------------------

def _tok_registry() -> SlackRegistry:
    """Two profiles whose bot/app tokens come from distinct env vars."""
    return SlackRegistry.from_profiles([
        SlackProfile(name="A", channel="#a", token_env="A_BOT", app_token_env="A_APP"),
        SlackProfile(name="B", channel="#b", token_env="B_BOT", app_token_env="B_APP"),
    ])


def _resolve(env, caplog):
    with caplog.at_level("WARNING"):
        return asyncio.run(resolve_active_profiles(
            _tok_registry(), env=env, client_factory=lambda t: FakeWebClient(t)))


def test_valid_tokens_go_active(caplog):
    env = {"A_BOT": "xoxb-good", "A_APP": "xapp-good"}
    active = _resolve(env, caplog)
    assert set(active) == {"A"}
    # Both tokens were live-checked before the profile went active.
    assert active["A"].bot_client.auth_calls == 1
    assert active["A"].bot_client.conn_calls == 1


def test_bad_bot_token_skipped_with_reason(caplog):
    # A's bot token is rejected by auth.test → A is skipped (not crashed).
    env = {"A_BOT": "xoxb-BADBOT", "A_APP": "xapp-good"}
    active = _resolve(env, caplog)
    assert active == {}
    assert "slack profile 'A' skipped: invalid_auth" in caplog.text


def test_bad_app_token_skipped_with_reason(caplog):
    # A's bot token is fine but the app-level token fails apps.connections.open → skipped.
    env = {"A_BOT": "xoxb-good", "A_APP": "xapp-BADAPP"}
    active = _resolve(env, caplog)
    assert active == {}
    assert "slack profile 'A' skipped: invalid_auth" in caplog.text


def test_mixed_fleet_only_good_profiles_active(caplog):
    # A: valid → active. B: bad app token → skipped. Neither crashes the resolve.
    env = {"A_BOT": "xoxb-good", "A_APP": "xapp-good",
           "B_BOT": "xoxb-good", "B_APP": "xapp-BADAPP"}
    active = _resolve(env, caplog)
    assert set(active) == {"A"}
    assert "slack profile 'B' skipped: invalid_auth" in caplog.text
    assert "slack profile 'A' skipped" not in caplog.text


def test_validation_failure_never_crashes_on_nonslack_error(caplog):
    # A non-Slack exception (e.g. a network blip with no .response) still just skips.
    async def boom(bot_client, app_token):
        raise TimeoutError("connection reset")

    with caplog.at_level("WARNING"):
        active = asyncio.run(resolve_active_profiles(
            _tok_registry(), env={"A_BOT": "x", "A_APP": "y"},
            client_factory=lambda t: FakeWebClient(t), validate=boom))
    assert active == {}
    assert "slack profile 'A' skipped: TimeoutError: connection reset" in caplog.text


def test_assemble_fails_fast_on_no_url_or_zero_profiles(tmp_path):
    reg = _registry()
    cur = CursorStore(tmp_path / "cur")
    # Missing service URL → fatal.
    with pytest.raises(BridgeConfigError):
        asyncio.run(assemble(registry=reg, base_url=None, http=object(), cursor=cur,
                             env={"A_BOT": "x", "A_APP": "y"},
                             client_factory=lambda t: FakeWebClient(t)))
    # Zero resolvable profiles (no token env on this box) → fatal.
    with pytest.raises(BridgeConfigError):
        asyncio.run(assemble(registry=reg, base_url="http://svc", http=object(), cursor=cur,
                             env={}, client_factory=lambda t: FakeWebClient(t)))


# -- (2) routing: a terminal for A posts to A only -------------------------

def test_terminal_routes_to_named_profile_only(tmp_path):
    profs = {"A": _active("A", "#a"), "B": _active("B", "#b")}
    svc = FakeService([_term(1, "A")])
    bridge = _bridge(profs, svc, tmp_path / "cur")

    handled = asyncio.run(bridge.poll_once())
    assert handled == 1
    # Exactly one message, to A's channel, via A's client — and NOTHING to B.
    assert len(profs["A"].bot_client.posts) == 1
    assert profs["A"].bot_client.posts[0]["channel"] == "#a"
    assert profs["B"].bot_client.posts == []
    # Metadata-only: the run id links to the 5b run page, no run content is present.
    blob = json.dumps(profs["A"].bot_client.posts[0])
    assert "http://svc/ui/runs/run-x" in blob
    assert "prompt" not in blob and "diff" not in blob


# -- (3) a null-profile (opt-out) notification posts nothing ---------------

def test_null_profile_posts_nothing_but_advances(tmp_path):
    profs = {"A": _active("A", "#a")}
    svc = FakeService([_term(1, None)])              # opt-out run
    cur_path = tmp_path / "cur"
    bridge = _bridge(profs, svc, cur_path)

    handled = asyncio.run(bridge.poll_once())
    assert handled == 1                              # handled (deliberate skip)
    assert profs["A"].bot_client.posts == []         # no egress
    assert CursorStore(cur_path).get() == 1          # cursor advanced past it


# -- (4) a notification for an inactive-on-this-box profile is skipped ------

def test_inactive_profile_skipped_without_crash(tmp_path):
    profs = {"A": _active("A", "#a")}                # only A active here
    svc = FakeService([_term(1, "C")])               # names a profile this box lacks
    cur_path = tmp_path / "cur"
    bridge = _bridge(profs, svc, cur_path)

    handled = asyncio.run(bridge.poll_once())        # must not crash
    assert handled == 1
    assert profs["A"].bot_client.posts == []         # not ours → nothing posted
    assert CursorStore(cur_path).get() == 1          # advanced (another box may own C)


# -- (5) cursor advances + survives a restart (no re-post, no drop) --------

def test_cursor_advances_and_survives_restart(tmp_path):
    cur_path = tmp_path / "cur"
    notes = [_term(1, "A"), _term(2, None), _term(3, "A")]  # 2 routable + 1 opt-out

    # First run: process the whole tail.
    profs1 = {"A": _active("A", "#a")}
    svc1 = FakeService(notes)
    handled = asyncio.run(_bridge(profs1, svc1, cur_path).poll_once())
    assert handled == 3
    assert [p["channel"] for p in profs1["A"].bot_client.posts] == ["#a", "#a"]  # seq 1 + 3
    assert CursorStore(cur_path).get() == 3          # persisted high-water mark

    # "Restart": a fresh bridge over the SAME cursor file + same outbox.
    profs2 = {"A": _active("A", "#a")}
    svc2 = FakeService(notes)
    handled2 = asyncio.run(_bridge(profs2, svc2, cur_path).poll_once())
    assert handled2 == 0                             # nothing new past the cursor
    assert profs2["A"].bot_client.posts == []        # no already-handled seq re-posted
    assert svc2.calls == [3]                          # polled strictly past the durable cursor


def test_delivery_failure_holds_cursor_for_at_least_once(tmp_path):
    # If a post fails, the cursor must NOT advance past it, so the next poll re-sends it.
    class BoomClient(FakeWebClient):
        async def chat_postMessage(self, **kwargs):
            raise RuntimeError("slack down")

    prof = ActiveProfile(name="A", bot_client=BoomClient(), channel="#a",
                         bot_token="b", app_token="a")
    cur_path = tmp_path / "cur"
    svc = FakeService([_term(1, "A"), _term(2, "A")])
    handled = asyncio.run(_bridge({"A": prof}, svc, cur_path).poll_once())
    assert handled == 0                              # stopped at the first failing seq
    assert CursorStore(cur_path).get() == 0          # cursor held before seq 1

    # Recover: a working client re-sends seq 1 (and 2) — nothing was dropped.
    good = _active("A", "#a")
    handled2 = asyncio.run(_bridge({"A": good}, FakeService([_term(1, "A"), _term(2, "A")]),
                                   cur_path).poll_once())
    assert handled2 == 2
    assert len(good.bot_client.posts) == 2


# -- (6) the full catalog: each kind renders metadata-only -----------------

def _blob(note) -> str:
    blocks, text = build_message(note, run_url="http://svc/ui/runs/run-x")
    return json.dumps(blocks) + "\n" + text


def test_launched_renders_metadata():
    blob = _blob(_launched(1, "A"))
    assert "left the pad" in blob                     # Controller left the pad
    assert "/repo/t" in blob and "run-x" in blob      # target + run id
    assert "http://svc/ui/runs/run-x" in blob         # link to the run page
    assert roles.BURN in blob                          # sim/burn label from roles


def test_gate_shows_not_yet_reconciled_never_zero():
    blob = _blob(_gate(2, "A"))
    assert "holding at go/no-go" in blob
    assert COST_UNRECONCILED in blob                  # honest: cost not reconciled at the gate
    assert "$0" not in blob and "$" not in blob       # never a dollar amount implying free
    assert "run-x" in blob and "http://svc/ui/runs/run-x" in blob


def test_terminal_headline_frames_by_status():
    # completion vs scrub vs failure read differently in the header.
    assert "completed" in _blob(_term(3, "A", status="applied"))
    assert "scrubbed" in _blob(_term(3, "A", status="scrubbed"))
    assert "failed" in _blob(_term(3, "A", status="failed"))
    assert "$0.1234" in _blob(_term(3, "A"))          # terminal shows reconciled total


def test_no_run_content_in_any_kind():
    for note in (_launched(1, "A"), _gate(2, "A"), _term(3, "A", status="failed")):
        blob = _blob(note).lower()
        for banned in ("prompt", "diff", "patch", "worker_summary", "secret"):
            assert banned not in blob


# -- (7) full lifecycle routing + per-run threading ------------------------

def test_full_lifecycle_routes_to_profile_and_threads(tmp_path):
    profs = {"A": _active("A", "#a"), "B": _active("B", "#b")}
    svc = FakeService([_launched(1, "A"), _gate(2, "A"), _term(3, "A")])
    bridge = _bridge(profs, svc, tmp_path / "cur")

    handled = asyncio.run(bridge.poll_once())
    assert handled == 3
    posts = profs["A"].bot_client.posts
    assert len(posts) == 3 and all(p["channel"] == "#a" for p in posts)  # all to A
    assert profs["B"].bot_client.posts == []                              # nothing to B

    # The launch message is the thread root; gate + terminal thread UNDER it.
    assert posts[0]["thread_ts"] is None                 # run_launched = root
    assert posts[1]["thread_ts"] == "ts-1"               # gate_awaiting threaded
    assert posts[2]["thread_ts"] == "ts-1"               # run_terminal threaded


def test_null_profile_whole_lifecycle_posts_nothing(tmp_path):
    profs = {"A": _active("A", "#a")}
    cur_path = tmp_path / "cur"
    svc = FakeService([_launched(1, None), _gate(2, None), _term(3, None)])
    bridge = _bridge(profs, svc, cur_path)

    handled = asyncio.run(bridge.poll_once())
    assert handled == 3                                  # all consumed
    assert profs["A"].bot_client.posts == []             # NOTHING posted across the lifecycle
    assert CursorStore(cur_path).get() == 3              # cursor still advanced


# -- (8) dedupe: a re-delivered seq posts nothing new ----------------------

def test_dedupe_redelivered_seq_posts_nothing(tmp_path):
    profs = {"A": _active("A", "#a")}
    cur_path = tmp_path / "cur"
    notes = [_launched(1, "A"), _term(2, "A")]
    svc = FakeService(notes, ignore_after=True)          # replays the WHOLE list each poll
    bridge = _bridge(profs, svc, cur_path)

    assert asyncio.run(bridge.poll_once()) == 2
    assert len(profs["A"].bot_client.posts) == 2

    # Second poll: the service replays seq 1 + 2, but both are already handled → dedupe.
    assert asyncio.run(bridge.poll_once()) == 0
    assert len(profs["A"].bot_client.posts) == 2         # nothing new

    # And across a restart (fresh bridge, same cursor file, still-replaying service).
    profs2 = {"A": _active("A", "#a")}
    b2 = _bridge(profs2, FakeService(notes, ignore_after=True), cur_path)
    assert asyncio.run(b2.poll_once()) == 0
    assert profs2["A"].bot_client.posts == []            # no already-handled seq re-posted


# -- (9) the privileged path: resolve the gate from Slack ------------------

def _gate_bridge(profiles, service, tmp_path) -> SlackBridge:
    return _bridge(profiles, service, tmp_path / "cur")


def test_gate_message_carries_go_nogo_buttons():
    # The gate_awaiting message attaches interactive GO / NO-GO buttons whose value is
    # ONLY the run_id (no run content).
    blocks, _text = build_message(_gate(2, "A", run_id="run-a"), run_url="u")
    actions = [b for b in blocks if b.get("type") == "actions"]
    assert len(actions) == 1
    ids = {e["action_id"]: e["value"] for e in actions[0]["elements"]}
    assert ids == {ACTION_GO: "run-a", ACTION_NOGO: "run-a"}


def test_authorized_go_calls_approve_once_and_updates_message(tmp_path):
    profs = {"A": _active("A", "#a", approvers=["U_A"])}
    svc = FakeGateService({"run-a": {"slack_profile": "A", "status": "awaiting_gate"}})
    bridge = _gate_bridge(profs, svc, tmp_path)
    respond, client = FakeRespond(), FakeUpdateClient()

    out = asyncio.run(bridge.handle_gate_action(
        profile_name="A", decision=roles.GO, run_id="run-a", user_id="U_A",
        respond=respond, client=client, channel="#a", message_ts="ts-1"))

    assert out == "resolved"
    assert svc.resolved == [("run-a", "approve")]        # exactly once, approve endpoint
    assert len(client.updates) == 1                       # original message updated
    blob = json.dumps(client.updates[0])
    assert "U_A" in blob                                  # shows who resolved
    assert ACTION_GO not in blob and ACTION_NOGO not in blob   # buttons removed


def test_cancel_maps_to_cancel_endpoint(tmp_path):
    profs = {"A": _active("A", "#a", approvers=["U_A"])}
    svc = FakeGateService({"run-a": {"slack_profile": "A", "status": "running"}})
    out = asyncio.run(_gate_bridge(profs, svc, tmp_path).handle_gate_action(
        profile_name="A", decision=DECISION_CANCEL, run_id="run-a", user_id="U_A",
        respond=FakeRespond(), client=FakeUpdateClient(), channel="#a", message_ts="ts"))
    assert out == "resolved"
    assert svc.resolved == [("run-a", "cancel")]


def test_unauthorized_user_refused_with_no_service_call(tmp_path):
    profs = {"A": _active("A", "#a", approvers=["U_A"])}     # U_STRANGER is NOT an approver
    svc = FakeGateService({"run-a": {"slack_profile": "A", "status": "awaiting_gate"}})
    respond = FakeRespond()
    out = asyncio.run(_gate_bridge(profs, svc, tmp_path).handle_gate_action(
        profile_name="A", decision=roles.GO, run_id="run-a", user_id="U_STRANGER",
        respond=respond, client=FakeUpdateClient(), channel="#a", message_ts="ts"))
    assert out == "denied"
    assert svc.resolved == [] and svc.get_calls == []       # NO service call at all
    assert "not authorized" in respond.text


def test_action_on_run_owned_by_other_profile_refused(tmp_path):
    # U_A is an approver of A and acts on A's connection, but the run belongs to B →
    # cross-workspace relay is refused (no resolve call).
    profs = {"A": _active("A", "#a", approvers=["U_A"]),
             "B": _active("B", "#b", approvers=["U_B"])}
    svc = FakeGateService({"run-b": {"slack_profile": "B", "status": "awaiting_gate"}})
    respond = FakeRespond()
    out = asyncio.run(_gate_bridge(profs, svc, tmp_path).handle_gate_action(
        profile_name="A", decision=roles.GO, run_id="run-b", user_id="U_A",
        respond=respond, client=FakeUpdateClient(), channel="#a", message_ts="ts"))
    assert out == "denied"
    assert svc.resolved == []                               # no state-changing call
    assert "not authorized" in respond.text


def test_double_submit_resolves_once_second_gets_conflict(tmp_path):
    profs = {"A": _active("A", "#a", approvers=["U_A"])}
    svc = FakeGateService({"run-a": {"slack_profile": "A", "status": "awaiting_gate"}})
    bridge = _gate_bridge(profs, svc, tmp_path)

    r1 = FakeRespond()
    out1 = asyncio.run(bridge.handle_gate_action(
        profile_name="A", decision=roles.GO, run_id="run-a", user_id="U_A",
        respond=r1, client=FakeUpdateClient(), channel="#a", message_ts="ts"))
    # A concurrent/second click — this time NO-GO — hits the seam's one-shot guard.
    r2, client2 = FakeRespond(), FakeUpdateClient()
    out2 = asyncio.run(bridge.handle_gate_action(
        profile_name="A", decision=roles.NO_GO, run_id="run-a", user_id="U_A",
        respond=r2, client=client2, channel="#a", message_ts="ts"))

    assert out1 == "resolved" and out2 == "conflict"        # resolved exactly once
    assert svc.resolved == [("run-a", "approve"), ("run-a", "reject")]  # both relayed; seam guarded
    assert "already resolved" in r2.text
    assert len(client2.updates) == 1                        # buttons disabled on conflict too


def test_slash_command_approve_and_status(tmp_path):
    profs = {"A": _active("A", "#a", approvers=["U_A"])}
    svc = FakeGateService({"run-a": {"slack_profile": "A", "status": "awaiting_gate",
                                     "cost_usd": 0.42}})
    bridge = _gate_bridge(profs, svc, tmp_path)

    # /mc approve <run_id> relays through the same authorization/relay core.
    out = asyncio.run(bridge.handle_command(
        profile_name="A", command={"text": "approve run-a", "user_id": "U_A"},
        respond=FakeRespond(), client=FakeUpdateClient()))
    assert out == "resolved" and svc.resolved == [("run-a", "approve")]

    # /mc status <run_id> is a metadata-only read scoped to the workspace.
    status_respond = FakeRespond()
    out = asyncio.run(bridge.handle_command(
        profile_name="A", command={"text": "status run-a", "user_id": "U_A"},
        respond=status_respond, client=None))
    assert out == "status"
    assert "run-a" in status_respond.text and "awaiting_gate" in status_respond.text


def test_slash_status_refuses_cross_workspace(tmp_path):
    profs = {"A": _active("A", "#a", approvers=["U_A"])}
    svc = FakeGateService({"run-b": {"slack_profile": "B", "status": "running"}})
    respond = FakeRespond()
    out = asyncio.run(_gate_bridge(profs, svc, tmp_path).handle_command(
        profile_name="A", command={"text": "status run-b", "user_id": "U_A"},
        respond=respond, client=None))
    assert out == "not-found"                               # B's run is invisible to A
    assert "not found" in respond.text


# -- (10) alert-class notifications: render, route, quiet ------------------

def test_cost_alert_renders_and_routes_to_profile(tmp_path):
    profs = {"A": _active("A", "#a"), "B": _active("B", "#b")}
    svc = FakeService([_cost_alert(1, "A")])
    bridge = _bridge(profs, svc, tmp_path / "cur")

    assert asyncio.run(bridge.poll_once()) == 1
    posts = profs["A"].bot_client.posts
    assert len(posts) == 1 and posts[0]["channel"] == "#a"     # routed to A only
    assert profs["B"].bot_client.posts == []
    blob = json.dumps(posts[0])
    assert "Cost alert" in blob and "$1.5000" in blob and "$1.0000" in blob  # metadata
    assert "prompt" not in blob.lower() and "diff" not in blob.lower()       # no content


def test_regression_alert_renders_axes_metadata(tmp_path):
    profs = {"A": _active("A", "#a")}
    svc = FakeService([_regression(1, "A")])
    bridge = _bridge(profs, svc, tmp_path / "cur")

    assert asyncio.run(bridge.poll_once()) == 1
    blob = json.dumps(profs["A"].bot_client.posts[0])
    assert "regression" in blob.lower()
    assert "quality_total" in blob                              # which metric
    assert "0.61" in blob and "0.7" in blob                     # observed + band
    for banned in ("prompt", "diff", "patch", "eval_output"):
        assert banned not in blob.lower()


def test_null_profile_alerts_post_nothing(tmp_path):
    # Defensive: even if a null-profile alert reached the feed, the bridge posts nothing.
    profs = {"A": _active("A", "#a")}
    svc = FakeService([_cost_alert(1, None), _regression(2, None)])
    bridge = _bridge(profs, svc, tmp_path / "cur")
    assert asyncio.run(bridge.poll_once()) == 2                 # consumed
    assert profs["A"].bot_client.posts == []                   # nothing posted


# -- (11) per-profile fleet digest ----------------------------------------

def test_post_digests_one_per_profile_scoped(tmp_path):
    profs = {"A": _active("A", "#a"), "B": _active("B", "#b")}
    digests = {
        "A": {"profile": "A", "runs": 3, "cost_usd": 2.5,
              "by_status": {"applied": 2, "failed": 1},
              "top_targets": [{"target": "/repo/a", "runs": 3, "cost_usd": 2.5}]},
        "B": {"profile": "B", "runs": 1, "cost_usd": 0.4,
              "by_status": {"scrubbed": 1}, "top_targets": []},
    }
    svc = FakeService([], digests=digests)
    bridge = _bridge(profs, svc, tmp_path / "cur")

    posted = asyncio.run(bridge.post_digests(window_hours=24))
    assert posted == 2
    assert set(svc.digest_calls) == {"A", "B"}                  # one fetch per active profile

    a_post = profs["A"].bot_client.posts[0]
    assert a_post["channel"] == "#a"
    a_blob = json.dumps(a_post)
    assert "Fleet digest" in a_blob and "/repo/a" in a_blob and "3" in a_blob
    # A's digest carries only A's numbers — B's target never appears in A's post.
    assert "/repo/a" in a_blob
    b_blob = json.dumps(profs["B"].bot_client.posts[0])
    assert profs["B"].bot_client.posts[0]["channel"] == "#b"
    assert "/repo/a" not in b_blob                              # no cross-profile bleed


# -- (12) ServiceClient authenticates against the seam ---------------------

class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class _RecordingHTTP:
    """Records the headers each request carried."""

    def __init__(self):
        self.headers_seen: list = []

    async def get(self, url, params=None, headers=None):
        self.headers_seen.append(headers or {})
        return _Resp(200, {"notifications": [], "total": 0, "last_seq": 0})

    async def post(self, url, headers=None):
        self.headers_seen.append(headers or {})
        return _Resp(200, {"status": "applied"})


def test_service_client_sends_bearer_when_configured():
    from mission_control.slack.bridge import ServiceClient

    http = _RecordingHTTP()
    svc = ServiceClient("http://svc", http, auth_token="tok-123")
    asyncio.run(svc.fetch_notifications(after=0))
    asyncio.run(svc.resolve("run-x", "approve"))
    assert all(h.get("Authorization") == "Bearer tok-123" for h in http.headers_seen)


def test_service_client_omits_auth_when_unset():
    from mission_control.slack.bridge import ServiceClient

    http = _RecordingHTTP()
    svc = ServiceClient("http://svc", http)                 # no token → open service
    asyncio.run(svc.resolve("run-x", "approve"))
    assert http.headers_seen == [{}]                        # no Authorization header sent
