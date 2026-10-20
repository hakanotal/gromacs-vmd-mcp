#!/usr/bin/env python3
"""MCP server for VMD: headless rendering and trajectory analysis, and interactive VMD sessions.

Every tool returns the flat envelope from vmd.result: status, error (on failure), warnings,
output_files, plus tool specific fields. Nothing is written to stdout except the MCP protocol.
"""

import logging
import os
import platform
import signal
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastmcp import FastMCP  # noqa: E402

import templates  # noqa: E402
from vmd import (  # noqa: E402
    COLORINGS,
    REPRESENTATIONS,
    PathNotAllowedError,
    VMDNotFoundError,
    check_choice,
    check_selection,
    existing_file,
    find_vmd,
    get_home,
    get_results_dir,
    get_workdir_root,
    output_path,
    parse_series,
    result,
    run_vmd_script,
    vmd_env,
)

SERVER_NAME = "vmd"
SERVER_VERSION = "1.0.0"

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vmd-mcp")

mcp = FastMCP(SERVER_NAME)

# Interactive sessions started by this server: pid -> details. Only these can be closed.
_SESSIONS: Dict[int, dict] = {}

_INPUT_ERRORS = (FileNotFoundError, PathNotAllowedError, ValueError, VMDNotFoundError)


def _inputs(structure: str, trajectory: Optional[str]) -> List[str]:
    s = existing_file(structure, "structure")
    t = existing_file(trajectory, "trajectory") if trajectory else None
    return [str(s), str(t) if t else "none"]


def _run(script: str, args: list, out: Path, timeout: Optional[int]) -> Optional[dict]:
    """Run a template; return an error envelope, or None when the output file was written."""
    res = run_vmd_script(script, args, timeout=timeout)
    if not res["success"] or not out.exists():
        reason = "; ".join(res["errors"]) or res["stderr"] or "VMD finished without writing its output"
        return result(False, {"command": res["command"], "stdout": res["stdout"]}, error=f"VMD failed: {reason}")
    return None


@mcp.tool()
def check_vmd_environment() -> dict:
    """
    Diagnose the setup: the VMD executable, its version in text mode, the workspace and
    sandbox, and optional features. Run this first when a tool fails.
    """
    data = {"server_version": SERVER_VERSION, "platform": platform.platform(),
            "workdir_restriction": str(get_workdir_root()) if get_workdir_root() else None,
            "home": str(get_home()), "tcl_enabled": os.environ.get("VMD_MCP_ALLOW_TCL") == "1"}
    try:
        import PIL  # noqa: F401
        data["png_output"] = True
    except ImportError:
        data["png_output"] = False
    try:
        data["vmd"] = find_vmd()[0]
    except VMDNotFoundError as e:
        return result(False, data, error=str(e))
    res = run_vmd_script(templates.VERSION, [], timeout=120)
    version = next((l.split(None, 1)[1] for l in res["stdout"].splitlines() if l.startswith("VMD_MCP_VERSION ")), None)
    data["vmd_version"] = version
    if not version:
        return result(False, {**data, "stdout": res["stdout"], "stderr": res["stderr"]},
                      error="VMD was found but did not run in text mode (-dispdev text)")
    warnings = [] if data["png_output"] else ["Pillow is not installed: images are written as .tga "
                                              "(pip install pillow for .png)"]
    return result(True, data, warnings=warnings)


@mcp.tool()
def list_templates() -> dict:
    """List the scripted operations and the render style presets (selection, representation, coloring)."""
    return result(True, {"operations": templates.DESCRIPTIONS, "styles": templates.STYLES,
                         "representations": list(REPRESENTATIONS), "colorings": list(COLORINGS)})


