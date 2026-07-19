"""The Slack bridge: a SEPARATE long-running client process (not part of the FastAPI
app). It connects OUT to Slack via Socket Mode (an outbound WebSocket — no public
endpoint, no request URL) and talks to the service over HTTP.

Shape:

* Load the NON-SECRET registry (``MC_SLACK_REGISTRY``). For EACH profile, resolve its
  bot + app tokens from the env-var NAMES the registry gives. A profile whose token
  env vars are ABSENT on THIS machine is SKIPPED with a clear log line — so a fleet of
  boxes can each own a different subset of profiles. Fail fast ONLY if zero profiles
  resolve or the service base URL is missing.
* For each ACTIVE profile: its own Socket Mode connection (app token) + its own bot
  client (bot token), held in a ``profile -> ActiveProfile`` map.
* One durable GLOBAL cursor (the outbox seq is global): poll
  ``GET /notifications?after=<cursor>`` on a few-second interval, route each note to
  the profile it names, and advance + persist the cursor only after a note is handled
  (posted or deliberately skipped) — at-least-once, dedupe on seq downstream.
* ROUTING by ``slack_profile``: null → skip (opt-out, no egress); a profile not active
  on THIS box → skip with a log line (another box may own it); otherwise post to THAT
  profile's channel via THAT profile's bot client.

This slice delivers ``run_terminal`` end-to-end as the first routed message (a
metadata-only Block Kit card). Other milestone kinds are consumed (cursor advances)
but not yet posted.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .. import roles
from ..runs_store import NOTIFY_RUN_LAUNCHED
from ..slack_registry import SlackRegistry
from .cursor import CursorStore
from .message import (
    ACTION_GO,
    ACTION_NOGO,
    build_digest_message,
    build_message,
    build_resolved_message,
    message_color,
)

log = logging.getLogger(__name__)

# The service base URL the bridge talks to (the seam). No default — a missing base URL
# is a fail-fast condition.
ENV_SERVICE_URL = "MC_SERVICE_URL"

# The API bearer token (shared with the service's MC_API_TOKEN). When set, the bridge
# sends it on every seam request so it's an authenticated principal on the mutating
# endpoints. Unset ⇒ no header (matches an open, no-auth service).
ENV_API_TOKEN = "MC_API_TOKEN"

# Default poll cadence for the pull feed (seconds). A few seconds is well within the
# "at-least-once" contract; lower it if push latency matters (or add /notifications/stream).
_DEFAULT_POLL_SECONDS = 3.0
_PULL_LIMIT = 200

# The slash command the bridge registers per profile connection.
SLASH_COMMAND = "/mc"

# A gate decision token -> the seam endpoint the UI/CLI also post to. go/no-go come from
# roles (metaphor vocabulary); ``cancel`` is a functional mid-node stop (its own token).
DECISION_CANCEL = "cancel"
_DECISION_ENDPOINT = {
    roles.GO: "approve",
    roles.NO_GO: "reject",
    DECISION_CANCEL: "cancel",
}
# Human label for a decision (for the resolved-message + audit line).
_DECISION_LABEL = {
    roles.GO: roles.GO.upper(),
    roles.NO_GO: roles.NO_GO.upper(),
    DECISION_CANCEL: DECISION_CANCEL,
}
# The button action_id -> decision token.
_BUTTON_DECISION = {ACTION_GO: roles.GO, ACTION_NOGO: roles.NO_GO}
# The /mc subcommand -> decision token (the resolving subcommands).
_COMMAND_DECISION = {"approve": roles.GO, "reject": roles.NO_GO, "cancel": DECISION_CANCEL}

_NOT_AUTHORIZED = "🚫 You are not authorized to resolve this gate."


class BridgeConfigError(RuntimeError):
    """A fatal misconfiguration — zero resolvable profiles, or no service URL."""


# -- the service seam (HTTP client) ----------------------------------------

class ServiceClient:
    """A thin async HTTP client for the parts of the seam the bridge consumes: the
    notification pull feed, the run-page URL for message links, and the gate-decision
    relay. When ``auth_token`` is set (from ``MC_API_TOKEN``), every request carries
    ``Authorization: Bearer <token>`` — so the bridge is an authenticated principal on
    the mutating endpoints, matching the service's auth gate."""

    def __init__(self, base_url: str, http, *, auth_token: Optional[str] = None) -> None:
        self._base = base_url.rstrip("/")
        self._http = http
        self._headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}

    @property
    def base_url(self) -> str:
        return self._base

    def run_url(self, run_id: str) -> str:
        """The 5b run page (``GET /ui/runs/{id}``) — the link in a routed message."""
        return f"{self._base}/ui/runs/{run_id}"

    async def fetch_notifications(self, *, after: int, limit: int = _PULL_LIMIT) -> dict:
        """The outbox tail past ``after`` (the pull contract). Returns the parsed
        ``{notifications, total, last_seq}`` body."""
        resp = await self._http.get(
            f"{self._base}/notifications", params={"after": after, "limit": limit},
            headers=self._headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_run(self, run_id: str) -> Optional[dict]:
        """The run's ledger row (``GET /runs/{id}``) — the SAME read the UI/CLI use.
        ``None`` when the run is unknown (404). Carries ``slack_profile`` for the
        per-profile authorization check."""
        resp = await self._http.get(f"{self._base}/runs/{run_id}", headers=self._headers)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def get_digest(self, profile: str, *, hours: Optional[float] = None) -> dict:
        """The per-profile fleet digest (``GET /notifications/digest``) — metadata-only
        aggregate of the runs that named ``profile``."""
        params: dict = {"profile": profile}
        if hours is not None:
            params["hours"] = hours
        resp = await self._http.get(f"{self._base}/notifications/digest", params=params,
                                    headers=self._headers)
        resp.raise_for_status()
        return resp.json()

    async def resolve(self, run_id: str, action: str) -> "GateRelay":
        """Relay a gate decision to the SAME endpoint the UI/CLI post to:
        ``approve`` / ``reject`` / ``cancel``. A 409 is the seam's one-shot guard
        (already resolved on another surface) — surfaced as ``conflict``, not an error."""
        resp = await self._http.post(f"{self._base}/runs/{run_id}/{action}", headers=self._headers)
        if resp.status_code == 409:
            return GateRelay(ok=False, conflict=True, status=None, detail=_error_detail(resp))
        resp.raise_for_status()
        body = resp.json()
        return GateRelay(ok=True, conflict=False, status=body.get("status"), detail=None)


@dataclass
class GateRelay:
    """The outcome of relaying a decision to the seam. ``conflict`` marks the one-shot
    guard firing (the gate was already resolved across some surface/profile)."""

    ok: bool
    conflict: bool
    status: Optional[str] = None
    detail: Optional[str] = None


def _error_detail(resp) -> Optional[str]:
    """Best-effort extraction of a FastAPI ``{"detail": ...}`` error string."""
    try:
        return resp.json().get("detail")
    except Exception:  # noqa: BLE001
        return None


# -- an active profile on THIS machine -------------------------------------

@dataclass
class ActiveProfile:
    """A profile whose tokens resolved on this box: its bot client (posting), channel,
    approvers, and (once connected) its Socket Mode handler (the outbound WebSocket)."""

    name: str
    bot_client: object                       # AsyncWebClient (or a mock in tests)
    channel: Optional[str]
    approvers: list[str] = field(default_factory=list)
    bot_token: Optional[str] = None
    app_token: Optional[str] = None
    app: object = None                        # the Bolt AsyncApp (listeners registered on it)
    handler: object = None                    # AsyncSocketModeHandler once connected


# A factory: bot token -> a bot Web client (posting). Injectable for tests.
ClientFactory = Callable[[str], object]
# A factory: bot token -> a Bolt app the bridge registers its listeners on. Injectable.
AppFactory = Callable[[str], object]
# A factory: (app, app_token) -> a Socket Mode handler with connect_async/close_async.
SocketFactory = Callable[[object, str], object]
# A live token check: (bot_client, app_token) -> awaitable that raises on failure.
TokenValidator = Callable[[object, str], Awaitable[None]]


def _default_client_factory(bot_token: str):
    from slack_sdk.web.async_client import AsyncWebClient

    return AsyncWebClient(token=bot_token)


async def _default_validate_tokens(bot_client, app_token: str) -> None:
    """Live-validate BOTH of a profile's tokens against Slack before it goes active:
    ``auth.test`` proves the bot token, and ``apps.connections.open`` proves the
    app-level token (it's the Socket Mode handshake — the lightest call that actually
    exercises an ``xapp-`` token). Raises on any failure; the caller turns that into a
    skip, never a crash."""
    await bot_client.auth_test()
    await bot_client.apps_connections_open(app_token=app_token)


def _skip_reason(exc: Exception) -> str:
    """A short, log-safe reason from a Slack/HTTP failure — the API error code when
    present (e.g. ``invalid_auth``), else the exception type + message. Never a token."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            err = resp.get("error")
        except Exception:  # noqa: BLE001 — a non-mapping response
            err = None
        if err:
            return str(err)
    return f"{type(exc).__name__}: {exc}"


def _default_app_factory(bot_token: str):
    from slack_bolt.app.async_app import AsyncApp

    return AsyncApp(token=bot_token)


def _default_socket_factory(app, app_token: str):
    from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler

    return AsyncSocketModeHandler(app, app_token)


async def resolve_active_profiles(
    registry: SlackRegistry,
    *,
    env: Optional[dict] = None,
    client_factory: ClientFactory = _default_client_factory,
    validate: TokenValidator = _default_validate_tokens,
) -> dict[str, ActiveProfile]:
    """Resolve the registry against THIS machine's environment: a profile goes ACTIVE
    only when BOTH its bot and app token env vars are present AND both tokens
    live-validate against Slack (bot via ``auth.test``, app-level via
    ``apps.connections.open``). A profile is SKIPPED — with a clear
    ``slack profile '<name>' skipped: <reason>`` WARNING — when its token env vars are
    missing OR a token is rejected by Slack. A skip is never fatal (multi-machine: a box
    activates only the profiles whose secrets it holds AND Slack accepts)."""
    source = env if env is not None else os.environ
    active: dict[str, ActiveProfile] = {}
    for name in registry.names():
        profile = registry.get(name)
        if profile is None:  # names() is derived from the registry, so this can't happen
            continue
        bot_token = source.get(profile.token_env or "")
        app_token = source.get(profile.app_token_env or "")
        if not bot_token or not app_token:
            log.warning(
                "slack profile '%s' skipped: missing token env (%s / %s not set on this host)",
                name, profile.token_env, profile.app_token_env,
            )
            continue
        bot_client = client_factory(bot_token)
        try:
            await validate(bot_client, app_token)
        except Exception as exc:  # noqa: BLE001 — a bad token skips ONE profile, never crashes
            log.warning("slack profile '%s' skipped: %s", name, _skip_reason(exc))
            continue
        active[name] = ActiveProfile(
            name=name,
            bot_client=bot_client,
            channel=profile.channel,
            approvers=list(profile.approvers),
            bot_token=bot_token,
            app_token=app_token,
        )
    return active


# -- the bridge ------------------------------------------------------------

class SlackBridge:
    """Routes the global notification feed to per-profile Slack channels over each
    profile's own Socket Mode connection + bot client."""

    def __init__(
        self,
        *,
        profiles: dict[str, ActiveProfile],
        service: ServiceClient,
        cursor: CursorStore,
        app_factory: AppFactory = _default_app_factory,
        socket_factory: SocketFactory = _default_socket_factory,
        poll_interval: float = _DEFAULT_POLL_SECONDS,
    ) -> None:
        self._profiles = profiles
        self._service = service
        self._cursor = cursor
        self._app_factory = app_factory
        self._socket_factory = socket_factory
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()
        # Dedupe on the global seq: the highest seq fully handled. Any note at or below
        # it is a re-delivery (the at-least-once window, or a service replay) and posts
        # nothing new. Seeded from the durable cursor at each poll, so dedupe survives a
        # restart. O(1) — seq is monotonic, so a single high-water mark suffices.
        self._handled_through = 0
        # Per-(profile, run_id) thread root: the ts of a run's launch message, so its
        # later milestones thread UNDER it — within that profile's workspace only.
        self._threads: dict[tuple[str, str], str] = {}

    @property
    def active_profiles(self) -> dict[str, ActiveProfile]:
        return self._profiles

    # -- connection lifecycle ---------------------------------------------

    async def connect(self) -> None:
        """Open one Socket Mode connection per active profile (outbound WebSocket, no
        public endpoint). Each profile gets its OWN Bolt app with the go/no-go button +
        /mc slash-command listeners registered on it, so interactions are handled per
        profile connection. A profile whose socket fails to connect is dropped from the
        active map with a log line — the rest still run."""
        for name, profile in list(self._profiles.items()):
            app = self._app_factory(profile.bot_token)
            self._register_listeners(app, profile)
            handler = self._socket_factory(app, profile.app_token)
            try:
                await handler.connect_async()
            except Exception:  # noqa: BLE001 — one bad socket must not sink the fleet
                log.exception("slack profile %r failed to open its Socket Mode connection", name)
                del self._profiles[name]
                continue
            profile.app = app
            profile.handler = handler
            log.info("slack profile %r connected (channel=%s)", name, profile.channel)
        log.info("slack bridge active profiles: %s", sorted(self._profiles))

    async def aclose(self) -> None:
        """Tear down every profile's Socket Mode connection."""
        for profile in self._profiles.values():
            handler = profile.handler
            if handler is not None:
                try:
                    await handler.close_async()
                except Exception:  # noqa: BLE001
                    log.exception("error closing socket for profile %r", profile.name)

    # -- the poll/route loop ----------------------------------------------

    async def poll_once(self) -> int:
        """One pull: fetch the tail past the cursor, route each note in seq order, and
        advance + persist the cursor after EACH handled note. Stops at the first note
        that fails to deliver (leaving the cursor before it) so the next poll re-sends
        it — at-least-once, never a dropped seq. Returns the count handled."""
        after = self._cursor.get()
        # Seed the dedupe high-water from the durable cursor so an already-handled seq
        # re-presented after a restart posts nothing new.
        self._handled_through = max(self._handled_through, after)
        data = await self._service.fetch_notifications(after=after, limit=_PULL_LIMIT)
        handled = 0
        for note in data.get("notifications", []):
            seq = note["seq"]
            if seq <= self._handled_through:
                continue  # dedupe: a re-delivered seq is idempotent — nothing new
            try:
                await self._handle(note)
            except Exception:  # noqa: BLE001 — do NOT advance past a failed seq
                log.exception("slack bridge failed to handle notification seq=%s; will retry", seq)
                break
            self._cursor.set(seq)              # advance + persist only after handling
            self._handled_through = seq
            handled += 1
        return handled

    async def _handle(self, note: dict) -> None:
        """Route one notification to its run's profile. Raises to signal a retryable
        delivery failure (the caller will not advance the cursor). A deliberate skip is
        NOT a failure — it returns normally so the cursor advances past it."""
        seq = note.get("seq")
        run_id = note.get("run_id", "")
        profile_name = note.get("slack_profile")

        if profile_name is None:
            log.debug("seq=%s: opt-out run (null profile) — no egress", seq)
            return  # handled: nothing to send

        profile = self._profiles.get(profile_name)
        if profile is None:
            log.info("seq=%s: profile %r not active on this host — skipping (another box may own it)",
                     seq, profile_name)
            return  # handled: not ours to deliver

        built = build_message(note, run_url=self._service.run_url(run_id))
        if built is None:
            log.debug("seq=%s: kind %r has no renderer — consumed", seq, note.get("kind"))
            return  # handled: nothing to post for this kind
        blocks, text = built

        kind = note.get("kind")
        thread_key = (profile_name, run_id)
        # The launch message is a run's thread ROOT; every later milestone threads under
        # it (per profile — a run has one profile, so its thread stays in one workspace).
        thread_ts = None if kind == NOTIFY_RUN_LAUNCHED else self._threads.get(thread_key)

        # One colored attachment carrying the blocks — a single render (no top-level +
        # attachment duplication), so a gate message's buttons appear exactly once.
        resp = await profile.bot_client.chat_postMessage(
            channel=profile.channel,
            text=text,
            attachments=[{"color": message_color(note), "blocks": blocks}],
            thread_ts=thread_ts,
        )

        if kind == NOTIFY_RUN_LAUNCHED:
            ts = resp.get("ts") if isinstance(resp, dict) else None
            if ts:
                self._threads[thread_key] = ts

        log.info("seq=%s: posted %s for run %s to %s/%s%s",
                 seq, kind, run_id, profile_name, profile.channel,
                 " (threaded)" if thread_ts else "")

    # -- the privileged path: resolve the gate from Slack ------------------

    def _register_listeners(self, app, profile: ActiveProfile) -> None:
        """Register this profile's go/no-go button + ``/mc`` slash-command listeners on
        ITS Bolt app, so interactions are handled per profile connection. The listeners
        are thin adapters: they ack, extract run_id + decision (never run content), and
        delegate to the authorization/relay core."""
        name = profile.name

        async def _on_button(ack, body, respond, client) -> None:
            await ack()  # Slack's 3s rule — ack first, then do the work
            action = (body.get("actions") or [{}])[0]
            decision = _BUTTON_DECISION.get(action.get("action_id"))
            await self.handle_gate_action(
                profile_name=name, decision=decision,
                run_id=action.get("value") or "",
                user_id=(body.get("user") or {}).get("id"),
                respond=respond, client=client,
                channel=(body.get("channel") or {}).get("id"),
                message_ts=(body.get("message") or {}).get("ts"),
            )

        app.action(ACTION_GO)(_on_button)
        app.action(ACTION_NOGO)(_on_button)

        async def _on_command(ack, command, respond, client) -> None:
            await ack()
            await self.handle_command(
                profile_name=name, command=command, respond=respond, client=client)

        app.command(SLASH_COMMAND)(_on_command)

    async def handle_gate_action(
        self,
        *,
        profile_name: str,
        decision: Optional[str],
        run_id: str,
        user_id: Optional[str],
        respond,
        client=None,
        channel: Optional[str] = None,
        message_ts: Optional[str] = None,
    ) -> str:
        """The identity gate for the Slack surface. Authorize PER PROFILE, then relay an
        authorized decision to the SAME endpoint the UI/CLI use. Returns an outcome
        token (``denied`` / ``resolved`` / ``conflict`` / ``error``) for testability.

        Order is deliberate: the acting user is checked against THIS profile's approver
        allowlist FIRST (cheap, no service call), so an unauthorized user triggers no
        read at all; then the run is looked up to confirm its profile MATCHES this
        connection's (reject cross-workspace relay) before any state-changing call."""
        prof = self._profiles.get(profile_name)
        if prof is None or not user_id or user_id not in prof.approvers:
            await respond(_NOT_AUTHORIZED)
            log.warning("AUDIT slack-gate DENIED reason=not-approver user=%s profile=%s "
                        "run=%s decision=%s", user_id, profile_name, run_id, decision)
            return "denied"

        run = await self._service.get_run(run_id)
        if run is None:
            await respond(f"Run `{run_id}` was not found.")
            log.warning("AUDIT slack-gate DENIED reason=unknown-run user=%s profile=%s run=%s",
                        user_id, profile_name, run_id)
            return "denied"
        if run.get("slack_profile") != profile_name:
            # Cross-workspace relay: an action on this profile's connection targeting a
            # run owned by a DIFFERENT profile. Refuse — never resolve another workspace.
            await respond(_NOT_AUTHORIZED)
            log.warning("AUDIT slack-gate DENIED reason=wrong-profile user=%s conn=%s run=%s "
                        "run_profile=%s", user_id, profile_name, run_id, run.get("slack_profile"))
            return "denied"

        endpoint = _DECISION_ENDPOINT.get(decision or "")
        if endpoint is None:
            await respond("Unrecognized decision.")
            return "error"

        relay = await self._service.resolve(run_id, endpoint)
        if relay.conflict:
            # One-shot across ALL surfaces/profiles: the seam already resolved this gate.
            await respond(f"↩︎ This gate was already resolved ({relay.detail or 'on another surface'}).")
            await self._render_resolved(client, channel, message_ts, run_id, decision, user_id,
                                        conflict=True)
            log.info("AUDIT slack-gate CONFLICT user=%s profile=%s run=%s decision=%s detail=%s",
                     user_id, profile_name, run_id, decision, relay.detail)
            return "conflict"
        if not relay.ok:
            await respond("Could not resolve the gate — please try again.")
            return "error"

        await self._render_resolved(client, channel, message_ts, run_id, decision, user_id)
        log.info("AUDIT slack-gate RESOLVED user=%s profile=%s run=%s decision=%s status=%s",
                 user_id, profile_name, run_id, decision, relay.status)
        return "resolved"

    async def handle_command(self, *, profile_name: str, command: dict, respond, client=None) -> str:
        """Handle a ``/mc`` slash command: ``approve|reject|cancel <run_id>`` (relayed
        through the same authorization/relay core as the buttons) or ``status
        [run_id]`` (a metadata-only read scoped to this workspace)."""
        parts = (command.get("text") or "").split()
        sub = parts[0].lower() if parts else ""
        arg = parts[1] if len(parts) > 1 else None
        user_id = command.get("user_id")

        if sub in _COMMAND_DECISION:
            if not arg:
                await respond(f"Usage: `{SLASH_COMMAND} {sub} <run_id>`")
                return "usage"
            return await self.handle_gate_action(
                profile_name=profile_name, decision=_COMMAND_DECISION[sub], run_id=arg,
                user_id=user_id, respond=respond, client=client)
        if sub == "status":
            return await self._status_reply(profile_name, arg, respond)

        await respond(f"Usage: `{SLASH_COMMAND} approve|reject|cancel <run_id>` "
                      f"or `{SLASH_COMMAND} status <run_id>`")
        return "usage"

    async def _status_reply(self, profile_name: str, run_id: Optional[str], respond) -> str:
        """Metadata-only status readout, scoped to this workspace (the run's profile
        must match this connection's — no cross-workspace peeking)."""
        if not run_id:
            await respond(f"Usage: `{SLASH_COMMAND} status <run_id>`")
            return "usage"
        run = await self._service.get_run(run_id)
        if run is None or run.get("slack_profile") != profile_name:
            await respond(f"Run `{run_id}` was not found in this workspace.")
            return "not-found"
        cost = run.get("cost_usd") or 0
        await respond(f"`{run_id}` — *{run.get('status')}* · ${float(cost):.4f}")
        return "status"

    async def _render_resolved(
        self, client, channel: Optional[str], message_ts: Optional[str],
        run_id: str, decision: Optional[str], user_id: Optional[str], *, conflict: bool = False,
    ) -> None:
        """Update the original gate message: drop the buttons and show who resolved it
        (or that it was already resolved on another surface). A no-op when there's no
        originating message (e.g. a slash command)."""
        if client is None or not channel or not message_ts:
            return
        label = _DECISION_LABEL.get(decision or "", decision or "")
        blocks, text = build_resolved_message(
            run_id, decision_label=label, user_id=user_id,
            run_url=self._service.run_url(run_id), conflict=conflict)
        try:
            await client.chat_update(
                channel=channel, ts=message_ts, text=text, blocks=[],
                attachments=[{"color": "#9aa0a6", "blocks": blocks}])
        except Exception:  # noqa: BLE001 — updating the message must never break the flow
            log.exception("failed to update resolved gate message run=%s", run_id)

    # -- scheduled fleet digest (per profile) ------------------------------

    async def post_digests(self, *, window_hours: Optional[float] = None) -> int:
        """Post ONE metadata-only fleet digest to each active profile's channel, scoped
        to the runs that named it (a null-profile run appears in no digest). Returns the
        number posted. Call on a schedule (daily / end-of-window)."""
        posted = 0
        for name, profile in list(self._profiles.items()):
            try:
                digest = await self._service.get_digest(name, hours=window_hours)
            except Exception:  # noqa: BLE001 — one profile's digest must not sink the rest
                log.exception("failed to fetch digest for profile %r", name)
                continue
            blocks, text = build_digest_message(digest)
            try:
                await profile.bot_client.chat_postMessage(
                    channel=profile.channel, text=text,
                    attachments=[{"color": "#4a90d9", "blocks": blocks}])
                posted += 1
            except Exception:  # noqa: BLE001
                log.exception("failed to post digest for profile %r", name)
        log.info("slack bridge posted %d fleet digest(s)", posted)
        return posted

    async def run_forever(self) -> None:
        """Connect, then poll/route on the interval until stopped."""
        await self.connect()
        if not self._profiles:
            raise BridgeConfigError("no active slack profiles after connect — nothing to run")
        try:
            while not self._stop.is_set():
                try:
                    await self.poll_once()
                except Exception:  # noqa: BLE001 — a transient service error must not kill the loop
                    log.exception("slack bridge poll failed; retrying after interval")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.aclose()

    def stop(self) -> None:
        self._stop.set()


# -- assembly + entry point ------------------------------------------------

async def assemble(
    *,
    registry: SlackRegistry,
    base_url: Optional[str],
    http,
    cursor: CursorStore,
    env: Optional[dict] = None,
    client_factory: ClientFactory = _default_client_factory,
    app_factory: AppFactory = _default_app_factory,
    socket_factory: SocketFactory = _default_socket_factory,
    validate: TokenValidator = _default_validate_tokens,
    auth_token: Optional[str] = None,
    poll_interval: float = _DEFAULT_POLL_SECONDS,
) -> SlackBridge:
    """Build a bridge from its parts, applying the fail-fast rules: a missing service
    base URL or zero resolvable profiles is fatal; a single unresolved profile is not.
    Profiles are live-validated against Slack during resolution (bad tokens are skipped,
    not fatal). ``auth_token`` (defaulting to ``MC_API_TOKEN``) is sent on every seam
    request so the bridge authenticates against the service's mutating endpoints."""
    if not base_url:
        raise BridgeConfigError(
            f"no service base URL — set {ENV_SERVICE_URL} to the Mission Control seam")
    source = env if env is not None else os.environ
    token = auth_token if auth_token is not None else (source.get(ENV_API_TOKEN) or None)
    profiles = await resolve_active_profiles(
        registry, env=env, client_factory=client_factory, validate=validate)
    if not profiles:
        raise BridgeConfigError(
            "no slack profiles resolved on this host — set the bot/app token env vars for "
            "at least one registry profile (or check MC_SLACK_REGISTRY)")
    return SlackBridge(
        profiles=profiles,
        service=ServiceClient(base_url, http, auth_token=token),
        cursor=cursor,
        app_factory=app_factory,
        socket_factory=socket_factory,
        poll_interval=poll_interval,
    )


async def _amain() -> None:
    import httpx

    registry = SlackRegistry.from_env()
    base_url = os.environ.get(ENV_SERVICE_URL)
    async with httpx.AsyncClient(timeout=30.0) as http:
        bridge = await assemble(
            registry=registry,
            base_url=base_url,
            http=http,
            cursor=CursorStore.from_env(),
        )
        await bridge.run_forever()


def main() -> None:
    """Console entry point (``mission-control-slack``)."""
    logging.basicConfig(
        level=os.environ.get("MC_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_amain())
    except BridgeConfigError as exc:
        log.error("slack bridge cannot start: %s", exc)
        raise SystemExit(2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":  # `python -m mission_control.slack.bridge` (used by launchd/systemd)
    main()
