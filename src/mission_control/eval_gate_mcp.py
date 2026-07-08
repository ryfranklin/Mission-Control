"""The Phase 3 eval-gate, exposed as an MCP server.

The gate already has a clean exit-code/JSON contract; this wraps it as an MCP
tool so ANY MCP client — this repo's worker, or another agent/IDE — can invoke it
over the protocol instead of a hardcoded call. The JSON returned is identical to
the CLI's (``GateResult.to_json()``): ``exit_code`` 0 = pass, nonzero = regression.

Run the server (stdio transport):

    python -m mission_control.eval_gate_mcp

Point another agent/IDE at it with an MCP stdio config, e.g.:

    {
      "mcpServers": {
        "mission-control-eval-gate": {
          "command": "python",
          "args": ["-m", "mission_control.eval_gate_mcp"]
        }
      }
    }

Then that client calls the `eval_gate` tool and reads `exit_code` / `passed`.
"""

from __future__ import annotations

from typing import Optional

from mcp.server.fastmcp import FastMCP

from .eval_gate import gate_result

mcp = FastMCP("mission-control-eval-gate")


@mcp.tool()
def eval_gate(
    baseline: str = "golden/baseline.json",
    tasks: str = "golden/tasks",
    sandbox: str = "golden/sandbox",
    k: Optional[float] = None,
    n: int = 1,
    demo: bool = False,
    worker_model: Optional[str] = None,
    judge_model: Optional[str] = None,
) -> dict:
    """Run the eval suite and gate it against baseline.json.

    Returns the gate result JSON: {passed, exit_code, k, n, current, axes, runs}.
    exit_code is 0 on pass and nonzero on a quality OR total-cost regression —
    the same contract as the `eval-gate` CLI, preserved across the MCP boundary.
    Set demo=true for the deterministic offline path (StubWorker, no judge).
    """
    return gate_result(
        baseline=baseline,
        tasks=tasks,
        sandbox=sandbox,
        k=k,
        n=n,
        demo=demo,
        worker_model=worker_model,
        judge_model=judge_model,
    ).to_json()


def main() -> None:  # pragma: no cover - process entry point
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
