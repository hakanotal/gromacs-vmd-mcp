"""System preparation tools: MDP creation and validation, grompp, minimization,
extending runs, and force field discovery."""

import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from gromacs_command import _check_parameter_names, _check_parameter_paths, check_forbidden_options
from lib import mdp_templates
from lib.gromacs_utils import (
    check_grompp_includes,
    GromacsNotFoundError,
    PathNotAllowedError,
    default_thread_count,
    find_gromacs_command,
    list_force_fields as _list_force_fields,
    parse_grompp_messages,
    resolve_in_workdir,
    run_command_full,
    summarize_gmx_error,
    unique_name,
)
from utils import format_tool_result, get_results_dir


def _resolve(path: str) -> Path:
    return resolve_in_workdir(path)


def _existing(path: Optional[str], label: str) -> Optional[Path]:
    if not path:
        return None
    p = _resolve(path)
    if not p.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
    return p


def create_mdp(
    stage: str,
    output_file: Optional[str] = None,
    time_ps: Optional[float] = None,
    temperature: Optional[float] = None,
    pressure: Optional[float] = None,
    overrides: Optional[dict] = None,
) -> dict:
    """
    Generate a sensible .mdp file for a simulation stage and explain each setting.

    Args:
        stage: em (minimization), ions (input for genion), nvt, npt, nve, free_energy, umbrella
        output_file: where to write the .mdp (optional; the text is always returned)
        time_ps: simulated time; sets nsteps from dt
        temperature: target temperature in K (ref-t and gen-temp)
        pressure: target pressure in bar (ref-p)
        overrides: any mdp options to set or replace, for example
            {"nsteps": 1000, "tau_t": 0.5, "nstxout-compressed": 1000}; a value of null removes the option

    Returns:
        mdp_text, params, explanations (why each option has its value), issues
        (lint results: error, warning, note) and output_file when written.

    Example:
        create_mdp("npt", output_file="npt.mdp", time_ps=100, temperature=310)
    """
    try:
        text, params = mdp_templates.render_stage(
            stage, overrides=overrides, time_ps=time_ps, temperature=temperature, pressure=pressure)
    except ValueError as e:
        return format_tool_result(False, error_message=str(e))

    issues = mdp_templates.lint_params(params)
    warnings = [f"{i['key']}: {i['message']}" for i in issues if i["level"] in ("warning", "error")]
    data: Dict[str, Any] = {
        "stage": stage,
        "mdp_text": text,
        "params": params,
        "explanations": mdp_templates.explain(params),
        "issues": issues,
    }
    out_files: List[str] = []
    if output_file:
        try:
            path = _resolve(output_file)
        except PathNotAllowedError as e:
            return format_tool_result(False, error_message=str(e))
        if path.exists():
            warnings.append(f"{path.name} existed and was overwritten")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        data["output_file"] = str(path)
        out_files.append(str(path))
    ok = not any(i["level"] == "error" for i in issues)
    return format_tool_result(ok, data=data, output_files=out_files, warnings=warnings,
                              error_message="The generated parameters contain errors; see issues")


