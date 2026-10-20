#!/usr/bin/env python3
"""
Script: gromacs_command.py
Description: Execute GROMACS command-line tools safely (no shell, whitelisted commands)

Usage:
    python scripts/gromacs_command.py --command <gromacs_tool> -i FLAG FILE -o FLAG FILE [--select TEXT ...]

Example:
    python scripts/gromacs_command.py --command energy -i f run.edr -o o energy.xvg --select Potential --select Temperature

Notes:
    * Commands are run as an argument list. No shell is involved, so quoting
      characters in file names or parameters are passed through literally.
    * Interactive group or term prompts are answered with --select values
      (the `selections` argument), written to the program's standard input.
    * Only commands registered in COMMANDS can be run.
"""

import argparse
import difflib
import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from lib.gromacs_utils import (
    FILE_SUFFIXES as _FILE_SUFFIXES,
    check_shell_safe_path,
    PathNotAllowedError,
    check_file_exists,
    check_grompp_includes,
    deep_merge,
    find_gromacs_command,
    get_gmx_version,
    get_workdir_root,
    resolve_in_workdir,
    run_command_full,
    summarize_gmx_error,
    GromacsNotFoundError,
)

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# ==============================================================================
# Command registry (whitelist)
# ==============================================================================
# Fields:
#   description        shown in listings
#   default_selections answers sent to stdin when the caller gives none
#   needs_selections   True if the command cannot run without explicit selections
#   terminator         extra line appended after caller selections (energy term lists end with 0)
#   stdout_to_file     the command has no -o; stdout is written to the output path instead
#   parameters         default extra flags
#   category           grouping used in listings
COMMANDS: Dict[str, Dict[str, Any]] = {
    # Analysis (original set)
    "rms": {"description": "RMSD vs reference (selections: fit group, output group)",
            "category": "analysis", "default_selections": ["0", "0"], "parameters": ["-tu", "ns"]},
    "rmsf": {"description": "RMS fluctuation per atom or residue (selection: group)",
             "category": "analysis", "default_selections": ["0"], "parameters": ["-res"]},
    "energy": {"description": "Energy terms from an .edr file (selections: term names, for example Potential Temperature)",
               "category": "analysis", "needs_selections": True, "terminator": "0"},
    "sasa": {"description": "Solvent accessible surface area (selection: group)",
             "category": "analysis", "default_selections": ["0"]},
    "hbond": {"description": "Hydrogen bonds (selections: donor group, acceptor group)",
              "category": "analysis", "default_selections": ["0", "0"]},
    "trjconv": {"description": "Convert or process trajectories (selections: output group, or center group then output group with -center)",
                "category": "trajectory", "default_selections": ["0"]},
    "dump": {"description": "Dump a binary file as text (stdout; use output_files to save it)",
             "category": "info", "stdout_to_file": True},
    "check": {"description": "Check trajectory, energy or topology file integrity (report is on stderr)",
              "category": "info"},
    # Additional analysis
    "rdf": {"description": "Radial distribution function (use -ref and -sel selection expressions, for example -ref 'name OW')",
            "category": "analysis"},
    "msd": {"description": "Mean square displacement and diffusion coefficient (-sel 'name OW')",
            "category": "analysis"},
    "gyrate": {"description": "Radius of gyration (selection: group)", "category": "analysis",
               "default_selections": ["0"]},
    "density": {"description": "Density profile (selections: group)", "category": "analysis",
                "default_selections": ["0"]},
    "distance": {"description": "Distance between selections (-select expression)", "category": "analysis"},
    "angle": {"description": "Angle distributions (-g1 -group1 ...)", "category": "analysis"},
    "rmsdist": {"description": "RMS distance matrix (selection: group)", "category": "analysis",
                "default_selections": ["0"]},
    "cluster": {"description": "Cluster structures (selections: fit group, output group)",
                "category": "analysis", "default_selections": ["0", "0"]},
    "covar": {"description": "Covariance matrix and eigenvectors (selections: fit group, analysis group)",
              "category": "analysis", "default_selections": ["0", "0"]},
    "anaeig": {"description": "Analyze eigenvectors (selections depend on options)", "category": "analysis"},
    "dssp": {"description": "Secondary structure (selection: group)", "category": "analysis",
             "default_selections": ["1"]},
    "pairdist": {"description": "Pair distances between selections (-ref, -sel)", "category": "analysis"},
    "potential": {"description": "Electrostatic potential across the box (selections: group)",
                  "category": "analysis", "default_selections": ["0"]},
    "dielectric": {"description": "Dielectric constant (selection: group)", "category": "analysis",
                   "default_selections": ["0"]},
    "traj": {"description": "Trajectory properties: box (-ob), positions (-ox), forces (-of) (selection: group)",
             "category": "analysis", "default_selections": ["0"]},
    # Preparation and file tools
    "pdb2gmx": {"description": "Make topology from a PDB (-ff, -water, -ignh; selections answer force field prompts if not given)",
                "category": "prepare"},
    "editconf": {"description": "Edit box and structure (-bt, -box, -d, -c)", "category": "prepare",
                 "default_selections": []},
    "solvate": {"description": "Add solvent (-cp, -cs, -p updates the topology)", "category": "prepare"},
    "genion": {"description": "Add ions (selections: solvent group to replace; use -neutral, -conc)",
               "category": "prepare", "needs_selections": True},
    "grompp": {"description": "Preprocess to a .tpr (-f mdp, -c, -p, -o; warnings are returned)",
               "category": "prepare"},
    "make_ndx": {"description": "Create index groups (selections: commands, ending with q)",
                 "category": "prepare", "terminator": "q"},
    "select": {"description": "Evaluate selection expressions (-select)", "category": "prepare"},
    "convert-tpr": {"description": "Modify a .tpr (-extend, -until, -nsteps; selection: group)",
                    "category": "prepare"},
    "trjcat": {"description": "Concatenate trajectories (-f a.xtc b.xtc)", "category": "trajectory"},
    "eneconv": {"description": "Concatenate energy files", "category": "trajectory"},
}

