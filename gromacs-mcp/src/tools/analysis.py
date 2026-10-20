"""Analysis tools that return numbers, not just file paths."""

import math
import re
import statistics
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gromacs_command import run_gromacs_command as _gmx
from lib.gromacs_utils import (
    PathNotAllowedError,
    block_average,
    detect_equilibration as _detect_equilibration,
    parse_check_output,
    parse_energy_menu,
    parse_energy_table,
    parse_msd_diffusion,
    parse_xvg,
    resolve_in_workdir,
    statistical_inefficiency,
    unique_name,
)
from utils import format_tool_result, get_results_dir

_DEFAULT_TERMS = ["Potential", "Temperature", "Pressure", "Density", "Volume"]
_STATE_TERMS = ("Potential", "Temperature", "Density", "Volume")  # used to find the equilibrated segment


def _resolve(path: str) -> Path:
    return resolve_in_workdir(path)


def _existing(path: Optional[str], label: str) -> Path:
    if not path:
        raise FileNotFoundError(f"{label} is required")
    p = _resolve(path)
    if not p.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
    return p


def _out_path(output: Optional[str], default_dir: str, name: str) -> Path:
    """The caller's path, or a fresh unique file under results/<default_dir>/ (fixed default names would
    collide between calls and pile up GROMACS #backup# files until it refuses to write)."""
    if output:
        p = _resolve(output)
    else:
        stem, suffix = Path(name).stem, Path(name).suffix
        p = get_results_dir() / default_dir / unique_name(stem, suffix)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _window(begin_ps: Optional[float], end_ps: Optional[float]) -> List[str]:
    params: List[str] = []
    if begin_ps is not None:
        params += ["-b", str(begin_ps)]
    if end_ps is not None:
        params += ["-e", str(end_ps)]
    return params


def _stats(values: List[float]) -> Dict[str, float]:
    if not values:
        return {}
    ba = block_average(values)
    std = statistics.pstdev(values) if len(values) > 1 else 0.0
    out = {"mean": ba["mean"], "std": std, "sem_blocks": ba["sem"], "n": len(values)}
    g = statistical_inefficiency(values)
    if g is not None:
        # frames are correlated: about n/g of them are independent, and the error of the mean follows from that
        out.update({"sem": std * math.sqrt(g / len(values)), "statistical_inefficiency": round(g, 2),
                    "n_independent": round(len(values) / g, 1)})
    return out


_MIN_INDEPENDENT = 10


def _few_samples_warning(stats: Dict[str, Any]) -> Optional[str]:
    few = []
    for name, entry in stats.items():
        seg = entry.get("equilibrated") or entry.get("full") or {}
        if seg.get("n_independent") is not None and seg["n_independent"] < _MIN_INDEPENDENT:
            few.append(f"{name} ({seg['n_independent']:g})")
    if not few:
        return None
    return (f"Fewer than {_MIN_INDEPENDENT} independent samples for {', '.join(few)}: those averages and "
            "their errors are rough; a longer run gives reliable numbers")


def read_xvg(
    path: str,
    max_points: int = 200,
    begin: Optional[float] = None,
    end: Optional[float] = None,
) -> dict:
    """
    Read an .xvg file (the output format of most gmx analysis tools) into structured data.

    Args:
        path: .xvg file
        max_points: rows returned are downsampled by stride to at most this many
            (statistics always use every row). 0 returns all rows
        begin: keep only rows with x >= begin (x is usually time in ps)
        end: keep only rows with x <= end

    Returns:
        title, xaxis, yaxis, legends, n_rows, n_columns, data (rows of [x, y1, y2, ...]) and
        stats per column (mean, std, min, max).
    """
    try:
        p = _existing(path, "xvg file")
    except (FileNotFoundError, PathNotAllowedError) as e:
        return format_tool_result(False, error_message=str(e))

    text = p.read_text(errors="replace")
    if begin is not None or end is not None:
        kept = []
        for line in text.splitlines():
            if line.startswith(("#", "@")) or not line.strip():
                kept.append(line)
                continue
            try:
                x = float(line.split()[0])
            except ValueError:
                continue
            if (begin is None or x >= begin) and (end is None or x <= end):
                kept.append(line)
        text = "\n".join(kept)
    parsed = parse_xvg(text, max_points=max_points or None)
    if parsed["n_rows"] == 0:
        return format_tool_result(False, error_message="No data rows found (check begin and end)")
    return format_tool_result(True, data={"file": str(p), **parsed})