def validate_mdp(
    mdp_file: Optional[str] = None,
    mdp_text: Optional[str] = None,
    structure: Optional[str] = None,
    topology: Optional[str] = None,
    index_file: Optional[str] = None,
) -> dict:
    """
    Check an .mdp file before submitting a run.

    Always runs lint rules: nsteps times dt, coupling during minimization, Berendsen
    barostat, short tau-p, missing tc-grps, gen-vel with continuation, no trajectory
    output, and so on. When structure and topology are also given, grompp is run in a
    temporary folder so real errors and warnings from GROMACS are returned too.

    Args:
        mdp_file: path to an .mdp file (or give mdp_text)
        mdp_text: the .mdp contents
        structure: .gro or .pdb for the grompp check (optional)
        topology: .top for the grompp check (optional)
        index_file: .ndx for the grompp check (optional)

    Returns:
        lint issues, and when grompp ran: grompp_ok, grompp_errors, grompp_warnings, grompp_notes.
    """
    try:
        if mdp_file:
            mdp_path = _existing(mdp_file, "mdp file")
            text = mdp_path.read_text()
        elif mdp_text:
            mdp_path, text = None, mdp_text
        else:
            return format_tool_result(False, error_message="Give mdp_file or mdp_text")
        struct_p = _existing(structure, "structure")
        top_p = _existing(topology, "topology")
        ndx_p = _existing(index_file, "index file")
    except (FileNotFoundError, PathNotAllowedError) as e:
        return format_tool_result(False, error_message=str(e))

    params = mdp_templates.parse_mdp(text)
    issues = mdp_templates.lint_params(params)
    data: Dict[str, Any] = {"params": params, "issues": issues}
    warnings = [f"{i['key']}: {i['message']}" for i in issues if i["level"] == "warning"]
    ok = not any(i["level"] == "error" for i in issues)

    if struct_p and top_p:
        try:
            gmx = find_gromacs_command()
        except GromacsNotFoundError as e:
            return format_tool_result(False, error_message=str(e))
        tmp_root = get_results_dir() / "tmp"  # inside GMX_MCP_WORKDIR when a sandbox is set
        tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
            tmpdir = Path(tmp)
            mdp_in = mdp_path or (tmpdir / "input.mdp")
            if mdp_path is None:
                mdp_in.write_text(text)
            try:
                check_grompp_includes(topology=top_p, mdp=mdp_in, cwd=tmpdir)
            except PathNotAllowedError as e:
                return format_tool_result(False, error_message=str(e))
            cmd = gmx + ["grompp", "-f", str(mdp_in), "-c", str(struct_p), "-p", str(top_p),
                         "-o", str(tmpdir / "check.tpr"), "-po", str(tmpdir / "mdout.mdp"), "-maxwarn", "0"]
            if ndx_p:
                cmd += ["-n", str(ndx_p)]
            res = run_command_full(cmd, working_dir=tmpdir, timeout=120)
        msgs = parse_grompp_messages(res["stderr"])
        data.update({
            "grompp_ok": res["success"],
            "grompp_errors": msgs["errors"],
            "grompp_warnings": msgs["warnings"],
            "grompp_notes": msgs["notes"],
            "command": res["command"],
        })
        warnings += [f"grompp: {w}" for w in msgs["warnings"]]
        if not res["success"]:
            ok = False
            if not msgs["errors"] and not msgs["warnings"]:
                data["grompp_error_text"] = summarize_gmx_error(res["stderr"])

    return format_tool_result(ok, data=data, warnings=warnings,
                              error_message="Validation found errors; see issues and grompp_errors")


def run_grompp(
    mdp_file: str,
    structure: str,
    topology: str,
    output_tpr: Optional[str] = None,
    index_file: Optional[str] = None,
    restraint_reference: Optional[str] = None,
    checkpoint: Optional[str] = None,
    maxwarn: int = 0,
    extra_parameters: Optional[List[str]] = None,
) -> dict:
    """
    Run grompp to produce a .tpr, returning every NOTE, WARNING and ERROR GROMACS printed.

    Args:
        mdp_file: simulation parameters (.mdp)
        structure: starting coordinates (-c)
        topology: topology (-p)
        output_tpr: where to write the .tpr (default: a new file under results/grompp/)
        index_file: index groups (-n)
        restraint_reference: reference coordinates for position restraints (-r)
        checkpoint: checkpoint to take velocities from (-t)
        maxwarn: number of warnings to tolerate. Default 0. Raising it is a deliberate
            choice: read the warnings first (they are returned even when this fails)
        extra_parameters: extra grompp arguments, one per list item

    Returns:
        output_tpr, grompp_errors, grompp_warnings, grompp_notes. When grompp stops because of
        warnings, the error says so and lists them.
    """
    try:
        mdp_p = _existing(mdp_file, "mdp file")
        struct_p = _existing(structure, "structure")
        top_p = _existing(topology, "topology")
        ndx_p = _existing(index_file, "index file")
        ref_p = _existing(restraint_reference, "restraint reference")
        cpt_p = _existing(checkpoint, "checkpoint")
        out_p = _resolve(output_tpr) if output_tpr else get_results_dir() / "grompp" / unique_name(mdp_p.stem, ".tpr")
        gmx = find_gromacs_command()
    except (FileNotFoundError, PathNotAllowedError, GromacsNotFoundError) as e:
        return format_tool_result(False, error_message=str(e))
    if int(maxwarn) < 0:
        return format_tool_result(False, error_message="maxwarn must be 0 or greater")

    extra = [str(x) for x in (extra_parameters or [])]
    managed = {"-f", "-c", "-p", "-o", "-po", "-n", "-r", "-t", "-maxwarn"}
    clash = sorted(managed.intersection(extra))
    if clash:
        return format_tool_result(False, error_message=f"Pass {', '.join(clash)} through the named arguments, "
                                                       "not extra_parameters")
    try:
        check_forbidden_options(extra)
        _check_parameter_names(extra)
        _check_parameter_paths(extra, out_p.parent)
        check_grompp_includes(topology=top_p, mdp=mdp_p, cwd=out_p.parent)
    except ValueError as e:  # PathNotAllowedError is a ValueError too
        return format_tool_result(False, error_message=str(e))

    out_p.parent.mkdir(parents=True, exist_ok=True)
    cmd = gmx + ["grompp", "-f", str(mdp_p), "-c", str(struct_p), "-p", str(top_p), "-o", str(out_p),
                 "-po", str(out_p.with_name(out_p.stem + "_mdout.mdp")), "-maxwarn", str(int(maxwarn))]
    for flag, p in (("-n", ndx_p), ("-r", ref_p), ("-t", cpt_p)):
        if p:
            cmd += [flag, str(p)]
    cmd += extra

    res = run_command_full(cmd, working_dir=out_p.parent, timeout=300)
    msgs = parse_grompp_messages(res["stderr"])
    data = {
        "output_tpr": str(out_p) if out_p.exists() else None,
        "grompp_errors": msgs["errors"],
        "grompp_warnings": msgs["warnings"],
        "grompp_notes": msgs["notes"],
        "maxwarn": int(maxwarn),
        "command": res["command"],
        "stderr": res["stderr"] if not res["success"] else "",
    }
    warnings = [f"grompp warning: {w}" for w in msgs["warnings"]]
    if res["success"]:
        if int(maxwarn) > 0 and msgs["warnings"]:
            warnings.append(f"maxwarn={maxwarn} allowed {len(msgs['warnings'])} warning(s) through")
        return format_tool_result(True, data=data, output_files=[str(out_p)], warnings=warnings)

    if re.search(r"Too many warnings", res["stderr"]):
        error = (f"grompp stopped on {len(msgs['warnings'])} warning(s). Fix them, or pass maxwarn="
                 f"{len(msgs['warnings'])} if you understand them:\n" + "\n".join(msgs["warnings"]))
    else:
        error = summarize_gmx_error(res["stderr"]) or "grompp failed"
    return format_tool_result(False, data=data, error_message=error, warnings=warnings)