DEFAULT_CONFIG = {
    "commands": COMMANDS,
    "execution": {
        "timeout": 300,
        "working_directory": None,
    },
}

_FLAG_RE = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
_STDERR_TAIL = 40


def _tail(text: str, n: int = _STDERR_TAIL) -> str:
    lines = (text or "").strip().splitlines()
    return "\n".join(lines[-n:])


def _validate_flag(flag: str) -> None:
    if not isinstance(flag, str) or not re.match(_FLAG_RE, flag):
        raise ValueError(f"Invalid file flag {flag!r}: use the option name without the dash, for example 'f' or 'o'")


# Options that make gmx start other programs through the shell: trjconv -exec runs its value for every
# output frame (checked with GROMACS 2026.4, which also accepts --exec), and -w opens results in a viewer
# (xmgrace or GMX_VIEW_*) with the file name on a shell command line. Refused for every command, in
# parameters and as file option names alike.
FORBIDDEN_OPTIONS = frozenset({"exec", "w"})

# grompp's file options: they must come as named files (run_grompp) or input/output_files, so the
# include check sees exactly what grompp will read
GROMPP_FILE_OPTIONS = frozenset({"f", "c", "r", "rb", "n", "p", "t", "e", "ref", "imd", "qmi", "po", "pp", "o"})

def check_forbidden_options(args: List[Any]) -> None:
    """Raise ValueError when an argument names a forbidden option, however many dashes or which case."""
    for a in args:
        if isinstance(a, str) and a.startswith("-") and a.lstrip("-").split("=", 1)[0].lower() in FORBIDDEN_OPTIONS:
            raise ValueError(f"Option {a} is not allowed: it makes gmx run other programs")


def _validate_parameters(parameters: Optional[List[Any]]) -> List[str]:
    out: List[str] = []
    for p in parameters or []:
        if not isinstance(p, str) or p == "" or "\x00" in p:
            raise ValueError(f"Invalid parameter {p!r}: parameters must be non-empty strings")
        out.append(p)
    return out




