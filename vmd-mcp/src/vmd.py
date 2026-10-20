"""Finding and running VMD, the path sandbox, input checks and the result envelope.

VMD always runs as an argument list, never through a shell:

    vmd -dispdev text -eofexit -e <script.tcl> -args <value> <value> ...

The Tcl scripts are fixed text (templates.py). Everything the client supplies (file paths,
selections, numbers) reaches Tcl only as elements of $argv, so it is data and never parsed as
Tcl. Atom selections are additionally limited to characters the VMD selection language needs.

Environment variables:
    VMD_MCP_VMD        path to the vmd executable (default: vmd on PATH, then VMD.app on macOS)
    VMD_MCP_WORKDIR    only allow paths below this directory (default: unrestricted)
    VMD_MCP_HOME       where results/ and logs/ go (default: the vmd-mcp folder, or
                       <VMD_MCP_WORKDIR>/vmd_mcp when a sandbox is set)
    VMD_MCP_ALLOW_TCL  set to 1 to enable run_vmd_tcl (arbitrary Tcl can run shell commands)
    VMD_MCP_TIMEOUT    seconds before a VMD run is stopped (default 600)
"""

import glob
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

PathLike = Union[str, Path]
ROOT = Path(__file__).resolve().parent.parent
MAX_OUTPUT_LINES = 60
START_MARKER = "VMD_MCP_SCRIPT_START"
ERROR_MARKER = "VMD_MCP_ERROR:"


def wrap_script(script: str) -> str:
    """
    The script as VMD runs it: a start marker, the body as one `catch` block, then `quit`.

    Run line by line, VMD 2 silently abandons an -e script at the first Tcl error, and can stop
    mid-script once it has seen the end of its (closed) stdin. As one block, the whole body runs,
    any error is printed with ERROR_MARKER, and quit always follows.
    """
    return (f'puts "{START_MARKER}"\n'
            "if {[catch {\n" + script + "\n} vmd_mcp_err]} {\n"
            f'    puts "{ERROR_MARKER} $vmd_mcp_err"\n'
            "}\nquit\n")


class VMDNotFoundError(RuntimeError):
    """No usable vmd executable."""


class PathNotAllowedError(ValueError):
    """A path resolves outside VMD_MCP_WORKDIR."""


# ==============================================================================
# Locations and the sandbox
# ==============================================================================
def get_workdir_root() -> Optional[Path]:
    root = os.environ.get("VMD_MCP_WORKDIR")
    return Path(root).expanduser().resolve() if root else None


def get_home() -> Path:
    explicit = os.environ.get("VMD_MCP_HOME")
    if explicit:
        return Path(explicit).expanduser()
    root = get_workdir_root()
    return root / "vmd_mcp" if root is not None else ROOT


def get_results_dir() -> Path:
    return get_home() / "results"


def resolve_in_workdir(path: PathLike) -> Path:
    """Resolve symlinks and `..`; with VMD_MCP_WORKDIR set, the result must be inside it."""
    resolved = Path(path).expanduser().resolve()
    root = get_workdir_root()
    if root is not None and resolved != root and root not in resolved.parents:
        raise PathNotAllowedError(f"Path {resolved} is outside the allowed directory {root} (VMD_MCP_WORKDIR)")
    return resolved


def existing_file(path: Optional[str], label: str) -> Path:
    if not path:
        raise FileNotFoundError(f"{label} is required")
    p = resolve_in_workdir(path)
    if not p.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
    return p


def output_path(path: Optional[str], subdir: str, stem: str, suffix: str) -> Path:
    """The caller's path, or a new uniquely named file under results/<subdir>/."""
    if path:
        p = resolve_in_workdir(path)
    else:
        p = resolve_in_workdir(get_results_dir() / subdir / unique_name(stem, suffix))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def unique_name(stem: str, suffix: str = "") -> str:
    return f"{stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{suffix}"


# ==============================================================================
# Input checks
# ==============================================================================
# The VMD selection language needs letters, digits, spaces, comparison and arithmetic operators,
# parentheses, dots and single quotes. Brackets, braces, $, ;, double quotes and backslashes are Tcl
# syntax and are refused, even though values only travel through $argv.
_SELECTION_RE = re.compile(r"^[A-Za-z0-9_ .,:'*+\-/<>=!()]{1,500}$")

REPRESENTATIONS = ("Lines", "Bonds", "CPK", "VDW", "Licorice", "Points", "Tube", "Trace",
                   "NewRibbons", "NewCartoon", "Cartoon", "QuickSurf", "Surf", "MSMS", "Polyhedra")
COLORINGS = ("Name", "Type", "Element", "ResName", "ResType", "ResID", "Chain", "SegName",
             "Structure", "Beta", "Occupancy", "Mass", "Charge", "Fragment", "Index", "Molecule")


def check_selection(text: str, label: str = "selection") -> str:
    if not isinstance(text, str) or not _SELECTION_RE.match(text):
        raise ValueError(f"{label} {text!r} is not a plain VMD atom selection: use letters, digits, spaces, "
                         "operators and parentheses only (no [ ] { } $ ; \\ or double quotes)")
    return text