def _energy_terms(edr: Path) -> Dict[int, str]:
    res = _gmx("energy", {"f": edr}, timeout=120)
    return parse_energy_menu((res.get("stdout") or "") + "\n" + (res.get("stderr") or ""))


def _match_terms(requested: List[str], available: Dict[int, str]) -> Tuple[List[str], List[str]]:
    lookup = {name.lower(): name for name in available.values()}
    matched, missing = [], []
    for req in requested:
        key = req.lower()
        if key in lookup:
            name = lookup[key]
        else:
            candidates = [n for k, n in lookup.items() if k.startswith(key)]
            name = candidates[0] if len(candidates) == 1 else None
        if name and name not in matched:
            matched.append(name)
        elif not name:
            missing.append(req)
    return matched, missing


def energy_summary(
    edr_file: str,
    terms: Optional[List[str]] = None,
    begin_ps: Optional[float] = None,
    end_ps: Optional[float] = None,
    output_xvg: Optional[str] = None,
    detect_equilibration: bool = True,
) -> dict:
    """
    Averages of energy terms with uncertainty, drift and automatic equilibration detection.

    Runs gmx energy for you (no term numbers needed), then reports GROMACS' own average
    table for the chosen window and, from the time series, statistics for both the full
    window and the equilibrated part (found automatically with the MSER-5 rule on the
    potential energy, temperature, density and volume).

    Args:
        edr_file: energy file (.edr)
        terms: term names, default Potential, Temperature, Pressure, Density, Volume.
            Terms that do not exist in the file (for example Density in an NVT run) are
            skipped with a warning and the available terms are listed
        begin_ps: window start in ps
        end_ps: window end in ps
        output_xvg: where to save the time series (default: a new file under results/energy/)
        detect_equilibration: also report equilibrated averages

    Returns:
        gmx_table ({term: average, err_est, rmsd, tot_drift, unit}), stats ({term: full and
        equilibrated mean, std, sem, sem_blocks, statistical_inefficiency, n_independent}; sem
        accounts for correlation between frames, n_independent is the number of effectively
        independent samples, and a warning flags terms with fewer than 10), equilibration_time_ps,
        n_frames, time_range_ps,
        output_xvg, terms_used, terms_missing. With fewer than 20 frames equilibration cannot
        be detected: equilibration_time_ps is null, there is no equilibrated block, and a
        warning says so.

    Example:
        energy_summary("npt.edr", terms=["Potential", "Density"], begin_ps=25)
    """
    try:
        edr = _existing(edr_file, "energy file")
        xvg_out = _out_path(output_xvg, "energy", f"{edr.stem}.xvg")
    except (FileNotFoundError, PathNotAllowedError) as e:
        return format_tool_result(False, error_message=str(e))

    available = _energy_terms(edr)
    if not available:
        return format_tool_result(False, error_message="Could not read the term list from the energy file")
    matched, missing = _match_terms(list(terms or _DEFAULT_TERMS), available)
    warnings = []
    if missing:
        warnings.append(f"Terms not in this file and skipped: {', '.join(missing)}")
    if not matched:
        return format_tool_result(False, data={"available_terms": list(available.values())},
                                  error_message="None of the requested terms exist in the file")

    res = _gmx("energy", {"f": edr}, {"o": xvg_out}, _window(begin_ps, end_ps), selections=matched, timeout=300)
    if not res["result"]:
        return format_tool_result(False, data={"command": res.get("command")}, error_message=res.get("error"))

    table = parse_energy_table(res["stdout"] + "\n" + res["stderr"])
    xvg = parse_xvg(xvg_out.read_text())
    rows = xvg["data"]
    times = [r[0] for r in rows]
    legends = xvg["legends"]

    series: Dict[str, List[float]] = {}
    for c, name in enumerate(legends, start=1):
        series[name] = [r[c] for r in rows if len(r) > c]

    eq_idx: Optional[int] = None
    if detect_equilibration:
        eq_idx, why_not = _equilibration_index(series)
        if why_not:
            warnings.append(why_not)
    eq_time = times[eq_idx] if eq_idx is not None and times else None

    stats: Dict[str, Any] = {}
    for name, vals in series.items():
        entry = {"full": _stats(vals), "unit": table.get(name, {}).get("unit")}
        if eq_idx is not None:
            entry["equilibrated"] = _stats(vals[eq_idx:])
        stats[name] = entry

    data = {
        "terms_used": matched,
        "terms_missing": missing,
        "gmx_table": table,
        "stats": stats,
        "n_frames": len(times),
        "time_range_ps": [times[0], times[-1]] if times else None,
        "output_xvg": str(xvg_out),
        "command": res["command"],
    }
    if detect_equilibration:
        data["equilibration_time_ps"] = eq_time
        if eq_idx:
            warnings.append(f"Equilibrated statistics start at {eq_time:g} ps; the earlier part still drifts")
    few = _few_samples_warning(stats)
    if few:
        warnings.append(few)
    return format_tool_result(True, data=data, output_files=[str(xvg_out)], warnings=warnings)


