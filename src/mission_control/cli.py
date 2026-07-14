"""Mission Control CLI — a thin CLIENT of the service API (the 5a seam).

Every command is an HTTP call to the FastAPI service in :mod:`mission_control.service`.
The CLI NEVER imports ``graph.py`` / the orchestrator — it only launches, queries,
and streams runs through the service endpoints. That proves the service is the
single entry point into the runtime and the CLI is just one client of it.

    The 5b web UI and the 5c Slack app are ADDITIONAL clients of these SAME
    endpoints (POST /runs, /runs/{id}/{approve,reject,scrub}, GET /runs,
    GET /runs/{id}, GET /runs/{id}/events, GET /metrics). No client re-implements
    orchestration; they all talk to the seam.

Commands:
    launch <target> --type sim|burn [--watch]   POST /runs
    watch|follow <run_id>                        GET  /runs/{id}/events  (SSE)
    runs [--status S] [--target T]               GET  /runs
    approve|reject|scrub <run_id>                POST /runs/{id}/...

Exit codes (a small, documented contract):
    0  success — command accepted, or a watched run reached ``applied`` / ``done``
    1  failure — a watched run ``failed``, an HTTP/transport error, or a rejected command
    2  scrubbed — a watched run ended ``scrubbed`` (no-go / killed), i.e. not applied

Metaphor vocabulary in the output comes straight from :mod:`.roles`.

Point it at a service with ``--base-url`` or ``$MC_SERVICE_URL`` (default
``http://127.0.0.1:8000`` — the localhost bind of ``python -m mission_control.service``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

import httpx

from . import roles
from .runs_store import (
    STATUS_APPLIED,
    STATUS_BLOCKED_SECRETS,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_MERGE_CONFLICT,
    STATUS_PUSH_REJECTED,
    STATUS_SCRUBBED,
    TERMINAL_STATUSES,
)

DEFAULT_BASE_URL = os.environ.get("MC_SERVICE_URL", "http://127.0.0.1:8000")

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_SCRUBBED = 2
EXIT_PUSH_REJECTED = 3  # applied locally, but the push didn't land
EXIT_MERGE_CONFLICT = 4  # approved, but integrating the remote conflicted (operator must resolve)
EXIT_BLOCKED_SECRETS = 5  # egress blocked: staged content had a secret/PII

# Terminal run status → CLI exit code (the watch/launch--watch contract).
_EXIT_FOR_STATUS = {
    STATUS_APPLIED: EXIT_OK,
    STATUS_DONE: EXIT_OK,
    STATUS_FAILED: EXIT_FAILURE,
    STATUS_SCRUBBED: EXIT_SCRUBBED,
    STATUS_PUSH_REJECTED: EXIT_PUSH_REJECTED,
    STATUS_MERGE_CONFLICT: EXIT_MERGE_CONFLICT,
    STATUS_BLOCKED_SECRETS: EXIT_BLOCKED_SECRETS,
}


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _http_error(exc: httpx.HTTPStatusError) -> int:
    """Render a 4xx/5xx from the service and map it to a failure exit code."""
    try:
        detail = exc.response.json().get("detail", exc.response.text)
    except Exception:  # noqa: BLE001
        detail = exc.response.text
    _err(f"{roles.ORCHESTRATOR}: request failed [{exc.response.status_code}] — {detail}")
    return EXIT_FAILURE


# -- commands --------------------------------------------------------------

def cmd_launch(client: httpx.Client, args: argparse.Namespace) -> int:
    """POST /runs — dispatch a Controller against a target through the seam."""
    body = {"target": args.target, "task_type": args.type}
    if args.prompt:
        body["prompt"] = args.prompt
    try:
        resp = client.post("/runs", json=body)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _http_error(exc)
    except httpx.HTTPError as exc:
        _err(f"{roles.ORCHESTRATOR}: cannot reach the service at {client.base_url} — {exc}")
        return EXIT_FAILURE

    run = resp.json()
    run_id = run["run_id"]
    print(
        f"{roles.ORCHESTRATOR} launched a '{run['task_type']}' run "
        f"→ {run_id}  ({roles.WORKER} dispatching against {run['target']})"
    )
    if args.watch:
        return _watch(client, run_id)
    print(f"  follow it:  mission-control watch {run_id}")
    return EXIT_OK


def cmd_watch(client: httpx.Client, args: argparse.Namespace) -> int:
    """GET /runs/{id}/events (SSE) — follow the merged live feed to a terminal state."""
    return _watch(client, args.run_id)


def cmd_runs(client: httpx.Client, args: argparse.Namespace) -> int:
    """GET /runs — the S2 registry, with optional status/target filters."""
    params = {}
    if args.status:
        params["status"] = args.status
    if args.target:
        params["target"] = args.target
    try:
        resp = client.get("/runs", params=params)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _http_error(exc)
    except httpx.HTTPError as exc:
        _err(f"{roles.ORCHESTRATOR}: cannot reach the service at {client.base_url} — {exc}")
        return EXIT_FAILURE

    runs = resp.json()["runs"]
    print(f"{roles.ORCHESTRATOR} — {len(runs)} run(s)")
    print(f"  {'run_id':40}  {'type':4}  {'status':13}  {'cost':>10}  target")
    for r in runs:
        print(
            f"  {r['run_id']:40}  {(r['task_type'] or '-'):4}  {r['status']:13}  "
            f"${r['cost_usd']:>9.6f}  {r['target'] or '-'}"
        )
    return EXIT_OK


def _decision_command(action: str, sent_label: str):
    """Build an approve/reject/scrub command that POSTs the matching endpoint."""

    def run(client: httpx.Client, args: argparse.Namespace) -> int:
        try:
            resp = client.post(f"/runs/{args.run_id}/{action}")
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return _http_error(exc)
        except httpx.HTTPError as exc:
            _err(f"{roles.ORCHESTRATOR}: cannot reach the service at {client.base_url} — {exc}")
            return EXIT_FAILURE
        row = resp.json()
        print(f"{roles.ORCHESTRATOR}: {sent_label} → {row['run_id']}  (status={row['status']})")
        return EXIT_OK

    return run


# -- the SSE follower ------------------------------------------------------

def _watch(client: httpx.Client, run_id: str) -> int:
    """Consume the run's SSE feed, printing node transitions and a live cost tick,
    then exit per the run's terminal status."""
    print(f"{roles.ORCHESTRATOR} following {run_id} — {roles.WORKER} live feed:")
    running_cost = 0.0
    saw_error = False
    try:
        # timeout=None: follow indefinitely (server pings keep the line warm); a
        # run that pauses at the gate blocks here until it's resolved elsewhere.
        with client.stream("GET", f"/runs/{run_id}/events", timeout=None) as resp:
            if resp.status_code == 404:
                _err(f"{roles.ORCHESTRATOR}: no such run: {run_id}")
                return EXIT_FAILURE
            resp.raise_for_status()
            event: Optional[str] = None
            data_line: Optional[str] = None
            for raw in resp.iter_lines():
                line = raw.rstrip("\r")
                if line.startswith(":"):          # keepalive comment
                    continue
                if line == "":                    # blank line dispatches one event
                    if event is not None:
                        running_cost, err = _print_event(event, data_line, running_cost)
                        saw_error = saw_error or err
                    event = data_line = None
                    continue
                field, _, value = line.partition(":")
                field, value = field.strip(), value.strip()
                if field == "event":
                    event = value
                elif field == "data":
                    data_line = value
    except httpx.HTTPStatusError as exc:
        return _http_error(exc)
    except httpx.HTTPError as exc:
        _err(f"{roles.ORCHESTRATOR}: lost the live feed for {run_id} — {exc}")
        return EXIT_FAILURE

    # The stream closes on a terminal run; read the final status for the exit code.
    status = _final_status(client, run_id)
    print(f"run {run_id} → {status}  (cost ${running_cost:.6f})")
    if saw_error and status not in TERMINAL_STATUSES:
        return EXIT_FAILURE
    return _EXIT_FOR_STATUS.get(status, EXIT_FAILURE)


