# Eval gate

The single command CI calls to decide pass/fail. It runs the eval suite, compares
the run to `baseline.json`, and **exits 0 on pass / nonzero on regression**.

```sh
python -m mission_control.eval_gate        # or the installed console script:
eval-gate --k 2 --n 3 --json gate.json
```

Jenkins (or any CI) just calls it — the exit code is the contract:

```sh
eval-gate || exit 1     # nonzero ⇒ quality regressed OR total cost regressed
```

## What it gates on

- **Quality**: aggregate `quality_total` below `baseline_mean − k·stddev` → regression.
- **Cost**: aggregate `cost_usd` **(total = worker + judge)** above `baseline_mean + k·stddev` → regression.

Nonzero exit if **either** axis regresses. It emits both a **human report**
(per-metric baseline mean±stddev, current run, pass/regression per axis) to stdout
and a **machine-readable JSON** (`--json`, default `evals/gate-result.json`).

## Flags / env

| flag | env | default | meaning |
|------|-----|---------|---------|
| `--k` | `MC_GATE_K` | `baseline.k` (2) | noise-band width in stddevs |
| `--n` | `MC_GATE_N` | 1 | repeats of the eval run to average before comparing |
| `--baseline` | `MC_GATE_BASELINE` | `golden/baseline.json` | baseline artifact |
| `--tasks` | `MC_GATE_TASKS` | `golden/tasks` | task-spec dir |
| `--sandbox` | `MC_GATE_SANDBOX` | `golden/sandbox` | target-repo fixture |
| `--json` | `MC_GATE_JSON` | `evals/gate-result.json` | JSON result path |
| `--worker-model` / `--judge-model` | — | Haiku / Opus | model overrides |

## Why the exit code is stable (the soft-signal problem)

The worker and especially the Opus judge are noisy. Three choices keep the gate
from flapping:

1. **Band, not a point.** A result only fails outside `mean ∓ k·stddev`; a wobble
   inside the band is variance, not a regression. Raise `k` (default 2, use 3 for a
   conservative gate) to trade sensitivity for fewer false positives.
2. **Average N repeats.** `--n` runs the suite N times and compares the mean;
   more repeats shrink the current estimate's variance.
3. **Gate on the aggregate.** Per-task bands aren't trustworthy yet (high-variance
   tasks have bands too wide to gate — see `docs/PHASE3_FINDINGS.md`), so the exit
   code is driven by the whole-suite aggregate, where per-task noise averages out.
   Per-task numbers are reported for triage but do not set the exit code.

**Re-baseline** (`python -m mission_control.baseline [N]`) whenever the
worker/judge model, the golden set, or the sandbox changes — a stale band makes
the gate meaningless.

## Over MCP (portable tool)

The same gate is exposed as an MCP server so it isn't framework-locked — any MCP
client (this repo's worker, or another agent/IDE) can invoke it over the protocol
and read the identical `exit_code` / `passed` contract.

Run the server (stdio transport):

```sh
python -m mission_control.eval_gate_mcp
```

Point another agent/IDE at it (MCP stdio config):

```json
{
  "mcpServers": {
    "mission-control-eval-gate": {
      "command": "python",
      "args": ["-m", "mission_control.eval_gate_mcp"]
    }
  }
}
```

The client then calls the `eval_gate` tool (same params as the CLI flags:
`baseline`, `tasks`, `sandbox`, `k`, `n`, `demo`, `worker_model`, `judge_model`)
and gets back the gate JSON — `exit_code` 0 = pass, nonzero = regression. The
contract survives the MCP round-trip unchanged.

This repo consumes it two ways:
- **Programmatically** — `call_eval_gate_over_mcp(**params)` spawns the server and
  returns the same dict as a direct call (used instead of a hardcoded gate call).
- **From the Controller** — `SdkWorker(eval_gate_mcp=True)` wires the stdio server
  into the worker's `mcp_servers`, so the agent can call `eval_gate` as a tool.
