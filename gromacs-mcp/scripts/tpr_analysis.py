#!/usr/bin/env python3
"""
Script: tpr_analysis.py
Description: Read and analyze GROMACS TPR files

Usage:
    python scripts/tpr_analysis.py --input <tpr_file> --output <output_file>
    python scripts/tpr_analysis.py --inputs a.tpr --inputs b.tpr --output-dir results/ --result-json summary.json

Example:
    python scripts/tpr_analysis.py --input examples/data/sample.tpr --output results/analysis.txt
"""

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from lib.gromacs_utils import (
    GromacsNotFoundError,
    check_file_exists,
    deep_merge,
    find_gromacs_command,
    parse_tpr_dump_output,
    resolve_in_workdir,
    run_command_full,
    summarize_gmx_error,
    write_json_atomic,
)

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "analysis": {
        "timeout": 60,
        "include_raw_dump": False,
        "output_format": "text",
    },
}


def run_tpr_analysis(
    input_file: Union[str, Path],
    output_file: Optional[Union[str, Path]] = None,
    config: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Read and analyze a GROMACS TPR file using gmx dump.

    Args:
        input_file: Path to TPR input file
        output_file: Path to save analysis results (text or json depending on `format`)
        config: Configuration dict
        **kwargs:
            verbose: enable debug logging
            format: "text" or "json" for the saved file
            raw_dump_path: if set, the full `gmx dump` text is written here
            timeout: seconds for gmx dump

    Returns:
        dict with result, tpr_info (parsed summary), analysis_text, output_file,
        raw_dump_file (path or None). The raw dump itself is never embedded.
    """
    input_path = Path(input_file)
    config = deep_merge(DEFAULT_CONFIG, config)
    analysis_cfg = config["analysis"]

    output_format = kwargs.get('format', analysis_cfg['output_format'])
    timeout = kwargs.get('timeout', analysis_cfg['timeout'])
    raw_dump_path = kwargs.get('raw_dump_path')
    if kwargs.get('verbose'):
        logger.setLevel(logging.DEBUG)

    logger.info("Analyzing TPR file: %s", input_path)
    if not check_file_exists(input_path, "TPR file"):
        raise FileNotFoundError(f"TPR file not found: {input_path}")

    try:
        gmx_cmd = find_gromacs_command()
        res = run_command_full(gmx_cmd + ["dump", "-s", str(input_path)], timeout=timeout)
        if not res["success"]:
            return {
                "result": False,
                "error": summarize_gmx_error(res["stderr"]) or "Failed to run gmx dump",
                "stderr": res["stderr"],
                "command": res["command"],
            }

        dump_text = res["stdout"]
        tpr_info = parse_tpr_dump_output(dump_text)
        tpr_info['file_size'] = input_path.stat().st_size
        tpr_info['file_path'] = str(input_path.absolute())
        analysis_text = format_analysis_text(tpr_info)

        raw_file = None
        if raw_dump_path:
            raw_file = Path(raw_dump_path)
            raw_file.parent.mkdir(parents=True, exist_ok=True)
            raw_file.write_text(dump_text)

        output_path = None
        if output_file:
            output_path = Path(output_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_format.lower() == "json":
                write_json_atomic(output_path, {
                    "tpr_info": tpr_info,
                    "analysis_text": analysis_text,
                    "raw_dump_file": str(raw_file) if raw_file else None,
                })
            else:
                output_path.write_text(analysis_text)
            logger.info("Analysis saved to: %s", output_path)

        return {
            "result": True,
            "tpr_info": tpr_info,
            "analysis_text": analysis_text,
            "output_file": str(output_path) if output_path else None,
            "raw_dump_file": str(raw_file) if raw_file else None,
            "metadata": {
                "input_file": str(input_path),
                "output_format": output_format,
                "file_size": tpr_info['file_size'],
            },
        }

    except GromacsNotFoundError as e:
        return {"result": False, "error": str(e)}
    except Exception as e:
        logger.error("Error during TPR analysis: %s", e)
        return {"result": False, "error": str(e), "metadata": {"input_file": str(input_path)}}


def run_tpr_batch(
    input_files: List[Union[str, Path]],
    output_dir: Optional[Union[str, Path]] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Analyze several TPR files. Writes <output_dir>/<stem>.<format> per file when
    output_dir is given. One failure does not stop the others: the batch succeeds when at
    least one file does, and failed files are named in warnings and in their own entries.
    """
    fmt = kwargs.get("format", "text")
    ext = "json" if fmt == "json" else "txt"
    entries = []
    for f in input_files:
        p = Path(f)
        out_file = Path(output_dir) / f"{p.stem}.{ext}" if output_dir else None
        try:
            res = run_tpr_analysis(p, output_file=out_file, **kwargs)
        except FileNotFoundError as e:
            res = {"result": False, "error": str(e)}
        entries.append({
            "input": str(p),
            "result": res["result"],
            "tpr_info": res.get("tpr_info"),
            "output_file": res.get("output_file"),
            "error": res.get("error"),
        })
    ok = sum(1 for e in entries if e["result"])
    failed = [e for e in entries if not e["result"]]
    out = {
        "result": ok > 0,
        "total": len(entries),
        "succeeded": ok,
        "failed": len(failed),
        "results": entries,
        "output_files": [e["output_file"] for e in entries if e.get("output_file")],
        "warnings": [f"{len(failed)} of {len(entries)} files failed: "
                     + "; ".join(f"{Path(e['input']).name}: {e['error']}" for e in failed)] if failed else [],
    }
    if not ok:
        out["error"] = f"All {len(entries)} files failed" + (f"; first: {failed[0]['error']}" if failed else "")
    return out


def format_analysis_text(tpr_info: Dict[str, Any]) -> str:
    """Format the parsed TPR information as readable text."""
    lines = ["=== TPR File Analysis Results ===",
             f"File: {tpr_info.get('file_path', 'Unknown')}",
             f"Size: {tpr_info.get('file_size', 0):,} bytes", ""]

    lines.append("Simulation Parameters:")
    if 'title' in tpr_info:
        lines.append(f"  Title: {tpr_info['title']}")
    if 'integrator' in tpr_info:
        lines.append(f"  Integrator: {tpr_info['integrator']}")
    if 'nsteps' in tpr_info:
        lines.append(f"  Number of steps: {tpr_info['nsteps']:,}")
    if 'dt' in tpr_info:
        lines.append(f"  Time step: {tpr_info['dt']} ps")
        if 'nsteps' in tpr_info:
            lines.append(f"  Total simulation time: {tpr_info['nsteps'] * tpr_info['dt']:.1f} ps")
    for key, label in (("coulombtype", "Electrostatics"), ("tcoupl", "Thermostat"), ("pcoupl", "Barostat")):
        if key in tpr_info:
            lines.append(f"  {label}: {tpr_info[key]}")
    lines.append("")

    lines.append("System Information:")
    if 'natoms' in tpr_info:
        lines.append(f"  Number of atoms: {tpr_info['natoms']:,}")
    if 'nmolecules' in tpr_info:
        lines.append(f"  Number of molecules: {tpr_info['nmolecules']:,}")
    if 'temperature' in tpr_info:
        lines.append(f"  Reference temperature: {tpr_info['temperature']} K")
    if 'pressure' in tpr_info:
        lines.append(f"  Reference pressure: {tpr_info['pressure']} bar")
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--input', '-i', help='Input TPR file path')
    parser.add_argument('--inputs', action='append',
                        help='TPR file for batch mode (repeat the option for each file)')
    parser.add_argument('--output', '-o', help='Output file path (single input)')
    parser.add_argument('--output-dir', help='Output directory (batch mode)')
    parser.add_argument('--result-json', help='Write the full result dict as JSON here')
    parser.add_argument('--format', '-f', choices=['text', 'json'], default='text')
    parser.add_argument('--config', '-c', help='Config file (JSON format)')
    parser.add_argument('--verbose', '-v', action='store_true')
    parser.add_argument('--include-raw', action='store_true',
                        help='Also save the full gmx dump next to the output file')
    parser.add_argument('--info', action='store_true', help='Show TPR file format information')
    args = parser.parse_args()

    if args.info:
        print("TPR File Format Information:")
        print("- TPR (Topology and Parameters Run) files contain all simulation settings")
        print("- Generated by gmx grompp from .mdp, .gro/.pdb, and .top files")
        print("- Binary format containing topology, parameters, and coordinates")
        print("- Required input for gmx mdrun")
        print("- Can be analyzed using gmx dump, gmx check, etc.")
        return 0

    if not args.input and not args.inputs:
        parser.error("one of --input or --inputs is required")

    config = None
    if args.config:
        with open(args.config) as f:
            config = json.load(f)

    kwargs = {'format': args.format}
    if args.verbose:
        kwargs['verbose'] = True

    try:
        if args.inputs:
            files = [str(resolve_in_workdir(f)) for f in args.inputs]  # re-check the sandbox here too
            result = run_tpr_batch(files, output_dir=args.output_dir, config=config, **kwargs)
        else:
            if args.include_raw and args.output:
                kwargs['raw_dump_path'] = str(Path(args.output).with_suffix('.dump.txt'))
            result = run_tpr_analysis(args.input, output_file=args.output, config=config, **kwargs)
            if result['result']:
                print(result['analysis_text'])
    except Exception as e:
        result = {"result": False, "error": str(e)}

    if args.result_json:
        write_json_atomic(args.result_json, result)

    if result['result']:
        print("Success: analysis completed")
        return 0
    print(f"Failed: {result.get('error', 'one or more files failed')}")
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
