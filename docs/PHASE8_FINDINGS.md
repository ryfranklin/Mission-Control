# Phase 8 findings — running AI-DLC v2 as methodology-in-target

Phase 8 makes Mission Control run the AWS **AI-DLC v2** methodology as *content installed
in the target repo*: **MC drives, v2 supplies the stage content, and MC never runs v2's
hooks or tools.** The methodology is vendored into `src/mission_control/aidlc_v2/` pinned
to an exact upstream commit (`awslabs/aidlc-workflows@d4fc34d`, `v2`) — stage definitions,
protocols, agent definitions, and knowledge; `hooks/` and `tools/` (all `.ts`) are
excluded. When a target carries a v2 install (`.aidlc/`), the planner derives its INCEPTION
walk and work-list **from the catalog**, and each stage-unit worker is steered by that one
stage's protocol + its `lead_agent` knowledge. MC substitutes its own orchestration,
go/no-go gate, and state.

**How this was measured.** `scripts/e2e_phase8.py` drives the whole story in-process (a
FastAPI TestClient over the real Postgres seam + real git bare-remote acquire/gate/push,
`StubWorker`). It opens a **greenfield** plan against a throwaway target that has v2
installed and committed to a real remote, walks INCEPTION to a finalized plan, then builds
through the design and code stages behind a real go/no-go GO. Numbers below are one
representative run (StubWorker, deterministic). The stage-level GO/NO-GO mechanics are
pinned by `tests/test_plan_build.py` and `tests/test_plan_docs.py`. v1 security model:
**localhost / no auth / single host.**

## The end-to-end run (`E2E_REPORT`, one pass)

| Step | Observed |
|---|---|
| greenfield open | defaults **`aidlc` / `aws`**; target carries v2 (`.aidlc/`) |
| INCEPTION walk | **17 turns → 17 plan stages laid down**, every one carrying its catalog `stage_slug` (not a built-in title) → `ready` |
| work-list | **7 construction units** (3 `sim` design + 4 `burn` code) + **7 `operation` units, all `deferred`** |
| deferred reason | surfaced on the plan: *"operation-phase stages need cloud credentials — parsed and recorded, but not dispatched in v1: …"* |
| build | `done`; **4 gate GOs** (the four construction burns); the `sim` design stage ran read-only, the `code-generation` `burn` applied |
| operation | **never dispatched** (recorded-but-deferred held) |
| git landing | `aidlc-docs/aidlc-state.md` + `flight-plan.yaml` committed **and pushed to the remote**; the burn's change pushed; **1 worktree** (no leak) |
| state markers | `code-generation` and `functional-design` flipped to `[x]`; a deferred `operation` stage stayed `[ ]` |

## Q1 — Does a v2 target drive its own stages/units from the catalog?

Yes. The greenfield walk was **catalog-driven, not built-in**: all 17 laid-down INCEPTION
units carry a real catalog `stage_slug` (`workspace-scaffold`, `workspace-detection`,
`state-init`, `intent-capture`, …), and the work-list is exactly the catalog's non-plan
applicable stages, in dependency order:

```
construction (sim):  functional-design · nfr-requirements · nfr-design
construction (burn): infrastructure-design · code-generation · build-and-test · ci-pipeline
operation (burn, DEFERRED): deployment-pipeline · environment-provisioning · deployment-execution ·
                            observability-setup · incident-response · performance-validation · feedback-optimization
```

A target **without** v2 falls back to MC's built-in five-stage INCEPTION walk, unchanged
(`tests/test_planner_engine.py::test_no_v2_target_uses_builtin_walk`). Detection is a
read-only `probe()` for the `.aidlc/` layout; v2 wins over any legacy AI-DLC install.

## Q2 — The sim/burn classification (MC-owned, one editable block)

The v2→MC mapping is **Mission Control's** call, not v2's, and lives in one obvious table
(`aidlc_v2/catalog.py`): a per-`(phase, slug)` override on top of a phase-level default.

| v2 stage | MC `kind` | Why |
|---|---|---|
| `initialization` / `ideation` / `inception` phases | `plan` | interactive, artifact-only planning (the INCEPTION walk) |
| `construction/{functional-design, nfr-requirements, nfr-design}` | `sim` | read-only design/analysis over the target |
| `construction/{code-generation, build-and-test, ci-pipeline, infrastructure-design}` | `burn` | mutate the target → gated |
| `operation/*` | `burn`, **`deferred`** | needs cloud credentials in v1 → recorded, not dispatched |

