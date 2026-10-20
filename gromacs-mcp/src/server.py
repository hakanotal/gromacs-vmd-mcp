#!/usr/bin/env python3
"""MCP Server for GROMACS

Synchronous tools (fast operations), submit tools (background jobs with tracking),
job management, and information tools. Every tool returns the flat result envelope
described in utils.format_tool_result: status, error, warnings, output_files, command.
"""

import os
import sys
from pathlib import Path
from typing import List, Optional

from fastmcp import FastMCP

# Setup paths
SCRIPT_DIR = Path(__file__).parent.resolve()
MCP_ROOT = SCRIPT_DIR.parent
SCRIPTS_DIR = MCP_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

from loguru import logger  # noqa: E402

from utils import (  # noqa: E402
    configure_logging,
    format_tool_result,
    get_example_data_path,
    setup_output_directory,
    validate_input_file,
)
from lib.gromacs_utils import (  # noqa: E402
    PathNotAllowedError,
    get_gmx_version,
    get_workdir_root,
    resolve_in_workdir,
    unique_name,
)

configure_logging()

from jobs.manager import job_manager  # noqa: E402
from tools.registry import ToolRegistry  # noqa: E402

SERVER_NAME = "gromacs"
SERVER_VERSION = "2.1.0"

mcp = FastMCP(SERVER_NAME)
registry = ToolRegistry(mcp)


def _from_script(result: dict) -> dict:
    """Wrap a script result dict (which carries a boolean `result`) in the envelope."""
    return format_tool_result(bool(result.get("result", False)), data=result)


def _check_path(path: str) -> Optional[str]:
    """Return an error string if the path is outside GMX_MCP_WORKDIR, else None."""
    try:
        resolve_in_workdir(path)
    except PathNotAllowedError as e:
        return str(e)
    return None


# ==============================================================================
# Job Management Tools (for async operations)
# ==============================================================================

@registry.tool("jobs")
def get_job_status(job_id: str) -> dict:
    """
    Get the status of a submitted job.

    Args:
        job_id: The job ID returned from a submit_* function

    Returns:
        job_status (queued, running, completed, stopped, failed, cancelled), timestamps, duration,
        output_dir, job_error (why it failed), stop_reason (for stopped: mdrun ended cleanly
        before its last step because of maxh or the time limit; resume_job continues it),
        queue_position (when queued) and, for MD jobs
        that have started, progress: step, time_ps, nsteps, percent, eta_seconds,
        ns_per_day (when finished) and warnings parsed from md.log.
    """
    res = job_manager.get_job_status(job_id)
    if res.get("status") == "error" and "job_id" not in res:
        return format_tool_result(False, error_message=res.get("error"))
    job_status = res.pop("status")
    job_error = res.pop("error", None)  # the envelope reserves `error` for failures of this call itself
    data = {**res, "job_status": job_status}
    if job_error:
        data["job_error"] = job_error
    return format_tool_result(True, data=data)


@registry.tool("jobs")
def get_job_result(job_id: str) -> dict:
    """
    Get the results of a completed or stopped job.

    Args:
        job_id: The job ID of a completed or stopped job

    Returns:
        result: the script's result dictionary (output files, warnings, metadata).
        For a failed job the error is returned together with any partial result.
    """
    res = job_manager.get_job_result(job_id)
    if res.get("status") == "success":
        job_result = res["result"]
        # records written before 2.1 hold "name (N bytes)" strings in output_files and the paths in output_paths
        files = job_result.get("output_paths") or job_result.get("output_files")
        return format_tool_result(True, data={"job_id": job_id, "job_result": job_result},
                                  output_files=files, warnings=job_result.get("warnings"))
    extra = {"job_result": res["result"]} if "result" in res else {}
    return format_tool_result(False, data={"job_id": job_id, **extra}, error_message=res.get("error"))


