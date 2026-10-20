"""MCP resources (job and example files) and prompt templates.

Resources let a client read job outputs through the protocol instead of absolute paths on the
server machine. Only text files are served, only from a job's own folder or its output
directory, and only by plain file name.
"""

import json
import re
from pathlib import Path
from typing import List

from jobs.manager import job_manager
from utils import get_example_data_path

TEXT_SUFFIXES = {".log", ".txt", ".mdp", ".gro", ".top", ".itp", ".xvg", ".ndx", ".json", ".out", ".pdb"}
MAX_BYTES = 200_000
_JOB_ID = re.compile(r"^[0-9a-f]{8}$")
_EXAMPLE_NAMES = ("water.gro", "topol.top", "em.mdp", "md.mdp")


def _job_dirs(job_id: str) -> List[Path]:
    if not _JOB_ID.match(job_id):
        raise ValueError("Invalid job id")
    meta = job_manager._load(job_id)
    if not meta:
        raise ValueError(f"Job {job_id} not found")
    dirs = [job_manager.jobs_dir / job_id]
    if meta.get("output_dir"):
        dirs.append(Path(meta["output_dir"]))
    return dirs


def _read_text_limited(path: Path) -> str:
    size = path.stat().st_size
    if size <= MAX_BYTES:
        return path.read_text(errors="replace")
    with open(path, "rb") as f:
        head = f.read(MAX_BYTES // 2)
        f.seek(size - MAX_BYTES // 2)
        tail = f.read()
    return (head.decode(errors="replace") + f"\n... [{size - MAX_BYTES} bytes omitted] ...\n"
            + tail.decode(errors="replace"))


def job_files(job_id: str) -> str:
    """List the readable files of a job as JSON (name, size, folder)."""
    entries = []
    for d in _job_dirs(job_id):
        if d.is_dir():
            for p in sorted(d.iterdir()):
                if p.is_file() and not p.name.startswith("."):
                    entries.append({"name": p.name, "size": p.stat().st_size, "folder": str(d),
                                    "readable": p.suffix.lower() in TEXT_SUFFIXES})
    return json.dumps({"job_id": job_id, "files": entries}, indent=2)


def job_file(job_id: str, filename: str) -> str:
    """Read a text file (md.log, job.log, output.json, .xvg, .mdp, .gro, ...) from a job."""
    if filename != Path(filename).name or filename.startswith("."):
        raise ValueError("Use a plain file name without folders")
    if Path(filename).suffix.lower() not in TEXT_SUFFIXES:
        raise ValueError(f"Only text files can be read ({', '.join(sorted(TEXT_SUFFIXES))})")
    for d in _job_dirs(job_id):
        candidate = d / filename
        if candidate.is_file() and candidate.resolve().parent == d.resolve():
            return _read_text_limited(candidate)
    raise ValueError(f"{filename} not found for job {job_id}")


def example_file(name: str) -> str:
    """Read one of the bundled example files (water.gro, topol.top, em.mdp, md.mdp)."""
    if name not in _EXAMPLE_NAMES:
        raise ValueError(f"Unknown example file. Available: {', '.join(_EXAMPLE_NAMES)}")
    path = get_example_data_path() / name
    if not path.is_file():
        raise ValueError("Example data missing: run python scripts/make_example_data.py")
    return path.read_text()


def setup_water_box(box_nm: float = 3.0, temperature_k: float = 300.0, production_ps: float = 100.0) -> str:
    """Set up, equilibrate and analyze a box of SPC/E water."""
    return (
        f"Simulate a {box_nm} nm cubic box of SPC/E water at {temperature_k} K using the gromacs tools.\n"
        "1. check_environment, to make sure GROMACS runs.\n"
        "2. Write topol.top with `#include \"oplsaa.ff/forcefield.itp\"`, `#include \"oplsaa.ff/spce.itp\"`, a [ system ] "
        "and an empty [ molecules ] section, then run_gromacs_command('solvate', "
        f"parameters=['-cs','spc216.gro','-box','{box_nm}','{box_nm}','{box_nm}'], ...) with -p topol.top.\n"
        "3. run_minimization on the result.\n"
        f"4. create_mdp('npt', time_ps={production_ps}, temperature={temperature_k}), run_grompp, then "
        "submit_md_simulation and follow it with get_job_status.\n"
        "5. energy_summary on the .edr (look at equilibration_time_ps), analyze_rdf with reference "
        "'name OW', analyze_msd with selection 'name OW'.\n"
        "Report density, temperature, the RDF first peak, and the diffusion coefficient with its error, "
        "and say how long the run was relative to what is needed for converged numbers."
    )


def equilibrate_protein(structure: str = "protein.pdb", force_field: str = "amber99sb-ildn",
                        water_model: str = "tip3p") -> str:
    """Equilibrate a protein in water and check the result."""
    return (
        f"Prepare and equilibrate {structure} with the gromacs tools.\n"
        "1. check_environment and list_force_fields (confirm the force field exists).\n"
        f"2. run_full_md_pipeline(structure='{structure}', force_field='{force_field}', "
        f"water_model='{water_model}', nvt_ps=100, npt_ps=100). Read the returned warnings; do not raise "
        "maxwarn unless you understand each warning.\n"
        "3. When it succeeds, trajectory_report on npt.xtc with npt.edr, and energy_summary on npt.edr "
        "(temperature, pressure, density should be stable after equilibration_time_ps).\n"
        "4. Submit the production md.tpr with submit_md_simulation and monitor get_job_status."
    )


def analyze_trajectory_rmsd(trajectory: str = "md.xtc", structure: str = "md.tpr") -> str:
    """RMSD and RMSF of a protein trajectory."""
    return (
        f"Analyze {trajectory} with reference {structure}.\n"
        "1. trajectory_report to confirm frames and time range and see any problems.\n"
        "2. trjconv_preset with preset 'whole' then 'fit' (fit group Backbone, output group Protein) "
        "to remove periodic jumps and rotation.\n"
        "3. run_gromacs_command('rms', ...) with selections ['Backbone','Backbone'] and -tu ns, then "
        "read_xvg on the result and plot_xvg.\n"
        "4. run_gromacs_command('rmsf', ...) with selections ['Backbone'] and ['-res'] for per residue "
        "fluctuations.\n"
        "Summarize whether the RMSD plateaus and which residues fluctuate most."
    )


def register(mcp, registry) -> None:
    mcp.resource("gromacs://jobs/{job_id}", mime_type="application/json")(job_files)
    mcp.resource("gromacs://jobs/{job_id}/{filename}", mime_type="text/plain")(job_file)
    mcp.resource("gromacs://examples/{name}", mime_type="text/plain")(example_file)
    for fn in (setup_water_box, equilibrate_protein, analyze_trajectory_rmsd):
        mcp.prompt()(fn)
