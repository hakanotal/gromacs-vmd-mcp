#!/usr/bin/env python3
"""MCP server for VMD: headless rendering and trajectory analysis, and interactive VMD sessions.

Every tool returns the flat envelope from vmd.result: status, error (on failure), warnings,
output_files, plus tool specific fields. Nothing is written to stdout except the MCP protocol.
"""

import logging
import os
import platform
import signal
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastmcp import FastMCP  # noqa: E402

import templates  # noqa: E402
from vmd import (  # noqa: E402
    COLORINGS,
    REPRESENTATIONS,
    PathNotAllowedError,
    VMDNotFoundError,
    check_choice,
    check_image_size,
    check_representations,
    check_selection,
    existing_file,
    find_ffmpeg,
    find_tachyon,
    find_vmd,
    fit_zoom,
    get_home,
    get_results_dir,
    get_workdir_root,
    output_path,
    parse_series,
    result,
    run_tachyon,
    run_vmd_script,
    vmd_env,
)

SERVER_NAME = "vmd"
SERVER_VERSION = "1.1.0"

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vmd-mcp")

mcp = FastMCP(SERVER_NAME)

# Interactive sessions started by this server: pid -> details. Only these can be closed.
_SESSIONS: Dict[int, dict] = {}

_INPUT_ERRORS = (FileNotFoundError, PathNotAllowedError, ValueError, VMDNotFoundError)


def _inputs(structure: str, trajectory: Optional[str]) -> List[str]:
    s = existing_file(structure, "structure")
    t = existing_file(trajectory, "trajectory") if trajectory else None
    return [str(s), str(t) if t else "none"]


def _run(script: str, args: list, out: Path, timeout: Optional[int]) -> Optional[dict]:
    """Run a template; return an error envelope, or None when the output file was written."""
    res = run_vmd_script(script, args, timeout=timeout)
    if not res["success"] or not out.exists():
        reason = "; ".join(res["errors"]) or res["stderr"] or "VMD finished without writing its output"
        if ".tpr" in reason and "Could not read file" in reason:
            reason += (". VMD cannot read GROMACS .tpr files (checked with VMD 2.0.0a7): use the .gro the run "
                       "wrote (md.gro, npt.gro) or a .pdb as the structure")
        return result(False, {"command": res["command"], "stdout": res["stdout"]}, error=f"VMD failed: {reason}")
    return None


@mcp.tool()
def check_vmd_environment() -> dict:
    """
    Diagnose the setup: the VMD executable, its version in text mode, the workspace and
    sandbox, and optional features. Run this first when a tool fails.
    """
    data = {"server_version": SERVER_VERSION, "platform": platform.platform(),
            "workdir_restriction": str(get_workdir_root()) if get_workdir_root() else None,
            "home": str(get_home()), "tcl_enabled": os.environ.get("VMD_MCP_ALLOW_TCL") == "1"}
    try:
        import PIL  # noqa: F401
        data["png_output"] = True
    except ImportError:
        data["png_output"] = False
    try:
        data["vmd"] = find_vmd()[0]
    except VMDNotFoundError as e:
        return result(False, data, error=str(e))
    data["tachyon"] = find_tachyon(find_vmd())
    data["ffmpeg"] = find_ffmpeg()
    res = run_vmd_script(templates.VERSION, [], timeout=120)
    version = next((l.split(None, 1)[1] for l in res["stdout"].splitlines() if l.startswith("VMD_MCP_VERSION ")), None)
    data["vmd_version"] = version
    if not version:
        return result(False, {**data, "stdout": res["stdout"], "stderr": res["stderr"]},
                      error="VMD was found but did not run in text mode (-dispdev text)")
    warnings = [] if data["png_output"] else ["Pillow is not installed: images are written as .tga "
                                              "(pip install pillow for .png)"]
    if not data["tachyon"]:
        warnings.append("No tachyon binary next to VMD: images are 512 x 512 (set VMD_MCP_TACHYON)")
    if not data["ffmpeg"]:
        warnings.append("ffmpeg not found: render_movie returns frames instead of an .mp4")
    return result(True, data, warnings=warnings)