@registry.tool("jobs")
def get_job_log(job_id: str, tail: int = 50) -> dict:
    """
    Get log output from a running or completed job.

    Args:
        job_id: The job ID to get logs for
        tail: Number of lines from the end (default 50, 0 for all)

    Returns:
        log_lines (the job's stdout and stderr), md_log_lines (the GROMACS md.log, when
        it exists) and total_lines.
    """
    res = job_manager.get_job_log(job_id, tail)
    if res.get("status") != "success":
        return format_tool_result(False, error_message=res.get("error"))
    res.pop("status")
    return format_tool_result(True, data=res)


@registry.tool("jobs")
def cancel_job(job_id: str) -> dict:
    """
    Cancel a queued or running job.

    Running jobs receive SIGTERM so mdrun writes a checkpoint and can be resumed with
    resume_job; a process that has not exited after 30 seconds is killed.
    """
    res = job_manager.cancel_job(job_id)
    if res.get("status") == "success":
        return format_tool_result(True, data={"message": res["message"], "job_id": job_id})
    return format_tool_result(False, error_message=res.get("error"))


@registry.tool("jobs")
def list_jobs(status: Optional[str] = None) -> dict:
    """
    List submitted jobs, newest first.

    Args:
        status: Filter by status (queued, running, completed, stopped, failed, cancelled)

    Returns:
        jobs: list of {job_id, job_name, kind, status, submitted_at, duration_seconds, output_dir}
    """
    res = job_manager.list_jobs(status)
    return format_tool_result(True, data={"jobs": res["jobs"], "total": res["total"]})


@registry.tool("jobs")
def resume_job(job_id: str) -> dict:
    """
    Continue a stopped, cancelled or failed MD job from its checkpoint (md.cpt).

    Submits a new job in the same output directory. To make a run longer, create a longer
    .tpr with extend_simulation first and pass resume=True to submit_md_simulation.
    """
    res = job_manager.resume_job(job_id)
    if res.get("status") == "submitted":
        return format_tool_result(True, data={k: v for k, v in res.items() if k != "status"})
    return format_tool_result(False, error_message=res.get("error"))


@registry.tool("jobs")
def delete_job(job_id: str) -> dict:
    """Delete a finished job's record (metadata and log). Simulation output files are kept."""
    res = job_manager.delete_job(job_id)
    if res.get("status") == "success":
        return format_tool_result(True, data={"message": res["message"]})
    return format_tool_result(False, error_message=res.get("error"))


@registry.tool("jobs")
def clean_jobs(older_than_days: Optional[float] = 7, keep_last: Optional[int] = None) -> dict:
    """
    Delete records of finished jobs older than N days.

    Args:
        older_than_days: Age threshold in days (None removes regardless of age)
        keep_last: Always keep the newest N finished jobs
    """
    res = job_manager.clean_jobs(older_than_days=older_than_days, keep_last=keep_last)
    return format_tool_result(True, data={"removed": res["removed"], "total_removed": res["total_removed"]})


# ==============================================================================
# Synchronous Tools (fast operations)
# ==============================================================================

@registry.tool("sync")
def analyze_tpr(
    input_file: str,
    output_format: str = "text",
    output_file: Optional[str] = None,
    include_raw: bool = False,
) -> dict:
    """
    Summarize a GROMACS TPR file: integrator, nsteps, dt, thermostat and barostat,
    reference temperature and pressure, atom and molecule counts.

    Args:
        input_file: Path to the .tpr file
        output_format: 'text' or 'json' (format of the saved file, when output_file is given)
        output_file: Optional path to save the summary
        include_raw: Save the full `gmx dump` text to a file and return its path as
            raw_dump_file (the dump itself is never embedded: it is hundreds of KB)

    Returns:
        tpr_info (parsed values), analysis_text, output_file, raw_dump_file
    """
    from tpr_analysis import run_tpr_analysis

    try:
        validation = validate_input_file(input_file, ".tpr")
        if not validation["valid"]:
            return format_tool_result(False, error_message=validation["error"])

        output_setup = setup_output_directory(output_file, "tpr_analysis")
        if not output_setup["success"]:
            return format_tool_result(False, error_message=output_setup["error"])

        kwargs = {"format": output_format}
        if include_raw:
            kwargs["raw_dump_path"] = str(Path(output_setup["output_dir"]) / unique_name(Path(input_file).stem, ".dump.txt"))

        result = run_tpr_analysis(
            input_file=input_file,
            output_file=output_setup.get("output_file"),
            **kwargs,
        )
        files = [result.get(k) for k in ("output_file", "raw_dump_file") if result.get(k)]
        return format_tool_result(bool(result.get("result")), data=result, output_files=files)

    except FileNotFoundError as e:
        return format_tool_result(False, error_message=f"File not found: {e}")
    except Exception as e:
        logger.error(f"TPR analysis failed: {e}")
        return format_tool_result(False, error_message=str(e))


