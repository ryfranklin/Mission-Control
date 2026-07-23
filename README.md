# Mission Control

**Durable, observable, cost-aware orchestration for coding agents.**

Mission Control runs AI coding workers the way you'd run anything you actually
trust in production: each task is **isolated** in its own git worktree, **metered**
with per-step token/cost telemetry, **gated** behind a human go/no-go before any
change lands, and **durable** — a run survives a process crash and resumes without
re-paying for the LLM work it already did.

It ships with an **eval harness** (golden tasks → deterministic asserts + an LLM
judge → a variance-aware baseline) and a single **CI gate** (`eval-gate`, exit
0/nonzero) that a pipeline calls to block a regression from being promoted.

You drive it over **one HTTP seam**: a FastAPI **service** wraps the runtime, and a
**CLI** plus a server-rendered **web UI** (the "control room") are thin clients of
it — launch a run, watch a live SSE feed of node transitions and priced telemetry,
approve/reject at the go/no-go gate, and read cross-run cost — none of them
re-implementing any orchestration.

```
 Flight Director ── dispatch ──►  git worktree (isolated)  ──►  Controller
 (orchestrator)                        │                        (Claude Agent SDK)
                                       │  per-step telemetry (JSONL: tokens, $, latency)
                                       ▼
                           go / no-go gate ──► apply change (burn)  ─┐
                                       └────►  scrub (discard)       │
                                       ▼                             │
                                   teardown ◄───────────────────────┘   (no worktree leaks)
```

▶ **Interactive architecture map:** [architecture.html](./architecture.html) (open in a browser) — tiered Clients → seam → runtime → durable state, click-through detail, metaphor overlay.

---

## Why it's different

| Capability | What it means |
|---|---|
| 🧭 **Isolated by default** | Every task runs in a throwaway `git worktree`; teardown leaves no leaks. |
| 💵 **Cost is first-class** | Per-step JSONL telemetry (input/output/cache tokens, `cost_usd`, latency) priced from a single model→price table. |
| ✅ **Human-gated side effects** | A `go`/`no-go` gate; nothing is applied without an approval on record. |
| ♻️ **Durable & resumable** | Runs are a LangGraph state machine checkpointed to Postgres — kill it mid-flight, resume, **don't re-pay** for completed steps. |
| 🧪 **Evals + regression gate** | Golden tasks scored by deterministic asserts **and** an LLM judge; a k·σ noise band tells a real regression from variance. |
| 🔌 **Portable tools (MCP)** | The eval-gate is exposed as an MCP server; any agent/IDE can call it — same exit-code contract. |
| 🧱 **Framework-thin** | The worker is a plain `Worker` interface; the SDK worker slots in unchanged whether orchestration is imperative or a LangGraph graph. |
| 🎛️ **One seam, many clients** | An HTTP service wraps the runtime; a CLI and a server-rendered htmx web UI are thin clients — launch, live-stream (SSE), gate, and inspect cost over the same API, no duplicated orchestration. |

---

## Quickstart

**Requirements:** Python 3.12+, Docker (for durable state), and an authenticated
[Claude Agent SDK](https://pypi.org/project/claude-agent-sdk/) (a logged-in
`claude` CLI or `ANTHROPIC_API_KEY`).

```sh
# install
uv venv --python 3.12 && uv pip install -e ".[dev]"      # or: python3.12 -m venv .venv && pip install -e ".[dev]"

# stand up Postgres for durable state
cp .env.example .env
docker compose up -d          # postgres:16.8-alpine, healthchecked

# run the tests
pytest                        # Postgres/live-LLM/web tests skip if unavailable
```

**See it work (no LLM needed — deterministic StubWorker):**

```sh
python -m mission_control.demo_graph      # a run through the durable LangGraph shell
python -m mission_control.demo_resume     # kill a run mid-flight → resume, no re-pay, no leak
python -m mission_control.demo_gate       # pause at go/no-go → kill → restart → decide
```

**With the real Claude worker** (makes live calls — defaults to the cheap Haiku tier):

```sh
python -m mission_control.demo_sdk        # a Controller investigating a sandbox repo
python -m mission_control.demo_phase4 --sdk   # the full flow, end to end
```

**Drive the control room (HTTP service → web UI + CLI):**

```sh
python -m mission_control.service         # FastAPI seam on 127.0.0.1:8000 (localhost, no auth)
# open http://127.0.0.1:8000/ui           # fleet dashboard, live run view (SSE), metrics
mission-control launch /path/to/repo --type burn --watch   # CLI: a client of the same API
```

---

## How it works

**Worker (the Controller).** A minimal `Worker` interface (`investigate(task,
workdir)`). `SdkWorker` wraps the Claude Agent SDK with **fully explicit context**
(`setting_sources=[]` — no ambient CLAUDE.md/settings), probes the target repo for
an **AI-DLC** install and composes its rules into the system prompt, and reports
per-step usage. `StubWorker` is a deterministic, offline stand-in.

