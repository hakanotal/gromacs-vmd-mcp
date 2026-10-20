# Changelog

Since 2.1.0 this server lives in the `gromacs-mcp/` folder of https://github.com/hakanotal/gromacs-vmd-mcp, next to a VMD server. Earlier history: https://github.com/MacromNex/gromacs_mcp.

## 2.2.0 (repository v1.1)

### Security

- The shell-safe file name check (no `$ \` ; & | < >` or newline, no `.gz`/`.Z`) only guarded `run_gromacs_command`. `run_grompp`, `run_minimization`, `validate_mdp`, `extend_simulation`, the workflows and the MD job script pass paths to gmx directly. The check now runs inside `run_command_full`, which every gmx call goes through, and before mdrun starts.
- `run_grompp(extra_parameters)` compared option names as typed, so `--f` or `-F` got past it. Names are now read the way gmx reads them, and every grompp file option (`-pp`, `-imd`, `-qmi`, ...) must come through the named arguments.

### Added

- Started natively on Windows, the server exits with a message pointing to WSL2 or Docker (the job manager needs POSIX). It used to fail with `No module named 'fcntl'`.
- SETUP.md at the repository root: macOS, Linux and Windows (WSL2, Docker, client configuration).

## 2.1.0

Fixes everything found in the end to end water box test of 2.0.0 (TODO.md section 13) and takes on part of section 12.

### Breaking changes

- New job state `stopped`: mdrun ended cleanly before its last step and wrote a checkpoint, because `maxh` or `max_runtime_seconds` was reached or because mdrun itself was stopped from outside (found, for example, after a server restart). A cancel still gives `cancelled`. Such a run used to end as `completed` (maxh) or `failed` (time limit, or any early stop found after a server restart). Clients that wait for `completed` or `failed` must also accept `stopped`; `get_job_status` gives `stop_reason`, and `resume_job` continues the run.
- `output_files` holds absolute paths in every tool. `run_gromacs_command` and MD jobs returned `"name (N bytes)"` strings (with the paths in `output_paths`, now removed) and workflows returned bare names. Sizes are in `file_sizes` (`{path: bytes}`). `get_job_result` still reads records written by 2.0.0.
- `run_gromacs_command` no longer returns `output` (a copy of `stdout`). On success the tool shortens `stderr` (banner, command line header, per frame progress lines, an answered energy term menu, the closing quote) and a `dump` saved to a file returns only its first 40 lines in `stdout`. Failures are returned in full.
- `energy_summary` returns `equilibration_time_ps: null`, no `equilibrated` block and a warning when the series has fewer than 20 frames. It used to report 0 ps and repeat the full statistics, and it skipped detection below 40 frames. `detect_equilibration` returns `None` in that case.
- `submit_batch_analysis` no longer refuses a batch because one file is missing: the file is reported as failed in its entry and the job completes when at least one file succeeds (it used to fail when any file failed). Paths outside the sandbox or not ending in `.tpr` are still refused up front.

### Security

- `run_gromacs_command("trjconv")` accepted `-exec`, which makes trjconv pass its value to the shell for every output frame, so a client could run any command despite the no-shell design (confirmed with GROMACS 2026.4, which also accepts `--exec`). The option is now refused for every command under any spelling, in `parameters`, as a file option name and in `run_grompp(extra_parameters)`. grompp's `-qmi` input file must also come through `input_files` now, like its other file options.
- More places where gmx hands file names to a shell, found by a second review: `-w` opens results in a viewer through the shell (now refused, and gmx runs without `DISPLAY` or `GMX_VIEW_*`), and compressed files are read through `gunzip -c < name` (file names with `$ \` ; & | < >` or a newline, and `.gz`/`.Z` files, are now refused). `wham` was removed from the whitelist (33 tools now): it reads file names from list files the server cannot check and opens `.gz` entries through the shell.
- File names in `parameters` were recognized by their looks (a slash or a known suffix), so a value such as `x$(...)` after `-f2` passed unchecked. Now the tool's own help decides which options take files: in `parameters` such an option takes exactly one plain name such as `spc216.gro`, only a real option name of the tool ends its value (gmx reads `-`, `--` and `-.5` as values), and a tool whose help cannot be read is refused rather than passed unchecked; and paths go through `input_files`/`output_files` (which accept a list for options that take several files, such as `trjcat -f`).
- With `GMX_MCP_WORKDIR` set, an `#include` line the checker cannot read is refused instead of skipped, so a preprocessor that parses includes more loosely cannot be used to read files outside the sandbox.

### Fixed