@registry.tool("sync")
def run_gromacs_command(
    command_name: str,
    input_files: Optional[dict] = None,
    output_files: Optional[dict] = None,
    parameters: Optional[List[str]] = None,
    selections: Optional[List[str]] = None,
    timeout: int = 300,
    working_directory: Optional[str] = None,
) -> dict:
    """
    Run a GROMACS tool. Only whitelisted commands are accepted (see list_available_commands);
    commands run without a shell, so file names and parameters are passed literally.

    Args:
        command_name: gmx subcommand, for example 'energy', 'rdf', 'grompp', 'check'
        input_files: {option: path}; the option is written without the dash,
            for example {'f': 'traj.xtc', 's': 'run.tpr', 'e': 'run.edr'}. A list gives several
            files to one option, for example {'f': ['a.xtc', 'b.xtc']} for trjcat. Every file
            option of the tool goes here or in output_files; in parameters a file option only
            takes a plain name such as spc216.gro (a GROMACS library file)
        output_files: {option: path}, for example {'o': 'energy.xvg'}. For 'dump' there is
            no -o option: the first path receives the text printed by gmx dump
        parameters: extra arguments, one list element per argument, for example
            ['-b', '25', '-ref', 'name OW', '-sel', 'name OW']
        selections: answers to interactive prompts, in order. For 'energy' these are the
            term names, for example ['Potential', 'Temperature', 'Pressure', 'Density'].
            For 'rms' they are the fit group then the output group (default ['0','0']).
            Commands that accept GROMACS selection syntax (rdf, msd, select, distance) are
            better driven with parameters such as -sel 'name OW' than with selections
        timeout: seconds before the command is stopped
        working_directory: where to run; default is the folder of the first output (or input)
            file, so stray files such as backups stay with your data

    Returns:
        stdout and stderr (separately), the exact command, returncode, output_files (absolute
        paths), file_sizes, warnings. On success stderr is shortened to what matters (banner,
        per frame progress lines and an answered term menu are removed) and a 'dump' saved to
        a file returns only its first 40 lines in stdout. On failure, error holds the GROMACS
        error text and stdout/stderr are complete. If 'energy' is called without selections
        the error lists the available terms.

    Example:
        run_gromacs_command('energy', {'f': 'npt.edr'}, {'o': 'e.xvg'},
                            selections=['Potential', 'Density'])
    """
    from gromacs_command import compact_for_client, run_gromacs_command as _run

    try:
        result = _run(
            command_name=command_name,
            input_files=input_files or {},
            output_files=output_files or {},
            parameters=parameters or [],
            selections=selections,
            timeout=timeout,
            working_directory=working_directory,
        )
        return _from_script(compact_for_client(result, command_name, selections_given=selections is not None))
    except Exception as e:
        logger.error(f"GROMACS command failed: {e}")
        return format_tool_result(False, error_message=str(e))


