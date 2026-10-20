#!/usr/bin/env python3
"""
GROMACS Utilities Library

Single place that knows how to find and run `gmx`, plus parsers for the text
formats GROMACS produces (gmx dump, gmx energy, .xvg, md.log).

Commands are always run as argv lists, never through a shell. Standard input
(for interactive group selections) is passed with `stdin_text`.

Environment variables:
    GMX_MCP_GMX       Explicit path to the gmx binary (highest priority)
    GMX_MCP_WORKDIR   If set, input/output paths must live under this directory
"""

import math
import os
import re
import shutil
import statistics
import subprocess
import threading
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]


class GromacsNotFoundError(RuntimeError):
    """Raised when no usable gmx executable can be located."""


class PathNotAllowedError(ValueError):
    """Raised when a path resolves outside GMX_MCP_WORKDIR."""


# ==============================================================================
# Locating and running gmx
# ==============================================================================
def find_gromacs_command() -> List[str]:
    """
    Locate the GROMACS executable.

    Search order: $GMX_MCP_GMX, `gmx`, `gmx_mpi`, then (only if mamba exists on
    PATH and ./env exists) `mamba run -p ./env gmx`.

    Returns:
        argv prefix, for example ['/opt/homebrew/bin/gmx']

    Raises:
        GromacsNotFoundError: with the list of places that were tried.
    """
    tried = []

    explicit = os.environ.get("GMX_MCP_GMX")
    if explicit:
        tried.append(f"$GMX_MCP_GMX={explicit}")
        resolved = shutil.which(explicit) or (explicit if Path(explicit).is_file() else None)
        if resolved:
            return [resolved]

    for name in ("gmx", "gmx_mpi"):
        tried.append(f"{name} on PATH")
        found = shutil.which(name)
        if found:
            return [found]

    mamba = shutil.which("mamba")
    env_path = Path(__file__).resolve().parent.parent.parent / "env"
    tried.append("mamba run -p ./env gmx")
    if mamba and env_path.exists():
        return [mamba, "run", "-p", str(env_path), "gmx"]

    raise GromacsNotFoundError(
        "Could not find GROMACS. Tried: " + "; ".join(tried)
        + ". Install GROMACS or set GMX_MCP_GMX to the gmx binary."
    )


# gmx reads compressed files through `gunzip -c < name` in a shell (and falls back to name.gz when name
# is missing), so file names must not carry characters the shell acts on, nor be compressed files.
_SHELL_ACTIVE = set("$`;&|<>\n\r")


def check_shell_safe_path(path: Union[str, Path]) -> None:
    text = str(path)
    bad = sorted(_SHELL_ACTIVE.intersection(text))
    if bad:
        raise ValueError(f"File name {text!r} contains {' '.join(repr(c) for c in bad)}; GROMACS may pass file "
                         "names to a shell, so these characters are not allowed")
    if text.lower().endswith((".gz", ".z")):
        raise ValueError(f"Compressed file {text!r}: GROMACS opens these through a shell; decompress it first")


FILE_SUFFIXES = (".xvg", ".xtc", ".trr", ".gro", ".pdb", ".tpr", ".edr", ".ndx", ".top", ".itp", ".mdp",
                  ".log", ".cpt", ".dat", ".txt", ".xpm", ".eps", ".png", ".csv", ".g96", ".tng", ".mtx")


def unsafe_argv_path(cmd: List[str]) -> Optional[str]:
    """
    The first argument that looks like a file name (a slash, or a GROMACS or compressed suffix) and
    is not shell safe, with the reason; None when all are fine. run_command_full applies this to
    every command, so every tool and workflow that passes paths to gmx is covered in one place.
    """
    for arg in cmd[1:]:
        if arg.startswith("-") and not arg.startswith(("-/", "-.")):
            continue
        if "/" in arg or os.sep in arg or arg.lower().endswith(FILE_SUFFIXES + (".gz", ".z")):
            try:
                check_shell_safe_path(arg)
            except ValueError as e:
                return str(e)
    return None