- `run_gromacs_command("dump")` into a folder that did not exist yet failed with a bare `[Errno 2]`, because gmx was started inside the missing folder. A `working_directory` that does not exist is now a clear error.
- `trajectory_report` left an energy `.xvg` under `results/energy/` on every call.
- `plot_xvg` labelled one plotted column with every unit in the file.
- The deliberate energy term probe was logged as an ERROR on every `energy_summary` and `trajectory_report` call.
- `examples/data/md.mdp` failed the server's own lint (`nstenergy` not a multiple of `nstcalcenergy`).
- `full_md` left `#solv.top.1#` and `#ions.top.1#` backups in its folder.
- The energy term menu parser lost terms when a 14 character name left one space before a three digit index (more than 99 terms, for example with energy groups). Checked against real output with 122 terms.
- After extend plus resume, progress and completion were measured against the first run's `nsteps` (mdrun `-append` does not print the parameters again), so a run stopped past the old step count looked complete. The step count now comes from the `.tpr`, read only as far as needed.
- A minimization submitted as a job, and a run with `nsteps=0`, now count as complete (a converged minimizer stops before `nsteps` by design).
- `analyze_tpr` did not list the files it wrote in `output_files`.

### Added

- `energy_summary` statistics include `sem` corrected for correlation between frames, `statistical_inefficiency` and `n_independent`, and a warning when a term has fewer than 10 independent samples.
- When gmx rejects an option, the error names the closest valid options and `valid_options` lists them all (read from `gmx help <tool>` once per tool).
- Workflow resume reuses a file's recorded hash when its size and modification time are unchanged, so large inputs are not read again.

### Removed

- `docs/archive/` (reports generated while building the 1.x server) and `tests/test_prompts.md` (manual prompts for the 12 tool 1.x server). Git history keeps them.
- The unused helpers `run_command` and `format_file_info`.

## 2.0.0

A rework driven by running a real water box simulation through the server and fixing everything that got in the way. TODO.md lists each item.

### Breaking changes

- The FastMCP server name is `gromacs` (was `gromacs-2025.4`). Clients address servers by the key in their own configuration, so existing `.mcp.json` entries keep working.
- `submit_md_simulation` without `nsteps` now runs the number of steps in the `.tpr`. It used to silently run 100 steps.
- MD job outputs are named `md.*` (`md.tpr`, `md.log`, `md.edr`, `md.gro`, `md.cpt`, `md.xtc`) instead of `topol.tpr`, `traj.trr`, `confout.gro`, `ener.edr`.
- Every tool returns the same flat envelope: `status`, `error` (on failure), `warnings`, `output_files`, `command`. Script results no longer carry a `result` key at the top level.
- `run_gromacs_command` only runs whitelisted gmx subcommands and no longer pipes `echo 0` into every interactive tool: pass `selections`.
- `examples/data/sample.tpr` is built locally (`scripts/make_example_data.py --tpr-only`) instead of being committed, because `.tpr` files are tied to the GROMACS version.
- The per-script config files were replaced by one `configs/default_config.json`; settings are merged recursively.
- Removed the stale `test_mcp_client.py`, `tests/run_integration_tests.py` and `tests/validate_mcp_tools.py` (they needed mamba and old internals). Generated pipeline reports moved to `docs/archive/` (removed in 2.1.0).

### Fixed

- Jobs failed instantly when mamba was not installed; they now run with the server's own Python.
- `mdrun` picked as many thread-MPI ranks as cores and crashed on small boxes; the default is one rank, with a retry on domain decomposition errors.
- Async jobs were killed after 300 s.
- Errors came back as `Unknown error`; the GROMACS error block (and stderr) is now returned, with hints for common problems.
- `gmx check` output was dropped because it is on stderr.
- `analyze_tpr` reported the pressure as 3 (from `ref-p (3x3)`), counted 6 atoms in a 1530 atom system, never read the time step, and returned a 190 KB response. It also printed to stdout, which is the MCP channel.
- `submit_batch_analysis` passed arguments the script did not accept; `get_job_result` could never find MD results.
- Cancelling a job could end as `failed`; jobs were stuck as `running` after a server restart; metadata writes were not atomic.
- Workflows always reported failure (no `result` key); the analysis workflow used a shell pipe and assumed protein groups; `preprocessing` only echoed text; `simple_md` truncated runs to 1000 steps.
- `mdrun -nsteps` (deprecated) replaced by `convert-tpr -nsteps`.

### Fixed after an independent review of this release

