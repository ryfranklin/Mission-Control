# Mission Control — build-scope instructions

Mission Control is an **agent-orchestration runtime**: it spawns workers, isolates them in git
worktrees, supervises them, records telemetry, and gates their side effects. Python 3.12+, src
layout, pytest.

## Metaphor rule (load-bearing)
The Mission Control vocabulary lives in **`roles.py` ONLY**. Everything else uses functional
names (`Worker`, `run_task`, `investigate`). Never hardcode a metaphor term outside `roles.py` —
a future metaphor swap must be a one-file change.

| Concept | Term (in roles.py) |
|---|---|
| Orchestrator | Flight Director |
| Worker | Controller |
| Read-only task | sim |
| Side-effectful task | burn |
| Approval / merge gate | go / no-go |
| Kill a task | scrub |

## Runtime rules
- **Worker context is fully explicit.** Construct the Claude Agent SDK worker with
  `setting_sources=[]` (Python) so NO filesystem settings / CLAUDE.md are auto-loaded. The
  worker's context is only what we compose (AI-DLC rules + task). This keeps telemetry honest.
- **Worktree isolation** — every task runs in its own git worktree; teardown leaves no leaks.
- **go/no-go** gates a `burn` before its changes are applied; `scrub` kills + tears down.

## Telemetry conventions
- Per-step events → JSONL, one file per run. Fields: input/output/cache tokens, `cost_usd`,
  `latency_ms`, `context_size_tokens`, `model`, `step_id`, `parent_step_id`, `task_id`,
  `task_type`, `outcome`.
- Model→price table lives in ONE module. Verify current pricing before hardcoding.

## AI-DLC boundary
- **This file contains NO AI-DLC rules.** AI-DLC is a methodology the runtime applies to *target
  repos* at run time (probe + compose into the worker's system prompt). Mission Control is not
  itself built under AI-DLC.

## Where things live
- Code lives in this repository.
- Planning/decisions/research live outside the repo; don't duplicate design notes here.

## Verify before commit
- `pytest` green; a manual run shows a Flight Director dispatching a Controller with clean
  teardown (no worktree leaks).