**AI-DLC v2 (methodology-in-target).** Mission Control can run the [AWS **AI-DLC
v2**](https://github.com/awslabs/aidlc-workflows) methodology as *content installed in
the target repo* — **MC drives, v2 supplies the stage content, and MC never runs v2's
hooks or tools.** The methodology is vendored into `src/mission_control/aidlc_v2/`
pinned to an exact upstream commit (stage definitions, protocols, agent definitions,
knowledge — `hooks/` and `tools/` are excluded). When a target has v2 installed
(`.aidlc/`), the planner **derives the INCEPTION walk and the work-list from the
catalog** instead of MC's built-in stages, and each stage-unit worker is steered by that
one stage's protocol + its `lead_agent`'s knowledge. MC substitutes its **own**
orchestration, go/no-go gate, and state:

- each catalog stage becomes an MC unit — a design stage runs as a read-only `sim`, a
  code stage as a gated `burn`;
- v2's approval gate maps onto MC's **go/no-go** (a `reviewer` in a stage's frontmatter
  collapses into the gate — a GO approves, a NO-GO with feedback is "request changes");
- MC keeps v2's own `aidlc-state.md` coherent (marking stages `[x]` on a GO) and commits
  it with the produced artifacts through the same content-guarded git-sync path;
- `operation`-phase stages are parsed and shown but **deferred** in v1 (they need cloud
  credentials) — recorded in the plan with a clear reason, never dispatched.

See `scripts/e2e_phase8.py` for the end-to-end run and `docs/PHASE8_FINDINGS.md`.

**Orchestration.** Two interchangeable shells over the *same* worker:
- **Imperative** (`Orchestrator`) — dispatch → run → gate → apply/scrub → teardown.
- **Durable** (`graph.py`, LangGraph `StateGraph`) — the same lifecycle as nodes,
  checkpointed to Postgres, resumable, with an `interrupt()`-based go/no-go that
  survives a restart. Nodes are idempotent (recovery is at node boundaries), and
  **apply-burn is its own node** so a crash never half-applies a change.

**Telemetry & cost.** Every model request is one JSONL row (tokens, cache split,
`cost_usd`, latency, model, step ids). Pricing lives in exactly one module.

**Evals.** A golden set of `sim`/`burn` tasks with a split contract:
*deterministic asserts* (files touched, tests green, output substrings, outcome)
plus a *judge rubric* scored by a stronger model. `baseline.py` runs the suite N
times and records mean/σ/min-max; the regression check flags a drop only **outside
a k·σ band** — separating signal from LLM noise.

**The gate & CI.** `eval-gate` runs the suite, compares to `baseline.json`, and
**exits 0 (pass) / nonzero (regression)** on quality *or* total cost — emitting
both a human report and machine JSON. The `Jenkinsfile` mirrors a Liquibase-style
pipeline: a nonprod stage runs the gate (fails the build on regression), and a
prod stage is a **manual go/no-go** reachable only if nonprod passed.

**Analytics (mini-medallion).** JSONL is the raw/bronze spine; **DuckDB** queries
it in place (`read_json_auto`, zero ETL) for cross-run cost/quality; **Postgres**
is the transactional system-of-record for run state. Different tools, different
jobs — nothing crammed into one store.

**The service is the seam (5a).** A thin **FastAPI** app wraps `graph.py` — it
launches / resolves / streams / queries runs, but owns **no orchestration logic**
(the graph still does all of it). One entry point into the runtime:

| Endpoint | What it does |
|---|---|
| `POST /runs` | Launch a run against a target; kicks off the graph in a background task keyed by `thread_id`. |
| `POST /runs/{id}/approve` · `/reject` | Resolve the durable go/no-go by **resuming the existing `interrupt()`** (approve → apply-burn; reject → scrub). |
| `POST /runs/{id}/scrub` | Scrub (kill) a run with clean teardown. |
| `GET /runs` · `GET /runs/{id}` | List runs (status/target filters) · run detail, from the Postgres registry. |
| `GET /runs/{id}/events` | **SSE** stream of the merged live feed: node transitions + priced telemetry + gate-waiting. |
| `GET /metrics` | Cross-run cost/quality from the DuckDB pass. |

**Every UI is a client of these same endpoints.** The **CLI** and the server-rendered
**web UI** (5b, served at `/ui`) both drive the runtime purely over HTTP and never
import the graph; a **Slack app** (5c) is planned as another client of the exact
same API. No client re-implements orchestration — they all talk to the seam.

**The control room (5b).** A server-rendered UI (Jinja + htmx + the htmx SSE
extension, **no JS build**) at `/ui`: a **fleet dashboard** (polled), a per-run
**live station** (an SSE timeline that replays durable history from a `run_events`
log before tailing live, so a reload or restart shows the *whole* run — not just
the resume leg), wired **go/no-go/scrub/cancel** actions, and a **cost/perf
dashboard**. Cost is labeled honestly: it only reconciles at teardown, so an
in-flight run reads *"not yet reconciled,"* never `$0`/free.

---

## Entrypoints

| Command | What it does |
|---|---|
| `python -m mission_control.service` | Run the FastAPI service (the seam) on `127.0.0.1:8000`. v1 = **localhost, no auth**. `MC_SERVICE_SDK=1` for the real worker. The control-room web UI is served at **`/ui`** (server-rendered Jinja + htmx, no JS build). |
| `mission-control` · `python -m mission_control.cli` | CLI over the service API: `launch` · `watch`/`follow` · `runs` · `approve`/`reject`/`scrub`. `--base-url`/`$MC_SERVICE_URL`. |
| `eval-gate` · `python -m mission_control.eval_gate` | The CI contract: run evals, gate vs `baseline.json`, exit 0/nonzero. `--k`/`--n`/`--demo`. |
| `python -m mission_control.baseline [N]` | Build/refresh `golden/baseline.json` (N repeats). |
| `python -m mission_control.analytics` | DuckDB cross-run cost/quality report over the JSONL. |
| `python -m mission_control.eval_gate_mcp` | Serve the eval-gate as an MCP tool (stdio). |
| `python -m mission_control.demo_phase4 [--sdk]` | Full durable run → analytics → eval-gate over MCP. |
| `ci/run_pipeline_demo.sh {clean\|regression}` | Local mirror of the Jenkins pipeline. |

---

## Design principles

- **One vocabulary, one file.** The domain metaphor (Flight Director, Controller,
  `sim`/`burn`, `go`/`no-go`, `scrub`) lives *only* in `roles.py`; everything else
  uses functional names, so a rename is a one-file change.
- **Explicit context.** The worker sees only what we compose — no ambient config —
  keeping runs reproducible and telemetry honest.
- **Idempotent by design.** Durable recovery re-runs whole nodes, so each node is
  safe to re-run; the change-applying step is isolated to its own boundary.
- **JSONL is the spine.** Bronze/raw everywhere; query layers (DuckDB) sit on top.

---

## Testing

```sh
pytest                       # full suite
pytest -k "not postgres"     # skip durability tests that need Docker Postgres
MC_LIVE_JUDGE=1 pytest -k tolerance   # opt-in live judge meta-eval
```

Tests are offline and deterministic by default (StubWorker); tests needing
Postgres or live LLM calls **skip** when those aren't available.

---

## Repository layout

```
src/mission_control/
  roles.py            metaphor vocabulary — the ONLY place metaphor terms live
  worker.py           Worker interface + StubWorker
  sdk_worker.py       Claude Agent SDK worker (explicit context, AI-DLC steering)
  orchestrator.py     imperative dispatch → gate → apply/scrub → teardown
  graph.py            durable LangGraph shell + PostgresSaver + interrupt() gate
  live.py             the merged live feed (node transitions + priced telemetry)
  runs_store.py       the Postgres runs registry (status/cost ledger)
  service/            FastAPI seam wrapping graph.py (5a) + web/ control-room UI (5b)
  cli.py              CLI: a client of the service API (never imports the graph)
  worktree.py         git-worktree isolation
  telemetry.py        per-step JSONL events
  pricing.py          the single model→price table
  evals.py            golden-set runner (deterministic asserts)
  judge.py            LLM-as-judge for rubric scoring
  baseline.py         N-run baseline + variance-aware regression check
  eval_gate.py        the eval-gate CLI (exit-code contract)
  eval_gate_mcp.py    eval-gate exposed as an MCP server
  analytics.py        DuckDB analytics over the JSONL spine
golden/               golden tasks, sandbox fixture, baseline.json
ci/                   Jenkins pipeline demo (local runnable)
docs/                 PHASE1–5 findings, EVAL_GATE.md
docker-compose.yml    Postgres for durable state
```

Deeper reading: `docs/EVAL_GATE.md`, the `docs/PHASE*_FINDINGS.md` (data-driven
notes on cost, judge reliability, the noise band, and durability), `golden/README.md`
(spec format), and `ci/README.md`. Build-scope conventions are in `CLAUDE.MD`.

---

## Status

Phases 1–5b are complete: instrumented worker, eval harness + judge, baseline +
CI gate, durable execution (LangGraph + Postgres) with MCP tool exposure, an HTTP
**service seam** (+ CLI), and a server-rendered **control-room web UI** (htmx + SSE,
with durable event replay).

**Demo-grade** by design — single-box Postgres, node-granularity recovery, and
**localhost / no auth**. The graduation gate to production is **auth + identity**
on every client and **multi-host durability** (a shared broker for live SSE
fan-out; a real job queue for launches). See `docs/PHASE4_FINDINGS.md` and the
`docs/PHASE5*_FINDINGS.md` for the honest demo-vs-production breakdowns.