@registry.tool("sync")
def run_gromacs_workflow(
    workflow_type: str,
    input_files: Optional[dict] = None,
    output_file: Optional[str] = None,
    verbose: bool = False,
    options: Optional[dict] = None,
) -> dict:
    """
    Run a predefined multi-step workflow.

    Workflow types: 'full_md' (structure to solvated, minimized, equilibrated system and a
    production tpr), 'simple_md', 'analysis', 'preprocessing', 'demo'. Each step is logged
    and a failed run can be resumed: finished steps are skipped when the same output
    directory is reused.

    Args:
        workflow_type: One of the types above
        input_files: Inputs by role, for example {'structure': 'protein.pdb'} for full_md
        output_file: Output directory; reuse it to resume (default: a new folder under results/workflows)
        verbose: Include per step stdout in the result
        options: Workflow options, for example {"nvt_ps": 50, "solvate": false, "stages": ["em", "nvt"]}.
            Common ones: force_field, water_model, box_distance, set_box, ions, em_steps, nvt_ps,
            npt_ps, production_ps, temperature, pressure, maxwarn, threads, resume,
            nsteps (simple_md), group and energy_terms (analysis). For full_md,
            run_full_md_pipeline has the same options with documented parameters.
    """
    from gromacs_workflow import run_gromacs_workflow as _run
    from utils import get_results_dir

    try:
        for role, path in (input_files or {}).items():
            err = _check_path(str(path))
            if err:
                return format_tool_result(False, error_message=f"{role}: {err}")
        if output_file:
            try:
                output_file = str(resolve_in_workdir(output_file))
            except PathNotAllowedError as e:
                return format_tool_result(False, error_message=str(e))
        else:
            from datetime import datetime
            output_file = str(get_results_dir() / "workflows" /
                              f"{workflow_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        result = _run(
            workflow_type=workflow_type,
            input_files=input_files,
            output_file=output_file,
            verbose=verbose,
            **(options or {}),
        )
        return _from_script(result)
    except Exception as e:
        logger.error(f"GROMACS workflow failed: {e}")
        return format_tool_result(False, error_message=str(e))


# ==============================================================================
# Submit Tools (long-running operations)
# ==============================================================================

