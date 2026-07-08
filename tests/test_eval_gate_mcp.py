"""eval-gate over MCP: the server exposes the tool, and a worker invoking it via
an MCP client gets the SAME pass/regression result as calling the gate directly.

Offline: the gate runs in --demo mode (StubWorker, no judge). Spawning the stdio
MCP server is a real subprocess, but no LLM/network is involved.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from mission_control.eval_gate import call_eval_gate_over_mcp, gate_result

_REPO = Path(__file__).resolve().parents[1]
_SERVER_CMD = [sys.executable, "-m", "mission_control.eval_gate_mcp"]


@pytest.fixture(scope="module", autouse=True)
def _demo_baselines():
    # ensure ci/demo/baseline.{pass,regressed}.json exist
    import subprocess

    subprocess.run([sys.executable, "ci/demo/setup.py"], cwd=_REPO, check=True,
                   capture_output=True)


def _params(baseline: str, out: str) -> dict:
    return {
        "baseline": str(_REPO / "ci/demo" / baseline),
        "tasks": str(_REPO / "ci/demo/tasks"),
        "sandbox": str(_REPO / "ci/demo/sandbox"),
        "demo": True,
        "out_dir": out,
    }


def test_server_exposes_eval_gate_tool(tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def _list():
        sp = StdioServerParameters(command=_SERVER_CMD[0], args=_SERVER_CMD[1:])
        async with stdio_client(sp) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                tools = await session.list_tools()
                return [t.name for t in tools.tools]

    names = asyncio.run(_list())
    assert "eval_gate" in names


def test_mcp_pass_matches_direct(tmp_path):
    params = _params("baseline.pass.json", str(tmp_path / "mcp"))
    over_mcp = call_eval_gate_over_mcp(**params)
    direct = gate_result(**params).to_json()

    assert over_mcp["passed"] is True and over_mcp["exit_code"] == 0
    assert over_mcp["passed"] == direct["passed"]
    assert over_mcp["exit_code"] == direct["exit_code"]   # exit-code contract survives MCP
    assert over_mcp["axes"] == direct["axes"]


def test_mcp_regression_matches_direct(tmp_path):
    params = _params("baseline.regressed.json", str(tmp_path / "mcp"))
    over_mcp = call_eval_gate_over_mcp(**params)
    direct = gate_result(**params).to_json()

    assert over_mcp["passed"] is False and over_mcp["exit_code"] == 1
    assert over_mcp["exit_code"] == direct["exit_code"]   # regression survives round-trip
    assert over_mcp["axes"]["cost_usd"]["regressed"] is True


def test_worker_wires_eval_gate_mcp_server():
    from mission_control import SdkWorker, Task, TaskType

    opts = SdkWorker(eval_gate_mcp=True)._options(
        Task("t", TaskType.READ_ONLY, "x"), Path("/tmp/wt")
    )
    assert "evalgate" in opts.mcp_servers
    assert opts.mcp_servers["evalgate"]["args"] == ["-m", "mission_control.eval_gate_mcp"]
    # default off → no MCP servers wired (behavior unchanged)
    assert SdkWorker()._options(Task("t", TaskType.READ_ONLY, "x"), Path("/tmp/wt")).mcp_servers == {}