def _parse_em_log(text: str) -> Dict[str, Any]:
    info: Dict[str, Any] = {}
    m = re.search(r"(Steepest Descents|Polak-Ribiere Conjugate Gradients|Low-Memory BFGS Minimizer)\s+"
                  r"(converged|did not converge)\s+to\s+Fmax\s*<\s*([\d.eE+-]+)\s+in\s+(\d+)\s+steps", text)
    if m:
        info["algorithm"] = m.group(1)
        info["converged"] = m.group(2) == "converged"
        info["emtol"] = float(m.group(3))
        info["steps"] = int(m.group(4))
    m = re.search(r"Potential Energy\s*=\s*([-\d.eE+]+)", text)
    if m:
        info["potential_energy_kj_mol"] = float(m.group(1))
    m = re.search(r"Maximum force\s*=\s*([-\d.eE+]+)\s+on atom\s+(\d+)", text)
    if m:
        info["max_force"] = float(m.group(1))
        info["max_force_atom"] = int(m.group(2))
    return info


def run_minimization(
    structure: str,
    topology: str,
    output_dir: Optional[str] = None,
    mdp_file: Optional[str] = None,
    emtol: float = 1000.0,
    nsteps: int = 5000,
    index_file: Optional[str] = None,
    threads: Optional[int] = None,
    maxwarn: int = 0,
    timeout: int = 900,
) -> dict:
    """
    Energy minimize a system (grompp then mdrun) and report how it went.

    Runs in its own output folder, so GROMACS side files (such as step*.pdb dumps written
    when constraints fail) stay there and are reported as warnings.

    Args:
        structure: starting coordinates (.gro or .pdb)
        topology: topology (.top)
        output_dir: working folder (default: a new folder under results/minimization/)
        mdp_file: your own minimization .mdp; by default one is generated from the em template
        emtol: stop when the largest force is below this (kJ/mol/nm); used for the generated .mdp
        nsteps: maximum steps; used for the generated .mdp
        index_file: optional index file
        threads: OpenMP threads (default: CPU count, at most 8). One rank is used
        maxwarn: grompp warnings to tolerate (default 0)
        timeout: seconds before mdrun is stopped

    Returns:
        converged, steps, potential_energy_kj_mol, max_force, max_force_atom, output_dir and
        the paths of em.gro, em.tpr, em.edr, em.log.
    """
    try:
        struct_p = _existing(structure, "structure")
        top_p = _existing(topology, "topology")
        ndx_p = _existing(index_file, "index file")
        mdp_user = _existing(mdp_file, "mdp file")
        out_dir = _resolve(output_dir) if output_dir else get_results_dir() / "minimization" / unique_name(struct_p.stem)
        check_grompp_includes(topology=top_p, mdp=mdp_user, cwd=out_dir)
        gmx = find_gromacs_command()
    except (FileNotFoundError, PathNotAllowedError, GromacsNotFoundError) as e:
        return format_tool_result(False, error_message=str(e))

    out_dir.mkdir(parents=True, exist_ok=True)
    warnings: List[str] = []
    if mdp_user:
        mdp_p = mdp_user
    else:
        text, _ = mdp_templates.render_stage("em", overrides={"emtol": emtol, "nsteps": nsteps})
        mdp_p = out_dir / "em.mdp"
        mdp_p.write_text(text)

    cmd = gmx + ["grompp", "-f", str(mdp_p), "-c", str(struct_p), "-p", str(top_p), "-o", "em.tpr",
                 "-po", "em_mdout.mdp", "-maxwarn", str(int(maxwarn))]
    if ndx_p:
        cmd += ["-n", str(ndx_p)]
    g = run_command_full(cmd, working_dir=out_dir, timeout=300)
    msgs = parse_grompp_messages(g["stderr"])
    warnings += [f"grompp warning: {w}" for w in msgs["warnings"]]
    if not g["success"]:
        if "Too many warnings" in g["stderr"]:
            err = "grompp stopped on warnings (set maxwarn to continue):\n" + "\n".join(msgs["warnings"])
        else:
            err = summarize_gmx_error(g["stderr"]) or "grompp failed"
        return format_tool_result(False, data={"output_dir": str(out_dir), "grompp_errors": msgs["errors"],
                                               "command": g["command"]},
                                  error_message=err, warnings=warnings)

    threads = threads or default_thread_count()
    m = run_command_full(gmx + ["mdrun", "-deffnm", "em", "-ntmpi", "1", "-ntomp", str(threads)],
                         working_dir=out_dir, timeout=timeout)
    log_path = out_dir / "em.log"
    info = _parse_em_log(log_path.read_text(errors="replace")) if log_path.exists() else {}
    if not m["success"]:
        return format_tool_result(False, data={"output_dir": str(out_dir), **info, "command": m["command"]},
                                  error_message=("mdrun timed out" if m["timed_out"]
                                                 else summarize_gmx_error(m["stderr"]) or "mdrun failed"),
                                  warnings=warnings)

    stray = sorted(p.name for p in out_dir.glob("step*.pdb"))
    if stray:
        warnings.append(f"{len(stray)} step*.pdb files written (SETTLE or LINCS could not satisfy a "
                        "constraint during large early steps). Usually harmless in minimization; "
                        "check that it converged")
    if info and not info.get("converged"):
        warnings.append("Minimization did not reach the force tolerance; continue it or loosen emtol")
    paths = [str(out_dir / n) for n in ("em.gro", "em.tpr", "em.edr", "em.log") if (out_dir / n).exists()]
    return format_tool_result(True, data={**info, "output_dir": str(out_dir), "output_gro": str(out_dir / "em.gro")},
                              output_files=paths, warnings=warnings)