@mcp.tool()
def list_templates() -> dict:
    """List the operations, the style presets (each a list of representations), representations and colorings."""
    return result(True, {"operations": templates.DESCRIPTIONS, "styles": templates.STYLES,
                         "representations": list(REPRESENTATIONS), "colorings": list(COLORINGS)})


def _scene_reps(style: Optional[str], representations: Optional[List[dict]], selection: str,
                representation: str, coloring: str) -> List[str]:
    """$argv triples for the scene: a style preset, an explicit list, or one selection/representation/coloring."""
    if style and representations:
        raise ValueError("Give either style or representations, not both")
    if style:
        if style not in templates.STYLES:
            raise ValueError(f"Unknown style {style!r}; available: {', '.join(templates.STYLES)}")
        return check_representations(templates.STYLES[style])
    if representations:
        return check_representations(representations)
    return check_representations([{"selection": selection, "representation": representation, "coloring": coloring}])


def _to_png(tga: Path, png: Path, warnings: List[str]) -> Path:
    """Convert with Pillow when available; otherwise keep the TGA and say so."""
    try:
        from PIL import Image
        with Image.open(tga) as img:
            img.save(png)
        tga.unlink()
        return png
    except ImportError:
        warnings.append("Pillow is not installed, so the image was kept as .tga (pip install pillow)")
    except OSError as e:
        warnings.append(f"Could not convert to PNG ({e}); the image was kept as .tga")
    return tga


@mcp.tool()
def render_image(
    structure: str,
    trajectory: Optional[str] = None,
    output_file: Optional[str] = None,
    style: Optional[str] = None,
    representations: Optional[List[dict]] = None,
    selection: str = "all",
    representation: str = "Lines",
    coloring: str = "Name",
    frame: int = -1,
    width: int = 800,
    height: int = 600,
    zoom: float = 1.0,
    timeout: int = 600,
) -> dict:
    """
    Render a structure, or one frame of a trajectory, to an image without a display (Tachyon).

    Args:
        structure: .pdb, .gro, .psf or another format VMD reads (.gro or .pdb for GROMACS runs)
        trajectory: optional .xtc, .trr, .dcd loaded into the same molecule
        output_file: .png (needs Pillow) or .tga; default: a new file under results/render/
        style: a preset from list_templates, for example protein_cartoon, protein_ligand, solvated
        representations: several representations in one image, up to 8, for example
            [{"selection": "protein", "representation": "NewCartoon", "coloring": "Structure"},
             {"selection": "resname LIG", "representation": "Licorice", "coloring": "Name"}]
        selection / representation / coloring: a single representation, when neither style nor
            representations is given (representations and colorings: see list_templates)
        frame: frame to show; -1 is the last one
        width / height: image size in pixels (needs the tachyon binary VMD ships; otherwise 512 x 512)
        zoom: magnification on top of the automatic fit (1 shows everything with a margin; 1.4 fills
            the frame for most systems; 0.1 to 10)

    Returns:
        output_file, format, width, height, and the commands that ran.
    """
    try:
        args = _inputs(structure, trajectory)
        reps = _scene_reps(style, representations, selection, representation, coloring)
        check_image_size(width, height)
        _check_zoom(zoom)
        out = output_path(output_file, "render", Path(structure).stem, ".png")
        if out.suffix.lower() not in (".png", ".tga"):
            raise ValueError("output_file must end in .png or .tga")
        tachyon = find_tachyon(find_vmd())
    except _INPUT_ERRORS as e:
        return result(False, error=str(e))

    warnings: List[str] = []
    tga = out if out.suffix.lower() == ".tga" else out.with_suffix(".tga")
    if tachyon:
        scene = tga.with_suffix(".scene.dat")
        failure = _run(templates.RENDER, args + [int(frame), "scene", fit_zoom(width, height) * float(zoom), str(scene)] + reps,
                       scene, timeout)
        if failure:
            return failure
        rendered = run_tachyon(tachyon, scene, tga, width, height, timeout=timeout)
        scene.unlink(missing_ok=True)
        if not rendered["success"]:
            return result(False, {"command": rendered["command"]}, error=f"Tachyon failed: {rendered['stderr']}")
        size = (int(width), int(height))
    else:
        failure = _run(templates.RENDER, args + [int(frame), "internal", fit_zoom(512, 512) * float(zoom), str(tga)] + reps,
                       tga, timeout)
        if failure:
            return failure
        size = (512, 512)
        warnings.append("No tachyon binary found (set VMD_MCP_TACHYON), so the image is 512 x 512")
    final = _to_png(tga, out, warnings) if out.suffix.lower() == ".png" else tga
    return result(True, {"output_file": str(final), "format": final.suffix.lstrip("."), "width": size[0],
                         "height": size[1]}, output_files=[str(final)], warnings=warnings)


