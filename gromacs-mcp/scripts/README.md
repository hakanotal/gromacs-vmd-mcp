# Scripts

These are the programs the MCP server runs. Each also works from the command line and accepts `--config file.json` (see `configs/default_config.json`).

| Script | Purpose | Example |
|---|---|---|
| `gromacs_command.py` | Run a whitelisted gmx tool without a shell; answer prompts with `--select` | `python scripts/gromacs_command.py --command energy -i f run.edr -o o e.xvg --select Potential` |
| `md_simulation.py` | mdrun with threading, GPU, checkpoint, resume and time limit options | `python scripts/md_simulation.py --input run.tpr --output-dir out/ --nsteps 5000` |
| `tpr_analysis.py` | Summarize one or many .tpr files | `python scripts/tpr_analysis.py --inputs a.tpr --inputs b.tpr --output-dir summaries/ --format json` |
| `gromacs_workflow.py` | Multi-step workflows with resume | `python scripts/gromacs_workflow.py -w full_md -s protein.pdb -o prep/ --option nvt_ps=50` |
| `make_example_data.py` | Build `examples/data` (`--tpr-only` builds just `sample.tpr`) | `python scripts/make_example_data.py --tpr-only` |

`--list-commands` and `--help-command NAME` on `gromacs_command.py` show the whitelist. `--result-json PATH` (all scripts but the example builder) writes the full result as JSON; the job manager uses it.

Shared code lives in `lib/`: `gromacs_utils.py` (finding and running gmx, path sandbox, parsers) and `mdp_templates.py` (templates and lint rules).