def _check_parameter_paths(params: List[str], base: Optional[Path]) -> None:
    """
    With GMX_MCP_WORKDIR set, file paths given inside `parameters` (for example ["-o", "/x.xvg"])
    must stay inside it too. A parameter counts as a path when it contains a slash or ends in
    a GROMACS file suffix; selection expressions such as "name OW" do not.
    """
    if get_workdir_root() is None:
        return
    for p in params:
        if p.startswith("-") and not p.startswith(("-/", "-.")):
            continue
        if "/" in p or os.sep in p or p.lower().endswith(_FILE_SUFFIXES):
            _resolve_path(p, base or Path.cwd())


def _check_parameter_names(params: List[str]) -> None:
    """Arguments that look like file names (a slash, or a GROMACS or compressed suffix) must be shell safe."""
    for p in params:
        if p.startswith("-") and not p.startswith(("-/", "-.")):
            continue
        if "/" in p or os.sep in p or p.lower().endswith(_FILE_SUFFIXES + (".gz", ".z")):
            check_shell_safe_path(p)


def _resolve_path(path: Union[str, Path], base: Optional[Path]) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute() and base is not None:
        p = base / p
    return resolve_in_workdir(p)


def _fail(error: str, **extra) -> Dict[str, Any]:
    out = {"result": False, "error": error}
    out.update(extra)
    return out