@mcp.tool()
def render_image(
    structure: str,
    trajectory: Optional[str] = None,
    output_file: Optional[str] = None,
    style: Optional[str] = None,
    selection: str = "all",
    representation: str = "Lines",
    coloring: str = "Name",
    frame: int = -1,
    timeout: int = 600,
) -> dict:
    """
    Render a structure, or one frame of a trajectory, to an image without a display (Tachyon).

    Args:
        structure: .pdb, .gro, .psf or another format VMD reads (a .tpr gives topology only)
        trajectory: optional .xtc, .trr, .dcd loaded into the same molecule
        output_file: .png (needs Pillow) or .tga; default: a new file under results/render/
        style: a preset from list_templates (protein_cartoon, protein_surface, ligand_licorice,
            water_points, all_lines); overrides selection, representation and coloring
        selection: VMD atom selection, for example 'protein' or 'resname SOL and within 5 of protein'
        representation: Lines, Licorice, NewCartoon, QuickSurf, VDW, ... (see list_templates)
        coloring: Name, Element, ResType, Structure, Chain, ... (see list_templates)
        frame: frame to show; -1 is the last one

    Images are 512 x 512 pixels: VMD's text mode cannot change the size reliably.

    Returns:
        output_file, format, and the VMD command.
    """
    try:
        args = _inputs(structure, trajectory)
        if style:
            preset = templates.STYLES.get(style)
            if not preset:
                raise ValueError(f"Unknown style {style!r}; available: {', '.join(templates.STYLES)}")
            selection, representation, coloring = preset["selection"], preset["representation"], preset["coloring"]
        check_selection(selection)
        check_choice(representation, REPRESENTATIONS, "representation")
        check_choice(coloring, COLORINGS, "coloring")
        out = output_path(output_file, "render", Path(structure).stem, ".png")
        if out.suffix.lower() not in (".png", ".tga"):
            raise ValueError("output_file must end in .png or .tga")
    except _INPUT_ERRORS as e:
        return result(False, error=str(e))

    tga = out if out.suffix.lower() == ".tga" else out.with_suffix(".tga")
    failure = _run(templates.RENDER, args + [int(frame), selection, representation, coloring, str(tga)], tga, timeout)
    if failure:
        return failure
    warnings = []
    final = tga
    if out.suffix.lower() == ".png":
        try:
            from PIL import Image
            with Image.open(tga) as img:
                img.save(out)
            tga.unlink()
            final = out
        except ImportError:
            warnings.append("Pillow is not installed, so the image was kept as .tga (pip install pillow)")
        except OSError as e:
            warnings.append(f"Could not convert to PNG ({e}); the image was kept as .tga")
    return result(True, {"output_file": str(final), "format": final.suffix.lstrip(".")},
                  output_files=[str(final)], warnings=warnings)


@mcp.tool()
def rmsd_over_trajectory(
    structure: str,
    trajectory: str,
    selection: str = "protein and name CA",
    reference_frame: int = 0,
    align: bool = True,
    output_file: Optional[str] = None,
    timeout: int = 900,
) -> dict:
    """
    RMSD of a selection in every frame against a reference frame (in Angstrom, VMD units).

    Args:
        structure: topology for the atoms, for example the .gro or .pdb the run started from
        trajectory: .xtc, .trr or .dcd
        selection: atoms to compare, for example 'protein and name CA' or 'backbone'
        reference_frame: frame used as the reference (0 is the first)
        align: fit each frame onto the reference first (removes rotation and translation)
        output_file: where to save "frame rmsd" rows (default: results/rmsd/)

    Returns:
        n_frames, rmsd (list of [frame, value]), mean, max, final, output_file.
    """
    try:
        args = _inputs(structure, trajectory)
        check_selection(selection)
        if int(reference_frame) < 0:
            raise ValueError("reference_frame must be 0 or greater")
        out = output_path(output_file, "rmsd", Path(trajectory).stem, "_rmsd.dat")
    except _INPUT_ERRORS as e:
        return result(False, error=str(e))
    failure = _run(templates.RMSD, args + [selection, int(reference_frame), 1 if align else 0, str(out)], out, timeout)
    if failure:
        return failure
    rows = [r[:2] for r in parse_series(out.read_text()) if len(r) >= 2]
    if not rows:
        return result(False, {"output_file": str(out)}, error="VMD wrote no RMSD values (empty selection?)")
    values = [r[1] for r in rows]
    return result(True, {"n_frames": len(rows), "unit": "Angstrom", "rmsd": rows,
                         "mean": sum(values) / len(values), "max": max(values), "final": values[-1],
                         "output_file": str(out)}, output_files=[str(out)])


