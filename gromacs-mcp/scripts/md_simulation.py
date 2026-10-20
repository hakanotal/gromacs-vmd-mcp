#!/usr/bin/env python3
"""
Script: md_simulation.py
Description: Run a molecular dynamics simulation with gmx mdrun

Usage:
    python scripts/md_simulation.py --input <tpr_file> --output-dir <output_dir> [options]

Example:
    python scripts/md_simulation.py --input examples/data/sample.tpr --output-dir results/md_sim --nsteps 5000

Outputs (in the output directory, named by -deffnm md):
    md.tpr  md.log  md.edr  md.gro  md.cpt  md.xtc  md.trr (those enabled in the .mdp)
    mdrun.out  captured stdout and stderr of mdrun

Notes:
    * Without --nsteps the number of steps stored in the .tpr is used. With --nsteps the run
      input is rewritten with convert-tpr (mdrun -nsteps is deprecated in recent GROMACS).
    * By default one thread-MPI rank is used with up to 8 OpenMP threads, which
      works for any box size. Domain decomposition across ranks is opt in (--ntmpi).
    * --maxh makes GROMACS stop cleanly and write a checkpoint after that many hours.
    * --resume continues from md.cpt in the output directory using the given .tpr
      (use it with a .tpr produced by convert-tpr -extend to lengthen a run).
"""

import argparse
import json
import logging
import signal
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from lib.gromacs_utils import (
    GromacsNotFoundError,
    PathNotAllowedError,
    check_file_exists,
    copy_input_file,
    deep_merge,
    default_thread_count,
    ensure_output_directory,
    find_gromacs_command,
    get_file_size,
    parse_mdlog_progress,
    read_tpr_nsteps,
    unsafe_argv_path,
    resolve_in_workdir,
    run_command_full,
    summarize_gmx_error,
    write_json_atomic,
)

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "simulation": {
        "deffnm": "md",
        "pin": "auto",
        "checkpoint_minutes": 15,
    },
    "output_suffixes": ["tpr", "log", "edr", "gro", "cpt", "xtc", "trr"],
}

_DECOMP_ERROR = "domain decomposition"


def _build_mdrun_command(
    gmx: List[str],
    deffnm: str,
    ntmpi: int,
    ntomp: int,
    gpu: bool,
    pin: str,
    maxh: Optional[float],
    cpt: Optional[int],
    resume: bool,
    multidir: Optional[List[str]],
    replex: Optional[int],
    verbose: bool,
) -> List[str]:
    cmd = gmx + ["mdrun", "-deffnm", deffnm, "-ntmpi", str(ntmpi), "-ntomp", str(ntomp)]
    if pin and pin != "auto":
        cmd += ["-pin", pin]
    if maxh:
        cmd += ["-maxh", str(maxh)]
    if cpt:
        cmd += ["-cpt", str(cpt)]
    if resume:
        cmd += ["-cpi", f"{deffnm}.cpt"]
    if gpu:
        cmd += ["-nb", "gpu", "-pme", "gpu", "-bonded", "gpu", "-update", "gpu"]
    if multidir:
        cmd += ["-multidir"] + list(multidir)
        if replex:
            cmd += ["-replex", str(replex)]
    if verbose:
        cmd += ["-v"]
    return cmd


# Set when this wrapper receives SIGTERM or SIGINT (a job cancel signals the whole process group).
# The wrapper keeps running until mdrun has written its checkpoint and exited, then reports it.
_STOP_SIGNAL: Dict[str, Optional[int]] = {"signal": None}


def _note_stop(signum, frame) -> None:
    _STOP_SIGNAL["signal"] = signum


def install_stop_handler() -> None:
    signal.signal(signal.SIGTERM, _note_stop)
    signal.signal(signal.SIGINT, _note_stop)