# ==============================================================================
# Core function
# ==============================================================================
def run_gromacs_command(
    command_name: str,
    input_files: Optional[Dict[str, Union[str, Path]]] = None,
    output_files: Optional[Dict[str, Union[str, Path]]] = None,
    parameters: Optional[List[str]] = None,
    config: Optional[Dict[str, Any]] = None,
    selections: Optional[List[str]] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Run a whitelisted GROMACS command.

    Args:
        command_name: gmx subcommand, must be a key of COMMANDS
        input_files: {flag: path}; flag is the option name without the dash ('f', 's', 'e')
        output_files: {flag: path}; for stdout_to_file commands (dump) the first path receives stdout
        parameters: extra argv items, one list element per argument, for example ["-b", "10", "-sel", "name OW"]
        config: optional override of DEFAULT_CONFIG
        selections: answers to interactive prompts (group names, numbers or energy term names)
        **kwargs: verbose, timeout, working_directory

    Returns:
        dict with result, command, returncode, stdout, stderr, output_files (absolute paths of
        the outputs that exist), file_sizes ({path: bytes}), warnings, error (on failure)
    """
    config = deep_merge(DEFAULT_CONFIG, config)
    commands = config.get("commands", COMMANDS)
    timeout = kwargs.get("timeout", config["execution"]["timeout"])
    verbose = kwargs.get("verbose", False)
    if verbose:
        logger.setLevel(logging.DEBUG)

    if command_name not in commands:
        return _fail(
            f"Command '{command_name}' is not allowed. Allowed commands: {', '.join(sorted(commands))}",
            command=command_name,
        )
    cmd_cfg = commands[command_name]

    try:
        working_dir = kwargs.get("working_directory") or config["execution"].get("working_directory")
        base = resolve_in_workdir(working_dir) if working_dir else None
        if base is not None and not base.is_dir():
            raise ValueError(f"working_directory does not exist or is not a folder: {base}")

        in_paths: Dict[str, Path] = {}  # first file of each option
        in_more: Dict[str, List[Path]] = {}  # further files of options that take several (trjcat -f a b)
        for flag, filepath in (input_files or {}).items():
            _validate_flag(flag)
            if not filepath:
                continue
            items = list(filepath) if isinstance(filepath, (list, tuple)) else [filepath]
            resolved_items = []
            for item in items:
                resolved = _resolve_path(item, base)
                if not check_file_exists(resolved, f"Input file (-{flag})"):
                    return _fail(f"Input file for -{flag} not found: {resolved}", command=command_name)
                resolved_items.append(resolved)
            in_paths[flag] = resolved_items[0]
            in_more[flag] = resolved_items[1:]

        out_paths: Dict[str, Path] = {}
        for flag, filepath in (output_files or {}).items():
            _validate_flag(flag)
            if not filepath:
                continue
            out_paths[flag] = _resolve_path(filepath, base)

        extra = _validate_parameters(parameters)
        check_forbidden_options(extra + [f"-{flag}" for flag in list(in_paths) + list(out_paths)])
        for path in list(in_paths.values()) + [q for qs in in_more.values() for q in qs] + list(out_paths.values()):
            check_shell_safe_path(path)
        _check_parameter_names(extra)
        if command_name == "grompp":
            given = {a.lstrip("-").lower() for a in extra if a.startswith("-")}
            clash = sorted(given & GROMPP_FILE_OPTIONS)
            if clash:
                raise ValueError(f"For grompp pass files as input_files or output_files, not in parameters "
                                 f"(found: {', '.join('-' + c for c in clash)})")
        # The folder gmx runs in: the data folder, so stray files (step*.pdb, #backups#) stay with the
        # data; the sandbox root when there are no files. Relative paths in parameters are checked
        # against this same folder.
        cwd = base
        if cwd is None:
            anchor = next(iter(out_paths.values()), None) or next(iter(in_paths.values()), None)
            cwd = anchor.parent if anchor else get_workdir_root()
        _check_parameter_paths(extra, cwd or Path.cwd())
        gmx = find_gromacs_command()
        if extra:
            _check_file_option_values(extra, *_tool_help(tuple(gmx), command_name))
    except (ValueError, PathNotAllowedError, GromacsNotFoundError) as e:
        return _fail(str(e), command=command_name)

    warnings: List[str] = []
    full_cmd = gmx + [command_name]
    for flag, path in in_paths.items():
        full_cmd += [f"-{flag}", str(path)] + [str(q) for q in in_more.get(flag, [])]

    # output folders exist before gmx starts: it may run inside one of them (cwd above)
    for path in out_paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    stdout_target: Optional[Path] = None
    if cmd_cfg.get("stdout_to_file"):
        if out_paths:
            stdout_target = next(iter(out_paths.values()))
    else:
        for flag, path in out_paths.items():
            if path.exists():
                warnings.append(f"Output {path.name} already exists and will be overwritten (GROMACS keeps a #{path.name}.N# backup)")
            full_cmd += [f"-{flag}", str(path)]

    full_cmd += cmd_cfg.get("parameters", [])
    full_cmd += extra

    # Interactive answers
    stdin_text: Optional[str] = None
    if selections is not None:
        if not isinstance(selections, list) or not all(isinstance(s, str) for s in selections):
            return _fail("selections must be a list of strings", command=command_name)
        answers = list(selections)
        term = cmd_cfg.get("terminator")
        if term is not None and (not answers or answers[-1] != term):
            answers.append(term)
        stdin_text = "\n".join(answers) + "\n"
    elif "default_selections" in cmd_cfg:
        stdin_text = "".join(f"{s}\n" for s in cmd_cfg["default_selections"]) or None
    elif cmd_cfg.get("needs_selections"):
        stdin_text = "\n"  # lets gmx print the menu of valid choices

    if command_name == "grompp":
        # check what grompp will actually read, including its defaults topol.top and grompp.mdp
        try:
            run_dir = Path(cwd).resolve() if cwd else Path.cwd()
            check_grompp_includes(topology=in_paths.get("p") or run_dir / "topol.top",
                                  mdp=in_paths.get("f") or run_dir / "grompp.mdp", cwd=run_dir)
        except PathNotAllowedError as e:
            return _fail(str(e), command=command_name)

    logger.info("Executing: %s", " ".join(full_cmd))
    res = run_command_full(full_cmd, working_dir=cwd, timeout=timeout, stdin_text=stdin_text)

    if stdout_target is not None and res["success"]:
        stdout_target.write_text(res["stdout"])

    produced: List[str] = []
    sizes: Dict[str, int] = {}
    all_outputs = list(out_paths.values()) if not stdout_target else [stdout_target]
    for path in all_outputs:
        if path.exists():
            produced.append(str(path))
            sizes[str(path)] = path.stat().st_size

    if cmd_cfg.get("needs_selections") and selections is None and not res["success"]:
        warnings.append(
            "This command needs `selections`. The valid choices are listed in stdout/stderr."
        )

    result = {
        "result": res["success"],
        "command": res["command"],
        "returncode": res["returncode"],
        "stdout": res["stdout"],
        "stderr": res["stderr"],
        "output_files": produced,
        "file_sizes": sizes,
        "warnings": warnings,
        "metadata": {
            "command_name": command_name,
            "gmx_version": get_gmx_version().get("version"),
            "working_directory": str(cwd) if cwd else None,
        },
    }
    if not res["success"]:
        if res["timed_out"]:
            result["error"] = f"{command_name} timed out after {timeout} seconds"
        else:
            result["error"] = (summarize_gmx_error(res["stderr"]) or summarize_gmx_error(res["stdout"])
                               or "Command failed with no output")
            hint = _unknown_option_hint(command_name, res["stderr"] + res["stdout"])
            if hint:
                result["error"] += "\n" + hint["message"]
                result["valid_options"] = hint["valid_options"]
        # asking for the menu on purpose (no selections) is not worth an ERROR line in the log
        probe = cmd_cfg.get("needs_selections") and selections is None
        logger.log(logging.DEBUG if probe else logging.ERROR, "GROMACS command '%s' failed: %s",
                   command_name, result["error"][:500])
    return result


# ==============================================================================
# Suggestions for options GROMACS does not know
# ==============================================================================
_HELP_OPTION_RE = re.compile(r"^ -(\[no\])?([A-Za-z][\w-]*)")


def parse_help_options(help_text: str) -> List[str]:
    """Option names (without the dash) from `gmx help <tool>`; -[no]name gives name and noname."""
    names: List[str] = []
    for line in help_text.splitlines():
        m = _HELP_OPTION_RE.match(line)
        if m:
            names.append(m.group(2))
            if m.group(1):
                names.append("no" + m.group(2))
    return list(dict.fromkeys(names))


_PLAIN_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def _check_file_option_values(params: List[str], all_options: frozenset, file_options: frozenset) -> None:
    """
    Decide by position, not by looks: the value after a file option (from the tool's own help) is a
    file name, and gmx may hand file names to a shell. In parameters a file option takes exactly one
    plain name such as spc216.gro (a library file, or one in the run folder); paths and several files
    go through input_files or output_files, where they are resolved, sandboxed and checked.

    Fails closed: only a real option name of the tool ends a file option's value. gmx reads "-", "--"
    and numbers such as -.5 as values, so anything that is not one of its options counts as a value.
    """
    option, values = None, 0
    for a in params:
        name = a.lstrip("-").lower()
        if a.startswith("-") and name in all_options:
            option, values = (name, 0) if name in file_options else (None, 0)
            continue
        if option is None:
            continue
        values += 1
        if values > 1:
            raise ValueError(f"-{option} takes one file name in parameters; pass several files as a list in "
                             "input_files")
        if not _PLAIN_FILE_NAME.match(a) or a.lower().endswith((".gz", ".z")):
            raise ValueError(f"-{option} {a!r}: in parameters a file option takes a plain file name only "
                             "(letters, digits, . _ + -); pass paths as input_files or output_files")


_HELP_FILE_OPTION_RE = re.compile(r"^ -([A-Za-z][\w-]*)\s+(\[<|<file>)")


def parse_help_file_options(help_text: str) -> List[str]:
    """Options of `gmx help <tool>` that take file names: `-f [<.xtc/.trr/...>]`, `-sf <file>`."""
    return list(dict.fromkeys(m.group(1) for m in map(_HELP_FILE_OPTION_RE.match, help_text.splitlines()) if m))


@lru_cache(maxsize=64)
def _tool_help(gmx_key: Tuple[str, ...], command_name: str) -> Tuple[frozenset, frozenset]:
    """(all option names, file option names) of a tool, read from `gmx help <tool>` once per tool.
    Raises ValueError when the help cannot be read: the file option check must not pass by default,
    and lru_cache does not keep a raised error, so the next call tries again."""
    res = run_command_full(list(gmx_key) + ["help", command_name], timeout=60)
    names = parse_help_options(res["stdout"]) if res["success"] else []
    if not names:
        raise ValueError(f"Could not read the options of gmx {command_name} from `gmx help {command_name}`, "
                         "so its parameters cannot be checked; pass files through input_files or "
                         "output_files and try again")
    return (frozenset(n.lower() for n in names),
            frozenset(o.lower() for o in parse_help_file_options(res["stdout"])))


def _unknown_option_hint(command_name: str, output: str) -> Optional[Dict[str, Any]]:
    """When gmx rejected an option, name the closest valid ones and list them all (read from
    `gmx help <tool>` once per tool). Only runs after such a failure, so successful calls cost nothing."""
    bad = re.findall(r"Unknown command-line option -(\S+)", output)
    if not bad:
        return None
    try:
        res = run_command_full(find_gromacs_command() + ["help", command_name], timeout=60)
    except GromacsNotFoundError:
        return None
    valid = parse_help_options(res["stdout"]) if res["success"] else []
    if not valid:
        return None
    parts = []
    for name in dict.fromkeys(bad):
        close = difflib.get_close_matches(name, valid, n=3, cutoff=0.6)
        if close:
            parts.append(f"-{name}: did you mean {' or '.join('-' + c for c in close)}?")
    parts.append(f"Valid options for gmx {command_name}: {' '.join('-' + v for v in valid)}")
    return {"message": "\n".join(parts), "valid_options": list(valid)}


# ==============================================================================
# Shortening results for MCP clients
# ==============================================================================
STDOUT_PREVIEW_LINES = 40
_HEADER_KEYS = ("Executable:", "Data prefix:", "Working dir:")
_PROGRESS_RE = re.compile(r"^\s*Reading (energy )?frame\s+\d+\s+time\s")


def strip_gmx_noise(text: str, drop_term_menu: bool = False) -> str:
    """
    Remove what gmx prints for a person at a terminal: the banner and command line header (the
    command is reported separately), per frame "Reading frame" progress lines, the closing
    quote, and with drop_term_menu the energy term menu that was answered by `selections`.
    Reports that live on stderr (gmx check, "Last frame", group lists, notes) are kept.
    """
    lines = (text or "").splitlines()
    out: List[str] = []
    i, n = 0, len(lines)
    while i < n:
        line, s = lines[i], lines[i].strip()
        if ":-) GROMACS - " in line or s.startswith(_HEADER_KEYS) or _PROGRESS_RE.match(line):
            i += 1
        elif s == "Command line:":
            i += 1
            while i < n and lines[i].startswith(" ") and lines[i].strip():
                i += 1
        elif s.startswith("GROMACS reminds you:"):
            i += 1
            while i < n and lines[i].strip():  # long quotes wrap
                i += 1
        elif drop_term_menu and s.startswith("Select the terms you want"):
            # instructions, a dashed rule, then rows of terms up to a blank line
            while i < n and not lines[i].startswith("---"):
                i += 1
            i += 1
            while i < n and lines[i].strip():
                i += 1
        else:
            out.append(line)
            i += 1
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def compact_for_client(result: Dict[str, Any], command_name: str, selections_given: bool) -> Dict[str, Any]:
    """
    A successful run_gromacs_command result made small enough for an MCP response: stderr
    without terminal noise and, when stdout was saved to a file (dump), only its first lines.
    Failures are returned unchanged because the full text helps to diagnose them.
    Library callers that parse output (the analysis tools) use the full result instead.
    """
    if not result.get("result"):
        return result
    out = dict(result)
    out["stderr"] = strip_gmx_noise(result.get("stderr", ""), drop_term_menu=selections_given)
    saved = result.get("output_files") or []
    if COMMANDS.get(command_name, {}).get("stdout_to_file") and saved:
        lines = (result.get("stdout") or "").splitlines()
        if len(lines) > STDOUT_PREVIEW_LINES:
            out["stdout"] = ("\n".join(lines[:STDOUT_PREVIEW_LINES])
                             + f"\n... [{len(lines) - STDOUT_PREVIEW_LINES} more lines in {saved[0]}]")
    return out


def list_available_commands() -> Dict[str, str]:
    """Command names mapped to descriptions."""
    return {name: info.get("description", f"GROMACS {name}") for name, info in COMMANDS.items()}


def list_commands_by_category() -> Dict[str, List[str]]:
    """Command names grouped by category."""
    grouped: Dict[str, List[str]] = {}
    for name, info in COMMANDS.items():
        grouped.setdefault(info.get("category", "other"), []).append(name)
    return grouped


def get_command_help(command_name: str) -> str:
    """Config derived summary for a whitelisted command."""
    cfg = COMMANDS.get(command_name)
    if not cfg:
        return f"Unknown command: {command_name}"
    lines = [f"gmx {command_name}: {cfg.get('description', '')}"]
    if "default_selections" in cfg:
        lines.append(f"Default selections: {cfg['default_selections']}")
    if cfg.get("needs_selections"):
        lines.append("Requires `selections`.")
    if cfg.get("parameters"):
        lines.append(f"Default parameters: {' '.join(cfg['parameters'])}")
    return "\n".join(lines)


def get_gmx_help(command_name: str, timeout: int = 60) -> Dict[str, Any]:
    """Return the real `gmx help <command>` text so valid flags can be looked up."""
    if command_name not in COMMANDS and command_name not in ("mdrun", "help", "commands"):
        return _fail(f"Unknown command '{command_name}'. Known: {', '.join(sorted(COMMANDS))}, mdrun")
    try:
        gmx = find_gromacs_command()
    except GromacsNotFoundError as e:
        return _fail(str(e))
    res = run_command_full(gmx + ["help", command_name], timeout=timeout)
    text = res["stdout"] + res["stderr"]
    return {"result": res["success"], "command": res["command"], "help": text,
            **({} if res["success"] else {"error": summarize_gmx_error(text)})}


# ==============================================================================
# CLI Interface
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--command', '-cmd', help='GROMACS command to execute (e.g. rms, energy, trjconv)')
    parser.add_argument('--input', '-i', action='append', nargs=2, metavar=('FLAG', 'FILE'),
                        help='Input file: -i FLAG FILE (repeatable)')
    parser.add_argument('--output', '-o', action='append', nargs=2, metavar=('FLAG', 'FILE'),
                        help='Output file: -o FLAG FILE (repeatable)')
    parser.add_argument('--param', '-p', action='append', help='Extra argument (repeatable, one per argv item)')
    parser.add_argument('--select', '-s', action='append', help='Answer for an interactive prompt (repeatable)')
    parser.add_argument('--config', '-c', help='Config file (JSON format)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable verbose output')
    parser.add_argument('--timeout', type=int, help='Command timeout in seconds (default: 300)')
    parser.add_argument('--list-commands', action='store_true', help='List available GROMACS commands')
    parser.add_argument('--help-command', help='Show help for specific command')
    args = parser.parse_args()

    if args.list_commands:
        print("Available GROMACS commands:")
        for cmd, desc in list_available_commands().items():
            print(f"  {cmd:<12} {desc}")
        return 0
    if args.help_command:
        print(get_command_help(args.help_command))
        return 0
    if not args.command:
        parser.error("--command is required unless using --list-commands or --help-command")

    config = None
    if args.config:
        with open(args.config) as f:
            config = json.load(f)

    kwargs = {}
    if args.verbose:
        kwargs['verbose'] = True
    if args.timeout:
        kwargs['timeout'] = args.timeout

    result = run_gromacs_command(
        command_name=args.command,
        input_files=dict(args.input) if args.input else None,
        output_files=dict(args.output) if args.output else None,
        parameters=args.param,
        selections=args.select,
        config=config,
        **kwargs
    )
    if result['result']:
        print(f"Success: gmx {args.command} completed")
        for info in result.get('output_files', []):
            print(f"  {info}")
        for w in result.get('warnings', []):
            print(f"  warning: {w}")
        return 0
    print(f"Failed: {result.get('error', 'Unknown error')}")
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