@registry.tool("submit")
def submit_md_simulation(
    input_file: str,
    nsteps: Optional[int] = None,
    output_dir: Optional[str] = None,
    job_name: Optional[str] = None,
    ntmpi: Optional[int] = None,
    ntomp: Optional[int] = None,
    gpu: bool = False,
    maxh: Optional[float] = None,
    max_runtime_seconds: Optional[int] = None,
    checkpoint_minutes: Optional[int] = None,
    pin: Optional[str] = None,
    resume: bool = False,
    multidir: Optional[List[str]] = None,
    replex: Optional[int] = None,
) -> dict:
    """
    Run mdrun on a .tpr file as a background job. Returns a job_id immediately.

    Outputs go to output_dir as md.log, md.edr, md.gro, md.cpt and md.xtc/md.trr when the
    .mdp writes them. Track with get_job_status (progress, ETA) and get_job_log.

    Args:
        input_file: Path to the .tpr (from grompp)
        nsteps: Override the step count stored in the .tpr (default: use the .tpr value)
        output_dir: Directory for outputs (default: a run folder inside the job directory)
        job_name: Label for the job
        ntmpi: Thread-MPI ranks (default 1: works for any box size; more ranks need a large box)
        ntomp: OpenMP threads per rank (default: CPU count divided by GMX_MCP_MAX_JOBS, at most 8)
        gpu: Offload nonbonded, PME, bonded and update to the GPU (needs a GPU build)
        maxh: Stop cleanly after this many hours and write a checkpoint (the job ends as 'stopped')
        max_runtime_seconds: Wall clock limit (default none); mdrun is asked to stop and checkpoint,
            and the job ends as 'stopped'
        checkpoint_minutes: Checkpoint interval (default 15)
        pin: Thread pinning: auto, on or off
        resume: Continue from md.cpt in output_dir with input_file (use a longer .tpr from
            extend_simulation to extend a finished run)
        multidir: Directories for a multi-simulation (each with its own md.tpr). Unverified here
        replex: Replica exchange interval in steps, with multidir. Unverified here

    Returns:
        job_id, job_status (queued or running), output_dir
    """
    validation = validate_input_file(input_file, ".tpr")
    if not validation["valid"]:
        return format_tool_result(False, error_message=validation["error"])
    if output_dir:
        try:
            output_dir = str(resolve_in_workdir(output_dir))  # absolute: the job runs from another folder
        except PathNotAllowedError as e:
            return format_tool_result(False, error_message=str(e))
    if resume:
        if not output_dir:
            return format_tool_result(False, error_message="resume requires output_dir (the folder holding md.cpt)")
        if not (Path(output_dir) / "md.cpt").exists():
            return format_tool_result(False, error_message=f"md.cpt not found in {output_dir}")
    if pin and pin not in ("auto", "on", "off"):
        return format_tool_result(False, error_message="pin must be auto, on or off")
    if multidir:
        if not output_dir:
            return format_tool_result(False, error_message="multidir requires output_dir (the directories are "
                                                           "resolved relative to it)")
        resolved_dirs = []
        for d in multidir:
            p = Path(d) if Path(d).is_absolute() else Path(output_dir) / d
            err = _check_path(str(p))
            if err:
                return format_tool_result(False, error_message=f"multidir entry {d}: {err}")
            resolved_dirs.append(str(p.resolve()))
        multidir = resolved_dirs

    if ntomp is None and not multidir:
        # share the cores between the job slots so concurrent runs do not oversubscribe the CPU
        ntomp = max(1, min(8, (os.cpu_count() or 1) // max(1, job_manager.max_concurrent)))

    script_path = str(SCRIPTS_DIR / "md_simulation.py")
    res = job_manager.submit_job(
        script_path=script_path,
        args={
            "input": validation["absolute_path"],
            "nsteps": nsteps,
            "ntmpi": ntmpi,
            "ntomp": ntomp,
            "gpu": gpu,
            "maxh": maxh,
            "timeout": max_runtime_seconds,
            "cpt": checkpoint_minutes,
            "pin": pin,
            "resume": resume,
            "multidir": multidir,
            "replex": replex,
        },
        job_name=job_name or f"md_sim_{Path(input_file).stem}",
        output_dir=output_dir,
        default_output_subdir="run",
        kind="md",
    )
    if res.get("status") == "submitted":
        return format_tool_result(True, data={k: v for k, v in res.items() if k != "status"})
    return format_tool_result(False, error_message=res.get("error"))


@registry.tool("submit")
def submit_batch_analysis(
    input_files: List[str],
    analysis_type: str = "tpr",
    output_dir: Optional[str] = None,
    job_name: Optional[str] = None
) -> dict:
    """
    Analyze several files in one background job.

    Args:
        input_files: List of file paths
        analysis_type: 'tpr' (summarize each .tpr)
        output_dir: Directory for one result file per input (<name>.json)
        job_name: Label for the batch job

    Paths outside the allowed directory or not ending in .tpr refuse the whole batch. A file
    that does not exist, or that GROMACS cannot read, is reported as failed in its own entry
    and the others are still analyzed; the job completes when at least one file succeeds.

    Returns:
        job_id (and a warning naming files that do not exist). get_job_result returns per file
        results: total, succeeded, failed, results[] (each with input, tpr_info or error).
    """
    if analysis_type != "tpr":
        return format_tool_result(False, error_message=f"Unsupported analysis type: {analysis_type}. Supported: tpr")
    if not input_files:
        return format_tool_result(False, error_message="input_files is empty")
    resolved, missing = [], []
    for input_file in input_files:
        try:
            path = resolve_in_workdir(input_file)
        except PathNotAllowedError as e:
            return format_tool_result(False, error_message=f"Invalid file {input_file}: {e}")
        if path.suffix.lower() != ".tpr":
            return format_tool_result(False, error_message=f"Invalid file {input_file}: expected a .tpr file")
        if not path.is_file():
            missing.append(input_file)
        resolved.append(str(path))
    if len(missing) == len(resolved):
        return format_tool_result(False, error_message="None of the files exist: " + ", ".join(missing))
    warnings = [f"Not found, reported as failed in the result: {', '.join(missing)}"] if missing else []
    if output_dir:
        try:
            output_dir = str(resolve_in_workdir(output_dir))
        except PathNotAllowedError as e:
            return format_tool_result(False, error_message=str(e))

    res = job_manager.submit_job(
        script_path=str(SCRIPTS_DIR / "tpr_analysis.py"),
        args={"inputs": resolved, "format": "json"},
        job_name=job_name or f"batch_{analysis_type}_{len(input_files)}_files",
        output_dir=output_dir,
        default_output_subdir="analysis",
        kind="analysis",
    )
    if res.get("status") == "submitted":
        return format_tool_result(True, data={k: v for k, v in res.items() if k != "status"}, warnings=warnings)
    return format_tool_result(False, error_message=res.get("error"))


# ==============================================================================
# Information Tools
# ==============================================================================

@registry.tool("info")
def list_available_commands() -> dict:
    """
    List the tools this server provides and the GROMACS commands run_gromacs_command accepts.

    Returns:
        tools by category, gromacs_commands with descriptions (grouped by category),
        workflow_types, example_data directory.
    """
    try:
        from gromacs_command import list_available_commands as cmds, list_commands_by_category
        from gromacs_workflow import list_workflow_types

        return format_tool_result(True, data={
            "tools": registry.categories,
            "gromacs_commands": cmds(),
            "gromacs_commands_by_category": list_commands_by_category(),
            "workflow_types": list_workflow_types(),
            "example_data": str(get_example_data_path()),
        })
    except Exception as e:
        return format_tool_result(False, error_message=str(e))


@registry.tool("info")
def get_server_info() -> dict:
    """
    Server version, the GROMACS version it is using, configuration and example usage.
    """
    root = get_workdir_root()
    example = get_example_data_path()
    return format_tool_result(True, data={
        "server_name": SERVER_NAME,
        "version": SERVER_VERSION,
        "gromacs": get_gmx_version(),
        "description": "MCP server providing GROMACS molecular dynamics tools",
        "capabilities": {
            "sync_operations": "Fast preparation, analysis and command execution",
            "async_operations": "Long-running simulations as jobs with progress, cancel and resume",
            "job_management": "Queue, status, logs, cancel, resume, cleanup",
            "batch_processing": "Multiple file processing in single jobs",
        },
        "configuration": {
            "workdir_restriction": str(root) if root else None,
            "max_concurrent_jobs": job_manager.max_concurrent,
            "jobs_dir": str(job_manager.jobs_dir),
        },
        "tool_counts": {k: len(v) for k, v in registry.categories.items()},
        "example_data": {"path": str(example), "exists": example.exists()},
        "example_usage": {
            "sync": "analyze_tpr with input_file 'examples/data/sample.tpr'",
            "async": "submit_md_simulation with input_file 'examples/data/sample.tpr'",
        },
        "job_workflow": [
            "1. Submit: Use submit_* tools to start long jobs",
            "2. Monitor: Use get_job_status(job_id) to check progress",
            "3. Retrieve: Use get_job_result(job_id) when completed",
            "4. Debug: Use get_job_log(job_id) for troubleshooting",
        ],
        "tip": "Run check_environment first when something does not work",
    })


# ==============================================================================
# Additional tool modules
# ==============================================================================
def _register_modules() -> None:
    from tools import analysis, env_tools, pipeline, prep, resources
    for module in (prep, pipeline, analysis, env_tools, resources):
        module.register(mcp, registry)


_register_modules()


def main() -> None:
    """Entry point: run the server over stdio."""
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
