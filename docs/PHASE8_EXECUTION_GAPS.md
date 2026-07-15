# Phase 8 — execution-model gaps (v2 builds produce no real artifacts)

Observed running a real greenfield v2 build (`analytics-workspace`, SDK worker): the
plan finalized, ~20 stage runs went `done`, but **no stage `produces:` artifact was
written** — only `flight-plan.yaml`, `requirements.md`, `aidlc-state.md`. Workers
reported *"required input artifacts missing"* / *"empty simulation"*, and
`infrastructure-design` invented a `default-unit` because no `unit-of-work` existed.

## The three failure modes

1. **Producing stages can't (or don't) write their artifacts.**
   - The artifact-producing stages (INCEPTION planning + construction *design*) are
     classified `sim` → dispatched read-only → the tool block forbids Write/Edit, so they
     **physically cannot** write their `produces:` docs. They can only emit a report.
   - INCEPTION stages were also short-circuited during the interactive walk to *placeholder*
     requirements (`"(captured)"`), never producing real artifacts.
2. **Consumers run before/without their producers.**
   - The build DAG runs the 17 INCEPTION units in parallel (`depends_on: []`), and each
     construction consumer's `requires_stage` entries that pointed at plan-stages were
     dropped — so consumers dispatch with no inputs on disk.
   - INCEPTION units are ALSO re-dispatched as build-time sims (double execution): they were
     "done" as planning, then run again read-only, producing nothing.
3. **No completeness check, no re-run loop.**
   - A unit is marked `done` on **run success**, not on **`produces:` artifacts present**. A
     stage that emitted a "can't find inputs" report still counts as done, and its
     dependents proceed. There is no verification step and no revise/re-question loop.

Plus a secondary dead-end (now fixed): when a burn *did* generate content, it embedded
secret-shaped example values → the egress guard blocked the commit → the whole stage
failed → downstream stages died. Fixed by instructing burns to use placeholders, never
secrets (`aidlc_v2/steering.py`).

## Root cause

v2 is a **sequential producing pipeline**: each stage's worker writes artifacts that the
next stage consumes, with *sensors* (`required-sections`, `upstream-coverage`) verifying
completeness and a reject/revise loop. Phase 8 replaced that with a **parallel DAG
scheduler + per-unit worker + a single go/no-go gate**, and mapped producing stages onto
the read-only `sim` type — which broke artifact production and hand-off.

## Fix approaches (the fork)

- **A — Sequential producing pipeline (recommended).** Run stages in catalog dependency
  order; each producing stage runs a *writable* worker that actually writes its artifacts
  (docs → `aidlc-docs/`, code → source); after each, an MC verification step (the "lead
  agent"/sensor) confirms the `produces:` artifacts landed and re-runs / requests-changes
  if not. Only **code-writing** stages pause at the human go/no-go gate; **doc/design**
  stages apply automatically (low-risk). Matches the operator's intuition (lead agent +
  populate-before-deploy + re-run loop) and keeps MC as the orchestrator.
- **B — Interactive walk produces INCEPTION artifacts.** The planner walk runs producing
  agents that write the inception artifacts during the conversation; the build then runs
  CONSTRUCTION only (designs read the inception artifacts; code writes source).
- **C — Reintroduce v2's own orchestrator/composer/reviewer agents**, with MC owning only
  the gate + state. Most faithful to v2, but re-adds the machinery Phase 8 deliberately
  stripped.

## The key decision (blocks A)

MC's model is `sim` = read-only, `burn` = gated write. Producing **design/doc** stages
need to WRITE (to `aidlc-docs/`) but shouldn't demand a human GO on each of ~20 stages.
So Approach A needs a **third execution mode: "writable but ungated"** for doc/design
stages, reserving the go/no-go gate for code-writing stages. That is a change to the
gate/safety model and needs sign-off before implementation.

## Resolution — Approach A implemented (gate code stages only)

