# CLAUDE.md

Guidance for working on this repository.

## What this is

An MCP server (FastMCP 4) that exposes GROMACS: system preparation, MD runs as background jobs, and analysis that returns numbers. 32 tools, 3 resource templates, 3 prompts. It runs from a checkout or Docker; it is not packaged as a wheel.

## Run and test

```bash
bash quick_setup.sh --dev                         # ./env with deps; finds or installs gmx; builds examples/data/sample.tpr
./env/bin/python src/server.py                    # stdio server
./env/bin/python -m pytest                        # full suite, about 55 s with GROMACS
./env/bin/python -m pytest -m "not slow"          # skip the end to end simulation
./env/bin/python -m pytest tests/test_jobs.py -k cancel
```

`GMX_MCP_HOME` decides where jobs/, results/ and logs/ go; the tests point it at a temporary folder (tests/conftest.py) so nothing is written into the repo.

## Layout

- `src/server.py`: core tools (jobs, analyze_tpr, run_gromacs_command, workflow, submit, info) and registration of the modules in `src/tools/`.
- `src/tools/`: `prep.py` (mdp, grompp, minimization, extend, force fields), `analysis.py` (energy, rdf, msd, report, xvg, plot, trjconv), `pipeline.py` (full_md), `env_tools.py` (check_environment, gmx help), `resources.py` (resources and prompts), `registry.py` (tool categories).
- `src/jobs/manager.py`: background jobs. Scripts run as `sys.executable scripts/<x>.py --flags --result-json jobs/<id>/output.json` in their own session; outputs default to `runs/<id>/`; metadata is written atomically under a lock; final state is decided only in `_finalize` (own jobs) and `_reconcile` (everything else), and a run mdrun ended cleanly before its last step is `stopped`, not completed or failed; processes are identified by pid plus start time; orphaned queued jobs are claimed under `fcntl.flock`.
- `src/utils.py`: result envelope (`format_tool_result`), output folders, logging (loguru, stderr and file, never stdout).
- `scripts/lib/gromacs_utils.py`: the only place that finds and runs gmx (`find_gromacs_command`, `run_command_full`), the path sandbox (`resolve_in_workdir`), and all parsers.
- `scripts/lib/mdp_templates.py`: mdp templates and lint rules.
- `scripts/gromacs_command.py`: the command whitelist (`COMMANDS`) and stdin selections.
- `scripts/md_simulation.py`, `tpr_analysis.py`, `gromacs_workflow.py`: what jobs and workflows execute; each also works as a CLI.
- `tests/fixtures/`: real GROMACS outputs used by the parser tests.

## Conventions

- Never run gmx through a shell. Build an argv list and pass interactive answers as `stdin_text` (or `selections` in `run_gromacs_command`).
- New gmx subcommands go into `COMMANDS` in `scripts/gromacs_command.py` with their default selections.
- Every tool returns `format_tool_result(...)`: flat dict with `status`, `warnings`, `output_files` (absolute paths, always; sizes go in `file_sizes`), and `error` on failure. Pass the script result as `data` so stderr and the command survive failures.
- Shorten output for clients only in the MCP tool layer (`compact_for_client` in `server.run_gromacs_command`). Library callers such as `analysis._gmx` parse the full stdout and stderr (the energy menu and `gmx check` report are on stderr).
- Register tools through `registry.tool(category)`. `tests/test_server.py::BAD_CALLS` must get an entry for every new tool (a test fails otherwise) and the envelope test then covers it.
- Resolve user paths with `resolve_in_workdir` so `GMX_MCP_WORKDIR` holds.
- Write parsers against real GROMACS output: save a sample in `tests/fixtures/` first. GROMACS formats change between versions (`dt` not `delta-t`, `ref-p` is a matrix) and with the data (past 99 energy terms an index can follow a name after one space).
- Library functions must not print; only CLI `main()` functions do. stdout is the MCP channel.
- `mdrun -nsteps` is deprecated: change step counts with `convert-tpr -nsteps`.
- Workflow steps must not edit their inputs in place; use `pre_copy` and give mdrun steps `reads=[...]` so resume can compare input content.
- Default output names must be unique (`unique_name`): fixed names collide between calls and GROMACS stops after 99 `#backup#` files.
- Never let a child process inherit stdin; `run_command_full` and the Popen calls pass `stdin=DEVNULL` when there is nothing to send.
- When a test guards a fix, check it fails with the fix removed (the job and workflow tests were checked this way).
