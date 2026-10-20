"""Run the server as a subprocess over stdio, exactly as an MCP client does.

Anything printed to stdout would corrupt the protocol and make these calls fail.
"""

import asyncio
import os
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport

ROOT = Path(__file__).resolve().parent.parent


def test_server_starts_and_answers(tmp_path, fake_vmd):
    env = {**os.environ, "VMD_MCP_HOME": str(tmp_path), "VMD_MCP_VMD": str(fake_vmd.path)}
    transport = PythonStdioTransport(script_path=ROOT / "src" / "server.py", env=env, cwd=str(tmp_path),
                                     python_cmd=sys.executable)

    async def main():
        async with Client(transport) as c:
            tools = {t.name for t in await c.list_tools()}
            check = (await c.call_tool("check_vmd_environment", {})).structured_content
            return tools, check
    tools, check = asyncio.run(main())
    assert {"render_image", "rmsd_over_trajectory", "launch_vmd_gui"} <= tools
    assert check["status"] == "success" and check["vmd_version"] == "1.9.4a57"
