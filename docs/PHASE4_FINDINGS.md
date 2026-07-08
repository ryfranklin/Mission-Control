# Phase 4 findings — durable execution (LangGraph) + MCP

The run is now a LangGraph `StateGraph` (dispatch → run_worker → gate → apply_burn
→ teardown) with a Postgres checkpointer; the `SdkWorker` and `roles.py` metaphor
are unchanged. End-to-end (`python -m mission_control.demo_phase4 --sdk`):

> burn runs (paid worker call) → **pauses at the durable go/no-go gate** →
> **hard-kill (exit 137)** → **restart** → interrupt survived, nothing applied →
> resume `go`: **worker not re-executed**, **applied once**, **clean teardown, no
> leak** → DuckDB analytics → **eval-gate over MCP → passed, exit_code 0**.

## Dollars saved: resume vs restart

Resume re-pays **$0** for work completed before the crash; a naive restart-from-zero
re-pays **100%** of it. Measured this run: the completed worker step cost
**$0.002636** (one Haiku call) — saved on resume (`worker calls=0`).

That's a few tenths of a cent because the demo has one cheap paid node. The saving
scales with completed, expensive work, and the analytics spine shows where it
lives: per 16-task run, **worker ≈ $0.07** and **judge ≈ $0.23** (Opus). A crash
after the worker steps but before completion saves the worker spend; after a
completed judge step, ~$0.03 each. Cross-run, **judge is 77% of all spend** — so
the biggest resume savings are judge steps, not worker steps.

## What node-boundary recovery cost in idempotency design

LangGraph recovers at **node boundaries** — a crash re-runs the *whole* node — so
every node had to be made idempotent. Concretely:

- **`dispatch`** reuses an existing worktree (guard on `worktree_path`); without it,
  a re-run would leak a second worktree / fail on branch-exists.
- **`apply_burn` is its own node** and re-run-safe: `commit` no-ops when clean,
  `git merge` no-ops when already merged → re-execution never double-applies. This
  is *why* apply-burn is a separate node — a clean boundary so a crash never
  half-applies a burn.
- **`teardown`** is forgiving (removing an already-gone worktree is a no-op).

Cost paid in a lesson: an `os._exit` **mid-node** loses that node's not-yet-committed
checkpoint (first attempt resumed from `dispatch`, not the gate). State is durable
**only at boundaries**. Corollary and real limitation: **`run_worker` is a single
node**, so a crash *during* the worker re-pays the entire worker call — resume only
saves you once the worker node has completed. Finer resume granularity would mean
splitting the worker into per-turn nodes (more nodes, more checkpoints).

## Interrupt gate vs the Phase 0–3 hand-rolled gate

| | Phase 0–3 gate | Phase 4 `interrupt()` gate |
|---|---|---|
| Mechanism | an `approval` callback called in-process | `interrupt()` → graph halts, persists, waits |
| Survives a process restart? | **No** — a "pause" died with the process | **Yes** — pause, kill, restart, then approve |
| Decision durability | in memory | persisted to Postgres, on record |
| Never-apply-without-approval | by control flow | by control flow **+** persisted decision + apply-burn guard |
| Cost | trivial (a function) | needs LangGraph + Postgres |

Same `go`/`no-go`/`scrub` semantics (`roles.py` untouched) — now a genuinely
durable human-in-the-loop pause. Demonstrated: `go` proceeds to apply-burn exactly
once; `no-go` scrubs with clean teardown; neither applies before approval.

## MCP round-trip surprises

- **The contract survived unchanged.** `exit_code`/`passed`/`axes` over MCP were
  byte-identical to the direct call (pass → 0, regression → 1). The Phase 3 CLI
  contract wrapped cleanly.
- **FastMCP structured content** was the one nuance: a dict-returning tool comes
  back as `structuredContent`, sometimes wrapped under a `result` key — the client
  handles both.
- **Per-call stdio spawn**: each MCP call launches the server subprocess + does the
  init handshake (~hundreds of ms). Fine for a CI gate; not for a hot loop.

## Demo-grade vs production (honest)

Demo-grade here:
- Recovery granularity is the **node**; `run_worker` is one node, so a mid-worker
  crash re-pays the whole worker call (see above).
- The "kill" is a real `os._exit`, but durable state exists only at node boundaries.
- Single-box **Postgres in Docker**, local-dev creds in `.env`, small pool, no
  TLS/auth/retention; `.setup()` run every start.
- The eval-gate **MCP server spawns per call** over stdio — no long-lived server,
  no auth; another agent/IDE pointing at it is trusted-local.
- Real dollars only with `--sdk`; `--demo`/StubWorker used for reproducibility.
- Idempotency holds because the **worktree is disposable** — a real worker's
  partial mid-node file writes aren't transactional; we rely on discard-on-crash.

Production graduation (documented, not built — per the decision doc): **Temporal**
for multi-host / long-lived (hours–days) runs, durable timers, and cross-process
retries; managed Postgres with auth/retention; a long-lived, authenticated MCP
server; and finer-grained worker nodes if per-turn resume savings justify the
extra checkpoints. LangGraph is right-sized for this single-box, human-gated proof.