@mcp.tool()
def contacts_over_trajectory(
    structure: str,
    trajectory: str,
    selection1: str,
    selection2: str,
    cutoff: float = 3.5,
    output_file: Optional[str] = None,
    timeout: int = 900,
) -> dict:
    """
    Number of atom pairs between two selections closer than a cutoff, in every frame. Pairs VMD
    considers bonded are not counted (VMD's measure contacts).

    Args:
        structure / trajectory: as for rmsd_over_trajectory
        selection1 / selection2: VMD atom selections, for example 'protein' and 'resname LIG'
        cutoff: distance in Angstrom (default 3.5)
        output_file: where to save "frame count" rows (default: results/contacts/)

    Returns:
        n_frames, contacts (list of [frame, count]), mean, max, output_file.
    """
    try:
        args = _inputs(structure, trajectory)
        check_selection(selection1, "selection1")
        check_selection(selection2, "selection2")
        if not 0 < float(cutoff) <= 50:
            raise ValueError("cutoff must be between 0 and 50 Angstrom")
        out = output_path(output_file, "contacts", Path(trajectory).stem, "_contacts.dat")
    except _INPUT_ERRORS as e:
        return result(False, error=str(e))
    failure = _run(templates.CONTACTS, args + [selection1, selection2, float(cutoff), str(out)], out, timeout)
    if failure:
        return failure
    rows = [r[:2] for r in parse_series(out.read_text()) if len(r) >= 2]
    if not rows:
        return result(False, {"output_file": str(out)}, error="VMD wrote no contact counts")
    counts = [r[1] for r in rows]
    return result(True, {"n_frames": len(rows), "contacts": rows, "mean": sum(counts) / len(counts),
                         "max": max(counts), "output_file": str(out)}, output_files=[str(out)])


@mcp.tool()
def launch_vmd_gui(structure: str, trajectory: Optional[str] = None) -> dict:
    """
    Open the files in an interactive VMD window on this machine (needs a display).

    The window belongs to the user; close it there, or with close_vmd_session. Returns the pid.
    """
    try:
        files = [str(existing_file(structure, "structure"))]
        if trajectory:
            files.append(str(existing_file(trajectory, "trajectory")))
        vmd = find_vmd()
    except _INPUT_ERRORS as e:
        return result(False, error=str(e))
    log_dir = get_results_dir() / "sessions"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"vmd_{len(_SESSIONS) + 1}_{os.getpid()}.log"
    cmd = vmd + ["-f"] + files  # -f loads every file into one molecule
    with open(log, "w") as fh:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                start_new_session=True, env=vmd_env(vmd))
    _SESSIONS[proc.pid] = {"pid": proc.pid, "files": files, "log": str(log), "process": proc}
    return result(True, {"pid": proc.pid, "command": " ".join(cmd), "log": str(log)})


@mcp.tool()
def list_vmd_sessions() -> dict:
    """List the interactive VMD sessions this server started and whether they are still open."""
    sessions = [{"pid": pid, "files": s["files"], "running": s["process"].poll() is None, "log": s["log"]}
                for pid, s in _SESSIONS.items()]
    return result(True, {"sessions": sessions, "total": len(sessions)})


@mcp.tool()
def close_vmd_session(pid: int) -> dict:
    """Close an interactive VMD session started by launch_vmd_gui (other processes are never touched)."""
    session = _SESSIONS.get(pid)
    if not session:
        return result(False, error=f"No session with pid {pid} was started by this server")
    proc = session["process"]
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    _SESSIONS.pop(pid, None)
    return result(True, {"pid": pid, "message": "closed"})


@mcp.tool()
def run_vmd_tcl(script: str, timeout: int = 600) -> dict:
    """
    Run your own Tcl script in VMD's text mode. Disabled unless the server was started with
    VMD_MCP_ALLOW_TCL=1, because Tcl can run any shell command (exec). Prefer the other tools.

    Args:
        script: Tcl commands (run as one block; `quit` is appended)
    """
    if os.environ.get("VMD_MCP_ALLOW_TCL") != "1":
        return result(False, error="run_vmd_tcl is disabled: arbitrary Tcl can run shell commands. "
                                   "Start the server with VMD_MCP_ALLOW_TCL=1 to enable it")
    try:
        res = run_vmd_script(script, [], timeout=timeout)
    except VMDNotFoundError as e:
        return result(False, error=str(e))
    data = {"stdout": res["stdout"], "stderr": res["stderr"], "command": res["command"]}
    if not res["success"]:
        return result(False, data, error="; ".join(res["errors"]) or "VMD failed")
    return result(True, data)


def main() -> None:
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