def _equilibration_index(series: Dict[str, List[float]]) -> Tuple[Optional[int], Optional[str]]:
    """First equilibrated frame over the state terms (the latest of them), or None with the reason
    detection could not run. None is never reported as 0: 0 claims the whole run is equilibrated."""
    state = [n for n in _STATE_TERMS if n in series]
    if not state:
        return None, ("Equilibration not detected: it needs one of " + ", ".join(_STATE_TERMS)
                      + " among the terms")
    idxs = [_detect_equilibration(series[n]) for n in state]
    if any(i is None for i in idxs):
        n = min(len(series[s]) for s in state)
        return None, (f"Equilibration not detected: {n} energy frames are too few (at least 20 are needed). "
                      "Use a longer window or write energies more often (nstenergy)")
    return max(idxs), None


def _first_peak_and_minimum(r: List[float], g: List[float]) -> Dict[str, Optional[float]]:
    """
    First peak: highest point of the first region where g(r) exceeds 1.
    First minimum: first smoothed local minimum after the peak that is below 1; when noise
    leaves none below 1, the lowest smoothed point within 0.3 nm after the peak.
    """
    n = len(g)
    if n < 5:
        return {}
    w = 3
    sm = [statistics.fmean(g[max(0, i - w):i + w + 1]) for i in range(n)]
    start = next((i for i, v in enumerate(sm) if v > 1.0), None)
    if start is None:
        return {}
    end = next((i for i in range(start, n) if sm[i] < 1.0), n)
    peak = max(range(start, end), key=lambda i: g[i])
    minimum = next((i for i in range(peak + 1, n - 1)
                    if sm[i] < 1.0 and sm[i] <= sm[i - 1] and sm[i] <= sm[i + 1]), None)
    if minimum is None:
        window = [i for i in range(peak + 1, n) if r[i] - r[peak] <= 0.3]
        if window:
            minimum = min(window, key=lambda i: sm[i])
    out: Dict[str, Optional[float]] = {"first_peak_r_nm": r[peak], "first_peak_g": g[peak]}
    if minimum is not None:
        out.update({"first_minimum_r_nm": r[minimum], "first_minimum_g": g[minimum]})
    return out


def _interp(xs: List[float], ys: List[float], x: float) -> Optional[float]:
    for i in range(1, len(xs)):
        if xs[i] >= x >= xs[i - 1]:
            span = xs[i] - xs[i - 1]
            return ys[i - 1] if span == 0 else ys[i - 1] + (ys[i] - ys[i - 1]) * (x - xs[i - 1]) / span
    return None


