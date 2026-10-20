"""Environment diagnostics and command help."""

import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

from gromacs_command import get_gmx_help
from lib.gromacs_utils import (
    GromacsNotFoundError,
    default_thread_count,
    find_gromacs_command,
    free_disk_mb,
    get_gmx_version,
    get_workdir_root,
    list_force_fields,
    run_command_full,
)
from utils import format_tool_result, get_example_data_path, get_home, get_results_dir


def _module_version(name: str) -> Any:
    try:
        module = __import__(name)
        return getattr(module, "__version__", "installed")
    except ImportError:
        return None


def check_environment() -> dict:
    """
    Diagnose the setup in one call: GROMACS binary and build, CPU and GPU, disk space,
    Python and optional packages, folders, limits, and example data.

    Run this first when a tool fails. Returns ok (false when something blocks simulations),
    problems (what to fix), and the details behind each check.
    """
    from jobs.manager import job_manager

    problems: List[str] = []
    notes: List[str] = []
    data: Dict[str, Any] = {}

    try:
        gmx = find_gromacs_command()
        info = get_gmx_version()
        data["gromacs"] = {"command": gmx, **info}
        probe = run_command_full(gmx + ["help", "commands"], timeout=60)
        data["gromacs"]["runs"] = probe["success"]
        if not probe["success"]:
            problems.append("gmx was found but `gmx help commands` failed: " + probe["stderr"][-200:])
        mpi = info.get("mpi", "")
        data["gromacs"]["mpi_mode"] = "thread-MPI" if "thread" in mpi.lower() else ("MPI" if mpi and "disabled" not in mpi.lower() else "none")
        gpu = info.get("gpu", "")
        data["gromacs"]["gpu_build"] = gpu or "unknown"
    except GromacsNotFoundError as e:
        problems.append(str(e))
        data["gromacs"] = {"error": str(e)}

    cpu = os.cpu_count() or 1
    gpus = [t for t in ("nvidia-smi", "rocm-smi") if shutil.which(t)]
    data["hardware"] = {
        "cpu_count": cpu,
        "default_omp_threads": default_thread_count(),
        "platform": platform.platform(),
        "gpu_tools_found": gpus,
    }
    gpu_build = str(data.get("gromacs", {}).get("gpu_build", "")).lower()
    if gpus and gpu_build and ("disabled" in gpu_build or gpu_build == "none"):
        notes.append("A GPU tool is present but this GROMACS build has no GPU support")

    data["disk"] = {
        "jobs_dir": str(job_manager.jobs_dir),
        "jobs_free_mb": round(free_disk_mb(job_manager.jobs_dir)),
        "results_dir": str(get_results_dir()),
        "results_free_mb": round(free_disk_mb(get_results_dir())),
    }
    if data["disk"]["jobs_free_mb"] < job_manager.min_free_mb:
        problems.append(f"Only {data['disk']['jobs_free_mb']} MB free where jobs are stored "
                        f"(minimum {job_manager.min_free_mb:.0f})")

    data["python"] = {
        "version": sys.version.split()[0],
        "executable": sys.executable,
        "fastmcp": _module_version("fastmcp"),
        "loguru": _module_version("loguru"),
        "matplotlib": _module_version("matplotlib"),
        "numpy": _module_version("numpy"),
    }
    if data["python"]["matplotlib"] is None:
        notes.append("matplotlib is not installed: plot_xvg is unavailable (pip install matplotlib)")

    data["optional_tools"] = {
        "plumed": bool(shutil.which("plumed") or os.environ.get("PLUMED_KERNEL")),
        "dssp": bool(shutil.which("dssp") or shutil.which("mkdssp")),
        "mamba": bool(shutil.which("mamba")),
    }
    root = get_workdir_root()
    if root is not None:
        for label, folder in (("GMX_MCP_HOME", get_home()), ("results", get_results_dir()),
                              ("job outputs (runs)", job_manager.runs_dir), ("job records", job_manager.jobs_dir)):
            resolved = Path(folder).expanduser().resolve()
            if resolved != root and root not in resolved.parents:
                problems.append(f"{label} folder {resolved} is outside GMX_MCP_WORKDIR {root}: tools and jobs "
                                "that use default outputs will be refused. Unset GMX_MCP_HOME or point it inside the sandbox")
    data["configuration"] = {
        "home": str(get_home()),
        "workdir_restriction": str(get_workdir_root()) if get_workdir_root() else None,
        "max_concurrent_jobs": job_manager.max_concurrent,
        "gmx_override": os.environ.get("GMX_MCP_GMX"),
    }
    running = job_manager.list_jobs("running")["total"]
    queued = job_manager.list_jobs("queued")["total"]
    data["jobs"] = {"running": running, "queued": queued}

    ff = list_force_fields()
    data["force_fields_installed"] = len(ff)
    if not ff:
        problems.append("No force field directory found (needed by pdb2gmx and topologies)")

    example = get_example_data_path()
    data["example_data"] = {"path": str(example), "present": (example / "sample.tpr").exists()}
    if not data["example_data"]["present"]:
        notes.append("Example data missing: run `python scripts/make_example_data.py`")

    return format_tool_result(not problems, data={**data, "ok": not problems, "problems": problems},
                              warnings=notes, error_message="Problems found: " + "; ".join(problems))


def get_command_help(command_name: str) -> dict:
    """
    Show the real GROMACS help text for a command, so valid flags can be looked up.

    Args:
        command_name: for example 'rdf', 'grompp', 'energy', 'trjconv', 'mdrun'
    """
    res = get_gmx_help(command_name)
    if res.get("result"):
        return format_tool_result(True, data={"command_name": command_name, "help": res["help"]},
                                  command=res["command"])
    return format_tool_result(False, error_message=res.get("error"))


def register(mcp, registry) -> None:
    registry.tool("info")(check_environment)
    registry.tool("info")(get_command_help)
