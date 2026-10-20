"""Pipeline tools: typed wrappers around the multi-step workflows."""

from pathlib import Path
from typing import List, Optional

from gromacs_workflow import list_workflow_types, run_gromacs_workflow
from jobs.manager import job_manager
from lib.gromacs_utils import PathNotAllowedError, resolve_in_workdir, unique_name
from utils import format_tool_result, get_results_dir

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"


def list_workflows() -> dict:
    """
    Describe the available workflows: what they do, their required and optional inputs, and steps.
    """
    return format_tool_result(True, data={"workflows": list_workflow_types()})


def run_full_md_pipeline(
    structure: str,
    output_dir: Optional[str] = None,
    topology: Optional[str] = None,
    force_field: str = "oplsaa",
    water_model: str = "spce",
    box_distance_nm: float = 1.0,
    box_type: str = "cubic",
    set_box: bool = True,
    solvate: bool = True,
    ions: bool = True,
    salt_concentration_molar: float = 0.15,
    em_steps: int = 5000,
    nvt_ps: float = 100.0,
    npt_ps: float = 100.0,
    production_ps: float = 1000.0,
    temperature_k: float = 300.0,
    pressure_bar: float = 1.0,
    stages: Optional[List[str]] = None,
    maxwarn: int = 0,
    resume: bool = True,
    submit_production: bool = False,
) -> dict:
    """
    Prepare, minimize and equilibrate a system, ending with a production-ready md.tpr.

    Steps: pdb2gmx (when no topology is given), editconf, solvate, genion (neutralize and add
    salt), energy minimization, NVT, NPT, then grompp for production. Runs synchronously, so
    keep nvt_ps and npt_ps modest for big systems or use `stages` to run part of it. Run it
    again with the same output_dir to resume: finished steps are skipped (changed inputs
    restart everything).

    Args:
        structure: starting structure (.pdb or .gro)
        output_dir: working folder (default: a new folder under results/full_md/); pass the same folder again to resume
        topology: existing .top; if given pdb2gmx is skipped (sibling .itp files are copied)
        force_field: for pdb2gmx, see list_force_fields
        water_model: spc, spce, tip3p, tip4p or tip5p
        box_distance_nm: solute to box edge distance
        box_type: cubic, dodecahedron, octahedron
        set_box: False keeps the box of the input structure
        solvate: add water (False for an already solvated input)
        ions: add counter ions and salt
        salt_concentration_molar: salt concentration (default 0.15 M)
        em_steps: maximum minimization steps
        nvt_ps / npt_ps: equilibration lengths
        production_ps: length written into md.tpr
        temperature_k / pressure_bar: targets
        stages: subset of ["em", "nvt", "npt", "production_tpr"] (default all four)
        maxwarn: grompp warnings to tolerate (default 0; warnings are reported either way)
        resume: skip steps that already finished
        submit_production: also start the production run as a background job and return its job_id

    Returns:
        step_results, completed_steps, skipped_steps, warnings, final_tpr (md.tpr), equilibrated_gro,
        output_dir and, with submit_production, production_job_id.

    Example:
        run_full_md_pipeline("protein.pdb", nvt_ps=100, npt_ps=100, production_ps=10000)
    """
    try:
        struct = resolve_in_workdir(structure)
        top = resolve_in_workdir(topology) if topology else None
        out = resolve_in_workdir(output_dir) if output_dir else get_results_dir() / "full_md" / unique_name(struct.stem)
    except PathNotAllowedError as e:
        return format_tool_result(False, error_message=str(e))

    inputs = {"structure": str(struct)}
    if top:
        inputs["topology"] = str(top)
    result = run_gromacs_workflow(
        "full_md", input_files=inputs, output_file=str(out), resume=resume, force_field=force_field,
        water_model=water_model, box_distance=box_distance_nm, box_type=box_type, set_box=set_box,
        solvate=solvate, ions=ions, salt_concentration=salt_concentration_molar, em_steps=em_steps,
        nvt_ps=nvt_ps, npt_ps=npt_ps, production_ps=production_ps, temperature=temperature_k,
        pressure=pressure_bar, stages=stages, maxwarn=maxwarn)

    ok = bool(result.get("result"))
    data = dict(result)
    if ok and submit_production:
        tpr = result.get("final_tpr")
        if not tpr:
            return format_tool_result(False, data=data, error_message="No md.tpr was produced; include "
                                                                      "'production_tpr' in stages")
        job = job_manager.submit_job(
            script_path=str(_SCRIPTS_DIR / "md_simulation.py"),
            args={"input": tpr},
            job_name=f"production_{struct.stem}",
            output_dir=str(out / "production"),
            kind="md")
        if job.get("status") == "submitted":
            data["production_job_id"] = job["job_id"]
        else:
            return format_tool_result(False, data=data, error_message=job.get("error"))
    return format_tool_result(ok, data=data, warnings=result.get("warnings"),
                              error_message=result.get("error"))


def register(mcp, registry) -> None:
    registry.tool("sync")(list_workflows)
    registry.tool("sync")(run_full_md_pipeline)