def analyze_rdf(
    trajectory: str,
    structure: str,
    reference: str,
    selection: Optional[str] = None,
    begin_ps: Optional[float] = None,
    end_ps: Optional[float] = None,
    bin_width_nm: float = 0.002,
    rmax_nm: Optional[float] = None,
    output_xvg: Optional[str] = None,
) -> dict:
    """
    Radial distribution function with the first peak, first minimum and coordination number.

    Args:
        trajectory: .xtc or .trr
        structure: .tpr (or .gro/.pdb with atom names)
        reference: GROMACS selection for the reference atoms, for example 'name OW'
        selection: selection for the atoms counted around the reference (default: same as reference)
        begin_ps: skip frames before this time (discard equilibration)
        end_ps: last frame time
        bin_width_nm: histogram bin (default 0.002)
        rmax_nm: largest distance (default: gmx chooses, up to half the box)
        output_xvg: where to save g(r) (default: a new file under results/rdf/); coordination number
            goes next to it as <name>_cn.xvg

    Returns:
        first_peak_r_nm, first_peak_g, first_minimum_r_nm, first_minimum_g, coordination_number
        (integrated up to the first minimum), n_points, output files.

    Example:
        analyze_rdf("npt.xtc", "npt.tpr", reference="name OW", begin_ps=25)
    """
    try:
        traj = _existing(trajectory, "trajectory")
        tpr = _existing(structure, "structure")
        out = _out_path(output_xvg, "rdf", f"{traj.stem}_rdf.xvg")
    except (FileNotFoundError, PathNotAllowedError) as e:
        return format_tool_result(False, error_message=str(e))
    cn_out = out.with_name(out.stem + "_cn.xvg")

    params = _window(begin_ps, end_ps) + ["-bin", str(bin_width_nm), "-ref", reference,
                                          "-sel", selection or reference]
    if rmax_nm:
        params += ["-rmax", str(rmax_nm)]
    res = _gmx("rdf", {"f": traj, "s": tpr}, {"o": out, "cn": cn_out}, params, timeout=900)
    if not res["result"]:
        return format_tool_result(False, data={"command": res.get("command")}, error_message=res.get("error"))

    rdf = parse_xvg(out.read_text())
    r = [row[0] for row in rdf["data"]]
    g = [row[1] for row in rdf["data"]]
    feats = _first_peak_and_minimum(r, g)
    data: Dict[str, Any] = {**feats, "n_points": len(r), "output_xvg": str(out), "command": res["command"]}
    files = [str(out)]
    if cn_out.exists():
        cn = parse_xvg(cn_out.read_text())
        data["coordination_xvg"] = str(cn_out)
        files.append(str(cn_out))
        if feats.get("first_minimum_r_nm") is not None:
            data["coordination_number"] = _interp([row[0] for row in cn["data"]], [row[1] for row in cn["data"]],
                                                  feats["first_minimum_r_nm"])
    warnings = [] if feats else ["No first peak found (g(r) never exceeded 1)"]
    return format_tool_result(True, data=data, output_files=files, warnings=warnings)


