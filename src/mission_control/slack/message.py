"""Block Kit rendering for the notification catalog — METADATA ONLY.

Every builder reads ONLY the notification's metadata fields (target name, sim/burn,
status, cost, timestamps). There is no access to and no field for prompt/code/diff/
target content, so a rendered message can never leak run content by construction.

Control-room styling is consistent across kinds: a header line with a status
emoji, a compact fields grid, and a context footer carrying the run id + a link to
the 5b run page. Metaphor labels come from :mod:`roles` — never spelled here.

One builder per outbox kind, dispatched by :func:`build_message`:

* ``run_launched`` — a Controller left the pad.
* ``gate_awaiting`` — a burn is HOLDING at go/no-go and needs a decision. Cost-so-far
  is shown honestly as "not yet reconciled" — never "$0" (a run at the gate has not
  reconciled cost; $0 would imply free — see 5a Q1 / 5b Q3).
* ``run_terminal`` — the run ended; framed by status (completed / scrubbed / failed).
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from .. import roles
from ..runs_store import (
    NOTIFY_COST_THRESHOLD,
    NOTIFY_GATE_AWAITING,
    NOTIFY_REGRESSION,
    NOTIFY_RUN_LAUNCHED,
    NOTIFY_RUN_TERMINAL,
    STATUS_APPLIED,
    STATUS_AWAITING_GATE,
    STATUS_BLOCKED_SECRETS,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_MERGE_CONFLICT,
    STATUS_PUSH_REJECTED,
    STATUS_QUEUED,
    STATUS_SCRUBBED,
)

# The phrase shown for a run's cost while it is still in flight (at the gate): its cost
# is NOT reconciled until teardown, so we never print a dollar amount that would imply
# $0 == free (5a Q1 / 5b Q3).
COST_UNRECONCILED = "not yet reconciled"

# Block Kit action ids for the go/no-go buttons on a gate message. Stable strings the
# bridge registers per-profile listeners against; each button carries ONLY the run_id
# as its value (never any run content).
ACTION_GO = "mc_go"
ACTION_NOGO = "mc_nogo"
_GATE_ACTIONS_BLOCK = "mc_gate_actions"

# status -> (emoji, Slack attachment color). Statuses are FUNCTIONAL labels (the runs
# ledger), not metaphor vocabulary, so they're safe to map here.
_STATUS_STYLE = {
    STATUS_QUEUED: ("🚀", "#4a90d9"),            # blue — just launched
    STATUS_AWAITING_GATE: ("🟡", "#daa038"),     # amber — holding at go/no-go
    STATUS_APPLIED: ("✅", "#2eb886"),           # green — burn merged
    STATUS_DONE: ("✅", "#2eb886"),              # green — sim complete
    STATUS_FAILED: ("🛑", "#a30200"),            # red — failed / cancelled mid-node
    STATUS_SCRUBBED: ("⚪", "#9aa0a6"),          # grey — scrubbed at the gate / torn down
    STATUS_PUSH_REJECTED: ("⚠️", "#daa038"),     # amber — approved, push non-ff
    STATUS_MERGE_CONFLICT: ("⚠️", "#daa038"),    # amber — approved, integrate conflicted
    STATUS_BLOCKED_SECRETS: ("🔒", "#daa038"),   # amber — egress blocked
}
_DEFAULT_STYLE = ("•", "#9aa0a6")

# The terminal headline verb by final status. A clean finish reads as "completed"; a
# scrub / failure / held outcome just echoes its ledger status (e.g. "scrubbed",
# "failed", "merge_conflict"), so the framing differs without inventing new vocabulary.
_TERMINAL_HEADLINE = {
    STATUS_APPLIED: "completed",
    STATUS_DONE: "completed",
}


# -- shared helpers --------------------------------------------------------

def _style(status: str) -> tuple[str, str]:
    return _STATUS_STYLE.get(status, _DEFAULT_STYLE)


def _task_label(task_type: Optional[str]) -> str:
    """The metaphor label for the task kind — sim / burn, sourced from roles."""
    return {roles.SIM: roles.SIM, roles.BURN: roles.BURN}.get(task_type or "", task_type or "—")


def _fmt_cost(cost) -> str:
    try:
        return f"${float(cost):.4f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_duration(started: Optional[str], ended: Optional[str]) -> Optional[str]:
    """Human elapsed time from two ISO timestamps, or None if either is missing/bad."""
    if not started or not ended:
        return None
    try:
        secs = int((datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds())
    except (TypeError, ValueError):
        return None
    secs = max(0, secs)
    if secs < 60:
        return f"{secs}s"
    mins, rem = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m {rem}s"
    hours, rem_m = divmod(mins, 60)
    return f"{hours}h {rem_m}m"


def _field(label: str, value: str) -> dict:
    return {"type": "mrkdwn", "text": f"*{label}*\n{value}"}


def _assemble(header: str, fields: list[dict], run_id: str, run_url: Optional[str]) -> list[dict]:
    """The consistent control-room layout: header + compact fields grid + a context
    footer with the run id and a link to the run page."""
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}},
        {"type": "section", "fields": fields},
    ]
    context = [{"type": "mrkdwn", "text": f"`{run_id}`"}]
    if run_url:
        context.append({"type": "mrkdwn", "text": f"<{run_url}|View run>"})
    blocks.append({"type": "context", "elements": context})
    return blocks


# -- per-kind builders -----------------------------------------------------

def build_launched_message(note: dict, *, run_url: Optional[str] = None) -> tuple[list[dict], str]:
    """``run_launched`` — a Controller left the pad."""
    payload = note.get("payload") or {}
    run_id = note.get("run_id", "")
    emoji, _c = _style(payload.get("status") or STATUS_QUEUED)
    target = payload.get("target") or "—"
    task = _task_label(payload.get("task_type"))
    header = f"{emoji}  {roles.WORKER} left the pad"
    fields = [_field("Target", target), _field("Type", task), _field("Run", f"`{run_id}`")]
    text = f"{roles.WORKER} {task} launched on {target}"
    return _assemble(header, fields, run_id, run_url), text


def build_gate_message(note: dict, *, run_url: Optional[str] = None) -> tuple[list[dict], str]:
    """``gate_awaiting`` — a burn is HOLDING at go/no-go and needs a decision. Cost is
    shown as "not yet reconciled" (never $0). Notify-only in this slice; go/no-go
    buttons arrive in S4."""
    payload = note.get("payload") or {}
    run_id = note.get("run_id", "")
    emoji, _c = _style(payload.get("status") or STATUS_AWAITING_GATE)
    target = payload.get("target") or "—"
    task = _task_label(payload.get("task_type"))
    header = f"{emoji}  {roles.WORKER} holding at go/no-go"
    fields = [
        _field("Target", target),
        _field("Type", task),
        _field("Cost so far", COST_UNRECONCILED),
        _field("Needs", "a go / no-go decision"),
    ]
    text = f"{roles.WORKER} holding at go/no-go on {target} — decision needed"
    blocks = _assemble(header, fields, run_id, run_url)
    # Interactive go/no-go buttons (an alternative to the /mc slash command). The button
    # value carries ONLY the run_id; labels come from roles (go / no-go). Inserted before
    # the context footer so the buttons sit under the fields.
    blocks.insert(-1, _gate_actions_block(run_id))
    return blocks, text


def _gate_actions_block(run_id: str) -> dict:
    return {
        "type": "actions",
        "block_id": _GATE_ACTIONS_BLOCK,
        "elements": [
            {"type": "button", "action_id": ACTION_GO, "style": "primary",
             "text": {"type": "plain_text", "text": roles.GO.upper()}, "value": run_id},
            {"type": "button", "action_id": ACTION_NOGO, "style": "danger",
             "text": {"type": "plain_text", "text": roles.NO_GO.upper()}, "value": run_id},
        ],
    }


def build_terminal_message(note: dict, *, run_url: Optional[str] = None) -> tuple[list[dict], str]:
    """``run_terminal`` — the run ended. Framed by final status (completed / scrubbed /
    failed / …), with the reconciled total cost and duration."""
    payload = note.get("payload") or {}
    run_id = note.get("run_id", "")
    status = payload.get("status") or "—"
    emoji, _c = _style(status)
    headline = _TERMINAL_HEADLINE.get(status, status)
    target = payload.get("target") or "—"
    task = _task_label(payload.get("task_type"))
    cost = _fmt_cost(payload.get("cost_usd"))
    duration = _fmt_duration(payload.get("started_at"), payload.get("ended_at"))

    header = f"{emoji}  {roles.WORKER} {headline}"
    fields = [
        _field("Target", target),
        _field("Type", task),
        _field("Status", status),
        _field("Cost", cost),
    ]
    if duration is not None:
        fields.append(_field("Duration", duration))
    text = f"{roles.WORKER} {task} on {target} — {status} ({cost})"
    return _assemble(header, fields, run_id, run_url), text


def build_resolved_message(
    run_id: str, *, decision_label: str, user_id: Optional[str] = None,
    run_url: Optional[str] = None, conflict: bool = False,
) -> tuple[list[dict], str]:
    """Render the REPLACEMENT for a gate message once it's been resolved: the go/no-go
    buttons are gone and a line records who decided (or that it was already resolved on
    another surface). Metadata only — decision + acting user id, no run content."""
    if conflict:
        line = "🔒  This gate was already resolved on another surface."
    elif user_id:
        line = f"✔️  Resolved *{decision_label}* by <@{user_id}>"
    else:
        line = f"✔️  Resolved *{decision_label}*"
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": line}}]
    context = [{"type": "mrkdwn", "text": f"`{run_id}`"}]
    if run_url:
        context.append({"type": "mrkdwn", "text": f"<{run_url}|View run>"})
    blocks.append({"type": "context", "elements": context})
    return blocks, line


def build_cost_alert_message(note: dict, *, run_url: Optional[str] = None) -> tuple[list[dict], str]:
    """``cost_threshold`` — a run crossed a per-run cost threshold, or its target's
    rolling window crossed a budget. Metadata only (amounts + threshold, no content)."""
    payload = note.get("payload") or {}
    run_id = note.get("run_id", "")
    target = payload.get("target") or "—"
    task = _task_label(payload.get("task_type"))
    header = "💸  Cost alert"
    if payload.get("reason") == "window":
        fields = [
            _field("Target", target),
            _field("Window total", _fmt_cost(payload.get("window_total"))),
            _field("Budget", _fmt_cost(payload.get("budget"))),
            _field("Window", f"{payload.get('window_hours', '—')}h"),
        ]
        text = f"Cost alert: {target} window total {_fmt_cost(payload.get('window_total'))} " \
               f"crossed budget {_fmt_cost(payload.get('budget'))}"
    else:
        fields = [
            _field("Target", target),
            _field("Type", task),
            _field("Cost", _fmt_cost(payload.get("cost_usd"))),
            _field("Threshold", _fmt_cost(payload.get("threshold"))),
        ]
        text = f"Cost alert: {task} on {target} cost {_fmt_cost(payload.get('cost_usd'))} " \
               f"crossed {_fmt_cost(payload.get('threshold'))}"
    return _assemble(header, fields, run_id, run_url), text


def build_regression_message(note: dict, *, run_url: Optional[str] = None) -> tuple[list[dict], str]:
    """``regression`` — the eval gate reported a quality/cost regression. Shows each
    regressed axis' baseline band vs observed value. Metadata only — never eval content."""
    payload = note.get("payload") or {}
    run_id = note.get("run_id", "")
    axes = payload.get("axes") or []
    header = "📉  Quality/cost regression"
    fields = [_field("Target", payload.get("target") or "—")]
    for axis in axes:
        metric = axis.get("metric", "metric")
        bound = "≤" if axis.get("higher_is_worse") else "≥"
        observed = _fmt_num(axis.get("current"))
        thr = _fmt_num(axis.get("threshold"))
        mean = _fmt_num(axis.get("baseline_mean"))
        fields.append(_field(metric, f"observed {observed}\nband {bound}{thr} (μ {mean})"))
    axis_names = ", ".join(a.get("metric", "?") for a in axes) or "—"
    text = f"Regression on {payload.get('target') or '—'}: {axis_names}"
    return _assemble(header, fields, run_id, run_url), text


