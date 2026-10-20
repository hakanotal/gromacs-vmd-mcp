"""Shared test setup. No VMD is needed: a fake `vmd` records how it was called.

VMD_MCP_HOME points at a temporary folder before the server is imported, so tests never write
into the repository.
"""

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_TEST_HOME = tempfile.mkdtemp(prefix="vmd_mcp_test_home_")
os.environ["VMD_MCP_HOME"] = _TEST_HOME
os.environ.pop("VMD_MCP_WORKDIR", None)
os.environ.pop("VMD_MCP_ALLOW_TCL", None)
sys.path.insert(0, str(ROOT / "src"))

# A 1x1 uncompressed true color TGA, what TachyonInternal writes (in miniature)
TGA_1X1 = bytes([0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 24, 0, 255, 0, 0])

FAKE_VMD = r'''#!{python}
import json, os, sys, time
argv = sys.argv[1:]
script = open(argv[argv.index("-e") + 1]).read() if "-e" in argv else ""
with open({log!r}, "a") as fh:
    fh.write(json.dumps({{"argv": argv, "script": script, "cwd": os.getcwd()}}) + "\n")
if os.environ.get("FAKE_VMD_PRESTART"):  # what VMD prints from its own start-up files
    print(os.environ["FAKE_VMD_PRESTART"])
if "VMD_MCP_SCRIPT_START" in script:
    print("VMD_MCP_SCRIPT_START")
if "VMD_MCP_VERSION" in script:
    print("VMD_MCP_VERSION 1.9.4a57")
elif "-args" in argv and os.environ.get("FAKE_VMD_WRITE", "1") == "1":
    args = argv[argv.index("-args") + 1:]
    tga = bytes({tga})
    if "lassign $argv structure trajectory frame mode zoom out" in script:      # render_image
        out = args[5]
        if args[3] == "scene":
            open(out, "w").write("Resolution 512 512\n")
        else:
            open(out, "wb").write(tga)
    elif "lassign $argv structure trajectory first last" in script:             # render_movie
        outdir, mode = args[9], args[7]
        for k in range(int(os.environ.get("FAKE_VMD_FRAMES", "3"))):
            name = os.path.join(outdir, "frame_%05d" % k)
            if mode == "scene":
                open(name + ".dat", "w").write("scene\n")
            else:
                open(name + ".tga", "wb").write(tga)
        if os.environ.get("FAKE_VMD_TRUNCATED"):
            print("VMD_MCP_TRUNCATED at 3 frames")
    elif args:
        out = args[-1]
        values = {{"measure rmsd": "0 0.0\n1 1.25\n2 1.75\n", "measure contacts": "0 4\n1 6\n2 5\n",
                  "measure rgyr": "0 10.5\n1 11.0\n", "measure sasa": "0 1200.0\n1 1180.0\n",
                  "measure hbonds": "0 3\n1 4\n", "ssrecalc": "0 CHHHHC\n1 CHHHEC\n"}}
        for key, text in values.items():
            if key in script:
                open(out, "w").write(text)
                break
if os.environ.get("FAKE_VMD_STDOUT"):
    print(os.environ["FAKE_VMD_STDOUT"])
if "-f" in argv:  # like the GUI: runs until its console input says quit or ends
    sys.stdin.readline()
sys.exit(int(os.environ.get("FAKE_VMD_RC", "0")))
'''


def pytest_unconfigure(config):
    shutil.rmtree(_TEST_HOME, ignore_errors=True)


@pytest.fixture
def fake_vmd(tmp_path, monkeypatch):
    """A stand-in vmd. Helper: .calls() gives [{argv, script, cwd}] of every run."""
    log = tmp_path / "fake_vmd_calls.jsonl"
    exe = tmp_path / "fakevmd"
    exe.write_text(FAKE_VMD.format(python=sys.executable, log=str(log), tga=list(TGA_1X1)))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("VMD_MCP_VMD", str(exe))

    class Helper:
        path = exe

        def calls(self):
            return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []

    return Helper()


FAKE_TOOL = r'''#!{python}
import json, sys
argv = sys.argv[1:]
open({log!r}, "a").write(json.dumps(argv) + "\n")
out = argv[argv.index("-o") + 1] if "-o" in argv else argv[-1]
open(out, "wb").write(bytes({tga}))
'''


def _fake_tool(tmp_path, monkeypatch, name, env):
    log = tmp_path / f"{name}_calls.jsonl"
    exe = tmp_path / name
    exe.write_text(FAKE_TOOL.format(python=sys.executable, log=str(log), tga=list(TGA_1X1)))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(env, str(exe))

    class Helper:
        path = exe

        def calls(self):
            return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return Helper()


@pytest.fixture
def fake_tachyon(tmp_path, monkeypatch):
    """A stand-in for VMD's tachyon binary: writes a tiny TGA to the -o path."""
    return _fake_tool(tmp_path, monkeypatch, "faketachyon", "VMD_MCP_TACHYON")


@pytest.fixture
def fake_ffmpeg(tmp_path, monkeypatch):
    """A stand-in ffmpeg: writes the output file (its last argument)."""
    return _fake_tool(tmp_path, monkeypatch, "fakeffmpeg", "VMD_MCP_FFMPEG")


@pytest.fixture(autouse=True)
def no_real_helpers(monkeypatch, tmp_path):
    """Tests use fakes unless they ask for them: never a tachyon or ffmpeg found on this machine."""
    monkeypatch.setenv("VMD_MCP_TACHYON", str(tmp_path / "no_tachyon"))
    monkeypatch.setenv("VMD_MCP_FFMPEG", str(tmp_path / "no_ffmpeg"))


@pytest.fixture
def files(tmp_path):
    """A structure and a trajectory (contents do not matter to the fake vmd)."""
    s, t = tmp_path / "conf.gro", tmp_path / "traj.xtc"
    s.write_text("water\n")
    t.write_bytes(b"x")
    return s, t