def analyze_msd(
    trajectory: str,
    structure: str,
    selection: str,
    begin_ps: Optional[float] = None,
    end_ps: Optional[float] = None,
    trestart_ps: float = 10.0,
    output_xvg: Optional[str] = None,
    temperature_k: Optional[float] = None,
    viscosity_mpas: Optional[float] = None,
    box_length_nm: Optional[float] = None,
) -> dict:
    """
    Mean square displacement and the self-diffusion coefficient from its slope.

    Args:
        trajectory: .xtc or .trr
        structure: .tpr
        selection: atoms to track, for example 'name OW' (water oxygens)
        begin_ps: start of the fitting range; GROMACS fits between 10% and 90% of [begin, end]
        end_ps: end of the range
        trestart_ps: spacing of time origins (default 10 ps; smaller gives better statistics)
        output_xvg: where to save the MSD curve (default: a new file under results/msd/)
        temperature_k, viscosity_mpas, box_length_nm: when all three are given, also returns
            the Yeh-Hummer finite size corrected D = D + 2.837 kT / (6 pi eta L)

    Returns:
        diffusion_coefficient (in 1e-5 cm^2/s), diffusion_error, unit, and when requested
        diffusion_corrected. Short runs and small boxes give rough values: check the error
        and the MSD curve.

    Example:
        analyze_msd("npt.xtc", "npt.tpr", selection="name OW", begin_ps=10)
    """
    try:
        traj = _existing(trajectory, "trajectory")
        tpr = _existing(structure, "structure")
        out = _out_path(output_xvg, "msd", f"{traj.stem}_msd.xvg")
    except (FileNotFoundError, PathNotAllowedError) as e:
        return format_tool_result(False, error_message=str(e))

    params = _window(begin_ps, end_ps) + ["-sel", selection, "-trestart", str(trestart_ps)]
    res = _gmx("msd", {"f": traj, "s": tpr}, {"o": out}, params, timeout=900)
    if not res["result"]:
        return format_tool_result(False, data={"command": res.get("command")}, error_message=res.get("error"))

    fit = parse_msd_diffusion(out.read_text())
    if not fit:
        return format_tool_result(False, data={"output_xvg": str(out)},
                                  error_message="No diffusion coefficient in the msd output; the fitting range may "
                                                "be too short (use more frames or a wider begin/end)")
    d, unit = fit["diffusion_coefficient"], fit["unit"]
    data: Dict[str, Any] = {**fit, "output_xvg": str(out), "command": res["command"]}
    warnings = []
    if all(v is not None for v in (temperature_k, viscosity_mpas, box_length_nm)) and unit.startswith("1e-5"):
        kb = 1.380649e-23
        corr_si = 2.837297 * kb * temperature_k / (6 * math.pi * viscosity_mpas * 1e-3 * box_length_nm * 1e-9)
        data["finite_size_correction"] = corr_si / 1e-9  # m^2/s to units of 1e-5 cm^2/s (= 1e-9 m^2/s)
        data["diffusion_corrected"] = d + data["finite_size_correction"]
    elif any(v is not None for v in (temperature_k, viscosity_mpas, box_length_nm)):
        warnings.append("Finite size correction needs temperature_k, viscosity_mpas and box_length_nm together")
    return format_tool_result(True, data=data, output_files=[str(out)], warnings=warnings)


def trajectory_report(
    trajectory: str,
    structure: Optional[str] = None,
    energy_file: Optional[str] = None,
) -> dict:
    """
    One call sanity report for a finished or running trajectory.

    Reports frames, time range and timestep (gmx check), box size change over the run (gmx
    traj), and, when an energy file is given, energy time series statistics, non-finite
    values and sudden spikes (robust z-score above 10).

    Args:
        trajectory: .xtc or .trr
        structure: .tpr, improves the box analysis (optional)
        energy_file: .edr (optional)

    Returns:
        n_frames, timestep_ps, last_time_ps, natoms, box (initial, final, change_percent per axis),
        energy (stats per term), spikes, non_finite, issues (plain language findings).
    """
    try:
        traj = _existing(trajectory, "trajectory")
        tpr = _existing(structure, "structure") if structure else None
        edr = _existing(energy_file, "energy file") if energy_file else None
    except (FileNotFoundError, PathNotAllowedError) as e:
        return format_tool_result(False, error_message=str(e))

    issues: List[str] = []
    check = _gmx("check", {"f": traj}, timeout=300)
    if not check["result"]:
        return format_tool_result(False, data={"command": check.get("command")}, error_message=check.get("error"))
    info = parse_check_output(check["stderr"] + "\n" + check["stdout"])
    data: Dict[str, Any] = {**info}

    tmp_root = get_results_dir() / "tmp"  # inside GMX_MCP_WORKDIR when a sandbox is set
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
        box_xvg = Path(tmp) / "box.xvg"
        inputs = {"f": traj}
        if tpr:
            inputs["s"] = tpr
        res = _gmx("traj", inputs, {"ob": box_xvg}, selections=["0"], timeout=300, working_directory=tmp)
        if not res["result"]:
            issues.append(f"Box analysis skipped: {res.get('error', '')[:200]}")
        if box_xvg.exists():
            box = parse_xvg(box_xvg.read_text())
            if box["data"]:
                first, last = box["data"][0], box["data"][-1]
                axes = ["x", "y", "z"]
                change = {}
                for i, ax in enumerate(axes, start=1):
                    if len(first) > i and first[i]:
                        change[ax] = round(100 * (last[i] - first[i]) / first[i], 3)
                data["box"] = {"initial_nm": first[1:4], "final_nm": last[1:4], "change_percent": change}
                if any(abs(v) > 10 for v in change.values()):
                    issues.append("Box changed by more than 10% over the run")
        else:
            data["box"] = None

        if edr:
            energy = energy_summary(str(edr), terms=["Potential", "Temperature", "Pressure", "Volume"],
                                    output_xvg=str(Path(tmp) / "energy.xvg"), detect_equilibration=False)
            if energy["status"] == "success":
                data["energy"] = energy["stats"]
                rows = parse_xvg(Path(energy["output_xvg"]).read_text())
                spikes, non_finite = [], 0
                xs = [row[0] for row in rows["data"]]
                for c, name in enumerate(rows["legends"], start=1):
                    col = [row[c] for row in rows["data"]]
                    bad = [i for i, v in enumerate(col) if not math.isfinite(v)]
                    non_finite += len(bad)
                    finite = [v for v in col if math.isfinite(v)]
                    if len(finite) > 10:
                        med = statistics.median(finite)
                        mad = statistics.median(abs(v - med) for v in finite) * 1.4826
                        if mad > 0 and name != "Pressure":
                            limit = 10 * mad
                            # a spike is isolated: both neighbors are back near the median (start-up
                            # relaxation and slow drifts are not spikes)
                            for i in range(1, len(col) - 1):
                                v = col[i]
                                if (math.isfinite(v) and abs(v - med) > limit
                                        and abs(col[i - 1] - med) <= limit and abs(col[i + 1] - med) <= limit):
                                    spikes.append({"term": name, "time_ps": xs[i], "value": v})
                data["non_finite"] = non_finite
                data["spikes"] = spikes[:10]
                if non_finite:
                    issues.append(f"{non_finite} non-finite energy values (the simulation blew up)")
                if spikes:
                    issues.append(f"{len(spikes)} sudden energy spike(s); first at {spikes[0]['time_ps']:g} ps ({spikes[0]['term']})")
            else:
                issues.append(f"Energy analysis failed: {energy.get('error')}")

    if info.get("n_frames", 0) < 2:
        issues.append("Fewer than 2 frames: nothing to analyze yet")
    data["issues"] = issues
    return format_tool_result(True, data=data, warnings=issues)