def _fmt_num(v) -> str:
    try:
        return f"{float(v):.4g}"
    except (TypeError, ValueError):
        return "—"


def build_digest_message(digest: dict) -> tuple[list[dict], str]:
    """A per-profile fleet digest (NOT an outbox row — the bridge posts it directly): N
    runs, status counts, total cost, top targets by cost. Metadata only."""
    profile = digest.get("profile", "—")
    runs = digest.get("runs", 0)
    by_status = digest.get("by_status") or {}
    window = digest.get("window_hours")
    span = f"last {window:g}h" if window else "all time"
    header = f"🛰️  Fleet digest — {profile} ({span})"

    def _count(*statuses) -> int:
        return sum(by_status.get(s, 0) for s in statuses)

    fields = [
        _field("Runs", str(runs)),
        _field("Total cost", _fmt_cost(digest.get("cost_usd"))),
        _field("Applied", str(_count(STATUS_APPLIED))),
        _field("Scrubbed", str(_count(STATUS_SCRUBBED))),
        _field("Failed", str(_count(STATUS_FAILED))),
    ]
    top = digest.get("top_targets") or []
    if top:
        lines = "\n".join(f"{t['target']} — {_fmt_cost(t['cost_usd'])}" for t in top[:5])
        fields.append(_field("Top targets by cost", lines))
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}},
        {"type": "section", "fields": fields},
    ]
    text = f"Fleet digest for {profile}: {runs} runs, {_fmt_cost(digest.get('cost_usd'))}"
    return blocks, text


# kind -> builder. The catalog the bridge renders; an unknown kind returns None so the
# bridge consumes it without posting.
_BUILDERS: dict[str, Callable[..., tuple[list[dict], str]]] = {
    NOTIFY_RUN_LAUNCHED: build_launched_message,
    NOTIFY_GATE_AWAITING: build_gate_message,
    NOTIFY_RUN_TERMINAL: build_terminal_message,
    NOTIFY_COST_THRESHOLD: build_cost_alert_message,
    NOTIFY_REGRESSION: build_regression_message,
}


def build_message(note: dict, *, run_url: Optional[str] = None) -> Optional[tuple[list[dict], str]]:
    """Render any catalog notification to ``(blocks, text)`` by its ``kind``; ``None``
    for a kind with no renderer (the bridge consumes it silently)."""
    builder = _BUILDERS.get(note.get("kind") or "")
    return builder(note, run_url=run_url) if builder else None


def message_color(note: dict) -> str:
    """The attachment color for a note's status (for callers that wrap blocks in a
    colored attachment)."""
    status = (note.get("payload") or {}).get("status") or ""
    return _style(status)[1]
