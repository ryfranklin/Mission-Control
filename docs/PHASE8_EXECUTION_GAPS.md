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

## Done so far
- Burns forbidden from writing secret-shaped values (steering) — prevents the guard
  dead-end.
- Code-writing burns now instructed to write real source, not only docs (`stage.kind`
  branch in `compose_stage_prompt`).