MAX_MOVIE_FRAMES = 2000


def _check_zoom(zoom: float) -> None:
    if not 0.1 <= float(zoom) <= 10:
        raise ValueError("zoom must be between 0.1 and 10")


@mcp.tool()
def render_movie(
    structure: str,
    trajectory: str,
    output_file: Optional[str] = None,
    style: Optional[str] = None,
    representations: Optional[List[dict]] = None,
    selection: str = "all",
    representation: str = "Lines",
    coloring: str = "Name",
    first: int = 0,
    last: int = -1,
    stride: int = 1,
    max_frames: int = 300,
    rotate_degrees: float = 0.0,
    width: int = 640,
    height: int = 480,
    zoom: float = 1.0,
    fps: int = 20,
    timeout: int = 1800,
) -> dict:
    """
    Render trajectory frames to images and, when ffmpeg is installed, assemble them into an .mp4.

    Args:
        structure / trajectory: as for render_image
        output_file: the .mp4 (default: results/movies/<name>/movie.mp4); frames go to a frames/
            folder next to it
        style / representations / selection / representation / coloring: as for render_image
        first / last / stride: frames to use (last -1 is the final frame)
        max_frames: stop after this many images (default 300, at most 2000)
        rotate_degrees: turn the view around y by this much per image (0 keeps it still)
        width / height: image size; rounded up to even numbers for the video
        zoom: magnification on top of the automatic fit, as for render_image
        fps: frames per second of the video

    Returns:
        output_file (.mp4, or the frames folder when ffmpeg is missing), n_frames, frames_dir.
    """
    try:
        args = _inputs(structure, trajectory)
        reps = _scene_reps(style, representations, selection, representation, coloring)
        width, height = int(width) + int(width) % 2, int(height) + int(height) % 2
        check_image_size(width, height)
        _check_zoom(zoom)
        if int(stride) < 1 or int(first) < 0:
            raise ValueError("stride must be 1 or more and first 0 or more")
        if not 1 <= int(max_frames) <= MAX_MOVIE_FRAMES:
            raise ValueError(f"max_frames must be between 1 and {MAX_MOVIE_FRAMES}")
        if not 1 <= int(fps) <= 120:
            raise ValueError("fps must be between 1 and 120")
        movie = (output_path(output_file, "movies", "movie", ".mp4") if output_file else
                 output_path(None, "movies", Path(trajectory).stem, "") / "movie.mp4")
        if movie.suffix.lower() != ".mp4":
            raise ValueError("output_file must end in .mp4")
        frames_dir = movie.parent / (movie.stem + "_frames")
        frames_dir.mkdir(parents=True, exist_ok=True)
        tachyon = find_tachyon(find_vmd())
    except _INPUT_ERRORS as e:
        return result(False, error=str(e))

    warnings: List[str] = []
    mode = "scene" if tachyon else "internal"
    scale = (fit_zoom(width, height) if tachyon else fit_zoom(512, 512)) * float(zoom)
    res = run_vmd_script(templates.MOVIE, args + [int(first), int(last), int(stride), float(rotate_degrees),
                                                  int(max_frames), mode, round(scale, 4), str(frames_dir)] + reps,
                         timeout=timeout)
    if not res["success"]:
        reason = "; ".join(res["errors"]) or res["stderr"] or "VMD failed"
        return result(False, {"command": res["command"]}, error=f"VMD failed: {reason}")
    if "VMD_MCP_TRUNCATED" in res["stdout"]:
        warnings.append(f"Stopped at max_frames={max_frames}; raise stride or max_frames for the rest")
    if tachyon:
        for scene in sorted(frames_dir.glob("frame_*.dat")):
            rendered = run_tachyon(tachyon, scene, scene.with_suffix(".tga"), width, height, timeout=timeout)
            scene.unlink(missing_ok=True)
            if not rendered["success"]:
                return result(False, {"frames_dir": str(frames_dir)}, error=f"Tachyon failed: {rendered['stderr']}")
    else:
        warnings.append("No tachyon binary found (set VMD_MCP_TACHYON), so frames are 512 x 512")
    frames = sorted(frames_dir.glob("frame_*.tga"))
    if not frames:
        return result(False, {"frames_dir": str(frames_dir)}, error="No frames were rendered (check first/last)")

    data = {"n_frames": len(frames), "frames_dir": str(frames_dir), "fps": int(fps)}
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        warnings.append("ffmpeg not found (set VMD_MCP_FFMPEG or install it): returning the frames only")
        return result(True, {**data, "output_file": str(frames_dir)}, output_files=[str(f) for f in frames],
                      warnings=warnings)
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-framerate", str(int(fps)), "-i", "frame_%05d.tga",
           "-pix_fmt", "yuv420p", "-c:v", "libx264", str(movie)]
    try:
        proc = subprocess.run(cmd, cwd=str(frames_dir), capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return result(False, {**data, "command": " ".join(cmd)}, error=f"ffmpeg failed: {e}")
    if proc.returncode != 0 or not movie.exists():
        return result(False, {**data, "command": " ".join(cmd)}, error=f"ffmpeg failed: {proc.stderr[-500:]}")
    return result(True, {**data, "output_file": str(movie), "command": " ".join(cmd)}, output_files=[str(movie)],
                  warnings=warnings)


@mcp.tool()
def rmsd_over_trajectory(
    structure: str,
    trajectory: str,
    selection: str = "protein and name CA",
    reference_frame: int = 0,
    align: bool = True,
    output_file: Optional[str] = None,
    timeout: int = 900,
) -> dict:
    """
    RMSD of a selection in every frame against a reference frame (in Angstrom, VMD units).

    Args:
        structure: topology for the atoms, for example the .gro or .pdb the run started from
        trajectory: .xtc, .trr or .dcd
        selection: atoms to compare, for example 'protein and name CA' or 'backbone'
        reference_frame: frame used as the reference (0 is the first)
        align: fit each frame onto the reference first (removes rotation and translation)
        output_file: where to save "frame rmsd" rows (default: results/rmsd/)

    Returns:
        n_frames, rmsd (list of [frame, value]), mean, max, final, output_file.
    """
    try:
        args = _inputs(structure, trajectory)
        check_selection(selection)
        if int(reference_frame) < 0:
            raise ValueError("reference_frame must be 0 or greater")
        out = output_path(output_file, "rmsd", Path(trajectory).stem, "_rmsd.dat")
    except _INPUT_ERRORS as e:
        return result(False, error=str(e))
    failure = _run(templates.RMSD, args + [selection, int(reference_frame), 1 if align else 0, str(out)], out, timeout)
    if failure:
        return failure
    rows = [r[:2] for r in parse_series(out.read_text()) if len(r) >= 2]
    if not rows:
        return result(False, {"output_file": str(out)}, error="VMD wrote no RMSD values (empty selection?)")
    values = [r[1] for r in rows]
    return result(True, {"n_frames": len(rows), "unit": "Angstrom", "rmsd": rows,
                         "mean": sum(values) / len(values), "max": max(values), "final": values[-1],
                         "output_file": str(out)}, output_files=[str(out)])


@mcp.tool()
def contacts_over_trajectory(
    structure: str,
    trajectory: str,
    selection1: str,
    selection2: str,
    cutoff: float = 3.5,
    output_file: Optional[str] = None,
    timeout: int = 900,
) -> dict:
    """
    Number of atom pairs between two selections closer than a cutoff, in every frame. Pairs VMD
    considers bonded are not counted (VMD's measure contacts).

    Args:
        structure / trajectory: as for rmsd_over_trajectory
        selection1 / selection2: VMD atom selections, for example 'protein' and 'resname LIG'
        cutoff: distance in Angstrom (default 3.5)
        output_file: where to save "frame count" rows (default: results/contacts/)

    Returns:
        n_frames, contacts (list of [frame, count]), mean, max, output_file.
    """
    try:
        args = _inputs(structure, trajectory)
        check_selection(selection1, "selection1")
        check_selection(selection2, "selection2")
        if not 0 < float(cutoff) <= 50:
            raise ValueError("cutoff must be between 0 and 50 Angstrom")
        out = output_path(output_file, "contacts", Path(trajectory).stem, "_contacts.dat")
    except _INPUT_ERRORS as e:
        return result(False, error=str(e))
    failure = _run(templates.CONTACTS, args + [selection1, selection2, float(cutoff), str(out)], out, timeout)
    if failure:
        return failure
    rows = [r[:2] for r in parse_series(out.read_text()) if len(r) >= 2]
    if not rows:
        return result(False, {"output_file": str(out)}, error="VMD wrote no contact counts")
    counts = [r[1] for r in rows]
    return result(True, {"n_frames": len(rows), "contacts": rows, "mean": sum(counts) / len(counts),
                         "max": max(counts), "output_file": str(out)}, output_files=[str(out)])


def _series(script: str, args: list, out: Path, timeout: int, key: str, unit: str) -> dict:
    """Run a per-frame template and return its rows with mean, min, max and the last value."""
    failure = _run(script, args + [str(out)], out, timeout)
    if failure:
        return failure
    rows = [r[:2] for r in parse_series(out.read_text()) if len(r) >= 2]
    if not rows:
        return result(False, {"output_file": str(out)}, error="VMD wrote no values (empty selection?)")
    values = [r[1] for r in rows]
    return result(True, {"n_frames": len(rows), "unit": unit, key: rows, "mean": sum(values) / len(values),
                         "min": min(values), "max": max(values), "final": values[-1], "output_file": str(out)},
                  output_files=[str(out)])


@mcp.tool()
def radius_of_gyration_over_trajectory(
    structure: str,
    trajectory: str,
    selection: str = "protein",
    output_file: Optional[str] = None,
    timeout: int = 900,
) -> dict:
    """
    Radius of gyration of a selection in every frame (Angstrom, mass weighted by VMD).

    Returns n_frames, rgyr (list of [frame, value]), mean, min, max, final, output_file.
    """
    try:
        args = _inputs(structure, trajectory) + [check_selection(selection)]
        out = output_path(output_file, "rgyr", Path(trajectory).stem, "_rgyr.dat")
    except _INPUT_ERRORS as e:
        return result(False, error=str(e))
    return _series(templates.RGYR, args, out, timeout, "rgyr", "Angstrom")


@mcp.tool()
def sasa_over_trajectory(
    structure: str,
    trajectory: str,
    selection: str = "protein",
    restrict_to: Optional[str] = None,
    probe_radius: float = 1.4,
    output_file: Optional[str] = None,
    timeout: int = 1800,
) -> dict:
    """
    Solvent accessible surface area of a selection in every frame (Angstrom^2).

    Args:
        selection: the atoms that make up the surface, for example 'protein'
        restrict_to: report only the part of that surface belonging to these atoms, for example
            'protein and resid 10 to 20' (the rest of the selection still buries it)
        probe_radius: solvent probe in Angstrom (1.4 for water)

    Returns n_frames, sasa (list of [frame, value]), mean, min, max, final, output_file.
    """
    try:
        args = _inputs(structure, trajectory) + [check_selection(selection)]
        if not 0 <= float(probe_radius) <= 10:
            raise ValueError("probe_radius must be between 0 and 10 Angstrom")
        args += [float(probe_radius), check_selection(restrict_to, "restrict_to") if restrict_to else "none"]
        out = output_path(output_file, "sasa", Path(trajectory).stem, "_sasa.dat")
    except _INPUT_ERRORS as e:
        return result(False, error=str(e))
    return _series(templates.SASA, args, out, timeout, "sasa", "Angstrom^2")


@mcp.tool()
def hbonds_over_trajectory(
    structure: str,
    trajectory: str,
    donor_selection: str = "protein",
    acceptor_selection: str = "protein",
    distance: float = 3.0,
    angle: float = 20.0,
    output_file: Optional[str] = None,
    timeout: int = 1800,
) -> dict:
    """
    Number of hydrogen bonds per frame between donors and acceptors (VMD's measure hbonds: donor to
    acceptor distance below `distance` Angstrom and a D-H-A angle within `angle` degrees of 180).

    Needs hydrogens in the structure (all-atom .gro or .pdb). Use the same selection twice for
    bonds within one group, for example 'protein' and 'protein'.

    Returns n_frames, hbonds (list of [frame, count]), mean, min, max, final, output_file.
    """
    try:
        args = _inputs(structure, trajectory) + [check_selection(donor_selection, "donor_selection"),
                                                 check_selection(acceptor_selection, "acceptor_selection")]
        if not 0 < float(distance) <= 6 or not 0 < float(angle) <= 90:
            raise ValueError("distance must be in (0, 6] Angstrom and angle in (0, 90] degrees")
        args += [float(distance), float(angle)]
        out = output_path(output_file, "hbonds", Path(trajectory).stem, "_hbonds.dat")
    except _INPUT_ERRORS as e:
        return result(False, error=str(e))
    return _series(templates.HBONDS, args, out, timeout, "hbonds", "count")


_SS_CLASSES = {"helix": "HGI", "sheet": "EB", "turn": "T", "coil": "C"}


@mcp.tool()
def secondary_structure_over_trajectory(
    structure: str,
    trajectory: str,
    selection: str = "protein and name CA",
    output_file: Optional[str] = None,
    timeout: int = 1800,
) -> dict:
    """
    Secondary structure per residue and frame, assigned by STRIDE (bundled with VMD). STRIDE needs
    protein chains of at least 5 residues with backbone atoms.

    Codes: H alpha helix, G 3-10 helix, I pi helix, E strand, B bridge, T turn, C coil.

    Args:
        selection: one atom per residue, for example 'protein and name CA' or 'protein and name CA and resid 1 to 50'

    Returns:
        n_frames, n_residues, per_frame ([frame, code string]), fractions per frame
        ({helix, sheet, turn, coil}), mean_fractions, final (code string of the last frame), output_file.
    """
    try:
        args = _inputs(structure, trajectory) + [check_selection(selection)]
        out = output_path(output_file, "secondary_structure", Path(trajectory).stem, "_ss.dat")
    except _INPUT_ERRORS as e:
        return result(False, error=str(e))
    failure = _run(templates.SECONDARY_STRUCTURE, args + [str(out)], out, timeout)
    if failure:
        if "stride" in failure["error"].lower():
            failure["error"] += (" (STRIDE, which VMD ships, assigned nothing: it skips chains shorter than 5 "
                                 "residues, needs the backbone atoms N, CA, C, O, and gives up when it finds no "
                                 "backbone hydrogen bonds)")
        return failure
    per_frame = []
    for line in out.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            per_frame.append([int(parts[0]), parts[1]])
    if not per_frame:
        return result(False, {"output_file": str(out)}, error="No secondary structure (is the selection a protein?)")
    fractions = []
    for _, codes in per_frame:
        fractions.append({k: round(sum(c in v for c in codes) / len(codes), 4) for k, v in _SS_CLASSES.items()})
    mean = {k: round(sum(f[k] for f in fractions) / len(fractions), 4) for k in _SS_CLASSES}
    return result(True, {"n_frames": len(per_frame), "n_residues": len(per_frame[0][1]), "per_frame": per_frame,
                         "fractions": fractions, "mean_fractions": mean, "final": per_frame[-1][1],
                         "output_file": str(out)}, output_files=[str(out)])


@mcp.tool()
def launch_vmd_gui(structure: str, trajectory: Optional[str] = None) -> dict:
    """
    Open the files in an interactive VMD window on this machine (needs a display).

    The window belongs to the user; close it there, or with close_vmd_session. Returns the pid.
    """
    try:
        files = [str(existing_file(structure, "structure"))]
        if trajectory:
            files.append(str(existing_file(trajectory, "trajectory")))
        vmd = find_vmd()
    except _INPUT_ERRORS as e:
        return result(False, error=str(e))
    log_dir = get_results_dir() / "sessions"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"vmd_{len(_SESSIONS) + 1}_{os.getpid()}.log"
    cmd = vmd + ["-f"] + files  # -f loads every file into one molecule
    with open(log, "w") as fh:
        # VMD reads commands from its console and quits at end of input, so it gets a pipe that stays open
        # (never the MCP stdin, never written to) for as long as the session lasts
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=fh, stderr=subprocess.STDOUT,
                                start_new_session=True, env=vmd_env(vmd))
    _SESSIONS[proc.pid] = {"pid": proc.pid, "files": files, "log": str(log), "process": proc}
    return result(True, {"pid": proc.pid, "command": " ".join(cmd), "log": str(log)})