def extend_simulation(
    input_tpr: str,
    output_tpr: str,
    extend_ps: Optional[float] = None,
    until_ps: Optional[float] = None,
    nsteps: Optional[int] = None,
) -> dict:
    """
    Make a longer .tpr from an existing one (gmx convert-tpr), to continue a finished run.

    Give exactly one of:
        extend_ps: add this much time (ps)
        until_ps: new absolute end time (ps)
        nsteps: new total number of steps

    Then call submit_md_simulation with the new .tpr, resume=True and the same output_dir
    (the folder that holds md.cpt) to continue from the checkpoint.
    """
    from md_simulation import extend_tpr

    try:
        in_p = _existing(input_tpr, "input tpr")
        out_p = _resolve(output_tpr)
    except (FileNotFoundError, PathNotAllowedError) as e:
        return format_tool_result(False, error_message=str(e))
    res = extend_tpr(in_p, out_p, extend_ps=extend_ps, until_ps=until_ps, nsteps=nsteps)
    if res["result"]:
        return format_tool_result(True, data={"output_tpr": res["output_file"], "command": res["command"]},
                                  output_files=[res["output_file"]])
    return format_tool_result(False, error_message=res.get("error"), data={"command": res.get("command")})


def list_force_fields() -> dict:
    """
    List the force fields installed with GROMACS and the water models each supports.

    Use the name with pdb2gmx (-ff) in run_gromacs_command, or force_field in the
    full_md workflow. Returns name, description and water_models per force field.
    """
    fields = _list_force_fields()
    if not fields:
        return format_tool_result(False, error_message="Could not locate the GROMACS force field directory "
                                                       "(set GMXLIB or check the installation)")
    return format_tool_result(True, data={"force_fields": fields, "total": len(fields)})


def register(mcp, registry) -> None:
    for fn in (create_mdp, validate_mdp, run_minimization, extend_simulation, list_force_fields):
        registry.tool("prepare")(fn)
    registry.tool("sync")(run_grompp)
