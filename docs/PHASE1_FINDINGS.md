# Phase 1 findings — token / cost / context

Data-driven notes from running the full flow end-to-end. **Sample is illustrative
(n=1 per task, Claude Haiku 4.5, one small sandbox), not a benchmark.**

## What was run
Sandbox target repo carrying a generic AI-DLC install (`AGENTS.md` +
`.aidlc-rule-details/`, detected by the probe → `flavor=generic`). Then:

1. **sim** — INCEPTION, greenfield, read-only. Target repo unchanged afterward.
2. **burn** — CONSTRUCTION, greenfield, behind go/no-go = **GO**. Wrote
   `aidlc-docs/inception.md` + a code change; both merged into the target repo.

Telemetry confirmed: **one JSONL file per run** (2 total), identical shape, with a
per-run summary printed next to the clean-teardown line.

## The numbers

| task | in | out | cache_read | cache_write | context | cost | latency |
|------|---:|----:|-----------:|------------:|--------:|-----:|--------:|
| sim (INCEPTION)  | 6 | 271 | 16,269 | 145 | 16,420 | $0.003278 | 13.4 s |
| burn (CONSTRUCTION) | 2 | 85 | 19,112 | 296 | 19,410 | $0.002930 | 15.2 s |

Cost decomposition (Haiku: in $1, out $5, cache-read 0.1×, cache-write 2×/MTok):

| component | sim | burn |
|-----------|----:|-----:|
| cache_read | $0.001627 (**49.6%**) | $0.001911 (**65.2%**) |
| output     | $0.001355 (41.3%) | $0.000425 (14.5%) |
| cache_write| $0.000290 (8.8%)  | $0.000592 (20.2%) |
| input      | $0.000006 (0.2%)  | $0.000002 (0.1%) |

## Where cost concentrates

- **Cache reads are the top line item in both runs (≈50–65%).** They're priced at
  0.1× input, but the *volume* wins: the carried context (Claude Code harness +
  our composed system prompt + the target's AI-DLC rules) is re-read every step.
- **`context_size` is ~99% cache-read tokens** (99.1% sim, 98.5% burn). The actual
  new task prompt (`input_tokens`) is **2–6 tokens** — negligible. Context size is
  almost entirely *cached steering/harness context*, not the task itself.
- **Output is the only other big lever**, and it dominates for the *sim* (41%): a
  read-only INCEPTION step is chatty (271 output tokens of analysis). The burn is
  terse (85) — it *did* more but *said* less.

## Surprising

- **The read-only sim cost ~12% more than the side-effectful burn** ($0.00328 vs
  $0.00293). Cost tracks **output verbosity + carried context**, not whether the
  task mutates code. "Read-only ≠ cheap."
- **Long-context steps are cheap-per-token but set the cost floor.** Almost all
  context is cached (0.1×), so a step's baseline cost is `~0.1 × carried_context`
  regardless of how little new work it does. Trimming what we compose into the
  system prompt (steering size) moves the floor more than trimming task prompts.
- **Cache writes bill at the 1-hour TTL (2×), not 5-minute (1.25×)** — the SDK/CLI
  chose 1h ephemeral caching. Small here (145–296 tokens) but it's the priciest
  per-token component; worth watching if steering churns between runs (each churn
  is a fresh 2× write).
- **INCEPTION-as-`sim` can't persist `aidlc-docs/`.** A sim is read-only *and* the
  gate discards read-only output at teardown, so the docs actually landed via the
  gated **CONSTRUCTION burn** merge (matching the spec's "merge commits rules +
  aidlc-docs"). If INCEPTION must persist docs on its own, it needs either
  scoped-write permission + its own commit path, or to run as a gated burn.

## Implications for later phases

- The cheapest optimization lever is **steering/system-prompt size**, since it sets
  the recurring cache-read floor on every step — not per-task prompt trimming.
- Prompt caching is already doing heavy lifting (input is ~2–6 tokens/step); keep
  the composed prefix byte-stable across steps to preserve cache hits.
- Telemetry captures the split cleanly (`input` vs `cache_read` vs `cache_creation`
  vs `output`), so cost attribution per step is already actionable.
