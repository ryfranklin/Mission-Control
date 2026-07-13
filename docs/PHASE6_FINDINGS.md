# Phase 6 findings — the interactive planner and the plan→run hand-off

Phase 6 puts a durable **plan** in front of the runtime: an interactive INCEPTION
session (operator ↔ planner) that accretes requirements + a CONSTRUCTION work-list,
then **hands the finalized plan to Mission Control**, which translates its units into
runs on the existing launch path. This records what a full end-to-end pass through
the service actually showed.

**How this was measured.** `scripts/e2e_phase6.py` boots the real service
(`build_default_manager`: PostgresSaver + runs ledger + PLAN store + planner engine +
`PlanBuilder`, `StubWorker`) and drives the whole story over HTTP — greenfield and
brownfield, defaults + override, and a hard kill+restart mid-session. Numbers below
are one representative run (StubWorker, deterministic `$0.00323/step`). The
mechanics that need a human at the gate (nothing-applied-without-go, rejected-gate)
are pinned by `tests/test_plan_build.py`. v1 security model: **localhost / no auth.**

## Both paths worked end-to-end

| Path | Observed |
|---|---|
| greenfield open | defaults **`aidlc` / `aws`** (env-overridable per request) |
| greenfield Q&A | 4 operator turns → 8-line transcript → `ready`; each reply carried AI-DLC `[Answer]:` questions until the last |
| greenfield hand-off | no target → a workspace was **scaffolded**; built **4 sim + 3 burn** runs (the 3 burns each approved at the gate) → `done`, marker applied, **1 worktree** (no leak), `$0.02261` |
| override | a session set `custom-mm` / `gcp` and it **stuck** |
| brownfield detection | opened `greenfield`, workspace detection **flipped it to `brownfield`** on the first turn |
| reverse-engineering | a real **`sim`** run (`done`, `$0.00323`, `[stub] investigated (read-only)`) folded `reverse_engineering:{summary,run}` into requirements + a `Reverse Engineering` stage unit |
| readiness loop | gate `{scope, components, acceptance, units}` all **false** → after 4 clarifying turns all **true** → `ready` |
| brownfield hand-off | built **2 sim + 3 burn**, all applied, no leak, `$0.01615` |
| durability | killed mid-session, restarted on the SAME Postgres → plan/transcript/requirements/units **intact**, session **resumed** to `ready` |

## Q1 — Does interactive INCEPTION produce a plan Mission Control can execute?

Yes. The greenfield walk produced a well-formed, executable work-list with **no
hand-editing**:

```
INCEPTION (sim):  Workspace Detection · Requirements Analysis · Workflow Planning · Units Generation
CONSTRUCTION (burn): Scaffold the project (targeting aws) · Implement the core logic · Add tests and wire CI
```

Every unit carries a `task_type` **derived from its phase** (`aidlc.task_type_for_phase`),
so the builder never guesses. The build consumed the list unchanged: 7 child runs, the
sims auto-completing and the burns gating, ending `done` with the change applied. The
`(targeting aws)` unit title confirms the plan is written **against `cloud_target`**.

## Q2 — Is the greenfield/brownfield branch + the readiness gate right?

Right, and the two gates are genuinely different:

- **Branch on detected code, not the operator's word.** The brownfield session was
  *opened* `greenfield`; workspace detection saw code in the target and set
  `mode=brownfield`, which then routed into reverse-engineering. Declared intent is a
  hint; the repo is the truth.
- **Greenfield gate = stages in place.** `ready` once the always-execute INCEPTION
  stages are laid down.
- **Brownfield gate = requirements readiness.** `{scope, components, acceptance,
  well-formed units}` — all four observed `false` immediately after RE, then flipping
  to `true` one clarifying turn at a time. `GET /plans/{id}` surfaces each criterion's
  pass/fail, so "what's still blocking" is explicit, and `finalize` stays refused
  (`409`) until the last one is green.

## Q3 — Is the plan↔run seam clean (units → sim/burn, gate respected)?

Clean, and it **reuses the runtime — no new orchestration**:

- Each run carries its `plan_id + plan_unit_seq`; `GET /plans/{id}` lists the child
  runs with live status + a rolled-up `build_cost` (`$0.02261` = 7 × `$0.00323`).
- `depends_on` is honored: the chained CONSTRUCTION units applied **one after another**
  (`sim,sim,sim,sim,burn,burn,burn` → `done,done,done,done,applied,applied,applied`),
  never in a conflicting parallel burst.
- **The gate holds.** `test_plan_build.py` pins it: a burn unit sits `awaiting_gate`
  with **nothing applied** until `approve`; a **no-go scrubs just that unit** (its
  dependents stay blocked) while the **plan itself still reaches `done`**, not
  scrubbed. Mission Control's existing durability / gate / teardown govern the build
  unchanged — the builder only *schedules*.

## Q4 — Does the Postgres plan store hold up as the instance's memory across a restart?

Yes. Killed hard mid-session and restarted against the same Postgres:

| | before kill | after restart |
|---|---|---|
| status / mode | `drafting` / `brownfield` | `drafting` / `brownfield` |
| transcript turns | 4 | 4 (**identical content**) |
| requirements | 3 | 3 |
| units | 2 | 2 |

The next turn continued the walk to `ready` (10 turns total) — the session resumes
because every turn re-reads state from the store; nothing lived only in the dead
process. The **build** is durable too: `PlanBuilder.resume_builds()` re-advances every
`building` plan on startup, so a unit whose dependencies durably succeeded before a
crash is dispatched on restart (`test_build_resumes_after_restart`); gate-paused burns
resume on `approve`, exactly as standalone runs do (Phase 5a).

## Two gaps closed this phase

1. **Greenfield now builds for real.** A `new` plan with no target previously finished
   with nothing to run; hand-off now **scaffolds a git workspace** (one per plan) and
   builds against it — the greenfield path above ran 7 real runs in a scaffolded repo.
2. **The build survives restart.** The scheduler was purely edge-triggered
   (in-process run observer); it now also **reconciles on startup**, so a kill+restart
   mid-build continues rather than stalling.

## Honest demo-vs-prod line

This is a **localhost, no-auth** system. What Phase 6 proves is the *shape*: interactive
INCEPTION yields an executable plan, the greenfield/brownfield branch and the readiness
gate behave, the plan↔run seam is clean and gate-respecting, and the Postgres plan
store is honest durable memory across a process death. What it does **not** yet have is
the graduation gate:

- **Auth / tenancy.** The service binds `127.0.0.1` with no identity, no authorization,
  no per-operator isolation. Anyone on the loopback can drive any plan or approve any
  gate.
- **Multi-host durability.** Durability is proven for **process death on one box against
  one Postgres**. Concurrent service instances, at-least-once dispatch under a racing
  restart, and worktree/workspace lifecycle across hosts are unproven — the builder's
  scheduler is single-writer per process today.
- **Cost is StubWorker-deterministic.** The dollar figures are the offline stub's
  `$0.00323/step`; real SDK cost and its variance are a separate measurement.

Auth + multi-host durability remain the gate between this demo and production.