def _print_event(event: str, data_line: Optional[str], running_cost: float) -> tuple[float, bool]:
    """Render one SSE event; return the updated running cost and whether it was an error."""
    data = {}
    if data_line:
        try:
            data = json.loads(data_line)
        except json.JSONDecodeError:
            data = {}
    if event == "node_transition":
        print(f"  → {data.get('node', '?')}")
    elif event == "step_metric":
        step = data.get("event", {})
        delta = float(step.get("cost_usd", 0.0))
        running_cost += delta
        print(
            f"  $ cost ${running_cost:.6f}  (+${delta:.6f}  "
            f"{step.get('model', '?')} step {step.get('step_id', '?')})"
        )
    elif event == "gate_waiting":
        print(f"  ⏸ {roles.WORKER} awaiting {roles.GO}/{roles.NO_GO} decision")
    elif event == "error":
        _err(f"  ✗ {data.get('message', 'run failed')}")
        return running_cost, True
    return running_cost, False


def _final_status(client: httpx.Client, run_id: str) -> str:
    try:
        resp = client.get(f"/runs/{run_id}")
        resp.raise_for_status()
        return resp.json()["status"]
    except httpx.HTTPError:
        return "unknown"


# -- parser + entry point --------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mission-control",
        description=f"{roles.ORCHESTRATOR} CLI — a client of the Mission Control service API.",
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL,
        help=f"service base URL (default: {DEFAULT_BASE_URL}; or $MC_SERVICE_URL)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_launch = sub.add_parser("launch", help=f"dispatch a {roles.WORKER} against a target")
    p_launch.add_argument("target", help="path to the target git repository")
    p_launch.add_argument(
        "--type", "-t", choices=[roles.SIM, roles.BURN], default=roles.SIM,
        help=f"'{roles.SIM}' (read-only) or '{roles.BURN}' (side-effectful); default {roles.SIM}",
    )
    p_launch.add_argument("--prompt", "-p", default=None, help="instruction for the worker")
    p_launch.add_argument("--watch", "-w", action="store_true", help="follow the live feed after launch")
    p_launch.set_defaults(func=cmd_launch)

    for name in ("watch", "follow"):
        p = sub.add_parser(name, help="follow a run's merged live feed (SSE)")
        p.add_argument("run_id")
        p.set_defaults(func=cmd_watch)

    p_runs = sub.add_parser("runs", help="list runs from the registry")
    p_runs.add_argument("--status", default=None, help="filter by status")
    p_runs.add_argument("--target", default=None, help="filter by target path")
    p_runs.set_defaults(func=cmd_runs)

    p_approve = sub.add_parser("approve", help=f"resolve the gate with {roles.GO} (→ apply-burn)")
    p_approve.add_argument("run_id")
    p_approve.set_defaults(func=_decision_command("approve", f"{roles.GO} sent"))

    p_reject = sub.add_parser("reject", help=f"resolve the gate with {roles.NO_GO} (→ {roles.SCRUB})")
    p_reject.add_argument("run_id")
    p_reject.set_defaults(func=_decision_command("reject", f"{roles.NO_GO} sent ({roles.SCRUB})"))

    p_scrub = sub.add_parser("scrub", help=f"{roles.SCRUB} a run (kill + clean teardown)")
    p_scrub.add_argument("run_id")
    p_scrub.set_defaults(func=_decision_command("scrub", f"{roles.SCRUB} sent"))

    return parser


def main(argv: Optional[list[str]] = None, *, client: Optional[httpx.Client] = None) -> int:
    """Parse args and dispatch. ``client`` is injectable so tests can drive the CLI
    against an in-process ``TestClient`` (the same httpx surface as a live server)."""
    args = build_parser().parse_args(argv)
    own_client = client is None
    if own_client:
        client = httpx.Client(base_url=args.base_url)
    try:
        return args.func(client, args)
    finally:
        if own_client:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