@mcp.tool()
def list_vmd_sessions() -> dict:
    """List the interactive VMD sessions this server started and whether they are still open."""
    sessions = [{"pid": pid, "files": s["files"], "running": s["process"].poll() is None, "log": s["log"]}
                for pid, s in _SESSIONS.items()]
    return result(True, {"sessions": sessions, "total": len(sessions)})


@mcp.tool()
def close_vmd_session(pid: int) -> dict:
    """Close an interactive VMD session started by launch_vmd_gui (other processes are never touched)."""
    session = _SESSIONS.get(pid)
    if not session:
        return result(False, error=f"No session with pid {pid} was started by this server")
    proc = session["process"]
    if proc.poll() is None and proc.stdin:
        try:  # ask VMD to quit on its own first
            proc.stdin.write(b"quit\n")
            proc.stdin.close()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    _SESSIONS.pop(pid, None)
    return result(True, {"pid": pid, "message": "closed"})


@mcp.tool()
def run_vmd_tcl(script: str, timeout: int = 600) -> dict:
    """
    Run your own Tcl script in VMD's text mode. Disabled unless the server was started with
    VMD_MCP_ALLOW_TCL=1, because Tcl can run any shell command (exec). Prefer the other tools.

    Args:
        script: Tcl commands (run as one block; `quit` is appended)
    """
    if os.environ.get("VMD_MCP_ALLOW_TCL") != "1":
        return result(False, error="run_vmd_tcl is disabled: arbitrary Tcl can run shell commands. "
                                   "Start the server with VMD_MCP_ALLOW_TCL=1 to enable it")
    try:
        res = run_vmd_script(script, [], timeout=timeout)
    except VMDNotFoundError as e:
        return result(False, error=str(e))
    data = {"stdout": res["stdout"], "stderr": res["stderr"], "command": res["command"]}
    if not res["success"]:
        return result(False, data, error="; ".join(res["errors"]) or "VMD failed")
    return result(True, data)


@mcp.prompt()
def visualize_md_run(structure: str = "md.gro", trajectory: str = "md.xtc") -> str:
    """Look at a finished MD run: images, a movie and per-frame numbers."""
    return (
        f"Visualize the simulation in {structure} with trajectory {trajectory} using the vmd tools.\n"
        "These are typically the outputs of the gromacs server: npt.gro and npt.xtc from equilibration, or "
        "md.gro and md.xtc in the output_dir of a submit_md_simulation job.\n"
        "1. check_vmd_environment.\n"
        "2. render_image of the last frame: style 'protein_cartoon' or 'protein_ligand' for proteins, "
        "'all_vdw' for small systems such as a water box.\n"
        "3. rmsd_over_trajectory and radius_of_gyration_over_trajectory for a protein (selection 'protein and "
        "name CA' and 'protein'); for a liquid, skip these.\n"
        "4. secondary_structure_over_trajectory when there is a protein.\n"
        "5. render_movie with a stride that gives about 100 images.\n"
        "Report whether the structure is stable (RMSD plateau, radius of gyration, secondary structure), and "
        "give the paths of the image and the movie."
    )


def main() -> None:
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
