# Phase 5b findings — the control-room web UI

Phase 5b put a server-rendered UI (Jinja + htmx + the htmx SSE extension, no JS
build) on the existing service: a fleet dashboard, a launch control, a per-run
live station, wired write actions, and a cost/perf dashboard. This records what a
full run through the UI — **in a real browser** — actually showed.

**How this was measured.** `scripts/e2e_phase5b_browser.py` boots the real service
(durable PostgresSaver + runs ledger + a call-counting StubWorker with a 2s/step
delay to pace the feed) and drives **Chromium via Playwright** through the whole
flow. It runs inside one container on the compose network (browser + service on
container loopback, service → Postgres by name) because this box has a broken
host→container port-forward — an environment issue, not the system. Numbers below
are one representative run.

## The flow worked end-to-end, in a browser

| Step | Observed |
|---|---|
| fleet dashboard | Flight Director masthead + fleet table render |
| launch burn → run page | SSE timeline streams `dispatch → run_worker`; **0 cost ticks pre-gate** |
| gate | GO / NO-GO / SCRUB banner shown; registry `awaiting_gate` |
| click GO | `gate → apply_burn → step_metric → teardown` stream in; **1 cost tick** |
| terminal | final banner **"applied · total $0.003230 · reconciled"** |
| teardown | `STUB_BURN.txt` applied once, **worktrees = 1** (no leak) |
| fleet + /ui/metrics | both reflect the applied run |

Latency: **~2.47s page-load→gate** (of which ~2.0s is the injected worker delay;
~0.47s is page load + dispatch + SSE connect/replay), and **98ms from GO to the
final banner** — the post-approval burst (`gate`, `apply_burn`, `step_metric`,
`teardown`, `terminal`) streamed and rendered in under 100ms.

## Q1 — Does htmx + SSE hold up as the live transport?

Yes, for a single-operator localhost control room.

- **Latency**: sub-100ms to deliver and render a 5-event burst (GO→final). The
  htmx SSE extension appends fragments as they arrive; no polling lag on the run
  page. (The fleet, deliberately, is *polled* every 4s — cheap and good enough.)
- **Event loss**: none observed. Every scenario's timeline contained the full node
  set `dispatch, run_worker, gate, apply_burn, teardown` (+ `step_metric`,
  `terminal`). The monotonic `seq` watermark in the replay→tail handoff means a
  client can't get a gap or a duplicate.
- **Reconnect**: the browser's `EventSource` auto-reconnects on drop and sends
  `Last-Event-ID`; the endpoint resumes after that seq. We exercised the strong
  case — a full **service restart** — by reloading the page against the fresh
  process (below). We did *not* stress mid-stream network flaps; that's a
  hardening item, not a design gap.

Sending priced telemetry as server-rendered HTML fragments (not JSON) on a
UI-specific SSE endpoint kept the JSON `/runs/{id}/events` contract intact for the
CLI/API while letting htmx append rows directly — no client-side templating.

## Q2 — Did the durable replay store close the 5a "SSE-is-live-only" gap?

Yes — confirmed through the UI. After **killing and restarting the service**,
reopening a completed run's page reconstructed the **entire** timeline
(`dispatch → run_worker → gate → apply_burn → teardown` + terminal) purely from
the Postgres `run_events` log — the in-process channel was empty on the fresh
process. A run that was paused at the gate *through* the restart, when approved on
the new process, showed **history-from-store + the live resume leg = the full
timeline**, not just the resume leg (the exact 5a complaint). Across the whole
restart+resume, `worker_calls` stayed at one-per-run: **no re-pay**.

So the split is clean and now visible end-to-end: **SSE = live tail; the JSONL
spine + the registry + `run_events` = durable history**, and the UI reconstructs
from the latter before tailing the former.

## Q3 — Pre-gate "not-yet-reconciled" cost honesty

The station and dashboard never imply a pre-gate run is free. Pre-gate the run
page shows **zero cost ticks** and the header reads **"not yet reconciled"**; the
`/ui/metrics` rollup labels the figure **"reconciled cost"** with the explicit note
*"in-flight runs contribute $0 until they finish — unreconciled, not free."* Cost
only becomes a dollar figure at the terminal event (`applied · total $… ·
reconciled`). This is honest, but note the consequence: a burn parked at the gate
shows `$0.000000` in the per-target breakdown — correct, but a real operator will
want an "in-flight (unreconciled)" indicator rather than a bare `$0` (backlog).

## Q4 — What the UI still wanted from the seam (→ 5c / hardening backlog)

- **Fleet filter/sort controls.** The fleet UI only wires paging; the API already
  supports `status`/`target`/`order`, but there are no fleet-level filter/sort
  controls yet. First 5c/backlog item.
- **A durable "run finished" push.** The page learns the outcome from the terminal
  SSE event, but nothing notifies an operator who isn't looking at that page →
  this is exactly the 5c Slack client's job (a client of the same terminal event).
- **Target-scoped historical trend.** The registry rollup scopes by target/time,
  but the DuckDB trend is global — the JSONL telemetry has no `target` column, so
  per-target *history* isn't available without enriching the spine.
- **Mid-stream reconnect / multi-tab fan-out** under real network conditions is
  untested; and there's no server-push cancel feedback to *other* watchers.

## Demo-grade vs production (the graduation gate)

Still **localhost, no auth, single box**. The durable store closed the *history*
gap, but the **live SSE fan-out is in-process** — a run driven on one process
isn't live-tailable from another until its events are persisted and replayed. Two
things remain the graduation gate to production:

1. **Auth + identity** on every endpoint (the UI, CLI, and 5c Slack are all
   unauthenticated clients today).
2. **Multi-host durability**: a shared broker (or SSE reconstructed continuously
   from `run_events`) for live fan-out across replicas, plus a real job queue for
   launches (today a launch is an in-process background task; only the *graph*
   state is durable, so an orphaned run must be re-triggered).

Everything else — durable gate, replay, one-shot resolution, clean teardown, no
re-pay — held up through the browser.
