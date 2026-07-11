# Phase 5a findings — the service seam

The runtime now has one HTTP entry point (FastAPI wrapping `graph.py`) with four
clients planned: the CLI (built this phase), the 5b web UI, and the 5c Slack app.
This records what a full end-to-end run through the seam actually showed.

**How this was measured.** `scripts/e2e_phase5a.py` boots the real service
(PostgresSaver + runs ledger + a call-counting `StubWorker`), then drives the whole
flow over HTTP: a `sim`, then a `burn` watched to the gate, hard-killed mid-run,
restarted, resumed via the API. Numbers below are one representative run (StubWorker,
deterministic `$0.00323/step`). It was executed inside a container on the compose
network because this box currently has a broken host→container port-forward
(container-internal loopback and container→container are unaffected — an
environment issue, not the system).

## The flow worked end-to-end

| Step | Observed |
|---|---|
| launch `burn` → SSE | `dispatch → run_worker → gate_waiting`; registry `awaiting_gate`, `started_at` set, `ended_at` null |
| durable pause | nothing applied (`target head unchanged`), 2 worktrees live |
| **kill** mid-run | target still unchanged; 1 worktree **leaked** by the kill |
| **restart** service | registry **still `awaiting_gate`** — interrupt survived the process death |
| approve → resume | `gate → apply_burn → step_metric → teardown`; final `applied`, cost `$0.00323` |
| apply once + teardown | `STUB_BURN.txt` merged, head changed, worktrees back to **1** (leak cleaned up) |
| `GET /runs` · `/runs/{id}` | single burn row, `status=applied`, `cost_usd=0.00323`, `started/ended` stamped |
| `GET /metrics` | telemetry rollup across the two runs: 2 steps, `$0.00646` |

## Q1 — Does emitting priced telemetry as a `custom` event cost / reorder anything?

No reordering of node transitions, and no measurable cost. The observed merged
feed for the `sim` was:

```
node_transition(dispatch) → node_transition(run_worker) → node_transition(gate)
→ step_metric($0.00323) → node_transition(teardown)
```

The `step_metric` lands exactly where it's emitted — inside `teardown`, just before
that node's state update flushes — so it interleaves at its emission point and never
displaces a transition. Mechanically it's a single in-process `get_stream_writer()(dict)`
call per step: no network, no disk, no extra DB round-trip. The durable JSONL spine is
written from the *same* enriched events and is byte-identical to before the seam
existed (asserted by `test_live.py`). So "priced telemetry in the live feed" is a free
rider on the write that was already happening.

One real consequence of emitting at `teardown` (where the final outcome is known):
**a `burn` shows zero cost ticks before its gate** — the pre-gate feed was
`dispatch → run_worker → gate_waiting` with `cost_ticks_before_gate = []`. The cost
tick (`$0.00323`) only appears on the post-approval leg. That's correct (the run
hasn't finished a costed step's accounting until teardown), but a UI must not imply
"$0 so far" means "free" — it means "not yet reconciled."

## Q2 — SSE vs the JSONL historical split, in practice

They are two different things and the run made that concrete. The SSE feed is an
**in-memory, per-process** live view; the JSONL spine and the Postgres registry are
the **durable** record.

After the kill+restart, reconnecting SSE to the same `run_id` replayed **only the
resume leg** (`gate → apply_burn → step_metric → teardown`) — the pre-gate events
(`dispatch/run_worker/gate_waiting`) were gone, because they lived in the killed
process's memory. Meanwhile the registry row and the JSONL steps were fully intact.

Takeaway: SSE is the right tool for *following a live run* and the wrong tool for
*history*. History belongs to JSONL (analytics) and the registry (state). A client
that wants "show me everything that ever happened to this run" cannot get it from
SSE today — see Q4.

## Q3 — Did the runs table drift from the checkpointer under resume?

No drift. Through kill → restart → resume the run kept **one** registry row
(`burn_row_count = 1`) whose status moved monotonically
`queued → running → awaiting_gate → applied`, with `started_at`/`ended_at` each
stamped once and `cost_usd` set once (`0.00323`, not doubled).

The two stores stayed consistent because they're written at the same node
boundaries the checkpointer recovers at, and both are idempotent: the registry via
upsert-by-`run_id`, the graph via whole-node re-run. The decisive evidence is the
**call-counting worker: 2 calls before the kill (sim + burn), still 2 after resume** —
the completed `run_worker` node was *not* re-executed, so resume paid `$0` extra, and
the registry didn't invent a second run or reset the first. Registry `awaiting_gate`
after restart matched the checkpointer's paused position exactly.

## Q4 — What the CLI-as-first-client refactor revealed about the seam

Building the CLI purely as an API client (it never imports `graph.py`) proved the
endpoint set is *sufficient* for the core lifecycle: `launch / watch / runs /
approve / reject / scrub / detail / metrics` were all the CLI needed. What the CLI
had to work *around* is the spec for the 5b UI:

- **No terminal event on the SSE stream.** `watch` can't learn the final
  status/cost from the stream itself — it has to `GET /runs/{id}` after the stream
  closes to compute its exit code. The stream should emit an explicit terminal event
  carrying `{status, cost_usd}`.
- **No durable event history / `Last-Event-ID` replay store.** After a restart the
  feed can't be reconstructed (Q2). A UI timeline needs replay from a store (or
  synthesized from the JSONL spine), not just the in-memory channel.
- **`GET /runs` has no paging/sort.** The shared registry already held **79 rows**
  in this environment; the endpoint returns all of them. The store supports `limit`;
  the API should expose `limit`/`offset`/`order` and a time filter.
- **`scrub` only works at the gate.** It resumes the interrupt with a no-go; there's
  no clean kill for a run mid-node. A UI "stop" button needs a real cancel path.
- **`/metrics` is global.** No per-target or time-range scoping — a UI dashboard
  will want both.
- **No identity/auth** on any endpoint (below).

None of these blocked the CLI; all of them are concrete 5b/5c requirements.

## Demo-grade vs production (honest boundaries)

- **Localhost, no auth.** The service binds `127.0.0.1` and every endpoint is open.
  Fine for one operator on one box; a prerequisite before any shared/UI/Slack use.
- **Single box, single process.** SSE fan-out is an in-process dict of channels, so
  it does not survive a restart (Q2) and would not work across replicas — horizontal
  scaling needs a shared broker (or SSE reconstructed from a durable log).
- **Launch is a background `asyncio` task, not a job queue.** No backpressure, no
  retry, no scheduling. What survives a crash is the *durable graph state*, not the
  in-flight driver: the kill orphaned the run at its checkpoint, and it only moved
  again because a human re-triggered it via `approve`. A production system wants a
  real queue/worker that auto-resumes orphaned runs and bounds concurrency.
- **Crash cleanup is deferred.** The kill leaked a worktree that was only reclaimed
  by the resume's `teardown`. A run that's killed and *never* resumed leaks until
  something reaps it — there is no sweeper.
- **`StubWorker`, single Postgres.** Deterministic offline cost; no real LLM spend,
  no HA/backups for the durable store.