Chosen and built:
1. **Gate decoupled from write-capability.** A stage can WRITE and AUTO-APPLY without a
   human GO (`Task.gated` → RunState → the gate node auto-approves when `gated=False`).
2. **Every producing stage writes; only code stages gate.** `catalog.gates()` (an MC-owned
   set: `code-generation`, `build-and-test`, `ci-pipeline`) decides. `build_units` emits
   all producing stages as writable (`BURN`) units; design/doc/IaC stages are ungated
   (write + auto-apply), code stages are human-gated. `plan_units.gated` persists it and
   it round-trips through `flight-plan.yaml`.
3. **Producing steering.** `compose_stage_prompt` branches on `gates()`: a design/doc
   stage is told to WRITE its artifacts under `aidlc-docs/` ("actually create the files")
   and not touch source; a code stage writes real source (and no secrets).
4. **INCEPTION stages stop running as blind read-only sims.** v2 plan-stage units are laid
   down `done` (planning records), so the builder no longer re-dispatches 17 read-only
   sims that produced nothing.
5. **Verification ("lead agent", v1).** A producing stage that SUCCEEDS but writes no
   artifacts is flagged with a `<slug>:no-output` requirement instead of silently
   counting as done.
- Secrets: burns forbidden from writing secret-shaped values (prevents the guard dead-end).

## CAPCOM — the build coordinator (resolves the partial-inputs gap)

`CAPCOM` (`roles.COORDINATOR`; Capsule Communicator) is MC's coordinator over the whole
build — it produces the full artifact chain in dependency order and won't deploy a stage
onto missing inputs:

- **Inception stages now PRODUCE.** `build_units` covers `inception → construction →
  operation` (initialization/ideation stay the walk's intent). Inception writes
  `requirements` / `application-design` / `unit-of-work`; construction consumes them — so
  a downstream stage always has real inputs on disk, not a guess.
- **Verification gate ("hold the fleet").** After each producing stage, CAPCOM checks its
  artifacts actually landed. A stage that succeeds but writes NOTHING is set `blocked`
  (not `done`) with a `<slug>:no-output` requirement, and its dependents are HELD — the
  code fleet never deploys onto missing inputs.
- **Gate code stages only.** Design/doc/inception stages write + auto-apply; the human
  go/no-go is reserved for the code-writing stages.

## Re-run loop + de-dup (both closed)

- **CAPCOM re-run loop.** A producing stage that writes nothing is RE-DISPATCHED (with an
  escalated "you MUST create the files" note) up to `MAX_STAGE_ATTEMPTS` (default 2,
  `MC_STAGE_MAX_ATTEMPTS`), tracked by `plan_units.attempts`. If a re-run produces, the
  stage completes; if it still writes nothing after the cap, it is HELD (blocked) and
  surfaced — bounded, so no runaway loops. Crash-recovery is conservative (hold, no
  retry).
- **No more duplication.** The interactive walk now covers only initialization + ideation
  (intent gathering); inception onward is produced by CAPCOM at build. Walk phases and
  build-unit phases are disjoint, so a stage appears exactly once in the plan.

## Diagnostic re-run (closed)

The re-run is no longer a blind nudge. When a stage produces nothing, CAPCOM inspects
which of the stage's `consumes:` artifacts are actually absent from the target's
`aidlc-docs/` (`plan.missing_inputs`) and re-dispatches with a **diagnosis** — it names
the missing inputs and tells the worker to proceed on what's present (and note what
isn't). If the stage is ultimately held, the `<slug>:no-output` requirement records the
diagnosed missing inputs, so *why* it's held is visible, not just *that* it is.

## Remaining (smaller)
- Presence is checked heuristically by filename stem (`unit-of-work` ⇒ `unit-of-work.md`);
  a producer emitting an artifact under a different filename could read as missing.
- A true multi-turn negotiation (CAPCOM ↔ the producing stage to regenerate a missing
  upstream artifact automatically) is still future; today CAPCOM diagnoses + re-runs the
  consumer, and holds with the reason if inputs truly can't be produced.
