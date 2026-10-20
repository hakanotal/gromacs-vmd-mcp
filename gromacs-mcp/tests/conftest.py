"""Shared pytest setup.

Runtime files (jobs, results, logs) go to a temporary GMX_MCP_HOME that is set before the server
is imported, so tests never write into the repository.

Markers:
    gmx   needs a working gmx binary (skipped otherwise)
    slow  runs a real simulation; deselect with -m "not slow"
"""

import asyncio
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLE_DATA = ROOT / "examples" / "data"

_TEST_HOME = tempfile.mkdtemp(prefix="gmx_mcp_test_home_")
os.environ["GMX_MCP_HOME"] = _TEST_HOME
os.environ.pop("GMX_MCP_WORKDIR", None)

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))


def pytest_configure(config):
    config.addinivalue_line("markers", "gmx: requires a working gmx binary")
    config.addinivalue_line("markers", "slow: runs a real simulation")


def pytest_unconfigure(config):
    shutil.rmtree(_TEST_HOME, ignore_errors=True)


def _gmx_works() -> bool:
    try:
        from lib.gromacs_utils import find_gromacs_command, run_command_full
        return run_command_full(find_gromacs_command() + ["--version"], timeout=30)["success"]
    except Exception:
        return False


HAVE_GMX = _gmx_works()


def pytest_collection_modifyitems(config, items):
    if HAVE_GMX:
        return
    skip = pytest.mark.skip(reason="gmx not available")
    for item in items:
        if "gmx" in item.keywords or "slow" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def allow_test_job_scripts(tmp_path_factory):
    """Jobs only run scripts from allowed folders; the fake job scripts of the tests live in pytest's temp folder."""
    os.environ["GMX_MCP_SCRIPT_DIRS"] = str(tmp_path_factory.getbasetemp().resolve())
    yield


@pytest.fixture(scope="session")
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def example_data() -> Path:
    """Example data directory. sample.tpr is built with the local gmx when it is missing or was
    written by a newer GROMACS than the one installed."""
    if not HAVE_GMX:
        pytest.skip("gmx unavailable")
    from lib.gromacs_utils import find_gromacs_command, run_command_full
    tpr = EXAMPLE_DATA / "sample.tpr"
    readable = tpr.exists() and run_command_full(find_gromacs_command() + ["dump", "-s", str(tpr)], timeout=60)["success"]
    if not readable:
        res = run_command_full([sys.executable, str(ROOT / "scripts" / "make_example_data.py"), "--tpr-only"],
                               timeout=300)
        assert res["success"], res["stdout"] + res["stderr"]
    return EXAMPLE_DATA


@pytest.fixture
def fake_gmx(tmp_path, monkeypatch):
    """
    A stand in `gmx` that records how it was called, so wrapper behavior can be tested without GROMACS.

    Returns a helper with .calls() (list of argv lists), .stdin() (text received on stdin),
    and settable behavior through environment variables FAKE_GMX_RC, FAKE_GMX_STDOUT, FAKE_GMX_STDERR.
    `help <tool>` prints FAKE_GMX_HELP (a file; default the real `gmx help rdf` text).
    """
    log = tmp_path / "fake_gmx_calls.jsonl"
    script = tmp_path / "fakegmx"
    script.write_text(
        "#!/bin/sh\n"
        "FAKE_STDIN=$(cat)\n"
        "export FAKE_STDIN\n"
        "python3 - \"$@\" <<'PY'\n"
        "import json, os, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'cwd': os.getcwd(), "
        "'stdin': os.environ.get('FAKE_STDIN', '')}) + '\\n')\n"
        "PY\n"
        # `gmx help <tool>` answers with a real help text (the wrapper reads file options from it)
        f"if [ \"$1\" = help ]; then cat \"${{FAKE_GMX_HELP:-{FIXTURES / 'gmx_help_rdf.txt'}}}\"; exit 0; fi\n"
        "if [ -n \"$FAKE_GMX_STDOUT\" ]; then printf '%s\\n' \"$FAKE_GMX_STDOUT\"; fi\n"
        "if [ -n \"$FAKE_GMX_STDERR\" ]; then printf '%s\\n' \"$FAKE_GMX_STDERR\" >&2; fi\n"
        "exit ${FAKE_GMX_RC:-0}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("GMX_MCP_GMX", str(script))

    class Helper:
        def _records(self):
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text().splitlines()]

        def _real(self):
            # the --version probe and the `help <tool>` lookup of file options are not the command under test
            return [r for r in self._records() if r["argv"] != ["--version"] and r["argv"][:1] != ["help"]]

        def calls(self):
            """argv of every real command."""
            return [r["argv"] for r in self._real()]

        def cwds(self):
            return [r["cwd"] for r in self._real()]

        def stdin(self):
            """Text received on stdin by the last real command. The shell strips trailing newlines
            of its command substitution, so compare with .rstrip()."""
            real = self._real()
            return real[-1]["stdin"] if real else ""

    return Helper()


@pytest.fixture(scope="session")
def mcp_call():
    """Call a tool on the in-memory server through a real MCP client; returns the result dict."""
    import server
    from fastmcp import Client

    def call(name: str, **args):
        async def run():
            async with Client(server.mcp) as c:
                r = await c.call_tool(name, args, raise_on_error=False)
                if r.structured_content is not None:
                    return r.structured_content
                return json.loads(r.content[0].text)
        return asyncio.run(run())

    return call