- `delete_job` and `clean_jobs` deleted simulation outputs, because the default output folder sat inside the job record. Outputs now go to `runs/<id>/` and deletion only removes record files.
- Workflow resume compared only command lines: changed options or upstream results were skipped and reported as success, and repeated fresh runs appended solvent to the same topology twice. Steps now compare the content of every file they read, inputs are re-copied when a run starts over, and solvate and genion edit copies (`solv.top`, `ions.top`).
- Two servers starting together both re-queued the same orphaned job; jobs are now claimed under a file lock, and two unfinished jobs cannot share an output folder.
- `full_md` failed at NPT for every protein: position restraints with pressure coupling need `refcoord-scaling = com`, now in the templates and checked by the linter.
- A run stopped by a cancel or `maxh` showed 100% and could be recorded as completed, because mdrun prints "Finished mdrun" in both cases. Progress now reports `complete` separately.
- A relative `output_dir` was checked against the server's folder but used from the repository root.
- Cancel freed the job slot before mdrun stopped, and the SIGKILL fallback targeted the wrong process. The MD wrapper now survives SIGTERM until mdrun has checkpointed, the manager signals the process group by its id and waits for it, and pid reuse is detected through process start times.
- With `GMX_MCP_WORKDIR` set, default output folders were outside the sandbox, so tools rejected their own outputs. Defaults now move inside it. Paths in `run_grompp(extra_parameters)` and `multidir` are checked, and commands without file arguments run inside it.
- `nsteps=0` was ignored on the command line.
- The free energy and NVE templates (with a temperature) and multi-group temperature coupling produced files grompp rejects. Every template is now run through grompp in the tests.
- `gmx energy` tables with `--` (short windows) parsed as empty.
- Fixed default output names hit GROMACS's 99-backup limit and collided between concurrent calls; default names are now unique.
- Child processes inherited the MCP stdin pipe, so interactive tools could read protocol messages; stdin is now closed unless answers are given.
- `get_job_status` dropped the reason a job failed (the envelope reserves `error` for the call itself); it is now `job_error`.
- A default job folder outside `GMX_MCP_WORKDIR` (for example `GMX_MCP_HOME=/data` in Docker with another sandbox) failed only once the job ran; it is refused at submission and reported by `check_environment`.

### Security

- No command runs through a shell; file names and parameters are passed literally.
- grompp can only read included files (topology `#include`, mdp `include = -I`) from the sandbox or the GROMACS force field directory. Includes are resolved the way grompp resolves them (relative `-I` against its working folder, every candidate location checked), unlocatable includes are refused, and for `run_gromacs_command("grompp")` every file must be given as `input_files` or `output_files` (grompp's default file names are checked too). Workflows re-check each topology right before grompp uses it, and `force_field` and `water_model` must be plain names.
- Jobs only run scripts from `scripts/` (plus `GMX_MCP_SCRIPT_DIRS`), job ids must be 8 hex characters, and list arguments reach job scripts as separate arguments (a comma inside a path used to split it); the scripts re-check the sandbox themselves.
- `GMX_MCP_WORKDIR` confines tool paths to one directory (symlinks and `..` are resolved first).
- Resources only serve text files from a job's own folders.

### Added

- Tools: `create_mdp`, `validate_mdp`, `run_grompp`, `run_minimization`, `extend_simulation`, `list_force_fields`, `run_full_md_pipeline`, `list_workflows`, `energy_summary`, `analyze_rdf`, `analyze_msd`, `trajectory_report`, `read_xvg`, `plot_xvg`, `trjconv_preset`, `check_environment`, `get_command_help`, `resume_job`, `delete_job`, `clean_jobs`.
- Job queue with a concurrency limit, progress and ETA from `md.log`, process group cancel with a checkpoint, recovery from on-disk evidence after restarts, resume from checkpoints.
- Real workflows: `full_md` (pdb2gmx to production `.tpr`) and `preprocessing`, both resumable.
- MDP templates (em, ions, nvt, npt, nve, free_energy, umbrella) with explanations and lint rules.
- 26 more whitelisted gmx tools (preparation, trajectory and analysis).
- MCP resources for job files and examples; three prompt templates.
- mdrun options: threads, GPU offload, pinning, `maxh`, checkpoint interval, resume, multi-simulation and replica exchange.
- About 300 tests (parsers on real GROMACS output, a fake gmx for the wrapper, job manager races, every tool's envelope, a stdio subprocess check, real simulations end to end), a CI workflow for Linux, macOS and Docker, `pyproject.toml` and requirements files, and a setup script that does not need conda when GROMACS is installed.
