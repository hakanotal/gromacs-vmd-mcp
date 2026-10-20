#!/usr/bin/env python3
"""
Script: make_example_data.py
Description: Generate the small example data set in examples/data

Creates a 2.2 nm cubic SPC/E water box (about 350 molecules) (OPLS-AA force field files), minimizes it, and writes:
    water.gro   solvated starting structure
    topol.top   topology
    em.mdp      minimization parameters
    md.mdp      short NVT dynamics parameters (500 steps)
    sample.tpr  run input for md.mdp starting from the minimized structure

Usage:
    python scripts/make_example_data.py [--output examples/data]   regenerate everything
    python scripts/make_example_data.py --tpr-only                  only (re)build sample.tpr

The text files are kept in git. sample.tpr is binary and tied to the GROMACS version that
wrote it (older versions cannot read newer .tpr files), so it is built locally with
--tpr-only by quick_setup.sh, the Docker build and the test suite. Requires gmx.
"""

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from lib import mdp_templates
from lib.gromacs_utils import find_gromacs_command, run_command_full, summarize_gmx_error

TOPOLOGY = """#include "oplsaa.ff/forcefield.itp"
#include "oplsaa.ff/spce.itp"

[ system ]
Water box

[ molecules ]
"""


def _run(cmd, cwd, stdin=None):
    res = run_command_full(cmd, working_dir=cwd, timeout=300, stdin_text=stdin)
    if not res["success"]:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {summarize_gmx_error(res['stderr'])}")
    return res


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", default=str(Path(__file__).resolve().parent.parent / "examples" / "data"))
    parser.add_argument("--tpr-only", action="store_true",
                        help="Build sample.tpr from the existing water.gro, topol.top, em.mdp and md.mdp")
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    gmx = find_gromacs_command()

    if args.tpr_only:
        missing = [n for n in ("water.gro", "topol.top", "em.mdp", "md.mdp") if not (out / n).exists()]
        if missing:
            print(f"Missing {', '.join(missing)} in {out}; run without --tpr-only to regenerate everything")
            return 1
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            for name in ("water.gro", "topol.top", "em.mdp", "md.mdp"):
                shutil.copy2(out / name, work / name)
            _run(gmx + ["grompp", "-f", "em.mdp", "-c", "water.gro", "-p", "topol.top", "-o", "em.tpr"], work)
            _run(gmx + ["mdrun", "-deffnm", "em", "-ntmpi", "1"], work)
            _run(gmx + ["grompp", "-f", "md.mdp", "-c", "em.gro", "-p", "topol.top", "-o", "sample.tpr"], work)
            shutil.copy2(work / "sample.tpr", out / "sample.tpr")
        print(f"Wrote {out / 'sample.tpr'}")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "topol.top").write_text(TOPOLOGY)
        _run(gmx + ["solvate", "-cs", "spc216.gro", "-box", "2.2", "2.2", "2.2", "-o", "water.gro",
                    "-p", "topol.top"], work)

        em_text, _ = mdp_templates.render_stage("em", overrides={"emtol": 100.0, "nsteps": 500})
        (work / "em.mdp").write_text(em_text)
        md_text, _ = mdp_templates.render_stage(
            "nvt", overrides={"nsteps": 500, "gen-seed": 42, "nstenergy": 50, "nstcalcenergy": 50,
                              "nstlog": 100, "nstxout-compressed": 50}, temperature=300)
        (work / "md.mdp").write_text(md_text)

        _run(gmx + ["grompp", "-f", "em.mdp", "-c", "water.gro", "-p", "topol.top", "-o", "em.tpr"], work)
        _run(gmx + ["mdrun", "-deffnm", "em", "-ntmpi", "1"], work)
        _run(gmx + ["grompp", "-f", "md.mdp", "-c", "em.gro", "-p", "topol.top", "-o", "sample.tpr"], work)

        for name in ("water.gro", "topol.top", "em.mdp", "md.mdp", "sample.tpr"):
            shutil.copy2(work / name, out / name)

    print(f"Wrote example data to {out}")
    for p in sorted(out.iterdir()):
        print(f"  {p.name} ({p.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