`task_type` follows the **kind**, not the phase — a design stage lives in the
`construction` phase but is a read-only `sim`. The classification is verified against the
real vendored catalog (`tests/test_aidlc_v2_catalog.py`, `tests/test_aidlc_v2_plan.py`).

## Q3 — Where does the gate map onto v2's approval, and is state kept coherent?

- **v2's approval gate → MC's go/no-go.** Each `burn` stage pauses at MC's gate; a **GO**
  is the approval, a **NO-GO with feedback** is v2's "request changes" (recorded as a
  `<slug>:changes-requested` requirement, the unit left not-done, only that unit scrubbed —
  the plan survives). A stage's `reviewer` / `reviewer_max_iterations` frontmatter is **not**
  a v2 sub-agent here; it **collapses into the gate**. The worker is explicitly forbidden
  from spawning a reviewer sub-agent or running any `aidlc-*.ts` tool/hook.
- **MC keeps `aidlc-state.md` coherent.** On a stage GO, MC flips that stage to `[x]` in
  the target's own `aidlc-state.md` (scaffolded from v2's vendored `state-template.md`),
  derived from plan state (git is authoritative; Postgres is a rebuildable cache). The
  state file, the produced artifacts, and `flight-plan.yaml` move in **one commit** through
  the **same Phase-7 `plan_docs` sync path and the same content guard** — the guard scans
  the whole `aidlc-docs/` tree, so a secret in a produced v2 artifact blocks the commit
  (`tests/test_plan_docs.py::test_content_guard_covers_committed_v2_artifacts`).
- **MC advances itself.** MC's builder dispatches the next dependency-satisfied stage; this
  **replaces v2's `aidlc-orchestrate.ts report` auto-advance** — MC never shells out to a
  `.ts` tool.

## Q4 — Is the worker steered by the stage, not the whole methodology?

Yes. A stage-unit run composes its system prompt from **that one stage's Markdown body +
its `lead_agent` definition + that agent's `knowledge/`** — not the whole methodology and
not the generic worker prompt (`tests/test_aidlc_v2_steering.py`). `setting_sources=[]` is
preserved so nothing auto-loads; the `sim` tool-block still applies to design stages while
`burn` code stages may write. Because the vendored text was written for v2's own runtime
(it references `.ts` tools and sub-agents), the composed steering ends with a loud MC
override that disables all of it.

## Scope guard: operation stages are visible but deferred

`operation`-phase stages are parsed, listed in the plan, and shown in `aidlc-state.md`, but
recorded with status `deferred` and **never dispatched** in v1 (they need cloud
credentials). The reason is surfaced on the plan (`operation:deferred` requirement), the
builder skips deferred units while still counting them as resolved so the plan completes,
and the e2e asserts none were dispatched.

## Honest demo-vs-prod line

Phase 8 proves the **shape**: MC runs v2 as methodology-in-target end to end — catalog-driven
INCEPTION → a finalized plan → a `sim` design stage and a gated `burn` code stage behind a
real GO, with artifacts + `aidlc-state.md` landing in git and the burn pushing, and MC never
running a v2 hook/tool. Honest limits:

- **`operation` deferred.** The whole operation phase (deploy/observe/incident) is recorded
  but not executed — it needs real cloud credentials and a deployment target, out of scope
  for v1.
- **Single host, no auth.** Same as prior phases: the service binds `127.0.0.1` with no
  identity/authorization; durability is proven for process death on one box against one
  Postgres, not for concurrent instances across hosts.
- **Reviewer collapsed into the gate.** v2's per-stage reviewer sub-agent + its revision
  loop are represented by MC's single go/no-go GO / request-changes — MC does not run v2's
  multi-round reviewer agent.
- **Cost is StubWorker-deterministic.** The e2e uses the offline stub; real SDK cost and
  the fidelity of a real worker actually writing every `produces:` artifact are a separate
  measurement. (The stub applies a marker change per burn, so the *plumbing* — apply, guard,
  push, state-marker — is real; the artifact *content* is not.)

The vendored tree is **content only**, pinned and reproducible (`scripts/vendor_aidlc_v2.py`
+ `VENDOR.json`); re-running the vendor script against a new upstream commit is the upgrade
path.
