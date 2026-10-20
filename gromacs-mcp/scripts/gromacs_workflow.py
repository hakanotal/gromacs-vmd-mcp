#!/usr/bin/env python3
"""
Script: gromacs_workflow.py
Description: Multi-step GROMACS workflows with logging and resume

Workflows:
    full_md        structure -> box -> solvent -> ions -> minimization -> NVT -> NPT -> production .tpr
    preprocessing  structure -> topology -> box -> solvent (optionally ions)
    simple_md      grompp -> mdrun from structure, topology and mdp
    analysis       rms, rmsf and energy terms from a finished run
    demo           environment self test (gmx --version, gmx help commands)

Usage:
    python scripts/gromacs_workflow.py --workflow full_md --structure protein.pdb --output workflow_results

Resume:
    Re-running with the same output directory skips steps that already finished (same command,
    outputs present). If the input files changed, all steps start over.
    The production run itself is not part of full_md: it ends with md.tpr, ready for
    submit_md_simulation (long runs belong in a background job).
"""

import argparse
import hashlib
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from lib import mdp_templates
from lib.gromacs_utils import (
    GromacsNotFoundError,
    PathNotAllowedError,
    check_file_exists,
    check_grompp_includes,
    copy_input_file,
    deep_merge,
    default_thread_count,
    ensure_output_directory,
    find_gromacs_command,
    get_workdir_root,
    list_force_fields,
    run_command_full,
    summarize_gmx_error,
    write_json_atomic,
)

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# ==============================================================================
# Configuration
# ==============================================================================
DEFAULT_CONFIG = {
    "workflows": {
        "full_md": {
            "description": "Prepare and equilibrate a system: pdb2gmx, editconf, solvate, genion, EM, NVT, NPT, production tpr",
            "required_inputs": ["structure"],
            "optional_inputs": ["topology", "index"],
            "steps": ["pdb2gmx", "editconf", "solvate", "genion", "em", "nvt", "npt", "production_tpr"],
        },
        "simple_md": {
            "description": "grompp then mdrun",
            "required_inputs": ["structure", "topology", "mdp"],
            "optional_inputs": ["index"],
            "steps": ["grompp", "mdrun"],
        },
        "analysis": {
            "description": "RMSD, RMSF and energy terms from a finished run",
            "required_inputs": ["structure", "trajectory"],
            "optional_inputs": ["energy"],
            "steps": ["rms", "rmsf", "energy"],
        },
        "preprocessing": {
            "description": "pdb2gmx, editconf, solvate (and genion when ions=True)",
            "required_inputs": ["structure"],
            "optional_inputs": ["topology"],
            "steps": ["pdb2gmx", "editconf", "solvate"],
        },
        "demo": {
            "description": "Environment self test: gmx --version and gmx help commands",
            "required_inputs": [],
            "optional_inputs": [],
            "steps": ["gmx_version", "gmx_help"],
        },
    },
    "execution": {
        "step_timeout": 600,
        "mdrun_timeout": 3600,
        "continue_on_error": False,
    },
}

_SOLVENT_BOX = {"spc": "spc216.gro", "spce": "spc216.gro", "tip3p": "spc216.gro",
                "tip4p": "tip4p.gro", "tip5p": "tip5p.gro"}

_STEP_LOG_LINES = 15


def list_workflow_types() -> Dict[str, Dict[str, Any]]:
    """Workflow names with description, required and optional inputs."""
    return {name: {"description": cfg["description"], "required_inputs": cfg["required_inputs"],
                   "optional_inputs": cfg.get("optional_inputs", []), "steps": cfg["steps"]}
            for name, cfg in DEFAULT_CONFIG["workflows"].items()}