def _run_mdrun(cmd: List[str], cwd: Path, timeout: Optional[int], out_file: Path) -> Dict[str, Any]:
    """Run mdrun with stdout and stderr streamed to a file (long runs do not buffer in memory)."""
    logger.info("Command: %s", " ".join(cmd))
    unsafe = unsafe_argv_path(cmd)
    if unsafe:
        out_file.write_text("Error in user input:\n" + unsafe + "\n")
        return {"returncode": 1, "timed_out": False}
    timed_out = False
    with open(out_file, "w") as fh:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=fh, stderr=subprocess.STDOUT, text=True,
                                stdin=subprocess.DEVNULL)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()  # mdrun writes a checkpoint on SIGTERM
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    return {"returncode": proc.returncode, "timed_out": timed_out}


def run_md_simulation(
    input_file: Union[str, Path],
    output_file: Optional[Union[str, Path]] = None,
    config: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Run an MD simulation from a .tpr file.

    Args:
        input_file: Path to the .tpr
        output_file: Output directory (created). Default: md_output
        config: configuration overrides
        **kwargs:
            nsteps: override step count (default: value in the .tpr)
            ntmpi: thread-MPI ranks (default 1)
            ntomp: OpenMP threads per rank (default min(cpu count, 8))
            gpu: offload nonbonded, PME, bonded and update to the GPU
            pin: auto, on or off
            maxh: stop cleanly after this many hours and write a checkpoint
            cpt: checkpoint interval in minutes
            timeout: hard wall clock limit in seconds (default none)
            resume: continue from md.cpt in the output directory
            multidir / replex: multi-simulation and replica exchange interval
            verbose: pass -v to mdrun

    Returns:
        dict with result, output_dir, output_files (absolute paths), file_sizes ({path: bytes}),
        warnings, metadata, complete (all steps done), stopped and stop_reason (ended cleanly
        before the last step because of maxh or the time limit; result is True and resume
        continues), and on failure error plus the tail of mdrun output.
    """
    input_path = Path(input_file).resolve()  # commands below run inside the output folder
    config = deep_merge(DEFAULT_CONFIG, config)
    sim = config["simulation"]
    deffnm = sim["deffnm"]

    nsteps = kwargs.get("nsteps")
    ntmpi = int(kwargs.get("ntmpi") or 1)
    ntomp = int(kwargs.get("ntomp") or default_thread_count())
    gpu = bool(kwargs.get("gpu", False))
    pin = kwargs.get("pin") or sim["pin"]
    maxh = kwargs.get("maxh")
    cpt = kwargs.get("cpt") or sim["checkpoint_minutes"]
    timeout = kwargs.get("timeout")
    resume = bool(kwargs.get("resume", False))
    multidir = kwargs.get("multidir")
    replex = kwargs.get("replex")
    verbose = bool(kwargs.get("verbose", False))
    if verbose:
        logger.setLevel(logging.DEBUG)

    logger.info("Running MD simulation from TPR file: %s", input_path)
    if not check_file_exists(input_path, "TPR file"):
        raise FileNotFoundError(f"TPR file not found: {input_path}")

    output_dir = ensure_output_directory(output_file or "md_output").resolve()
    logger.info("Output directory: %s", output_dir)

    if resume and not (output_dir / f"{deffnm}.cpt").exists():
        return {"result": False, "error": f"Cannot resume: {deffnm}.cpt not found in {output_dir}",
                "output_dir": str(output_dir)}

    try:
        gmx = find_gromacs_command()
    except GromacsNotFoundError as e:
        return {"result": False, "error": str(e), "output_dir": str(output_dir)}

    warnings: List[str] = []
    try:
        run_tpr = output_dir / f"{deffnm}.tpr"
        if nsteps is not None:
            # mdrun -nsteps is deprecated; write the step count into the run input instead
            conv = run_command_full(gmx + ["convert-tpr", "-s", str(input_path), "-o", str(run_tpr),
                                           "-nsteps", str(nsteps)], working_dir=output_dir, timeout=120)
            if not conv["success"]:
                return {"result": False, "output_dir": str(output_dir),
                        "error": "convert-tpr failed: " + (summarize_gmx_error(conv["stderr"]) or "unknown error")}
        elif input_path.resolve() != run_tpr.resolve():
            copy_input_file(input_path, output_dir, run_tpr.name)
        # the step count this run must reach; md.log alone is not enough after extend and resume
        target_nsteps = nsteps if nsteps is not None else read_tpr_nsteps(run_tpr)
        out_file = output_dir / "mdrun.out"
        attempts = []

        def attempt(n_ranks: int) -> Dict[str, Any]:
            cmd = _build_mdrun_command(gmx, deffnm, n_ranks, ntomp, gpu, pin, maxh, cpt,
                                       resume, multidir, replex, verbose)
            res = _run_mdrun(cmd, output_dir, timeout, out_file)
            res["command"] = " ".join(cmd)
            res["ntmpi"] = n_ranks
            attempts.append(res)
            return res

        res = attempt(ntmpi)
        if res["returncode"] != 0 and ntmpi != 1 and not multidir:
            text = out_file.read_text(errors="replace")
            if _DECOMP_ERROR in text.lower():
                warnings.append(f"Domain decomposition failed with {ntmpi} ranks; retried with 1 rank")
                logger.warning(warnings[-1])
                res = attempt(1)

        out_text = out_file.read_text(errors="replace") if out_file.exists() else ""
        stopped_by = _STOP_SIGNAL["signal"]
        log_path = output_dir / f"{deffnm}.log"
        progress = (parse_mdlog_progress(log_path.read_text(errors="replace"), nsteps=target_nsteps)
                    if log_path.exists() else {})
        complete = bool(progress.get("complete"))
        # mdrun ended cleanly (final checkpoint written) before the last step, and nobody cancelled it:
        # the run is stopped, not failed, and resume continues it
        stop_reason = None
        if stopped_by is None and progress.get("finished") and not complete:
            if res["timed_out"]:
                stop_reason = f"time limit of {timeout} s"
            elif res["returncode"] == 0:
                stop_reason = f"maxh={maxh} h" if maxh else "mdrun ended before the last step"
        success = (res["returncode"] == 0 and not res["timed_out"] and stopped_by is None) or stop_reason is not None
        if stop_reason:
            warnings.append(f"Stopped at step {progress.get('step')} of {progress.get('nsteps')} ({stop_reason}); "
                            "a checkpoint was written and resume continues the run")

        output_files = []
        file_sizes = {}
        for suffix in config["output_suffixes"]:
            p = output_dir / f"{deffnm}.{suffix}"
            if p.exists():
                output_files.append(str(p))
                file_sizes[str(p)] = get_file_size(p)

        stray = sorted(p.name for p in output_dir.glob("step*.pdb"))
        if stray:
            warnings.append(
                f"{len(stray)} step*.pdb files written: GROMACS dumped structures after constraint "
                "failures (SETTLE or LINCS). Check the system or minimize more before dynamics.")

        if stopped_by is not None:
            warnings.append("Stopped by a signal (cancelled); a checkpoint was written (use resume to continue)")

        result = {
            "result": success,
            "output_dir": str(output_dir),
            "output_files": output_files,
            "file_sizes": file_sizes,
            "warnings": warnings,
            "metadata": {
                "input_file": str(input_path),
                "nsteps": nsteps,
                "target_nsteps": target_nsteps,
                "ntmpi": res["ntmpi"],
                "ntomp": ntomp,
                "gpu": gpu,
                "resume": resume,
                "command": res["command"],
                "returncode": res["returncode"],
                "total_output_files": len(output_files),
            },
            "complete": complete,
            "stopped": stop_reason is not None,
            "stop_reason": stop_reason,
            "progress": progress,
        }
        if not success:
            if stopped_by is not None:
                result["error"] = "Stopped by a signal (cancelled); resume continues from the checkpoint"
            elif res["timed_out"]:
                result["error"] = f"mdrun timed out after {timeout} seconds without writing a final checkpoint"
            else:
                result["error"] = summarize_gmx_error(out_text) or "mdrun failed"
            result["mdrun_output_tail"] = "\n".join(out_text.splitlines()[-30:])
        return result

    except Exception as e:
        logger.error("Error during MD simulation: %s", e)
        return {"result": False, "error": str(e), "output_dir": str(output_dir),
                "metadata": {"input_file": str(input_path)}}


def extend_tpr(
    input_tpr: Union[str, Path],
    output_tpr: Union[str, Path],
    extend_ps: Optional[float] = None,
    until_ps: Optional[float] = None,
    nsteps: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Create a longer .tpr with `gmx convert-tpr`. Give exactly one of extend_ps
    (add time), until_ps (absolute end time) or nsteps (new total step count).
    """
    given = [x for x in (extend_ps, until_ps, nsteps) if x is not None]
    if len(given) != 1:
        return {"result": False, "error": "Specify exactly one of extend_ps, until_ps, nsteps"}
    if not check_file_exists(input_tpr, "TPR file"):
        return {"result": False, "error": f"TPR file not found: {input_tpr}"}
    flag, value = (("-extend", extend_ps) if extend_ps is not None
                   else ("-until", until_ps) if until_ps is not None
                   else ("-nsteps", nsteps))
    try:
        gmx = find_gromacs_command()
    except GromacsNotFoundError as e:
        return {"result": False, "error": str(e)}
    Path(output_tpr).parent.mkdir(parents=True, exist_ok=True)
    res = run_command_full(gmx + ["convert-tpr", "-s", str(input_tpr), "-o", str(output_tpr),
                                  flag, str(value)], timeout=120)
    out = {"result": res["success"], "command": res["command"], "output_file": str(output_tpr)}
    if not res["success"]:
        out["error"] = summarize_gmx_error(res["stderr"]) or "convert-tpr failed"
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--input', '-i', required=True, help='Input TPR file path')
    parser.add_argument('--output-dir', '--output', '-o', dest='output_dir',
                        help='Output directory path (default: md_output)')
    parser.add_argument('--result-json', help='Write the result dict as JSON here')
    parser.add_argument('--nsteps', '-n', type=int, help='Override number of steps (default: from the TPR)')
    parser.add_argument('--ntmpi', type=int, help='Thread-MPI ranks (default 1)')
    parser.add_argument('--ntomp', type=int, help='OpenMP threads per rank')
    parser.add_argument('--gpu', action='store_true', help='Offload to GPU')
    parser.add_argument('--pin', choices=['auto', 'on', 'off'])
    parser.add_argument('--maxh', type=float, help='Stop cleanly after this many hours')
    parser.add_argument('--cpt', type=int, help='Checkpoint interval in minutes')
    parser.add_argument('--timeout', type=int, help='Hard time limit in seconds (default: none)')
    parser.add_argument('--resume', action='store_true', help='Continue from md.cpt in the output directory')
    parser.add_argument('--multidir', action='append',
                        help='Simulation directory for a multi-simulation (repeat for each directory)')
    parser.add_argument('--replex', type=int, help='Replica exchange attempt interval in steps')
    parser.add_argument('--config', '-c', help='Config file (JSON format)')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args()

    config = None
    if args.config:
        with open(args.config) as f:
            config = json.load(f)

    kwargs = {k: v for k, v in dict(
        nsteps=args.nsteps, ntmpi=args.ntmpi, ntomp=args.ntomp, gpu=args.gpu, pin=args.pin,
        maxh=args.maxh, cpt=args.cpt, timeout=args.timeout, resume=args.resume,
        replex=args.replex, verbose=args.verbose,
    ).items() if v is not None and v is not False}  # 0 is a valid value (nsteps=0)
    try:
        resolve_in_workdir(args.input)  # the server checked these already; check again where they are used
        if args.output_dir:
            resolve_in_workdir(args.output_dir)
        if args.multidir:
            kwargs['multidir'] = [str(resolve_in_workdir(d)) for d in args.multidir]
    except PathNotAllowedError as e:
        result = {"result": False, "error": str(e)}
        if args.result_json:
            write_json_atomic(args.result_json, result)
        print(f"Failed: {e}")
        return 1

    install_stop_handler()
    try:
        result = run_md_simulation(args.input, args.output_dir, config=config, **kwargs)
    except Exception as e:
        result = {"result": False, "error": str(e)}

    if args.result_json:
        write_json_atomic(args.result_json, result)

    if result['result']:
        print("Success: MD simulation completed")
        print(f"  Output directory: {result['output_dir']}")
        for info in result['output_files']:
            print(f"  {info}")
        for w in result.get('warnings', []):
            print(f"  warning: {w}")
        return 0
    print(f"Failed: {result.get('error', 'Unknown error')}")
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
