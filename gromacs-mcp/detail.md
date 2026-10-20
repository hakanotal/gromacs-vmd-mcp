# GROMACS MCP: reference

This document covers installation details, every tool, the result format, jobs, workflows, the command line scripts and known limitations. The [README](README.md) has the short version.

## Contents

- [Installation](#installation)
- [How results look](#how-results-look)
- [Tools](#tools)
- [Background jobs](#background-jobs)
- [Workflows](#workflows)
- [Resources and prompts](#resources-and-prompts)
- [Command line scripts](#command-line-scripts)
- [Security model](#security-model)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)

## Installation

### Supported setups

| GROMACS from | How the server finds it | Notes |
|---|---|---|
| Homebrew (`brew install gromacs`) | `gmx` on PATH | Works with `quick_setup.sh`; no conda needed |
| Cluster module or system package | `gmx` or `gmx_mpi` on PATH | Set `GMX_MCP_GMX` when the MCP client starts with a minimal PATH |
| conda-forge | `./env/bin/gmx` via PATH, or `GMX_MCP_GMX` | `quick_setup.sh` installs it into `./env` when gmx is missing and conda or mamba exists |
| Docker image | `/opt/conda/bin/gmx` | GROMACS version set by the `GROMACS_VERSION` build argument |

MCP clients often start servers with a reduced PATH. If `check_environment` says GROMACS was not found, add `"env": {"GMX_MCP_GMX": "/full/path/to/gmx"}` to the server entry.

### Local install

```bash
bash quick_setup.sh [--dev] [--plots] [--gromacs 2025.4]
```

The script creates `./env` (a venv, or a conda env when it also has to install GROMACS), installs `requirements.txt`, builds `examples/data/sample.tpr` with your GROMACS, and prints the registration command and a `.mcp.json` block. `uv sync --extra dev` is an alternative that uses `pyproject.toml`.

### Example data

`examples/data` holds a 2.2 nm cubic box of SPC/E water (OPLS-AA): `water.gro`, `topol.top`, `em.mdp` and `md.mdp` (500 NVT steps). `sample.tpr` is not in git because `.tpr` files are tied to the GROMACS version that wrote them; `python scripts/make_example_data.py --tpr-only` builds it, and the setup script, Docker build and test suite do this for you.

## How results look

Every tool returns one flat JSON object:

| Field | Always present | Meaning |
|---|---|---|
| `status` | yes | `success` or `error` |
| `error` | on error | What went wrong. For GROMACS failures this is the actual GROMACS error block (banner removed), plus a hint for well known problems |
| `warnings` | yes | List of things worth reading even when the call succeeded |
| `output_files` | yes | Absolute paths of the files written (`run_gromacs_command` and MD jobs add `file_sizes`, `{path: bytes}`) |
| `command` | when gmx ran | The exact command line |
| `truncated` | when used | Fields shortened to keep the response small (see `GMX_MCP_MAX_CHARS`) |

Tool specific fields sit next to these. A failed command still returns its `stdout`, `stderr` and `command`. When you do not give an output path, tools write a new uniquely named file under `results/`, so repeated or concurrent calls never overwrite each other.

## Tools

### Preparation

| Tool | What it does |
|---|---|
| `create_mdp(stage, output_file?, time_ps?, temperature?, pressure?, overrides?)` | Writes a parameter file from a template (`em`, `ions`, `nvt`, `npt`, `nve`, `free_energy`, `umbrella`), explains each option, and lints the result |
| `validate_mdp(mdp_file? or mdp_text?, structure?, topology?, index_file?)` | Lint rules (coupling during minimization, Berendsen, short tau-p, missing tc-grps, no trajectory output, nstenergy vs nstcalcenergy, and more). With structure and topology it also runs grompp in a temporary folder and returns its errors and warnings |
| `run_grompp(mdp_file, structure, topology, output_tpr?, index_file?, restraint_reference?, checkpoint?, maxwarn=0)` | Builds a `.tpr` and returns every NOTE, WARNING and ERROR. `maxwarn` stays 0 unless you raise it, and raising it is reported |
| `run_minimization(structure, topology, output_dir?, emtol=1000, nsteps=5000, ...)` | grompp plus mdrun for energy minimization in its own folder. Returns converged, steps, final potential energy and maximum force |
| `extend_simulation(input_tpr, output_tpr, extend_ps? or until_ps? or nsteps?)` | A longer `.tpr` via `convert-tpr`, to continue a run with `submit_md_simulation(resume=True)` |
| `list_force_fields()` | Installed force fields with descriptions and supported water models |
| `run_full_md_pipeline(structure, ...)` | pdb2gmx, box, solvent, ions, minimization, NVT, NPT and a production `.tpr`, resumable. Optional `submit_production=True` starts the production run as a job |

### Running

| Tool | What it does |
|---|---|
| `submit_md_simulation(input_file, nsteps?, output_dir?, ntmpi?, ntomp?, gpu?, maxh?, max_runtime_seconds?, checkpoint_minutes?, pin?, resume?, multidir?, replex?)` | mdrun as a background job. Defaults to one thread-MPI rank (works for any box size) and up to 8 OpenMP threads. Without `nsteps` the `.tpr` value is used; with it the run input is rewritten by `convert-tpr` |
| `run_gromacs_command(command_name, input_files?, output_files?, parameters?, selections?, timeout=300, working_directory?)` | Any of 33 whitelisted gmx tools. `selections` answers interactive prompts in order (energy term names, group names). Commands that accept selection syntax (`rdf`, `msd`, `select`, `distance`) take `parameters=["-sel", "name OW"]`. On success `stderr` is shortened to what matters (no banner, per frame progress or answered term menu) and a `dump` saved to a file returns its first 40 lines; failures come back in full. An option gmx does not know gets "did you mean" suggestions and `valid_options` |
| `run_gromacs_workflow(workflow_type, input_files?, output_file?, options?)` | See [Workflows](#workflows) |
| `submit_batch_analysis(input_files, analysis_type="tpr", output_dir?)` | Summarizes many `.tpr` files in one job. A missing or unreadable file is reported in its own entry and does not stop the rest; the job completes when at least one file succeeds |

### Analysis

| Tool | Returns |
|---|---|
| `energy_summary(edr_file, terms?, begin_ps?, end_ps?)` | GROMACS' average table (average, error estimate, RMSD, drift, unit) and, from the time series, full and equilibrated statistics: mean, std, `sem` corrected for correlation between frames, block error, `statistical_inefficiency` and `n_independent` (a warning flags terms with fewer than 10 independent samples). The equilibration point is found with the MSER-5 rule on potential energy, temperature, density and volume; with fewer than 20 frames it is reported as `null` with a warning. Unknown terms are skipped with a warning |
| `analyze_rdf(trajectory, structure, reference, selection?, begin_ps?, ...)` | First peak position and height, first minimum, coordination number up to the first minimum |
| `analyze_msd(trajectory, structure, selection, begin_ps?, trestart_ps=10, ...)` | Self-diffusion coefficient with its error; optional Yeh-Hummer finite size correction from temperature, viscosity and box length |
| `trajectory_report(trajectory, structure?, energy_file?)` | Frames, timestep, time range, atom count, box change, non-finite energies and isolated energy spikes, as plain findings |
| `read_xvg(path, max_points=200, begin?, end?)` | Title, axis labels, legends, downsampled data and per column statistics of any `.xvg` |
| `plot_xvg(xvg_file, output_png?, columns?)` | A PNG plot (needs matplotlib) |
| `trjconv_preset(trajectory, structure, output_file, preset, group="System", ...)` | `whole`, `nojump`, `mol`, `center`, `fit`, `window`, `pdb`, with the right group prompts answered |
| `analyze_tpr(input_file, include_raw=False)` | Integrator, steps, time step, couplings, reference temperature and pressure, atom and molecule counts. The full `gmx dump` is written to a file only when asked |

### Information

`check_environment` (run this first when something fails), `get_server_info`, `list_available_commands` (generated from the registry and the command whitelist), `get_command_help(command_name)` (the real `gmx help` text).

## Background jobs

```
queued -> running -> completed | stopped | failed | cancelled
```

`stopped` means mdrun ended cleanly before its last step and wrote a checkpoint: `maxh` or `max_runtime_seconds` was reached, or mdrun was stopped from outside the server (for example found after a server restart). A cancel gives `cancelled`. `get_job_status` gives the reason in `stop_reason`, the outputs so far are in `get_job_result`, and `resume_job` continues the run.

- At most `GMX_MCP_MAX_JOBS` jobs run at once; the rest wait in order. `get_job_status` shows `queue_position`.
- Running MD jobs report `progress`: step, simulated time, percent, ETA and, at the end, ns/day, plus warnings found in `md.log`.
- `cancel_job` sends SIGTERM to the whole process group so mdrun writes a checkpoint, then SIGKILL after 30 s. A cancelled job stays cancelled.
- `resume_job(job_id)` continues a stopped, cancelled or failed MD job from `md.cpt` in a new job.
- Jobs outlive the server. Every MCP client reconnect restarts a stdio server; on start, and on every status query, a job whose processes are gone is finalized from evidence: its result file, or `md.log` showing the last step reached `nsteps`. mdrun also prints "Finished mdrun" after a cancel or `maxh`, so that line alone means the job is `stopped` (or `cancelled` when a cancel was requested), and progress reports `finished` and `complete` separately.
- Several server instances can share one jobs folder (two clients on one checkout). Processes are identified by pid plus start time, so a reused pid is not mistaken for a job; a job whose processes are alive is never touched; queued jobs of a server that went away are claimed under a file lock, so exactly one instance runs them; and an output folder can only be used by one unfinished job at a time.
- Job records live in `$GMX_MCP_HOME/jobs/<id>/` (`metadata.json`, `job.log`, `output.json`). MD outputs go to `output_dir` (default `$GMX_MCP_HOME/runs/<id>/run/`, outside the record) as `md.tpr`, `md.log`, `md.edr`, `md.gro`, `md.cpt`, `md.xtc`.
- `delete_job` removes a finished job's record files only, never outputs (even for older jobs whose outputs sit inside the record folder). `clean_jobs(older_than_days, keep_last)` does that in bulk.
- A cancelled MD job keeps its slot until mdrun has written its checkpoint and exited (SIGKILL after 30 s); the job's result then says it was stopped by a signal.

## Workflows

| Type | Inputs | Steps |
|---|---|---|
| `full_md` | `structure` (+ `topology`, `index`) | pdb2gmx (only without a topology), editconf, solvate, grompp + genion, em, nvt, npt, production grompp |
| `preprocessing` | `structure` (+ `topology`) | pdb2gmx, editconf, solvate (and ions with `ions=true`) |
| `simple_md` | `structure`, `topology`, `mdp` | grompp, (convert-tpr for `nsteps`), mdrun |
| `analysis` | `structure`, `trajectory` (+ `energy`) | rms, rmsf (group `System` by default, set `group`), energy terms |
| `demo` | none | `gmx --version`, `gmx help commands` (a self test) |

Options go in `options` (or as typed parameters of `run_full_md_pipeline`): `force_field` (oplsaa), `water_model` (spce), `box_distance` (1.0 nm), `box_type` (cubic), `set_box`, `solvate`, `ions`, `salt_concentration` (0.15 M), `em_steps`, `nvt_ps`, `npt_ps`, `production_ps`, `temperature`, `pressure`, `maxwarn` (0), `threads`, `stages`, `resume`.

Re-running a workflow with the same output folder skips a step only when its command and the content of every file it reads are unchanged (a file whose size and modification time match the last run is not read again), so changing an option (for example `em_steps` or `box_distance`) reruns that step and everything downstream of it. Changed input files, or `resume=false`, restart from fresh copies of the inputs. Steps never edit their inputs: solvate and genion write `solv.top` and `ions.top` (reported as `final_topology`). When you supply your own topology and ions are added, the workflow inserts `#include "<forcefield>.ff/ions.itp"` into the working copy if it is missing. The production run is not part of `full_md`; submit `md.tpr` as a job.

## Resources and prompts

- `gromacs://jobs/{job_id}` lists a job's files; `gromacs://jobs/{job_id}/{filename}` returns a text file (`md.log`, `job.log`, `.xvg`, `.mdp`, `.gro`, ...) up to 200 KB (longer files show the start and the end). Binary files are not served.
- `gromacs://examples/{name}` returns `water.gro`, `topol.top`, `em.mdp` or `md.mdp`.
- Prompts: `setup_water_box`, `equilibrate_protein`, `analyze_trajectory_rmsd`.

## Command line scripts

The scripts in `scripts/` are what the server runs, and they work on their own:

```bash
python scripts/gromacs_command.py --command energy -i f run.edr -o o e.xvg --select Potential --select Density
python scripts/tpr_analysis.py --input run.tpr
python scripts/tpr_analysis.py --inputs a.tpr --inputs b.tpr --output-dir summaries/ --format json
python scripts/md_simulation.py --input run.tpr --output-dir out/ --nsteps 5000 --ntomp 4
python scripts/gromacs_workflow.py -w full_md -s protein.pdb -o prep/ --option nvt_ps=50
python scripts/make_example_data.py [--tpr-only]
```

Each accepts `--config file.json`; `configs/default_config.json` shows every section and default.

## Security model

- gmx is always started with an argument list, never through a shell, so quotes, `;` and `$(...)` in file names or parameters are passed through literally.
- Only whitelisted gmx subcommands run (`list_available_commands` shows them). `mdrun` only runs through the job tools. Options that make gmx start other programs (`trjconv -exec`, `-w`) are refused in any spelling, and file names with characters a shell acts on, or compressed files, are refused because gmx opens some files through a shell. Which options take files is read from each tool's help: in `parameters` they only accept plain names (such as the library file `spc216.gro`); paths go through `input_files` or `output_files`.
- With `GMX_MCP_WORKDIR` set, every input and output path (after resolving symlinks and `..`) must be inside it, including file paths inside `parameters`, `extra_parameters` and `multidir`. Default folders (`GMX_MCP_HOME`, results, job outputs, temporary files) move inside it automatically unless set explicitly, and commands without file arguments run inside it.
- In Docker `GMX_MCP_HOME` is `/data`. To use a sandbox there, set both, for example `-e GMX_MCP_WORKDIR=$(pwd) -e GMX_MCP_HOME=$(pwd)/.gromacs_mcp`; `check_environment` reports folders that fall outside the sandbox, and jobs whose default folder would be outside are refused at submission.
- grompp may only read included files (topology `#include`, mdp `include = -I...`) from the sandbox or the GROMACS force field directory.
- Jobs only run scripts from `scripts/` (plus `GMX_MCP_SCRIPT_DIRS`), job ids must be 8 hex characters, and list arguments reach the scripts as separate arguments, so a comma in a path cannot split it.
- Child processes never inherit the server's stdin (the MCP channel): they get the answers you pass in `selections`, or a closed input.
- Resources only serve text files from a job's own folders, by plain file name.
- The server process writes nothing to stdout except the MCP protocol; logs go to stderr and `$GMX_MCP_HOME/logs/gromacs_mcp.log`.

## Known limitations

- GPU offload, multi-simulations (`multidir`, `replex`) and the free energy and umbrella templates are wired up but have not been run on GPU or MPI hardware in testing.
- Parameters are passed to gmx as given after basic validation; the whitelist limits which tool runs, not every option it accepts. gmx refuses unknown options itself, and the error then suggests the valid ones.
- Analysis of a short run is noisy: check the error estimates, the equilibration time and the number of frames before trusting a number.
- The server is not packaged as a wheel; run it from a checkout (or Docker).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Could not find GROMACS` | Set `GMX_MCP_GMX` in the server's `env` block; MCP clients may not see your shell PATH |
| `There is no domain decomposition for N ranks` | The box is small; use `ntmpi=1` (the default) |
| `reading tpx file ... version X with version Y` | The `.tpr` came from a newer GROMACS; rerun grompp with this one |
| grompp `Too many warnings` | Read `grompp_warnings`; fix them, or pass `maxwarn` only if each one is understood |
| `No such moleculetype NA` | The topology lacks `#include "<ff>.ff/ions.itp"` |
| `-dt cannot be larger than -trestart` (msd) | Trajectory frames are further apart than `trestart_ps`; raise `trestart_ps` or save frames more often |
| `step*.pdb` files appear | GROMACS dumped structures after constraint failures; common early in minimization, a problem during dynamics |