def plot_xvg(
    xvg_file: str,
    output_png: Optional[str] = None,
    columns: Optional[List[int]] = None,
    title: Optional[str] = None,
    begin: Optional[float] = None,
    end: Optional[float] = None,
) -> dict:
    """
    Plot an .xvg file to a PNG (requires matplotlib: pip install matplotlib).

    Args:
        xvg_file: input .xvg
        output_png: where to save (default: next to the input, same name)
        columns: which y columns to plot, 1 based (default all)
        title: plot title (default: the one in the file)
        begin / end: x range to plot

    Returns:
        output_png path and the legends plotted.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return format_tool_result(False, error_message="matplotlib is not installed. Install it with "
                                                       "`pip install matplotlib` (optional extra: plots)")
    try:
        src = _existing(xvg_file, "xvg file")
        png = _resolve(output_png) if output_png else src.with_suffix(".png")
    except (FileNotFoundError, PathNotAllowedError) as e:
        return format_tool_result(False, error_message=str(e))

    parsed = parse_xvg(src.read_text())
    rows = [r for r in parsed["data"]
            if (begin is None or r[0] >= begin) and (end is None or r[0] <= end)]
    if not rows:
        return format_tool_result(False, error_message="No data rows in the requested range")
    ncols = parsed["n_columns"]
    cols = columns or list(range(1, ncols))
    bad = [c for c in cols if c < 1 or c >= ncols]
    if bad:
        return format_tool_result(False, error_message=f"Columns {bad} out of range; the file has y columns 1 to {ncols - 1}")

    fig, ax = plt.subplots(figsize=(7, 4))
    xs = [r[0] for r in rows]
    plotted = []
    for c in cols:
        label = parsed["legends"][c - 1] if c - 1 < len(parsed["legends"]) else f"column {c}"
        ax.plot(xs, [r[c] for r in rows], label=label, linewidth=1.2)
        plotted.append(label)
    ax.set_xlabel(parsed["xaxis"])
    ax.set_ylabel(_y_label(parsed["yaxis"], parsed["legends"], cols))
    ax.set_title(title or parsed["title"])
    if len(cols) > 1:
        ax.legend()
    fig.tight_layout()
    png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png, dpi=150)
    plt.close(fig)
    return format_tool_result(True, data={"output_png": str(png), "plotted": plotted}, output_files=[str(png)])


def _y_label(yaxis: str, legends: List[str], cols: List[int]) -> str:
    """gmx energy writes one unit per series ("(kJ/mol), (K), (bar)"); keep those of the plotted columns."""
    parts = [part.strip() for part in (yaxis or "").split(",")]
    if len(legends) > 1 and len(parts) == len(legends):
        return ", ".join(dict.fromkeys(parts[c - 1] for c in cols))
    return yaxis


_PRESETS = {
    "whole": (["-pbc", "whole"], 1, "make molecules whole across periodic boundaries (selection: output group)"),
    "nojump": (["-pbc", "nojump"], 1, "remove jumps of atoms across boundaries, for diffusion (selection: output group)"),
    "mol": (["-pbc", "mol"], 1, "put molecule centers of mass in the box (selection: output group)"),
    "center": (["-pbc", "mol", "-center", "-ur", "compact"], 2, "center a group in the box (selections: center group, output group)"),
    "fit": (["-fit", "rot+trans"], 2, "remove rotation and translation (selections: fit group, output group)"),
    "window": ([], 1, "extract a time window (use begin_ps, end_ps, dt_ps; selection: output group)"),
    "pdb": ([], 1, "write PDB frames (output file must end in .pdb; use dump_ps for a single frame)"),
}


def trjconv_preset(
    trajectory: str,
    structure: str,
    output_file: str,
    preset: str,
    group: str = "System",
    center_group: Optional[str] = None,
    begin_ps: Optional[float] = None,
    end_ps: Optional[float] = None,
    dt_ps: Optional[float] = None,
    dump_ps: Optional[float] = None,
) -> dict:
    """
    Common trajectory processing with the right group selections filled in.

    Presets: whole, nojump, mol, center, fit, window, pdb (see each description below).

    Args:
        trajectory: input .xtc or .trr
        structure: .tpr
        output_file: result (.xtc, .trr, .gro or .pdb)
        preset: one of whole, nojump, mol, center, fit, window, pdb
        group: group to write out (default System). For `fit` this is the output group
        center_group: group to center or fit on (default: same as group)
        begin_ps / end_ps: time window
        dt_ps: keep one frame every dt_ps
        dump_ps: write just the frame at this time

    Returns:
        output file, command, and the preset's meaning.
    """
    if preset not in _PRESETS:
        return format_tool_result(False, error_message=f"Unknown preset '{preset}'. Available: {', '.join(_PRESETS)}")
    try:
        traj = _existing(trajectory, "trajectory")
        tpr = _existing(structure, "structure")
        out = _resolve(output_file)
    except (FileNotFoundError, PathNotAllowedError) as e:
        return format_tool_result(False, error_message=str(e))
    if preset == "pdb" and out.suffix.lower() != ".pdb":
        return format_tool_result(False, error_message="The pdb preset needs an output file ending in .pdb")

    flags, n_sel, meaning = _PRESETS[preset]
    params = list(flags) + _window(begin_ps, end_ps)
    if dt_ps is not None:
        params += ["-dt", str(dt_ps)]
    if dump_ps is not None:
        params += ["-dump", str(dump_ps)]
    selections = [center_group or group, group] if n_sel == 2 else [group]
    res = _gmx("trjconv", {"f": traj, "s": tpr}, {"o": out}, params, selections=selections, timeout=900)
    if not res["result"]:
        return format_tool_result(False, data={"command": res.get("command")}, error_message=res.get("error"))
    return format_tool_result(True, data={"output_file": str(out), "preset": preset, "meaning": meaning,
                                          "command": res["command"]}, output_files=[str(out)],
                              warnings=res.get("warnings", []))


def register(mcp, registry) -> None:
    for fn in (read_xvg, energy_summary, analyze_rdf, analyze_msd, trajectory_report, plot_xvg, trjconv_preset):
        registry.tool("analysis")(fn)