def check_choice(value: str, allowed: tuple, label: str) -> str:
    if value not in allowed:
        raise ValueError(f"{label} must be one of {', '.join(allowed)}; got {value!r}")
    return value


# ==============================================================================
# Finding and running VMD
# ==============================================================================
def find_vmd() -> List[str]:
    """argv prefix for vmd. Order: $VMD_MCP_VMD, vmd on PATH, VMD.app on macOS (VMD 2, then 1.9), /usr/local/bin/vmd."""
    tried = []
    explicit = os.environ.get("VMD_MCP_VMD")
    if explicit:
        tried.append(f"$VMD_MCP_VMD={explicit}")
        found = shutil.which(explicit) or (explicit if Path(explicit).is_file() else None)
        if found:
            return [found]
    tried.append("vmd on PATH")
    found = shutil.which("vmd")
    if found:
        return [found]
    # VMD 1.9 apps keep the binary in Contents/vmd, VMD 2 apps in Contents/vmd2/lib. The csh launchers
    # next to them can carry the packager's paths, so the binary is run directly (vmd_env sets VMDDIR).
    for pattern in ("/Applications/VMD*.app/Contents/vmd2/lib/vmd_MACOSX*",
                    "/Applications/VMD*.app/Contents/vmd/vmd_MACOSX*"):
        tried.append(pattern)
        apps = sorted(p for p in glob.glob(pattern) if os.access(p, os.X_OK))
        if apps:
            return [apps[-1]]
    tried.append("/usr/local/bin/vmd")
    if os.access("/usr/local/bin/vmd", os.X_OK):
        return ["/usr/local/bin/vmd"]
    raise VMDNotFoundError("Could not find VMD. Tried: " + "; ".join(tried)
                           + ". Install VMD or set VMD_MCP_VMD to the vmd executable.")


def vmd_env(vmd: List[str]) -> Dict[str, str]:
    """The environment for VMD. The binary inside VMD.app needs VMDDIR (the `vmd` launcher sets it)."""
    env = dict(os.environ)
    binary = Path(vmd[0])
    if binary.name.startswith("vmd_") and "VMDDIR" not in env:
        env["VMDDIR"] = str(binary.parent)
    return env


def _tail(text: str, n: int = MAX_OUTPUT_LINES) -> str:
    lines = (text or "").strip().splitlines()
    return "\n".join(lines[-n:])


def run_vmd_script(script: str, args: List[Any], timeout: Optional[int] = None) -> Dict[str, Any]:
    """
    Run a Tcl script in VMD's text mode. args reach the script as $argv (data, never code).

    Returns success, returncode, stdout and stderr (last lines), command, timed_out.
    """
    vmd = find_vmd()
    timeout = timeout or int(os.environ.get("VMD_MCP_TIMEOUT", 600))
    tmp_root = get_results_dir() / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
        script_path = Path(tmp) / "script.tcl"
        script_path.write_text(wrap_script(script))
        cmd = vmd + ["-dispdev", "text", "-eofexit", "-e", str(script_path), "-args"] + [str(a) for a in args]
        try:
            proc = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL, env=vmd_env(vmd))
            ok, rc, out, err, timed_out = proc.returncode == 0, proc.returncode, proc.stdout, proc.stderr, False
        except subprocess.TimeoutExpired as e:
            ok, rc, out, err, timed_out = False, None, (e.stdout or "") if isinstance(e.stdout, str) else "", \
                f"VMD timed out after {timeout} seconds", True
        except OSError as e:
            ok, rc, out, err, timed_out = False, None, "", str(e), False
    # VMD reports Tcl errors on stdout and may still exit 0. Only lines after the script started count:
    # VMD 2 prints "ERROR) Duplicate resname" lines from its own start-up files on every run.
    after = out.split(START_MARKER, 1)[1] if START_MARKER in out else out
    errors = [l.strip() for l in (after + "\n" + err).splitlines()
              if l.strip().startswith(("ERROR)", ERROR_MARKER, "can't ", "invalid command"))]
    if errors:
        ok = False
    return {"success": ok, "returncode": rc, "stdout": _tail(out), "stderr": _tail(err),
            "command": " ".join(cmd), "timed_out": timed_out, "errors": errors[:10]}


def parse_series(text: str) -> List[List[float]]:
    """Rows of numbers from a .dat file written by the analysis scripts ("frame value")."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if not parts or line.lstrip().startswith("#"):
            continue
        try:
            rows.append([float(x) for x in parts])
        except ValueError:
            continue
    return rows


# ==============================================================================
# Result envelope (the same shape as gromacs-mcp)
# ==============================================================================
def result(success: bool, data: Optional[Dict[str, Any]] = None, error: Optional[str] = None,
           output_files: Optional[List[str]] = None, warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    """Flat dict: status, error (on failure), warnings, output_files, plus tool specific fields."""
    out: Dict[str, Any] = {"status": "success" if success else "error"}
    if not success:
        out["error"] = error or "Unknown error"
    out.update(data or {})
    out["warnings"] = list(warnings or [])
    out["output_files"] = list(output_files or [])
    return out
