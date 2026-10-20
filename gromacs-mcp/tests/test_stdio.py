"""Run the real server as a subprocess over stdio, exactly as an MCP client does.

Anything the server printed to stdout would corrupt the protocol and make these calls fail.
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport

ROOT = Path(__file__).resolve().parent.parent


def run_session(tmp_path, body):
    env = {**os.environ, "GMX_MCP_HOME": str(tmp_path)}
    transport = PythonStdioTransport(script_path=ROOT / "src" / "server.py", env=env, cwd=str(tmp_path),
                                     python_cmd=sys.executable)

    async def main():
        async with Client(transport) as c:
            return await body(c)
    return asyncio.run(main())


def data(result):
    return result.structured_content


def test_server_starts_lists_tools_and_answers(tmp_path):
    async def body(c):
        tools = {t.name for t in await c.list_tools()}
        info = data(await c.call_tool("get_server_info", {}))
        bad = data(await c.call_tool("run_gromacs_command", {"command_name": "rm"}, raise_on_error=False))
        return tools, info, bad
    tools, info, bad = run_session(tmp_path, body)
    assert len(tools) >= 30 and {"submit_md_simulation", "energy_summary", "check_environment"} <= tools
    assert info["status"] == "success" and info["server_name"] == "gromacs"
    assert bad["status"] == "error" and "not allowed" in bad["error"]
    assert (tmp_path / "logs" / "gromacs_mcp.log").exists()


@pytest.mark.gmx
def test_noisy_tools_do_not_break_the_protocol(tmp_path, example_data):
    """analyze_tpr used to print the whole analysis to stdout; gmx commands print banners."""
    async def body(c):
        a = data(await c.call_tool("analyze_tpr", {"input_file": str(example_data / "sample.tpr")}))
        b = data(await c.call_tool("run_gromacs_command", {"command_name": "check",
                                                           "input_files": {"c": str(example_data / "water.gro")}}))
        e = data(await c.call_tool("check_environment", {}))
        return a, b, e
    a, b, e = run_session(tmp_path, body)
    assert a["status"] == "success" and a["tpr_info"]["nsteps"] == 500
    assert b["status"] == "success"
    assert e["ok"] is True