def _grompp_warnings(stderr: str) -> List[str]:
    out = []
    lines = (stderr or "").splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^WARNING \d+ \[", line):
            msg = " ".join(l.strip() for l in lines[i + 1:i + 4] if l.strip() and not l.startswith("For more information"))
            out.append(msg[:300])
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ==============================================================================
# Core Workflow Classes
# ==============================================================================
class WorkflowStep:
    """
    One command with expected outputs, optional stdin, and resume bookkeeping.

    A step is skipped on resume only when its marker records the same command, the same stdin
    and the same content (sha256) of every file it reads, and its outputs exist. Because inputs
    are compared by content, a step whose upstream step reran with a different result reruns
    too, and so does a step whose .mdp changed because an option changed. Files whose size and
    modification time match the marker are not read again.

    Steps never modify their inputs: commands that edit a topology in place (solvate, genion)
    get a fresh copy through `pre_copy` (source, destination) and edit that.
    """

    def __init__(self, name: str, command: List[str], stdin_text: Optional[str] = None,
                 outputs: Optional[List[str]] = None, depends_on: Optional[List[str]] = None,
                 timeout: Optional[int] = None, reads: Optional[List[str]] = None,
                 pre_copy: Optional[Tuple[str, str]] = None, pre_check: Optional[Callable[[], None]] = None):
        self.name = name
        self.command = [str(c) for c in command]
        self.stdin_text = stdin_text
        self.outputs = outputs or []
        self.depends_on = depends_on or []
        self.timeout = timeout
        self.reads = reads or []
        self.pre_copy = pre_copy
        self.pre_check = pre_check  # runs before the command, cached or not; raising fails the step
        self.status = "pending"  # pending, running, completed, skipped, failed
        self.result: Optional[Dict[str, Any]] = None

    def _marker(self, working_dir: Path) -> Path:
        return working_dir / ".workflow" / f"{self.name}.json"

    def _input_names(self, working_dir: Path) -> List[str]:
        produced = set(self.outputs) | ({self.pre_copy[1]} if self.pre_copy else set())
        names = set(self.reads)
        if self.pre_copy:
            names.add(self.pre_copy[0])
        for arg in self.command[1:]:
            if arg not in produced and "/" not in arg and (working_dir / arg).is_file():
                names.add(arg)
        return sorted(n for n in names if n not in produced)

    def _input_state(self, working_dir: Path, previous: Optional[Dict[str, Any]] = None
                     ) -> Tuple[Dict[str, Optional[str]], Dict[str, List[int]]]:
        """sha256 and [size, mtime_ns] of every file the step reads. A hash recorded in a previous
        marker is reused when size and modification time are exactly the same, so resuming does not
        read large trajectories and run inputs again; anything else is hashed."""
        old_hashes = (previous or {}).get("inputs") or {}
        old_stats = (previous or {}).get("stats") or {}
        hashes: Dict[str, Optional[str]] = {}
        stats: Dict[str, List[int]] = {}
        for name in self._input_names(working_dir):
            path = working_dir / name
            if not path.is_file():
                hashes[name] = None
                continue
            st = path.stat()
            stats[name] = [st.st_size, st.st_mtime_ns]
            if old_stats.get(name) == stats[name] and old_hashes.get(name):
                hashes[name] = old_hashes[name]
            else:
                hashes[name] = _sha256(path)
        return hashes, stats

    def _cached(self, working_dir: Path) -> bool:
        marker = self._marker(working_dir)
        if not marker.exists():
            return False
        try:
            data = json.loads(marker.read_text())
        except (OSError, ValueError):
            return False
        if data.get("command") != self.command or data.get("stdin") != self.stdin_text:
            return False
        if not all((working_dir / o).exists() for o in self.outputs):
            return False
        return data.get("inputs") == self._input_state(working_dir, previous=data)[0]

    def execute(self, working_dir: Path, timeout: int = 300, resume: bool = True) -> bool:
        if self.pre_check is not None:
            try:
                self.pre_check()
            except (PathNotAllowedError, ValueError) as e:
                self.status = "failed"
                self.result = {"success": False, "duration": 0.0, "stdout": "", "stderr": "", "error": str(e)}
                logger.error("Step '%s' refused: %s", self.name, e)
                return False
        if resume and self._cached(working_dir):
            self.status = "skipped"
            self.result = {"success": True, "duration": 0.0, "stdout": "", "stderr": "", "skipped": True}
            logger.info("Step '%s' skipped (already done with the same inputs)", self.name)
            return True

        self.status = "running"
        start = time.time()
        logger.info("Executing step: %s", self.name)
        self._marker(working_dir).unlink(missing_ok=True)
        inputs, stats = self._input_state(working_dir)
        env = None
        if self.pre_copy:
            shutil.copy2(working_dir / self.pre_copy[0], working_dir / self.pre_copy[1])
            # the copy exists only for this step to edit (solvate -p, genion -p); a #backup# of it is clutter
            env = {"GMX_MAXBACKUP": "-1"}
        res = run_command_full(self.command, working_dir=working_dir,
                               timeout=self.timeout or timeout, stdin_text=self.stdin_text, env=env)
        duration = time.time() - start

        missing = [o for o in self.outputs if not (working_dir / o).exists()]
        success = res["success"] and not missing
        error = None
        if not res["success"]:
            error = ("timed out" if res["timed_out"] else
                     summarize_gmx_error(res["stderr"]) or summarize_gmx_error(res["stdout"]) or "command failed")
        elif missing:
            error = f"expected output not produced: {', '.join(missing)}"

        self.result = {"success": success, "duration": duration, "stdout": res["stdout"],
                       "stderr": res["stderr"], "error": error, "command": res["command"]}
        if success:
            self.status = "completed"
            write_json_atomic(self._marker(working_dir), {"command": self.command, "stdin": self.stdin_text,
                                                           "outputs": self.outputs, "inputs": inputs,
                                                           "stats": stats})
            logger.info("Step '%s' completed (%.1fs)", self.name, duration)
        else:
            self.status = "failed"
            logger.error("Step '%s' failed: %s", self.name, error)
        return success


class WorkflowManager:
    """Runs steps in order, honoring dependencies."""

    def __init__(self, output_dir: Path, config: Optional[Dict[str, Any]] = None):
        self.output_dir = output_dir
        self.config = config or DEFAULT_CONFIG
        self.steps: List[WorkflowStep] = []
        self.gmx_cmd = find_gromacs_command()

    def add_step(self, step: WorkflowStep) -> None:
        self.steps.append(step)

    def _check_dependencies(self, step: WorkflowStep) -> bool:
        for dep in step.depends_on:
            dep_step = next((s for s in self.steps if s.name == dep), None)
            if not dep_step or dep_step.status not in ("completed", "skipped"):
                return False
        return True

    def execute_workflow(self, continue_on_error: bool = False, resume: bool = True,
                         verbose: bool = False) -> Dict[str, Any]:
        logger.info("Starting workflow with %d steps", len(self.steps))
        exec_cfg = self.config.get("execution", DEFAULT_CONFIG["execution"])
        results: Dict[str, Any] = {
            "total_steps": len(self.steps), "completed_steps": 0, "skipped_steps": 0,
            "failed_steps": 0, "step_results": [], "overall_success": True, "warnings": [],
        }

        for step in self.steps:
            if not self._check_dependencies(step):
                step.status = "failed"
                step.result = {"success": False, "duration": 0.0, "error": "a dependency did not complete"}
                results["failed_steps"] += 1
                results["overall_success"] = False
                results["step_results"].append({"name": step.name, "status": "failed", "success": False,
                                                "duration": 0.0, "error": "a dependency did not complete"})
                if not continue_on_error:
                    break
                continue

            success = step.execute(self.output_dir, timeout=exec_cfg.get("step_timeout", 600), resume=resume)
            entry = {"name": step.name, "status": step.status, "success": success,
                     "duration": round(step.result["duration"], 2)}
            if step.result.get("error"):
                entry["error"] = step.result["error"]
            if step.result.get("command"):
                entry["command"] = step.result["command"]
            if verbose or not success:
                entry["stdout_tail"] = "\n".join((step.result.get("stdout") or "").splitlines()[-_STEP_LOG_LINES:])
                entry["stderr_tail"] = "\n".join((step.result.get("stderr") or "").splitlines()[-_STEP_LOG_LINES:])
            gw = _grompp_warnings(step.result.get("stderr", ""))
            if gw:
                results["warnings"].extend(f"{step.name}: {w}" for w in gw)
            results["step_results"].append(entry)

            if step.status == "skipped":
                results["skipped_steps"] += 1
            elif success:
                results["completed_steps"] += 1
            else:
                results["failed_steps"] += 1
                results["overall_success"] = False
                if not continue_on_error:
                    break
        return results


# ==============================================================================
# Workflow builders
# ==============================================================================
def _require_inputs(input_files: Optional[Dict[str, Any]], required: List[str]) -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    for req in required:
        if not input_files or req not in input_files or not input_files[req]:
            raise ValueError(f"Required input '{req}' not provided")
        if not check_file_exists(input_files[req], f"{req} file"):
            raise FileNotFoundError(f"{req} file not found: {input_files[req]}")
        out[req] = Path(input_files[req])
    for opt, val in (input_files or {}).items():
        if opt not in out and val and Path(val).exists():
            out[opt] = Path(val)
    return out


def _fingerprint(paths: Dict[str, Path]) -> Dict[str, List[int]]:
    return {k: [p.stat().st_size, p.stat().st_mtime_ns] for k, p in sorted(paths.items())}


def _stage_inputs(manager: WorkflowManager, inputs: Dict[str, Path], resume: bool) -> Dict[str, Path]:
    """Copy inputs into the output directory. If they differ from the last run, discard step markers."""
    state_dir = manager.output_dir / ".workflow"
    state_dir.mkdir(parents=True, exist_ok=True)
    fp_file = state_dir / "inputs.json"
    fp = _fingerprint(inputs)
    previous = None
    if fp_file.exists():
        try:
            previous = json.loads(fp_file.read_text())
        except (OSError, ValueError):
            previous = None
    fresh = not resume or previous != fp
    if fresh:
        for marker in state_dir.glob("*.json"):
            if marker.name != "inputs.json":
                marker.unlink()
    write_json_atomic(fp_file, fp)

    # Whenever the run starts over, inputs are copied again from the originals so nothing a
    # previous run did to the working copies survives.
    staged: Dict[str, Path] = {}
    for role, path in inputs.items():
        dst = manager.output_dir / path.name
        if fresh or not dst.exists():
            copy_input_file(path, manager.output_dir)
        staged[role] = dst
    topology = inputs.get("topology")
    if topology and fresh:
        for itp in topology.parent.glob("*.itp"):
            if itp.is_file() and not itp.is_symlink():
                shutil.copy2(itp, manager.output_dir / itp.name)
    if "topology" in staged:
        # after every file grompp will see is in place, so the check covers exactly those files
        check_grompp_includes(topology=staged["topology"], mdp=staged.get("mdp"), cwd=manager.output_dir)
    return staged


def _includes_check(out: Path, topology: str, mdp: str) -> Callable[[], None]:
    """Check, right before a grompp step, the includes of the topology it will read (generated
    topologies included). A no-op without a sandbox."""
    return lambda: check_grompp_includes(topology=out / topology, mdp=out / mdp, cwd=out)


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")


def _validate_names(opts: Dict[str, Any]) -> None:
    """force_field and water_model are names, never paths; under a sandbox the force field must be installed."""
    for key in ("force_field", "water_model"):
        value = str(opts[key])
        if not _NAME_RE.match(value) or ".." in value:
            raise ValueError(f"{key} must be a plain name such as oplsaa or spce, not {value!r}")
    if get_workdir_root() is not None:
        installed = {ff["name"] for ff in list_force_fields()}
        if installed and opts["force_field"] not in installed:
            raise ValueError(f"force_field {opts['force_field']!r} is not installed; available: "
                             f"{', '.join(sorted(installed))}")


def _mdrun(gmx: List[str], deffnm: str, threads: int, timeout: int) -> List[str]:
    return gmx + ["mdrun", "-deffnm", deffnm, "-ntmpi", "1", "-ntomp", str(threads)]


def _write_mdp(output_dir: Path, name: str, stage: str, **kwargs) -> None:
    text, _ = mdp_templates.render_stage(stage, **kwargs)
    (output_dir / name).write_text(text)


def _ensure_ions_include(topology: Path) -> Optional[str]:
    """
    Make sure a user supplied topology defines the ion molecule types genion will add.

    pdb2gmx writes the ions include itself, a hand written topology usually does not.
    Inserts `#include "<forcefield>.ff/ions.itp"` before [ system ] when it is missing.
    Returns a note when a change was made.
    """
    text = topology.read_text()
    if re.search(r'#include\s+"[^"]*ions\.itp"', text):
        return None
    m = re.search(r'#include\s+"([^"/]+\.ff)/forcefield\.itp"', text)
    if not m:
        return None
    include = f'#include "{m.group(1)}/ions.itp"\n\n'
    idx = re.search(r"^\s*\[\s*system\s*\]", text, re.MULTILINE)
    if not idx:
        return None
    topology.write_text(text[:idx.start()] + include + text[idx.start():])
    return f"Added {m.group(1)}/ions.itp include to {topology.name} so ions can be added"


def _add_prepare_steps(manager: WorkflowManager, staged: Dict[str, Path], opts: Dict[str, Any],
                       with_ions: bool) -> str:
    """Add pdb2gmx/editconf/solvate(/genion) steps. Returns the name of the final structure file."""
    gmx = manager.gmx_cmd
    out = manager.output_dir
    structure = staged["structure"].name
    has_topology = "topology" in staged
    topology = staged["topology"].name if has_topology else "topol.top"
    last_deps: List[str] = []

    if not has_topology:
        manager.add_step(WorkflowStep(
            "pdb2gmx",
            gmx + ["pdb2gmx", "-f", structure, "-o", "processed.gro", "-p", "topol.top", "-i", "posre.itp",
                   "-ff", opts["force_field"], "-water", opts["water_model"], "-ignh"],
            outputs=["processed.gro", "topol.top"]))
        conf, last_deps = "processed.gro", ["pdb2gmx"]
    else:
        conf = structure

    if opts["set_box"]:
        manager.add_step(WorkflowStep(
            "editconf",
            gmx + ["editconf", "-f", conf, "-o", "newbox.gro", "-c", "-d", str(opts["box_distance"]),
                   "-bt", opts["box_type"]],
            outputs=["newbox.gro"], depends_on=last_deps))
        conf, last_deps = "newbox.gro", ["editconf"]

    if topology in ("solv.top", "ions.top"):
        raise ValueError(f"Please rename the topology: {topology} is used for files the workflow writes")

    if opts["solvate"]:
        box_file = _SOLVENT_BOX.get(opts["water_model"], "spc216.gro")
        manager.add_step(WorkflowStep(
            "solvate",
            gmx + ["solvate", "-cp", conf, "-cs", box_file, "-o", "solv.gro", "-p", "solv.top"],
            pre_copy=(topology, "solv.top"), outputs=["solv.gro", "solv.top"], depends_on=last_deps))
        conf, topology, last_deps = "solv.gro", "solv.top", ["solvate"]

    if with_ions:
        if has_topology:
            note = _ensure_ions_include(staged["topology"])  # the staged copy; idempotent
            if note:
                logger.info(note)
        _write_mdp(out, "ions.mdp", "ions")
        manager.add_step(WorkflowStep(
            "ions_grompp",
            gmx + ["grompp", "-f", "ions.mdp", "-c", conf, "-p", topology, "-o", "ions.tpr",
                   "-maxwarn", str(opts["maxwarn"])],
            outputs=["ions.tpr"], depends_on=last_deps, pre_check=_includes_check(out, topology, "ions.mdp")))
        manager.add_step(WorkflowStep(
            "genion",
            gmx + ["genion", "-s", "ions.tpr", "-o", "solv_ions.gro", "-p", "ions.top", "-pname", "NA",
                   "-nname", "CL", "-neutral", "-conc", str(opts["salt_concentration"])],
            stdin_text="SOL\n", pre_copy=(topology, "ions.top"), outputs=["solv_ions.gro", "ions.top"],
            depends_on=["ions_grompp"]))
        conf, topology, last_deps = "solv_ions.gro", "ions.top", ["genion"]

    manager.prepared_structure = conf  # type: ignore[attr-defined]
    manager.prepared_topology = topology  # type: ignore[attr-defined]
    manager.last_step = last_deps[-1] if last_deps else None  # type: ignore[attr-defined]
    return conf


def _build_full_md_workflow(manager: WorkflowManager, input_files, config, **kwargs):
    inputs = _require_inputs(input_files, ["structure"])
    opts = _options(kwargs)
    staged = _stage_inputs(manager, inputs, opts["resume"])
    gmx, out = manager.gmx_cmd, manager.output_dir
    threads = opts["threads"]
    stages = opts["stages"]

    conf = _add_prepare_steps(manager, staged, opts, with_ions=opts["ions"])
    top = manager.prepared_topology  # type: ignore[attr-defined]
    prev = manager.last_step  # type: ignore[attr-defined]
    posres = "-DPOSRES" if "topology" not in staged and opts["position_restraints"] else None
    dyn_extra = {"define": posres} if posres else {}
    mdrun_timeout = manager.config["execution"].get("mdrun_timeout", 3600)

    if "em" in stages:
        _write_mdp(out, "em.mdp", "em", overrides={"nsteps": opts["em_steps"]})
        manager.add_step(WorkflowStep(
            "em_grompp", gmx + ["grompp", "-f", "em.mdp", "-c", conf, "-p", top, "-o", "em.tpr",
                                "-maxwarn", str(opts["maxwarn"])],
            outputs=["em.tpr"], depends_on=[prev] if prev else [], pre_check=_includes_check(out, top, "em.mdp")))
        manager.add_step(WorkflowStep("em", _mdrun(gmx, "em", threads, mdrun_timeout), reads=["em.tpr"],
                                      outputs=["em.gro"], depends_on=["em_grompp"], timeout=mdrun_timeout))
        conf, prev = "em.gro", "em"

    if "nvt" in stages:
        _write_mdp(out, "nvt.mdp", "nvt", time_ps=opts["nvt_ps"], temperature=opts["temperature"],
                   overrides=dyn_extra)
        manager.add_step(WorkflowStep(
            "nvt_grompp", gmx + ["grompp", "-f", "nvt.mdp", "-c", conf, "-r", conf, "-p", top, "-o", "nvt.tpr",
                                 "-maxwarn", str(opts["maxwarn"])],
            outputs=["nvt.tpr"], depends_on=[prev] if prev else [], pre_check=_includes_check(out, top, "nvt.mdp")))
        manager.add_step(WorkflowStep("nvt", _mdrun(gmx, "nvt", threads, mdrun_timeout), reads=["nvt.tpr"],
                                      outputs=["nvt.gro", "nvt.cpt"], depends_on=["nvt_grompp"],
                                      timeout=mdrun_timeout))
        conf, prev = "nvt.gro", "nvt"

    if "npt" in stages:
        cpt = ["-t", "nvt.cpt"] if "nvt" in stages else []
        _write_mdp(out, "npt.mdp", "npt", time_ps=opts["npt_ps"], temperature=opts["temperature"],
                   pressure=opts["pressure"],
                   overrides={**dyn_extra, **({} if "nvt" in stages else {"continuation": "no", "gen-vel": "yes",
                                                                           "gen-temp": opts["temperature"]})})
        manager.add_step(WorkflowStep(
            "npt_grompp", gmx + ["grompp", "-f", "npt.mdp", "-c", conf, "-r", conf, "-p", top, "-o", "npt.tpr",
                                 "-maxwarn", str(opts["maxwarn"])] + cpt,
            outputs=["npt.tpr"], depends_on=[prev] if prev else [], pre_check=_includes_check(out, top, "npt.mdp")))
        manager.add_step(WorkflowStep("npt", _mdrun(gmx, "npt", threads, mdrun_timeout), reads=["npt.tpr"],
                                      outputs=["npt.gro", "npt.cpt"], depends_on=["npt_grompp"],
                                      timeout=mdrun_timeout))
        conf, prev = "npt.gro", "npt"

    if "production_tpr" in stages:
        cpt = ["-t", "npt.cpt"] if "npt" in stages else []
        _write_mdp(out, "md.mdp", "npt", time_ps=opts["production_ps"], temperature=opts["temperature"],
                   pressure=opts["pressure"],
                   overrides={"nstxout-compressed": 5000,
                              **({} if "npt" in stages else {"continuation": "no", "gen-vel": "yes"})})
        manager.add_step(WorkflowStep(
            "production_tpr", gmx + ["grompp", "-f", "md.mdp", "-c", conf, "-p", top, "-o", "md.tpr",
                                     "-maxwarn", str(opts["maxwarn"])] + cpt,
            outputs=["md.tpr"], depends_on=[prev] if prev else [], pre_check=_includes_check(out, top, "md.mdp")))


def _build_preprocessing_workflow(manager, input_files, config, **kwargs):
    inputs = _require_inputs(input_files, ["structure"])
    opts = _options(kwargs)
    staged = _stage_inputs(manager, inputs, opts["resume"])
    _add_prepare_steps(manager, staged, opts, with_ions=kwargs.get("ions", False))


def _build_simple_md_workflow(manager, input_files, config, **kwargs):
    inputs = _require_inputs(input_files, ["structure", "topology", "mdp"])
    opts = _options(kwargs)
    staged = _stage_inputs(manager, inputs, opts["resume"])
    gmx = manager.gmx_cmd
    nsteps = kwargs.get("nsteps")
    tpr = "md_grompp.tpr" if nsteps is not None else "md.tpr"
    grompp = gmx + ["grompp", "-f", staged["mdp"].name, "-c", staged["structure"].name,
                    "-p", staged["topology"].name, "-o", tpr, "-maxwarn", str(opts["maxwarn"])]
    if "index" in staged:
        grompp += ["-n", staged["index"].name]
    manager.add_step(WorkflowStep("grompp", grompp, outputs=[tpr], pre_check=_includes_check(
        manager.output_dir, staged["topology"].name, staged["mdp"].name)))
    last = "grompp"
    if nsteps is not None:
        # mdrun -nsteps is deprecated; set the step count in the run input instead
        manager.add_step(WorkflowStep("set_nsteps", gmx + ["convert-tpr", "-s", tpr, "-o", "md.tpr",
                                                           "-nsteps", str(nsteps)],
                                      outputs=["md.tpr"], depends_on=["grompp"]))
        last = "set_nsteps"
    mdrun_timeout = manager.config["execution"].get("mdrun_timeout", 3600)
    manager.add_step(WorkflowStep("mdrun", _mdrun(gmx, "md", opts["threads"], 0), reads=["md.tpr"],
                                  outputs=["md.gro", "md.edr", "md.log"], depends_on=[last], timeout=mdrun_timeout))


def _build_analysis_workflow(manager, input_files, config, **kwargs):
    inputs = _require_inputs(input_files, ["structure", "trajectory"])
    opts = _options(kwargs)
    staged = _stage_inputs(manager, inputs, opts["resume"])
    gmx = manager.gmx_cmd
    group = str(kwargs.get("group", "System"))
    struct, traj = staged["structure"].name, staged["trajectory"].name
    manager.add_step(WorkflowStep(
        "rms", gmx + ["rms", "-s", struct, "-f", traj, "-o", "rms.xvg", "-tu", "ns"],
        stdin_text=f"{group}\n{group}\n", outputs=["rms.xvg"]))
    manager.add_step(WorkflowStep(
        "rmsf", gmx + ["rmsf", "-s", struct, "-f", traj, "-o", "rmsf.xvg", "-res"],
        stdin_text=f"{group}\n", outputs=["rmsf.xvg"]))
    if "energy" in staged:
        terms = list(kwargs.get("energy_terms") or ["Potential", "Temperature"])
        manager.add_step(WorkflowStep(
            "energy", gmx + ["energy", "-f", staged["energy"].name, "-o", "energy.xvg"],
            stdin_text="\n".join(terms + ["0"]) + "\n", outputs=["energy.xvg"]))


def _build_demo_workflow(manager, input_files, config, **kwargs):
    gmx = manager.gmx_cmd
    manager.add_step(WorkflowStep("gmx_version", gmx + ["--version"]))
    manager.add_step(WorkflowStep("gmx_help", gmx + ["help", "commands"]))


def _options(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    stages = kwargs.get("stages") or ["em", "nvt", "npt", "production_tpr"]
    return {
        "force_field": kwargs.get("force_field", "oplsaa"),
        "water_model": kwargs.get("water_model", "spce"),
        "box_distance": kwargs.get("box_distance", 1.0),
        "box_type": kwargs.get("box_type", "cubic"),
        "set_box": kwargs.get("set_box", True),
        "solvate": kwargs.get("solvate", True),
        "ions": kwargs.get("ions", True),
        "salt_concentration": kwargs.get("salt_concentration", 0.15),
        "em_steps": kwargs.get("em_steps", 5000),
        "nvt_ps": kwargs.get("nvt_ps", 100.0),
        "npt_ps": kwargs.get("npt_ps", 100.0),
        "production_ps": kwargs.get("production_ps", 1000.0),
        "temperature": kwargs.get("temperature", 300.0),
        "pressure": kwargs.get("pressure", 1.0),
        "position_restraints": kwargs.get("position_restraints", True),
        "maxwarn": kwargs.get("maxwarn", 0),
        "threads": kwargs.get("threads") or default_thread_count(),
        "resume": kwargs.get("resume", True),
        "stages": stages,
    }


_BUILDERS = {
    "full_md": _build_full_md_workflow,
    "simple_md": _build_simple_md_workflow,
    "analysis": _build_analysis_workflow,
    "preprocessing": _build_preprocessing_workflow,
}


def run_gromacs_workflow(
    workflow_type: str,
    input_files: Optional[Dict[str, Union[str, Path]]] = None,
    output_file: Optional[Union[str, Path]] = None,
    config: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Execute a predefined workflow.

    Args:
        workflow_type: full_md, simple_md, analysis, preprocessing or demo
        input_files: inputs by role, for example {"structure": "protein.pdb"}; see list_workflow_types
        output_file: output directory (reuse it to resume)
        config: configuration override
        **kwargs: verbose, continue_on_error, resume (default True) and workflow options:
            force_field, water_model, box_distance, box_type, set_box (False keeps the input box), solvate, ions, salt_concentration,
            em_steps, nvt_ps, npt_ps, production_ps, temperature, pressure, maxwarn, threads,
            stages (subset of em, nvt, npt, production_tpr), nsteps (simple_md), group and
            energy_terms (analysis)

    Returns:
        dict with result (bool), step_results, completed/skipped/failed counts, warnings,
        output_dir, output_files (absolute paths) and for full_md the final .tpr/.gro paths
    """
    config = deep_merge(DEFAULT_CONFIG, config)
    verbose = kwargs.get("verbose", False)
    continue_on_error = kwargs.get("continue_on_error", config["execution"]["continue_on_error"])
    resume = kwargs.get("resume", True)
    if verbose:
        logger.setLevel(logging.DEBUG)

    workflow_config = config["workflows"].get(workflow_type)
    if not workflow_config:
        available = ", ".join(config["workflows"])
        return {"result": False, "overall_success": False,
                "error": f"Unknown workflow type: {workflow_type}. Available: {available}"}

    missing = [r for r in workflow_config["required_inputs"] if not (input_files or {}).get(r)]
    if missing:
        return {"result": False, "overall_success": False, "workflow_type": workflow_type,
                "error": f"Required input(s) not provided: {', '.join(missing)}. "
                         f"{workflow_type} needs {', '.join(workflow_config['required_inputs'])}"}

    if workflow_type in ("full_md", "preprocessing"):
        try:
            _validate_names(_options(kwargs))
        except ValueError as e:
            return {"result": False, "overall_success": False, "workflow_type": workflow_type, "error": str(e)}

    output_dir = ensure_output_directory(output_file or f"{workflow_type}_output").resolve()
    logger.info("Running workflow %s in %s", workflow_type, output_dir)

    try:
        manager = WorkflowManager(output_dir, config)
        if workflow_type == "demo":
            _build_demo_workflow(manager, input_files, workflow_config, **kwargs)
        else:
            _BUILDERS[workflow_type](manager, input_files, workflow_config, **kwargs)

        results = manager.execute_workflow(continue_on_error=continue_on_error, resume=resume, verbose=verbose)
        results.update({
            "result": results["overall_success"],
            "workflow_type": workflow_type,
            "output_dir": str(output_dir),
            "input_files": {k: str(v) for k, v in (input_files or {}).items()},
        })
        failed = next((s for s in results["step_results"] if not s["success"]), None)
        if failed:
            results["error"] = f"Step '{failed['name']}' failed: {failed.get('error', 'unknown error')}"

        final = {}
        for name, key in (("md.tpr", "final_tpr"), ("md.gro", "final_gro"), ("npt.gro", "equilibrated_gro")):
            if (output_dir / name).exists():
                final[key] = str(output_dir / name)
        prepared_top = getattr(manager, "prepared_topology", None)
        if prepared_top and (output_dir / prepared_top).exists():
            final["final_topology"] = str(output_dir / prepared_top)
        results.update(final)
        results["output_files"] = sorted(str(p) for p in output_dir.iterdir()
                                         if p.is_file() and not p.name.startswith(("#", ".")))
        write_json_atomic(output_dir / "workflow_result.json", results)
        return results

    except (ValueError, FileNotFoundError, GromacsNotFoundError) as e:
        return {"result": False, "overall_success": False, "workflow_type": workflow_type,
                "error": str(e), "output_dir": str(output_dir)}
    except Exception as e:
        logger.error("Error during workflow execution: %s", e)
        return {"result": False, "overall_success": False, "workflow_type": workflow_type,
                "error": str(e), "output_dir": str(output_dir)}


# ==============================================================================
# CLI Interface
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--workflow', '-w', required=True, choices=list(DEFAULT_CONFIG["workflows"]),
                        help='Workflow type')
    parser.add_argument('--structure', '-s', help='Structure file (.pdb or .gro)')
    parser.add_argument('--topology', '-p', help='Topology file (.top)')
    parser.add_argument('--mdp', '-m', help='MDP file (simple_md)')
    parser.add_argument('--trajectory', '-f', help='Trajectory file (analysis)')
    parser.add_argument('--energy', '-e', help='Energy file (analysis)')
    parser.add_argument('--output', '-o', help='Output directory')
    parser.add_argument('--result-json', help='Write the result dict as JSON here')
    parser.add_argument('--no-resume', action='store_true', help='Run all steps even if finished before')
    parser.add_argument('--option', action='append', metavar='KEY=VALUE',
                        help='Workflow option, for example --option nvt_ps=50 (repeatable)')
    parser.add_argument('--config', '-c', help='Config file (JSON format)')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args()

    config = None
    if args.config:
        with open(args.config) as f:
            config = json.load(f)

    input_files = {k: v for k, v in dict(structure=args.structure, topology=args.topology, mdp=args.mdp,
                                         trajectory=args.trajectory, energy=args.energy).items() if v}
    kwargs: Dict[str, Any] = {"verbose": args.verbose, "resume": not args.no_resume}
    for item in args.option or []:
        key, _, value = item.partition("=")
        try:
            kwargs[key] = json.loads(value)
        except ValueError:
            kwargs[key] = value

    result = run_gromacs_workflow(args.workflow, input_files or None, args.output, config=config, **kwargs)
    if args.result_json:
        write_json_atomic(args.result_json, result)
    if result["result"]:
        print(f"Success: workflow {args.workflow} completed "
              f"({result['completed_steps']} run, {result['skipped_steps']} skipped)")
        print(f"  Output directory: {result['output_dir']}")
        return 0
    print(f"Failed: {result.get('error', 'workflow failed')}")
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