def run_command_full(
    cmd: List[str],
    working_dir: Optional[PathLike] = None,
    timeout: Optional[int] = 300,
    stdin_text: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Run a command (argv list, no shell) and return everything about the run.

    Args:
        cmd: argv list
        working_dir: working directory
        timeout: seconds, or None for no limit
        stdin_text: text written to the process's standard input
        env: extra environment variables

    Returns:
        dict with success, returncode, stdout, stderr, command, timed_out
    """
    cmd = [str(c) for c in cmd]
    unsafe = unsafe_argv_path(cmd)
    if unsafe:
        return {"success": False, "returncode": None, "stdout": "", "stderr": "Error in user input:\n" + unsafe,
                "command": " ".join(cmd), "timed_out": False}
    # gmx -w opens results in a viewer through the shell (GMX_VIEW_*, xmgrace); never give it a display
    run_env = {k: v for k, v in os.environ.items() if k != "DISPLAY" and not k.startswith("GMX_VIEW_")}
    run_env.update(env or {})
    logger.debug("Running command: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(working_dir) if working_dir else None,
            timeout=timeout,
            # with no answers to give, close stdin: it must never inherit the MCP protocol pipe
            **({"input": stdin_text} if stdin_text is not None else {"stdin": subprocess.DEVNULL}),
            env=run_env,
        )
        return {
            "success": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "command": " ".join(cmd),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as e:
        logger.error("Command timed out after %s seconds", timeout)
        return {
            "success": False,
            "returncode": None,
            "stdout": (e.stdout or "") if isinstance(e.stdout, str) else "",
            "stderr": f"Command timed out after {timeout} seconds",
            "command": " ".join(cmd),
            "timed_out": True,
        }
    except Exception as e:
        logger.error("Command failed with exception: %s", e)
        return {
            "success": False,
            "returncode": None,
            "stdout": "",
            "stderr": str(e),
            "command": " ".join(cmd),
            "timed_out": False,
        }


@lru_cache(maxsize=8)
def _gmx_version_for(cmd_key: Tuple[str, ...]) -> Dict[str, str]:
    res = run_command_full(list(cmd_key) + ["--version"], timeout=30)
    text = res["stdout"] + res["stderr"]
    info: Dict[str, str] = {"raw": text.strip().splitlines()[0] if text.strip() else ""}
    for key, label in (
        ("version", r"GROMACS version:\s*(.+)"),
        ("precision", r"Precision:\s*(.+)"),
        ("mpi", r"MPI library:\s*(.+)"),
        ("simd", r"SIMD instructions:\s*(.+)"),
        ("gpu", r"GPU support:\s*(.+)"),
        ("executable", r"Executable:\s*(.+)"),
    ):
        m = re.search(label, text)
        if m:
            info[key] = m.group(1).strip()
    if "version" not in info:
        m = re.search(r"GROMACS\s*-\s*gmx,\s*([^\s(]+)", text)
        if m:
            info["version"] = m.group(1)
    return info


def get_gmx_version() -> Dict[str, str]:
    """Return parsed `gmx --version` (version, precision, mpi, simd, gpu). Cached."""
    try:
        return dict(_gmx_version_for(tuple(find_gromacs_command())))
    except GromacsNotFoundError as e:
        return {"error": str(e)}


# ==============================================================================
# Paths and files
# ==============================================================================
def get_home() -> Path:
    """
    Base directory for runtime files (jobs/, runs/, results/, logs/).

    $GMX_MCP_HOME when set; otherwise <GMX_MCP_WORKDIR>/gromacs_mcp when a sandbox is set, so
    default outputs stay inside it; otherwise the repository root.
    """
    explicit = os.environ.get("GMX_MCP_HOME")
    if explicit:
        return Path(explicit).expanduser()
    root = get_workdir_root()
    if root is not None:
        return root / "gromacs_mcp"
    return Path(__file__).resolve().parent.parent.parent


def get_results_dir() -> Path:
    return Path(os.environ.get("GMX_MCP_RESULTS_DIR", get_home() / "results")).expanduser()


def get_runs_dir() -> Path:
    """Default folders for job outputs. Kept apart from job records so deleting a record never deletes data."""
    return Path(os.environ.get("GMX_MCP_RUNS_DIR", get_home() / "runs")).expanduser()


def unique_name(stem: str, suffix: str = "") -> str:
    """A file or folder name that will not collide with another call (time stamp plus random part)."""
    import uuid
    from datetime import datetime
    return f"{stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{suffix}"


def get_workdir_root() -> Optional[Path]:
    """Return the sandbox root from GMX_MCP_WORKDIR, or None when unrestricted."""
    root = os.environ.get("GMX_MCP_WORKDIR")
    if not root:
        return None
    return Path(root).expanduser().resolve()


def resolve_in_workdir(path: PathLike) -> Path:
    """
    Resolve a path (following symlinks and `..`) and enforce GMX_MCP_WORKDIR.

    Raises:
        PathNotAllowedError: if a sandbox root is set and the path is outside it.
    """
    resolved = Path(path).expanduser().resolve()
    root = get_workdir_root()
    if root is not None:
        try:
            resolved.relative_to(root)
        except ValueError:
            raise PathNotAllowedError(
                f"Path {resolved} is outside the allowed directory {root} (GMX_MCP_WORKDIR)"
            )
    return resolved


def ensure_output_directory(output_dir: PathLike) -> Path:
    """Create the directory if needed and return it as a Path."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    return output_path


def check_file_exists(file_path: PathLike, description: str = "File") -> bool:
    """Check that a file exists, logging an error if not."""
    path = Path(file_path)
    if not path.exists():
        logger.error("%s not found: %s", description, file_path)
        return False
    return True


def get_file_size(file_path: PathLike) -> int:
    """File size in bytes, 0 if the file does not exist."""
    try:
        return Path(file_path).stat().st_size
    except OSError:
        return 0


def copy_input_file(input_file: PathLike, output_dir: PathLike,
                    new_name: Optional[str] = None) -> Path:
    """Copy a file into a directory, optionally renaming it."""
    src_path = Path(input_file)
    dst_dir = Path(output_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_path = dst_dir / (new_name or src_path.name)
    shutil.copy2(src_path, dst_path)
    logger.info("Copied %s to %s", src_path, dst_path)
    return dst_path


def deep_merge(base: Dict[str, Any], override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Recursively merge override into a copy of base (nested dicts are merged, not replaced)."""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def write_json_atomic(path: PathLike, data: Any) -> None:
    """Write JSON to a temp file in the same directory, then rename over the target."""
    import json
    import tempfile
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def free_disk_mb(path: PathLike) -> float:
    """Free disk space in MB for the filesystem containing path."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free / (1024 * 1024)


def default_thread_count(cap: int = 8) -> int:
    """Sensible OpenMP thread default: CPU count capped at `cap`."""
    return max(1, min(os.cpu_count() or 1, cap))


# ==============================================================================
# gmx dump parsing
# ==============================================================================
_INPUTREC_KV = re.compile(r"^ {3}(\S[^=:]*?)\s*=\s*(.*?)\s*$")


def _to_number(value: str) -> Union[int, float, str]:
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def parse_tpr_dump_output(dump_output: str) -> Dict[str, Any]:
    """
    Parse `gmx dump -s file.tpr` output.

    Values are read from the `inputrec:` block (top level keys are indented by
    exactly three spaces), the `grpopts:` block (ref-t, tau-t), the pressure
    coupling matrix, the `header:` block (natoms) and the `topology:` block.
    """
    info: Dict[str, Any] = {}
    section = None
    inputrec: Dict[str, str] = {}
    ref_p_rows: List[List[float]] = []
    ref_t: List[float] = []
    tau_t: List[float] = []
    nmolecules = 0

    for line in dump_output.splitlines():
        stripped = line.strip()
        if not line.startswith(" ") and stripped.endswith(":") and stripped[:-1].isalpha():
            section = stripped[:-1]
            continue

        if section == "inputrec":
            m = _INPUTREC_KV.match(line)
            if m and m.group(1) not in inputrec:
                inputrec[m.group(1)] = m.group(2)
            rp = re.match(r"^\s+ref-p\[\s*\d+\]=\{\s*([^}]*)\}", line)
            if rp:
                ref_p_rows.append([float(x) for x in rp.group(1).split(",")])
        elif section == "grpopts":
            m = re.match(r"^\s+ref-t:\s*(.*)$", line)
            if m:
                ref_t = [float(x) for x in m.group(1).split()]
            m = re.match(r"^\s+tau-t:\s*(.*)$", line)
            if m:
                tau_t = [float(x) for x in m.group(1).split()]
        elif section == "header":
            m = re.match(r"^\s+natoms\s*=\s*(\d+)", line)
            if m:
                info["natoms"] = int(m.group(1))
        elif section == "topology":
            m = re.match(r'^\s+name="([^"]*)"', line)
            if m and "title" not in info:
                info["title"] = m.group(1)
            m = re.match(r"^\s+#atoms\s*=\s*(\d+)", line)
            if m and "natoms" not in info:
                info["natoms"] = int(m.group(1))
            m = re.match(r"^\s+#molecules\s*=\s*(\d+)", line)
            if m:
                nmolecules += int(m.group(1))

    int_keys = {"nsteps": "nsteps", "nstlist": "nstlist", "nstenergy": "nstenergy",
                "nstlog": "nstlog", "nstxout-compressed": "nstxout_compressed"}
    float_keys = {"dt": "dt", "tinit": "tinit", "tau-p": "tau_p", "rcoulomb": "rcoulomb",
                  "rvdw": "rvdw"}
    str_keys = {"integrator": "integrator", "tcoupl": "tcoupl", "pcoupl": "pcoupl",
                "coulombtype": "coulombtype", "cutoff-scheme": "cutoff_scheme"}

    for src, dst in int_keys.items():
        if src in inputrec:
            v = _to_number(inputrec[src])
            if isinstance(v, int):
                info[dst] = v
    for src, dst in float_keys.items():
        if src in inputrec:
            v = _to_number(inputrec[src])
            if isinstance(v, (int, float)):
                info[dst] = float(v)
    for src, dst in str_keys.items():
        if src in inputrec:
            info[dst] = inputrec[src]

    if ref_t:
        info["ref_t"] = ref_t
        info["temperature"] = ref_t[0]
    if tau_t:
        info["tau_t"] = tau_t
    if ref_p_rows:
        info["pressure"] = ref_p_rows[0][0]
    if nmolecules:
        info["nmolecules"] = nmolecules
    return info


# ==============================================================================
# .xvg parsing and statistics
# ==============================================================================
def parse_xvg(text: str, max_points: Optional[int] = None) -> Dict[str, Any]:
    """
    Parse the text of an .xvg file.

    Returns title, axis labels, legends, number of rows/columns, data rows
    (downsampled by stride to at most max_points) and per column statistics.
    """
    title = xlabel = ylabel = ""
    legends: Dict[int, str] = {}
    rows: List[List[float]] = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if line.startswith("@"):
            m = re.match(r'@\s+title\s+"(.*)"', line)
            if m:
                title = m.group(1)
            m = re.match(r'@\s+xaxis\s+label\s+"(.*)"', line)
            if m:
                xlabel = m.group(1)
            m = re.match(r'@\s+yaxis\s+label\s+"(.*)"', line)
            if m:
                ylabel = m.group(1)
            m = re.match(r'@\s+s(\d+)\s+legend\s+"(.*)"', line)
            if m:
                legends[int(m.group(1))] = m.group(2)
            continue
        try:
            rows.append([float(x) for x in line.split()])
        except ValueError:
            continue

    ncols = max((len(r) for r in rows), default=0)
    stats = []
    for c in range(1, ncols):
        col = [r[c] for r in rows if len(r) > c]
        if not col:
            continue
        mean = statistics.fmean(col)
        stats.append({
            "column": c,
            "legend": legends.get(c - 1, ""),
            "mean": mean,
            "std": statistics.pstdev(col) if len(col) > 1 else 0.0,
            "min": min(col),
            "max": max(col),
        })

    data = rows
    downsampled = False
    if max_points and len(rows) > max_points > 0:
        stride = math.ceil(len(rows) / max_points)
        data = rows[::stride]
        downsampled = True

    return {
        "title": title,
        "xaxis": xlabel,
        "yaxis": ylabel,
        "legends": [legends[k] for k in sorted(legends)],
        "n_rows": len(rows),
        "n_columns": ncols,
        "downsampled": downsampled,
        "data": data,
        "stats": stats,
    }


def parse_msd_diffusion(text: str) -> Optional[Dict[str, Any]]:
    """
    Read the diffusion coefficient `gmx msd` stores in the .xvg legend, for example
    `D[   name OW] = 2.4942 (+/- 0.03) (1e-5 cm^2/s)`. Returns None when it is absent.
    """
    m = re.search(r'D\[.*?\]\s*=\s*([-+\d.eE]+)\s*\(\+/-\s*([-+\d.eE]+)\)\s*\(([^)]*)\)', text)
    if not m:
        return None
    return {"diffusion_coefficient": float(m.group(1)), "diffusion_error": float(m.group(2)),
            "unit": m.group(3)}


def block_average(values: List[float], nblocks: int = 5) -> Dict[str, float]:
    """Mean and standard error of the block means."""
    n = len(values)
    if n == 0:
        return {"mean": float("nan"), "sem": float("nan"), "nblocks": 0}
    nblocks = max(1, min(nblocks, n))
    size = n // nblocks
    means = [statistics.fmean(values[i * size:(i + 1) * size]) for i in range(nblocks)]
    sem = statistics.stdev(means) / math.sqrt(nblocks) if nblocks > 1 else 0.0
    return {"mean": statistics.fmean(values), "sem": sem, "nblocks": nblocks}


_MAX_CORR_POINTS = 20000
_MAX_CORR_OPS = 3_000_000
_EXACT_LAGS = 50


def _inefficiency(x: List[float], max_ops: Optional[int] = None) -> Tuple[Optional[float], bool]:
    """(g, finished). finished is False when max_ops multiply-adds were spent before C(t) reached zero."""
    n = len(x)
    if n < 10:
        return None, True
    mean = statistics.fmean(x)
    d = [v - mean for v in x]
    var = sum(v * v for v in d) / n
    if var <= 0:
        return None, True
    g, t, step, ops = 1.0, 1, 1, 0
    while t < n - 1:
        if max_ops is not None and ops > max_ops:
            return None, False
        c = sum(d[i] * d[i + t] for i in range(n - t)) / ((n - t) * var)
        ops += n - t
        if c <= 0:
            break
        g += 2.0 * c * (1.0 - t / n) * step
        t += step
        if t > _EXACT_LAGS:  # sum the first lags exactly, then take growing steps over the long tail
            step += 1
    return max(1.0, g), True


def statistical_inefficiency(values: List[float]) -> Optional[float]:
    """
    Statistical inefficiency g of a time series, in frames: about g consecutive frames carry one
    independent sample, so the standard error of the mean is std * sqrt(g / n).

    g = 1 + 2 * sum_t (1 - t/n) C(t), with the normalized autocorrelation C summed until it first
    drops to zero or below (Chodera et al., JCTC 2007). The first 50 lags are summed exactly, longer
    ones with a growing step as in pymbar's fast mode. A series longer than 20000 points whose
    correlation outlasts a fixed amount of work is thinned to 20000 points and g is scaled back to
    frames (accurate there, because such a series stays correlated across the thinning stride).
    Returns None for series shorter than 10 points or without variation.
    """
    if len(values) <= _MAX_CORR_POINTS:
        return _inefficiency(values)[0]
    g, finished = _inefficiency(values, max_ops=_MAX_CORR_OPS)
    if finished:
        return g
    stride = -(-len(values) // _MAX_CORR_POINTS)
    g_thin = _inefficiency(values[::stride])[0]
    return None if g_thin is None else g_thin * stride


def detect_equilibration(values: List[float], batch: int = 5) -> Optional[int]:
    """
    Return the index at which a time series looks equilibrated (MSER-5).

    Chooses the truncation point (within the first half) that minimizes the
    standard error of the mean of the remainder, using batch means. Returns None when
    the series is too short to tell (fewer than 4 batches), which is not the same as 0
    (equilibrated from the start).
    """
    n = len(values)
    if n < 4 * batch:
        return None
    nb = n // batch
    means = [statistics.fmean(values[i * batch:(i + 1) * batch]) for i in range(nb)]
    best_d, best_stat = 0, float("inf")
    for d in range(0, nb // 2 + 1):
        tail = means[d:]
        if len(tail) < 2:
            break
        m = statistics.fmean(tail)
        stat = sum((x - m) ** 2 for x in tail) / (len(tail) ** 2)
        if stat < best_stat:
            best_stat, best_d = stat, d
    return best_d * batch


# ==============================================================================
# gmx energy table and md.log parsing
# ==============================================================================
_ENERGY_ROW = re.compile(
    r"^(?P<name>\S.*?)\s+(?P<avg>[-+]?\d\S*)\s+(?P<err>\S+)\s+(?P<rmsd>\S+)\s+"
    r"(?P<drift>\S+)\s+\((?P<unit>[^)]*)\)\s*$"
)


def _float_or_none(value: str) -> Optional[float]:
    try:
        return float(value)
    except ValueError:
        return None


def parse_energy_table(text: str) -> Dict[str, Dict[str, Any]]:
    """
    Parse the average table printed by `gmx energy`.

    Returns {term: {average, err_est, rmsd, tot_drift, unit}}.
    """
    out: Dict[str, Dict[str, Any]] = {}
    in_table = False
    for line in text.splitlines():
        if re.match(r"^Energy\s+Average\s+Err\.Est\.", line):
            in_table = True
            continue
        if not in_table or line.startswith("---"):
            continue
        if not line.strip():
            if out:
                break
            continue
        m = _ENERGY_ROW.match(line)
        if not m:
            continue
        try:
            average = float(m.group("avg"))
        except ValueError:
            continue
        out[m.group("name").strip()] = {
            "average": average,
            "err_est": _float_or_none(m.group("err")),  # gmx prints "--" when the window is too short
            "rmsd": _float_or_none(m.group("rmsd")),
            "tot_drift": _float_or_none(m.group("drift")),
            "unit": m.group("unit"),
        }
    return out


_WARN_PATTERNS = (
    r"WARNING", r"can not be settled", r"Pressure scaling more than",
    r"Large VCM", r"Fatal error",
)


def parse_mdlog_progress(text: str, nsteps: Optional[int] = None) -> Dict[str, Any]:
    """
    Extract run progress from md.log text: last step and time, percent,
    performance (when finished) and warnings.

    `finished` means mdrun exited normally, which it also does after SIGTERM or -maxh.
    `complete` means the run did all its work: the last step reached nsteps (0 included), or a
    minimizer reported convergence.

    nsteps, when given, replaces the value in the log. Pass it from the .tpr (read_tpr_nsteps):
    a run continued with -append after extending does not print its parameters again, so the log
    keeps the nsteps of the first run.
    """
    progress: Dict[str, Any] = {"finished": bool(re.search(r"Finished mdrun", text))}

    steps = re.findall(r"^\s+Step\s+Time\s*\n\s+(\d+)\s+([-\d.eE+]+)", text, re.MULTILINE)
    if steps:
        step, time_ps = steps[-1]
        progress["step"] = int(step)
        progress["time_ps"] = float(time_ps)

    m = (re.search(r"Overriding nsteps with value passed on the command line:\s*(-?\d+)\s+steps", text)
         or re.search(r"^\s*nsteps\s*=\s*(-?\d+)", text, re.MULTILINE))
    if nsteps is not None or m:
        progress["nsteps"] = int(nsteps) if nsteps is not None else int(m.group(1))
        if progress["nsteps"] > 0 and "step" in progress:
            progress["percent"] = round(min(100.0, 100.0 * progress["step"] / progress["nsteps"]), 1)
    nsteps = progress.get("nsteps")
    # a minimizer that converged stops before nsteps by design; nsteps -1 (no limit) is never complete
    converged = bool(re.search(r"converged to (Fmax|machine precision)", text))
    progress["complete"] = bool(progress["finished"] and (
        converged or (nsteps is not None and nsteps >= 0 and progress.get("step", -1) >= nsteps)))
    if progress["complete"]:
        progress["percent"] = 100.0

    perf = re.search(r"^Performance:\s+([\d.]+)\s+([\d.]+)", text, re.MULTILINE)
    if perf:
        progress["ns_per_day"] = float(perf.group(1))
        progress["hours_per_ns"] = float(perf.group(2))

    warnings = []
    for line in text.splitlines():
        if any(re.search(p, line) for p in _WARN_PATTERNS):
            warnings.append(line.strip())
    seen = []
    for w in warnings:
        if w not in seen:
            seen.append(w)
    if seen:
        progress["warnings"] = seen[:20]
    return progress


_ERROR_MARKERS = ("Fatal error:", "Error in user input:", "Invalid command-line options",
                  "Software inconsistency error:", "Program:", "Cannot match")


_ERROR_HINTS = (
    (r"reading tpx file .* version (\d+) with version (\d+)",
     "Hint: this .tpr was written by a newer GROMACS than the one running it. Re-run grompp with "
     "this GROMACS, or point GMX_MCP_GMX at the newer gmx."),
    (r"There is no domain decomposition",
     "Hint: the box is too small for this many MPI ranks; run with ntmpi=1."),
    (r"Too many warnings",
     "Hint: read the warnings above; fix them, or pass maxwarn only if you understand each one."),
    (r"No such moleculetype",
     "Hint: the topology lacks an #include for this molecule (for ions: <forcefield>.ff/ions.itp)."),
    (r"Atom .* in residue .* was not found in rtp entry|Residue .* not found in residue topology database",
     "Hint: pdb2gmx does not know this residue; check the force field or build a ligand topology separately."),
)


def _with_hint(message: str, full_text: str) -> str:
    for pattern, hint in _ERROR_HINTS:
        if re.search(pattern, full_text):
            return f"{message}\n{hint}" if message else hint
    return message


def summarize_gmx_error(text: str, max_lines: int = 40) -> str:
    """Error block from GROMACS output plus a hint for well known problems."""
    return _with_hint(_summarize_gmx_error(text, max_lines), text or "")


def _summarize_gmx_error(text: str, max_lines: int = 40) -> str:
    """
    Pull the meaningful error block out of GROMACS output.

    GROMACS wraps errors in a banner and ends with a "For more information"
    pointer. Return the text from the first error marker up to that pointer,
    or the last `max_lines` lines if no marker is present.
    """
    lines = (text or "").strip().splitlines()
    if not lines:
        return ""
    # grompp reports the specific problems as "ERROR n [file ...]:" blocks before a generic summary
    blocks = []
    for i, line in enumerate(lines):
        if re.match(r"^ERROR \d+ \[", line):
            block = [line]
            for nxt in lines[i + 1:]:
                if not nxt.strip():
                    break
                block.append(nxt)
            blocks.append("\n".join(block))
    if blocks:
        return "\n".join(blocks[:5])[:3000]
    if any("Too many warnings" in line for line in lines):
        warns = []
        for i, line in enumerate(lines):
            if re.match(r"^WARNING \d+ \[", line):
                block = [line]
                for nxt in lines[i + 1:]:
                    if not nxt.strip():
                        break
                    block.append(nxt)
                warns.append("\n".join(block))
        if warns:
            return ("grompp stopped on warnings:\n" + "\n".join(warns[:5]))[:3000]
    start = None
    for i, line in enumerate(lines):
        if any(line.strip().startswith(m) for m in ("Fatal error:", "Error in user input:",
                                                    "Software inconsistency error:",
                                                    "Inconsistency in user input:")):
            start = i
            break
    if start is None:
        for i, line in enumerate(lines):
            if line.strip().startswith("Program:"):
                start = i
                break
    if start is None:
        return "\n".join(lines[-max_lines:])
    block = []
    for line in lines[start:]:
        if line.startswith("For more information and tips for troubleshooting"):
            break
        block.append(line)
    return "\n".join(block[:max_lines]).strip()


def read_tpr_nsteps(tpr: PathLike, timeout: float = 60) -> Optional[int]:
    """
    nsteps stored in a .tpr. gmx dump prints it among the first lines (before the topology), so
    the dump is read only that far and the cost does not grow with the system. None when the file
    cannot be read.
    """
    try:
        cmd = find_gromacs_command() + ["dump", "-s", str(tpr)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, text=True)
    except (GromacsNotFoundError, OSError):
        return None
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    try:
        for line in proc.stdout:
            m = re.match(r"^\s+nsteps\s*=\s*(-?\d+)", line)
            if m:
                return int(m.group(1))
            if line.startswith("topology:"):
                break
        return None
    finally:
        timer.cancel()
        proc.kill()
        proc.wait()


def tail_lines(path: PathLike, n: int, block: int = 8192) -> List[str]:
    """Read the last n lines of a file without loading all of it."""
    p = Path(path)
    if n <= 0:
        return p.read_text(errors="replace").splitlines(keepends=True)
    with open(p, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        data = b""
        pos = end
        while pos > 0 and data.count(b"\n") <= n:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    lines = data.decode(errors="replace").splitlines(keepends=True)
    return lines[-n:]


# ==============================================================================
# More parsers: gmx energy menu, gmx check, grompp messages, force fields
# ==============================================================================
def parse_energy_menu(text: str) -> Dict[int, str]:
    """
    Parse the term menu `gmx energy` prints when it asks for a selection.

    Each entry is printed as "%3d  name" (index, two spaces, name) in padded columns. Names may
    contain single spaces, and a name that fills its column leaves a single space (or none)
    before a three digit index, so columns are not split on whitespace: the indices are looked
    up in order (1, 2, 3, ...) and each name runs to the start of the next index.
    Returns {number: name}.
    """
    terms: Dict[int, str] = {}
    in_menu = False
    expected: Optional[int] = None
    for line in text.splitlines():
        if line.startswith("---"):
            in_menu = True
            continue
        if not in_menu:
            continue
        if not line.strip():
            if terms:
                break
            continue
        if expected is None:
            first = re.match(r"\s*(\d+)  \S", line)
            if not first:
                continue
            expected = int(first.group(1))
        found: List[Tuple[int, int, int]] = []  # (index, where the number starts, where the name starts)
        pos = 0
        while True:
            m = (re.compile(rf"(?<!\d){expected}  (?=\S)").search(line, pos)
                 or re.compile(rf"{expected}  (?=\S)").search(line, pos))  # glued to a name ending in a digit
            if not m:
                break
            found.append((expected, m.start(), m.end()))
            pos = m.end()
            expected += 1
        if not found:  # a layout this parser does not know: fall back to two space columns
            for m in re.finditer(r"(\d+)\s+(.+?)(?=\s{2,}\d+\s|\s*$)", line):
                terms[int(m.group(1))] = m.group(2).strip()
            if terms:
                expected = max(terms) + 1
            continue
        for i, (k, _, name_start) in enumerate(found):
            name_end = found[i + 1][1] if i + 1 < len(found) else len(line)
            terms[k] = line[name_start:name_end].strip()
    return terms


def parse_check_output(text: str) -> Dict[str, Any]:
    """Parse `gmx check -f traj` or `gmx check -e file.edr` output (it is written to stderr)."""
    info: Dict[str, Any] = {}
    m = re.search(r"#\s*Atoms\s+(\d+)", text)
    if m:
        info["natoms"] = int(m.group(1))
    m = re.search(r"Precision\s+([\d.]+)\s+\(nm\)", text)
    if m:
        info["precision_nm"] = float(m.group(1))
    m = re.search(r"Last frame\s+(\d+)\s+time\s+([-\d.]+)", text)
    if m:
        info["last_frame_index"] = int(m.group(1))
        info["last_time_ps"] = float(m.group(2))
    m = re.search(r"^Coords\s+(\d+)\s+([-\d.eE+]+)", text, re.MULTILINE)
    if m:
        info["n_frames"] = int(m.group(1))
        info["timestep_ps"] = float(m.group(2))
    m = re.search(r"^Box\s+(\d+)\s+", text, re.MULTILINE)
    if m:
        info["n_box_frames"] = int(m.group(1))
    m = re.search(r"Found\s+(\d+)\s+frames with a timestep of\s+([-\d.eE+]+)\s+ps", text)
    if m:
        info["n_frames"] = int(m.group(1))
        info["timestep_ps"] = float(m.group(2))
    m = re.search(r"Last energy frame read\s+(\d+)\s+time\s+([-\d.]+)", text)
    if m:
        info["last_frame_index"] = int(m.group(1))
        info["last_time_ps"] = float(m.group(2))
    m = re.search(r"(\d+)\s+groups in energy file", text)
    if m:
        info["n_energy_terms"] = int(m.group(1))
    return info


def parse_grompp_messages(text: str) -> Dict[str, List[str]]:
    """
    Collect NOTE, WARNING and ERROR blocks from grompp output.

    Each block starts with `WARNING 1 [file topol.top, line 5]:` followed by indented
    text until a blank line. Returns {errors, warnings, notes} as lists of strings.
    """
    out: Dict[str, List[str]] = {"errors": [], "warnings": [], "notes": []}
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(NOTE|WARNING|ERROR)\s+(\d+)\s+\[(.*?)\]:?", line)
        if not m:
            continue
        body = []
        for nxt in lines[i + 1:]:
            if not nxt.strip():
                break
            body.append(nxt.strip())
        entry = f"[{m.group(3)}] " + " ".join(body)
        out[{"NOTE": "notes", "WARNING": "warnings", "ERROR": "errors"}[m.group(1)]].append(entry[:600])
    return out


def get_gmx_top_dir() -> Optional[Path]:
    """Directory with the force field folders (share/gromacs/top), from GMXLIB or the data prefix."""
    gmxlib = os.environ.get("GMXLIB")
    if gmxlib and Path(gmxlib).is_dir():
        return Path(gmxlib)
    info = get_gmx_version()
    prefix = None
    # `Data prefix:` is printed by gmx --version; re-read it
    try:
        res = run_command_full(find_gromacs_command() + ["--version"], timeout=30)
        m = re.search(r"Data prefix:\s*(.+)", res["stdout"] + res["stderr"])
        if m:
            prefix = m.group(1).strip()
    except GromacsNotFoundError:
        return None
    if not prefix:
        return None
    for candidate in (Path(prefix) / "share" / "gromacs" / "top", Path(prefix) / "share" / "top"):
        if candidate.is_dir():
            return candidate
    return None


def list_force_fields() -> List[Dict[str, Any]]:
    """Force fields installed with GROMACS, with description and water models."""
    top = get_gmx_top_dir()
    if top is None:
        return []
    fields = []
    for ff in sorted(top.glob("*.ff")):
        desc = ""
        doc = ff / "forcefield.doc"
        if doc.exists():
            desc = doc.read_text(errors="replace").strip().splitlines()[0] if doc.read_text(errors="replace").strip() else ""
        waters = []
        wm = ff / "watermodels.dat"
        if wm.exists():
            for line in wm.read_text(errors="replace").splitlines():
                parts = line.split(None, 2)
                if len(parts) >= 2 and not line.startswith(";"):
                    waters.append(parts[0])
        fields.append({"name": ff.name[:-3], "directory": ff.name, "description": desc, "water_models": waters})
    return fields


# ==============================================================================
# Sandbox: files grompp reads through #include and mdp include = -I...
# ==============================================================================
_INCLUDE_LINE_RE = re.compile(r'^\s*#\s*include\b(.*)$', re.MULTILINE)
_INCLUDE_PATH_RE = re.compile(r'^\s*["<]([^">]+)[">]')


def _include_names(text: str, source: str) -> List[str]:
    """File names of the #include lines. A line whose name cannot be read the way gmxcpp reads it
    ("name" or <name>) is refused rather than skipped: another preprocessor version might accept it."""
    names = []
    for rest in _INCLUDE_LINE_RE.findall(text):
        m = _INCLUDE_PATH_RE.match(rest)
        if not m:
            raise PathNotAllowedError(f"{source}: cannot verify the include '#include{rest.rstrip()}'; "
                                      'write it as #include "file.itp"')
        names.append(m.group(1))
    return names


def _inside(path: Path, roots: List[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


def check_grompp_includes(topology: Optional[PathLike] = None, mdp: Optional[PathLike] = None,
                          mdp_text: Optional[str] = None, cwd: Optional[PathLike] = None,
                          max_depth: int = 12) -> None:
    """
    With GMX_MCP_WORKDIR set, make sure grompp cannot read files outside the sandbox through
    `include = -I<dir>` in the .mdp or `#include "..."` in the topology and the files it includes.
    Does nothing without a sandbox.

    Relative -I directories are resolved against `cwd`, the folder grompp runs in (that is what
    grompp does). For each #include every location the preprocessor might use (the including
    file's folder, cwd, the -I folders, the force field folder) is checked, so the order in which
    it searches does not matter. An include found nowhere is refused, because it could not be
    verified. Allowed: the sandbox and the GROMACS force field directory.

    Raises:
        PathNotAllowedError naming the offending include.
    """
    root = get_workdir_root()
    if root is None:
        return
    top_dir = get_gmx_top_dir()
    allowed = [root] + ([top_dir.resolve()] if top_dir else [])
    base = Path(cwd).resolve() if cwd else root

    include_dirs: List[Path] = []
    text = mdp_text if mdp_text is not None else (
        Path(mdp).read_text(errors="replace") if mdp and Path(mdp).is_file() else "")
    for raw in text.splitlines():
        line = raw.split(";", 1)[0]
        if "=" not in line:
            continue
        key, value = (x.strip() for x in line.split("=", 1))
        if key.replace("_", "").replace("-", "").lower() not in ("include", "define"):
            continue
        tokens = value.split()
        for i, tok in enumerate(tokens):
            if tok.startswith("-I"):
                target = tok[2:] or (tokens[i + 1] if i + 1 < len(tokens) else "")
                p = Path(target).expanduser()
                p = (p if p.is_absolute() else base / p).resolve()
                if not _inside(p, allowed):
                    raise PathNotAllowedError(f"mdp include directory {target} is outside the allowed directory {root}")
                include_dirs.append(p)

    if not topology:
        return
    stack = [(Path(topology).resolve(), 0)]
    seen = set()
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            raise PathNotAllowedError(f"Includes nested deeper than {max_depth} levels at {current.name}; "
                                      "cannot verify them")
        if current in seen or not current.is_file():
            continue
        seen.add(current)
        for name in _include_names(current.read_text(errors="replace"), current.name):
            p = Path(name).expanduser()
            if p.is_absolute():
                candidates = [p]
            else:
                candidates = [current.parent / p, base / p] + [d / p for d in include_dirs] + (
                    [top_dir / p] if top_dir else [])
            existing = []
            for c in candidates:
                r = c.resolve()
                if r.exists() and r not in existing:
                    existing.append(r)
            if not existing:
                raise PathNotAllowedError(
                    f"{current.name} includes {name}, which could not be located to verify it is inside "
                    f"the allowed directory {root}; put the file in the sandbox")
            for found in existing:
                if not _inside(found, allowed):
                    raise PathNotAllowedError(f"{current.name} includes {name}, outside the allowed directory {root}")
                stack.append((found, depth + 1))  # follow every candidate: any of them could be the one read
