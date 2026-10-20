# gromacs_mcp TODO

Improvement list for the GROMACS MCP server. Sections 1 to 9 were compiled after running a water NPT demo through the original server; version 2.0.0 (see CHANGELOG.md) works through them. Section 10 lists problems found while implementing, section 11 the findings of an independent review of 2.0.0 (all fixed), section 12 further improvements, and section 13 the findings of an end to end water box test of the running 2.0.0 server. Version 2.1.0 fixes all of section 13 and takes on part of section 12; section 14 lists what was found along the way.

Status marks:

* `[x]` done and covered by tests that run here (macOS, GROMACS 2026.4 Homebrew; Docker image with GROMACS 2025.4)
* `[~]` partly done, or done but not verifiable on this machine; the note says what is missing
* `[ ]` open

---

## 1. Blocking bugs

- [x] **1.1 `submit_md_simulation` fails when mamba is not installed.** Jobs run with `sys.executable`. Test: `test_jobs.py::TestLifecycle::test_completes_without_mamba_on_path`.
- [x] **1.2 `find_gromacs_command` mamba fallback.** Order is `$GMX_MCP_GMX`, `gmx`, `gmx_mpi`, then mamba only if it exists; a clear error lists what was tried.
- [x] **1.3 `energy` can never select terms.** `selections` parameter fed on stdin; `energy` without selections returns the term menu and says so.
- [x] **1.4 Real error messages are swallowed.** The envelope keeps the script's error, stderr and command; GROMACS errors are reduced to their error block (grompp `ERROR n` blocks, `Inconsistency in user input`, and so on) plus hints for common problems.
- [x] **1.5 `check` returns almost no output.** stdout and stderr are returned separately; `trajectory_report` parses the check report.
- [x] **1.6 `analyze_tpr` returns wrong values.** Structural parsing of `gmx dump`: pressure from the matrix, atoms from the header, `dt` (the old pattern looked for `delta-t`, which does not exist). Tests run on a real dump.
- [x] **1.7 `analyze_tpr` JSON output is huge.** Summary only; `include_raw=True` writes the dump to a file and returns its path.
- [x] **1.8 `submit_batch_analysis` passes unsupported arguments.** `tpr_analysis.py --inputs` batch mode; per file results; one failure does not stop the rest.
- [x] **1.9 Job output path collides with the output directory.** Separate `--output-dir` and `--result-json`; MD jobs now write a result file (they never did, so `get_job_result` could not work).
- [x] **1.10 mdrun crashes with the default rank count.** One thread-MPI rank by default, retry with one rank on a domain decomposition error.
- [x] **1.11 Hard 300 s timeout.** No limit by default; `max_runtime_seconds` and `maxh` stop cleanly with a checkpoint.
- [x] **1.12 Version and naming.** Server name `gromacs`, real version from `gmx --version`, Docker version is a build argument.
- [x] **1.13 Example data missing.** `examples/data` (water box text files) is committed; `sample.tpr` is built locally because `.tpr` files are version specific (found when the 2026.4 file failed in the 2025.4 Docker image).

## 2. Security

- [x] **2.1 Shell injection through `bash -c`.** No shell anywhere (also removed from the analysis workflow). Tests feed `;`, `$(...)`, backticks and quotes and check nothing runs.
- [x] **2.2 Command whitelist and path sandbox.** 33 whitelisted subcommands (`wham` removed in 2.1.0, see 14.5); `GMX_MCP_WORKDIR` checks inputs, outputs, working directories, workflow inputs, paths inside `parameters`, `extra_parameters` and `multidir`, and grompp includes (symlinks and `..` resolved); default folders move inside the sandbox.
- [~] **2.3 Parameter validation.** Types, flag names and overwrites (warning) are checked. Options are not validated before the run, but when gmx rejects one the error names the closest valid options (12.3).
- [~] **2.4 Resource limits.** Concurrent job cap, queue and free disk check are done. No cap on `nsteps`, run time per user, or output size.

## 3. Job system reliability

- [x] **3.1 Cancel overwritten by the finishing thread.** Cancel always wins; covered by a test that fails if the fix is removed.
- [x] **3.2 Jobs lost on restart.** Recovery decides from evidence (result file, or md.log showing the last step reached), never touches a live process (pid plus start time), claims orphaned queued jobs under a file lock so exactly one server runs them; `resume_job` continues from `md.cpt`.
- [x] **3.3 Metadata writes.** Lock plus atomic replace; tested with eight threads.
- [x] **3.4 Cancel stops the process tree.** Own session, SIGTERM to the group by its id; the MD wrapper survives until mdrun has checkpointed; the slot stays busy until the group is gone; SIGKILL after 30 s. Tested against a real mdrun.
- [x] **3.5 Progress reporting.** Step, time, percent, ETA, ns/day and log warnings; `finished` and `complete` reported separately (mdrun prints "Finished mdrun" after a cancel too); honors `-nsteps` overrides in old logs.
- [x] **3.6 `get_job_log`.** Seek based tail of the job log plus the tail of `md.log`.
- [x] **3.7 Housekeeping.** `delete_job`, `clean_jobs(older_than_days, keep_last)`, newest first listing with duration and output folder.
- [x] **3.8 Logging.** loguru to stderr and `$GMX_MCP_HOME/logs` (rotating); stdlib logging from the scripts is routed into it; fastmcp banner off.

## 4. Preparation tools

- [x] **4.1 Preparation commands.** pdb2gmx, editconf, solvate, genion, grompp, make_ndx, select, convert-tpr, trjcat, eneconv whitelisted; `run_grompp` returns all NOTE, WARNING and ERROR blocks and makes `maxwarn` explicit.
- [x] **4.2 Synchronous minimization.** `run_minimization` reports convergence, steps, energy and maximum force.
- [x] **4.3 MDP generation and validation.** `create_mdp` (7 stages, explanations), `validate_mdp` (lint rules plus a grompp check).
- [x] **4.4 Force field discovery.** `list_force_fields` with water models.
- [x] **4.5 End to end workflow.** `full_md` and `run_full_md_pipeline`, resumable, optional production submission. Adds the ions include to user topologies when needed (found while testing).

## 5. Analysis tools

- [x] **5.1 More analyses.** rdf, msd, gyrate, density, distance, angle, rmsdist, cluster, covar, anaeig, dssp, pairdist, potential, dielectric, traj whitelisted (`wham` was, until 14.5).
- [x] **5.2 Parse `.xvg`.** `read_xvg` with downsampling and statistics.
- [x] **5.3 Energy analysis with numbers.** `energy_summary`: GROMACS table, full and equilibrated statistics (MSER-5), missing terms skipped with a warning.
- [x] **5.4 RDF and MSD results.** First peak, first minimum, coordination number; D with error and optional finite size correction.
- [x] **5.5 Trajectory helpers.** `trjconv_preset` (whole, nojump, mol, center, fit, window, pdb).
- [x] **5.6 Plots.** `plot_xvg` (matplotlib optional, clear message when missing).
- [~] **5.7 Trajectory sanity report.** Frames, timestep, box change, non-finite energies, isolated energy spikes. Not yet: NaN coordinates and atoms leaving the box (12.19).

## 6. MCP interface

- [x] **6.1 Small results.** Per field limit with a `truncated` marker.
- [x] **6.2 Files as resources.** Job files and example files; text only, size limited.
- [x] **6.3 Prompts.** Three templates; a test checks that every tool they name exists.
- [x] **6.4 Tool descriptions.** Arguments, selection meaning and an example in every docstring; a test checks each tool has a description.
- [x] **6.5 One result schema.** Flat envelope; a test calls every registered tool with bad input and checks it.
- [x] **6.6 Generated command list.** From the tool registry and the whitelist.
- [x] **6.7 `check_environment`.**
- [x] **6.8 `get_command_help`.**

## 7. Performance and hardware

- [~] **7.1 GPU support.** `gpu=True` passes `-nb gpu -pme gpu -bonded gpu -update gpu`; `check_environment` reports the build's GPU support. Not run on a GPU.
- [x] **7.2 Threading.** `ntmpi`, `ntomp`, `pin`; the default `ntomp` splits the cores between job slots.
- [x] **7.3 Checkpoint and extend.** `cpt`, `resume`, `extend_simulation`; the extend then resume path is tested.
- [x] **7.4 Queue.** FIFO with `GMX_MCP_MAX_JOBS`.
- [~] **7.5 Replica exchange and multi-simulation.** `multidir` and `replex` are passed to mdrun. Not run (needs an MPI or multi-directory setup).
- [~] **7.6 Free energy and enhanced sampling.** free_energy and umbrella templates with lint rules, PLUMED detection. Not built: an umbrella window pipeline, `gmx bar` summary, PLUMED `-plumed` option.

## 8. Testing and CI

- [x] **8.1 Runnable tests.** 382 tests, about 55 s, also on Python 3.10; without GROMACS 298 pass and the 84 that need it skip.
- [x] **8.2 Regression tests for section 1.**
- [x] **8.3 Job manager tests,** including mutation checks: the cancel, recovery, run-once claim and workflow resume tests fail when their fixes are removed.
- [x] **8.4 Security tests** (injection, traversal, symlink escape, whitelist, paths in parameters).
- [x] **8.5 End to end test** through the MCP client: minimize, NPT, job, energy, RDF, MSD, report.
- [x] **8.6 Real client test.** `tests/test_stdio.py` starts the server as a subprocess over stdio; the stale mamba based scripts were removed.
- [~] **8.7 CI.** `.github/workflows/tests.yml`: unit (Python 3.10 and 3.12), Linux conda-forge GROMACS, macOS Homebrew GROMACS, Docker smoke test. Written and the YAML parses, but it has not run on GitHub yet.
- [x] **8.8 Generated artifacts.** Moved to `docs/archive/` in 2.0.0, then removed in 2.1.0 together with the stale `tests/test_prompts.md` (both described the 12 tool 1.x server; git history keeps them).

## 9. Setup, packaging and docs

- [x] **9.1 Setup script.** Uses an installed gmx; conda only when GROMACS must be installed; prints a valid `.mcp.json` block.
- [~] **9.2 Packaging.** `pyproject.toml` (uv non-package mode, extras, pytest config) and requirements files. No wheel or `gromacs-mcp` entry point: see 12.1.
- [x] **9.3 Dockerfile.** Version build argument, non-root user, `/data` home for `--user` runs, healthcheck, `sample.tpr` built in the image; built and a job run inside it here.
- [x] **9.4 Documentation.** README, detail.md (full reference), CLAUDE.md, scripts/README.md, CHANGELOG.md.
- [x] **9.5 Configuration.** One `configs/default_config.json`, recursive merge (a partial config used to raise KeyError), environment variables documented.
- [x] **9.6 Duplicated fallbacks.** Removed; scripts import `lib` only.

---

## 10. Found and fixed during implementation

Recorded so they are not reintroduced.

- [x] `run_tpr_analysis` printed the analysis to stdout inside the server process (stdout carries the MCP protocol).
- [x] The old workflow tool always reported failure: the script never returned a `result` key.
- [x] `simple_md` ran 1000 steps and `submit_md_simulation` 100 steps unless told otherwise.
- [x] `mdrun -nsteps` is deprecated in GROMACS 2026; step counts are set with `convert-tpr -nsteps`.
- [x] md.log keeps the original `nsteps` when `-nsteps` overrides it, which made progress wrong.
- [x] A relative input path broke `md_simulation.py --nsteps` (convert-tpr runs inside the output folder).
- [x] Workflow tool without `output_file` created folders in the server's working directory, outside `GMX_MCP_HOME` and the sandbox.
- [x] Workflows looked for gmx before checking inputs, so a missing input was reported as "GROMACS not found".
- [x] RDF first minimum picked noise on short runs; now the first smoothed local minimum below 1.
- [x] The start-up relaxation of the potential energy was reported as a spike; spikes must now be isolated points.
- [x] Committed `.tpr` example could not be read by older GROMACS.
- [x] `get_job_status` hid why a job failed (found running a sandboxed job in Docker); now `job_error`.
- [x] Default job folders outside the sandbox failed late; now refused at submission and reported by `check_environment`.

## 11. Found in the independent review of 2.0.0

All fixed, each with a regression test (two of them checked by removing the fix).

- [x] **11.1** `delete_job` and `clean_jobs` deleted simulation outputs (default output folder was inside the record). Outputs now live in `runs/<id>/`; deletion removes record files only.
- [x] **11.2** Workflow resume returned stale results after option or upstream changes, and repeated fresh runs corrupted `topol.top`. Content hashed step inputs, fresh input copies on restart, `solv.top` and `ions.top` instead of in place edits.
- [x] **11.3** An orphaned queued job could run in two servers at once. File lock claim; one unfinished job per output folder.
- [x] **11.4** `full_md` failed at NPT for proteins (position restraints without `refcoord-scaling`). Fixed in the templates, lint rule added, tested with a generated trialanine through pdb2gmx.
- [x] **11.5** Cancelled or `maxh` stopped runs showed 100% or were recorded as completed. `complete` versus `finished`.
- [x] **11.6** A relative `output_dir` escaped the sandbox check and broke tracking. Resolved to an absolute path before submission.
- [x] **11.7** Cancel freed the slot early, SIGKILL never reached mdrun, pids could be reused. Wrapper traps SIGTERM, group signalled by id, slot waits, start times recorded.
- [x] **11.8** Default outputs fell outside `GMX_MCP_WORKDIR`. Defaults follow the sandbox.
- [x] **11.9** `nsteps=0` was dropped on the command line.
- [x] **11.10** Free energy, NVE with temperature, and multi-group coupling templates were rejected by grompp. Every template now runs through grompp in the tests.
- [x] **11.11** Energy tables with `--` parsed as empty.
- [x] **11.12** Fixed default file names collided and hit the 99-backup limit. Unique names.
- [x] **11.13** Child processes inherited the MCP stdin pipe. stdin closed unless answers are given.
- [x] **11.14** Commands without file arguments ran in the server's folder under a sandbox. They run inside the sandbox.

Raised by automated security reviews of the changes (also fixed, with tests):

- [x] **11.15** `run_grompp(extra_parameters)` and `submit_md_simulation(multidir)` skipped the sandbox check.
- [x] **11.16** Path lists were comma joined on the way to job scripts, so a path containing a comma could split into one outside the sandbox. Lists are now repeated arguments, and scripts re-check the sandbox.
- [x] **11.17** Job ids were not validated inside the job manager (`../..` could reach a foreign `metadata.json` whose script would be run). Ids must be 8 hex characters; only scripts from allowed folders run.
- [x] **11.18** grompp could read files outside the sandbox through `#include` and `include = -I`. Includes must resolve into the sandbox or the force field directory.
- [x] **11.19** `run_gromacs_command("grompp")` checked only `input_files`, not `-f`/`-p` in `parameters` or grompp's default `topol.top` and `grompp.mdp`. Those flags must come through `input_files`, and the defaults are checked.
- [x] **11.20** Relative `-I` folders were resolved against the .mdp folder instead of grompp's working folder, and includes that could not be found were skipped. Now resolved like grompp does; every location an include could come from is checked; unlocatable includes are refused.
- [x] **11.21** Workflows checked includes before copying the topology's sibling `.itp` files (which also broke multi-file topologies once unlocatable includes were refused). The check now runs on the staged files.
- [x] **11.22** Relative paths in `parameters` were checked against a different folder than gmx ran in when no files were given. One run folder is computed and used for both.
- [x] **11.23** The grompp parameter check compared exact strings (`--p`, `-P` slipped through) and only covered `-f`/`-p`. All grompp file options are refused in `parameters` after normalizing dashes and case.
- [x] **11.24** The include scan stopped silently past its depth limit, matched mdp keys more strictly than GROMACS does, and followed only the first of several candidate files. It now fails closed, normalizes keys, and follows every candidate.
- [x] **11.25** Workflows checked includes only for the user topology; topologies written by pdb2gmx, solvate and genion were not checked, and `force_field`/`water_model` could be paths. Each grompp step now checks its topology first, and those options must be plain names (installed force fields under a sandbox).

## 12. Further improvements

Open unless marked. Roughly ordered by value for effort.

### Robustness

- [ ] **12.1 Package properly.** Move `src/` and `scripts/` into one `gromacs_mcp` package (scripts become modules run with `python -m`), publish a wheel with a `gromacs-mcp` entry point so `uvx gromacs-mcp` works. Today top level module names (`utils`, `tools`, `jobs`, `lib`) would collide in site-packages.
- [x] **12.2 PID reuse protection.** Done in the review fixes: pid plus start time for jobs and server owners.
- [~] **12.3 Validate options per tool.** When gmx rejects an option, `run_gromacs_command` reads `gmx help <tool>` (once per tool and process), names the closest valid options and returns them all as `valid_options`. Tests: `test_gmx_integration.py::TestRunCommand::test_unknown_option_gets_suggestions`, `test_parsing.py::TestHelpOptions`. Not done: rejecting unknown flags before the run (gmx already refuses them, so the gain is small).
- [ ] **12.4 Windows support.** Process groups, `killpg` and `fcntl` locks are POSIX only; use job objects and `msvcrt` locks, or document WSL.
- [ ] **12.5 MPI launcher configuration.** Support `gmx_mpi` with `mpirun -np N` or `srun` for multi-node runs.
- [ ] **12.6 Version matrix in CI.** Run the integration tests against several GROMACS releases (2023, 2024, 2025, 2026); parsers depend on output formats.
- [ ] **12.7 Limits per request.** Maximum `nsteps`, wall time and output size per job, configurable.

### MCP experience

- [ ] **12.8 Push progress instead of polling.** Use MCP progress notifications (or resource update notifications on `gromacs://jobs/{id}`) while a job runs.
- [ ] **12.9 Typed outputs.** Declare output schemas (pydantic models) so clients get structured, validated results.
- [ ] **12.10 Remote use.** Streamable HTTP transport with authentication, and resource based upload and download (binary blobs for `.xtc`, `.tpr`) so the client does not need the server's file system.
- [ ] **12.11 Per session workspaces.** A fresh folder per conversation under `GMX_MCP_HOME`, with quotas and cleanup.

### Science features

- [ ] **12.12 Structure intake.** Fetch a PDB entry by ID, repair missing atoms and residues (PDBFixer), choose protonation states (pdb2pqr or propka).
- [ ] **12.13 Ligands.** Parameterize small molecules (GAFF through acpype, or CGenFF) and merge them into the topology.
- [ ] **12.14 Free energy pipelines.** Lambda window generation, submission as a job array, `gmx bar` summary; umbrella sampling windows and `wham` PMF with error bars (wham needs its own tool that writes the list files itself and checks every listed path: see 14.5).
- [ ] **12.15 PLUMED.** Pass a PLUMED input to mdrun when the build supports it.
- [ ] **12.16 More summaries.** Hydrogen bond counts over time, DSSP secondary structure per residue, PCA with projection plots, clustering with representative structures, contact maps, per residue SASA.
- [~] **12.17 Convergence tools.** `energy_summary` statistics now carry `statistical_inefficiency`, `n_independent` and an autocorrelation corrected `sem` next to the block averages, and warn when a term has fewer than 10 independent samples. Pure Python, under a second for long series (exact sum over the first 50 lags, growing steps after that, thinning only for slowly decorrelating series over 20000 points). Tests: `test_parsing.py::TestStatistics::test_statistical_inefficiency_of_ar1_series` (AR(1) series with known g), `test_gmx_integration.py::TestAnalysisTools::test_energy_summary`. Not done: a converged or not verdict with reasons.
- [ ] **12.18 Sanity rules against references.** Flag results far from known values (water density, temperature drift, energy conservation in NVE).
- [ ] **12.19 Coordinate checks in `trajectory_report`.** NaN coordinates, atoms leaving the box, molecules split across boundaries. There is no cheap way with gmx tools alone: every frame has to be dumped as text. Worth doing together with an optional MDAnalysis dependency.

### Operations

- [ ] **12.20 Cluster backends.** Submit jobs to Slurm or PBS (sbatch scripts, status from squeue) behind the same job tools.
- [ ] **12.21 Performance memory.** Keep ns/day per system size and hardware, and suggest thread counts or GPU use for new runs.
- [ ] **12.22 Visual output.** Render snapshots or short movies of a trajectory (PyMOL or MDAnalysis plus a renderer) for quick inspection.
- [ ] **12.23 Tutorial.** A worked lysozyme in water example (pdb2gmx to analysis) in the docs and as a prompt.

### Raised by the review, not yet done

- [x] **12.24 Energy menu with more than 99 terms.** Confirmed on real output (the water box with 9 energy groups, 122 terms): `99  Coul-SR:EE-IJK 100  LJ-SR:EE-IJK` has one space before 100, and the old parser lost three terms. The parser now looks the indices up in order and each name runs to the next one. Tests: `test_parsing.py::TestEnergyParsing::test_menu_with_more_than_99_terms` (fixture `gmx_energy_menu_122_terms.txt`), `test_menu_index_glued_to_a_name`.
- [x] **12.25 Server internal temp files under the sandbox.** `validate_mdp` and `trajectory_report` use `results/tmp`, including the energy series of `trajectory_report` since 13.3. (`make_example_data.py` is a setup script, not run by the server, and keeps the system temp folder.)
- [ ] **12.26 Pick up orphaned queued jobs without waiting for a query.** Today another server instance claims them on start or on the next status or list call; a periodic sweep would start them sooner.
- [x] **12.27 A distinct state for runs stopped by `maxh`.** New terminal state `stopped`: mdrun ended cleanly before its last step (maxh, `max_runtime_seconds`, or mdrun stopped from outside, for example found after a server restart). A SIGTERM to a watched job's whole process group without a cancel still ends as `failed` ("Stopped by a signal"), because the wrapper sees it. The same run used to be `completed` (maxh, watched), `failed` (time limit) or `failed` (any early stop recovered after a restart). Cancel still wins; `get_job_status` gives `stop_reason`; `get_job_result` and `resume_job` accept it. Automatic resume was left out on purpose: the client decides whether to continue. Tests: `test_jobs.py::TestLifecycle::test_run_that_stopped_early_is_stopped`, `test_cancel_wins_over_a_stopped_result`, `TestReviewRegressions::test_stopped_early_is_not_completed`, `test_gmx_integration.py::TestMdRun::test_run_stopped_by_a_limit_is_stopped_not_failed` (real mdrun, maxh and time limit).
- [x] **12.28 Cheaper resume checks for large inputs.** Step markers also record size and `mtime_ns`; a recorded hash is reused only on an exact match, anything else is hashed. Markers from 2.0.0 still resume. Tests: `test_workflow_steps.py` (no GROMACS needed); the content based resume tests of 11.2 pass unchanged.

---

## 13. Found in the water box demo test (2026-10-18)

The `examples/data` water box (348 SPC/E waters) was run end to end through the live 2.0.0 server: minimization, 20 ps NVT, 200 ps NPT cancelled at 13% and resumed, `full_md` with ions and a second call to check resume, batch analysis, and every analysis tool. Physics matched the earlier run in `../demo/` (density 997 kg/m³, potential -46.7 kJ/mol per molecule, O-O RDF peak 0.276 nm with g = 3.07, D = 2.48e-5 cm²/s). Cancel, resume, job states, default output folders and `full_md` resume all behaved as documented. Test outputs are outside the repo in `../results/mcp_demo_test/`. Nothing below blocks use; items are ordered by what a user hits first.

### Bugs

- [x] **13.1 `energy_summary` reports an equilibration result it never computed.** Detection only runs with 40 or more frames (`src/tools/analysis.py:204`). Below that the tool still returns `equilibration_time_ps: 0` and an `equilibrated` block identical to `full`, with no warning. Repro: `energy_summary` on the `md.edr` of a 20 ps run from the `nvt` template (21 frames, because the template writes energies every 500 steps). The potential starts at -13,648 kJ/mol against a plateau near -16,180 (std about 120). `detect_equilibration` called directly on the same series returns index 10 (10 ps), and it already refuses series shorter than `4 * batch` (20 frames), so the 40 frame gate only hides results. Fix: drop the gate or lower it to 20; when detection is skipped or impossible, return `equilibration_time_ps: null`, leave out `equilibrated`, and add a warning. Test: a short series with a high first block must be truncated or carry the warning. (The 2001 frame NPT run correctly gave 0.) Done: the gate is gone, `detect_equilibration` returns None below 20 frames, and the tool then reports `null`, no `equilibrated` block and a warning. Tests: `test_gmx_integration.py::TestAnalysisTools::test_energy_summary_short_series` (fails with the old gate), `test_parsing.py::TestStatistics::test_short_run_with_a_relaxing_start_is_truncated`, `test_too_short_for_detection_says_so`.
- [x] **13.2 `run_gromacs_command("dump")` fails when the output folder does not exist.** For `stdout_to_file` commands the folder is created only after the run (`scripts/gromacs_command.py:313-315`), but gmx is started inside it (`cwd = anchor.parent`, line 261), so the launch fails first and the tool returns a bare `[Errno 2] No such file or directory` with no hint. Other commands are fine because line 280 creates the folder before the run. Repro: `dump` with `output_files={"o": "<new folder>/x.txt"}`. Fix: create `stdout_target.parent` before running. Related: an explicit `working_directory` that does not exist also returns the raw OSError; give a clear error or create it. Done: output folders are created before gmx starts, and a missing `working_directory` is a clear error (not created: a typo should not make folders). Tests: `test_command_wrapper.py::TestFilesAndOutput::test_dump_into_a_folder_that_does_not_exist_yet` (fails with the old code), `test_missing_working_directory_is_a_clear_error`.
- [x] **13.3 `trajectory_report` leaves an energy file behind on every call.** The box analysis uses a temporary folder, but the energy part calls `energy_summary` without `output_xvg` (`src/tools/analysis.py:460`), so `results/energy/md_<timestamp>.xvg` is written, never removed, and not listed in `output_files`. Fix: pass an `output_xvg` inside the temporary folder and read it before the folder goes away. Test: `results/energy/` is unchanged after a `trajectory_report` call. Done: `test_gmx_integration.py::TestAnalysisTools::test_trajectory_report` checks that nothing under `results/` changes (fails with the old code).

### MCP interface

- [x] **13.4 Oversized responses from `run_gromacs_command`.** `output` is a copy of `stdout` (`scripts/gromacs_command.py:334-336`), so every response carries the text twice. For `dump` that is about 40 KB after shortening, although the full text was saved to the requested file. `energy` returns the whole term menu and every "Reading energy frame" line in `stderr`. Fix: keep one of `output` and `stdout`; for `stdout_to_file` commands return the first lines plus the saved path; on success strip progress lines and the banner and quote from `stderr`. Done: `output` is gone; the MCP tool (only there, the analysis tools still parse the full text) shortens successful results with `compact_for_client`: banner, command line header, "Reading frame" lines, the quote and an answered energy term menu are removed, reports on stderr (`gmx check`, "Last frame", notes) are kept, and a saved `dump` returns its first 40 lines. The real energy stderr shrinks to under a quarter. Tests: `test_command_wrapper.py::TestCompactForClient` (real fixtures).
- [x] **13.5 `output_files` has three formats.** `run_gromacs_command` and `get_job_result` return `"name (N bytes)"` strings, `run_full_md_pipeline` and `run_gromacs_workflow` return bare file names, and `run_grompp`, `run_minimization`, `energy_summary`, `analyze_rdf` and the rest return absolute paths. A client chaining calls has to know which field to read in each tool. Fix: absolute paths in `output_files` everywhere, with sizes in a separate field if wanted. Done: absolute paths everywhere, sizes in `file_sizes` ({path: bytes}) for `run_gromacs_command` and MD jobs, `output_paths` removed; `get_job_result` still reads 2.0.0 records. `analyze_tpr` and batch jobs now list their files too. Tests: `test_command_wrapper.py::TestFilesAndOutput::test_dump_writes_stdout_to_file_and_has_no_o_flag`, `test_gmx_integration.py::TestMdRun::test_uses_tpr_steps_when_nsteps_not_given`, `TestWorkflows::test_analysis_workflow`, `TestAnalyzeTpr::test_saved_files_are_listed_as_absolute_paths`.
- [x] **13.6 `submit_batch_analysis` rejects the whole batch for one missing path.** The parameter description says one failing file does not stop the rest, but a nonexistent path fails the submission before any file is analyzed. Either analyze the valid files and report the missing one in `results` with an `error`, or say in the description that paths are checked up front. Done: paths outside the sandbox or not ending in .tpr still refuse the batch; a missing file is a warning at submission and a failed entry in the result; the job completes when at least one file succeeds (it used to fail when any file failed). Tests: `test_gmx_integration.py::TestAnalyzeTpr::test_batch_reports_a_missing_file_and_analyzes_the_rest`, `test_batch_refuses_bad_paths_up_front`.

### Cosmetic

- [x] **13.7 `plot_xvg` y label lists every unit in the file.** It uses the file's `yaxis` (`src/tools/analysis.py:552`), which for `gmx energy` output is "(kJ/mol), (K), (bar), (nm^3), (kg/m^3)" even when one column is plotted. Fix: when the label has one comma separated entry per legend, keep only the entries for the plotted columns. Done, test: `test_parsing.py::TestPlotLabels`.
- [x] **13.8 The energy term probe is logged as an ERROR.** The term list is read by running `gmx energy` with no selections on purpose (`src/tools/analysis.py:117`); GROMACS exits with "No energy terms selected" and `scripts/gromacs_command.py:352` logs that at ERROR level on every `energy_summary` and `trajectory_report` call, which reads like a failure in the server log. Fix: log at debug level when the command `needs_selections` and none were given. Done, test: `test_command_wrapper.py::TestErrorPropagation::test_menu_probe_is_not_logged_as_an_error`.
- [x] **13.9 The example `md.mdp` fails the server's own lint.** `scripts/make_example_data.py:83` sets `nstenergy` to 50 but keeps the template's `nstcalcenergy = 100`, so `validate_mdp` on `examples/data/md.mdp` warns "nstenergy should be a multiple of nstcalcenergy" (grompp quietly lowers nstcalcenergy). Fix: also set `nstcalcenergy: 50` and regenerate the examples. Test: run the linter over `examples/data/*.mdp` and expect no warnings. Done: the one line changed in `md.mdp` (`water.gro` was not regenerated), `sample.tpr` rebuilt. Test: `test_parsing.py::TestExampleData`.
- [x] **13.10 `full_md` leaves a `#ions.top.1#` backup in its output folder.** The genion step copies the topology to `ions.top` (`pre_copy`) and genion then rewrites it in place (`scripts/gromacs_workflow.py:470-472`), so GROMACS keeps a backup. Harmless clutter; remove it after the step or let genion write to a name that does not exist yet. Done: solvate left `#solv.top.1#` the same way; steps that edit a `pre_copy` run with `GMX_MAXBACKUP=-1`. Tests: `test_gmx_integration.py::TestWorkflows::test_full_md_end_to_end_and_resume`, `test_repeated_fresh_runs_do_not_corrupt_the_topology` (both fail without it).

---

## 14. Found while working on 2.1.0

- [x] **14.0 `trjconv -exec` ran shell commands (security, found by an automated review).** trjconv's `-exec` passes its value to the shell for every output frame, and `run_gromacs_command("trjconv", parameters=["-sep", "-exec", "..."])` ran it (confirmed with GROMACS 2026.4; `--exec` works too). Refused for every command under any spelling, in parameters, as a file option name and in `run_grompp(extra_parameters)`. A scan of the help texts missed `-w` (its help only says "View output"); see 14.5. grompp `-qmi` added to the file options that must come through `input_files`. Tests: `test_gmx_integration.py::TestRunCommand::test_trjconv_exec_never_runs` (real gmx; runs the command when the guard is removed), `test_command_wrapper.py::TestNoShell::test_options_that_run_programs_are_refused`, `test_exec_as_a_file_option_is_refused`, `test_run_grompp_refuses_exec_in_extra_parameters`.
- [x] **14.1 Progress after extend plus resume.** mdrun `-append` does not print the parameters again, so after extend and resume md.log only holds the first run's `nsteps`: percent showed 100 as soon as the old count was passed, and a run stopped by maxh past it counted as complete (now `completed` instead of `stopped`). The MD wrapper and the job manager now take the target from the `.tpr` (`read_tpr_nsteps` streams `gmx dump` and stops at the line, before the topology); the manager keeps it in the job record. Tests: `test_gmx_integration.py::TestMdRun::test_extended_run_stopped_by_maxh_is_not_complete` (real mdrun; fails without the fix), `test_parsing.py::TestCheckAndLog::test_nsteps_from_the_tpr_overrides_the_log`.
- [x] **14.2 Minimization jobs and zero step runs looked stopped.** A converged steepest descent stops before `nsteps` by design, and `nsteps=0` was never counted as complete, so both would have become `stopped` with a hint to resume. `complete` now also means a minimizer converged, or 0 of 0 steps. Tests: `test_parsing.py::TestCheckAndLog::test_converged_minimization_is_complete` (real em md.log), `test_zero_steps_and_no_limit`, `test_gmx_integration.py::TestMdRun::test_minimization_as_a_job_is_complete_when_converged`, `test_nsteps_zero_is_respected`.
- [x] **14.3 `analyze_tpr` did not list the files it wrote** (the summary and the raw dump) in `output_files`. Test: `test_gmx_integration.py::TestAnalyzeTpr::test_saved_files_are_listed_as_absolute_paths`.
- [x] **14.4 Statistical inefficiency estimate.** The first version took growing lag steps from the start (pymbar fast mode) and overestimated g by 20 to 30 percent on AR(1) series; thinning long series also turned white noise into g = 2. The first 50 lags are now exact and thinning only happens for slowly decorrelating series. Test: `test_parsing.py::TestStatistics::test_statistical_inefficiency_of_ar1_series`.
- [x] **14.5 More ways gmx reaches a shell (security, second automated review).** `-w` (most analysis tools) opens results in a viewer through the shell with the file name on the command line: refused, and every gmx child runs without `DISPLAY` and `GMX_VIEW_*`. gmx reads compressed files through `gunzip -c < name` (strings found in libgromacs), also as a fallback when `name` is missing and `name.gz` exists: file names with `$ \` ; & | < >` or a newline, and `.gz`/`.Z` names, are refused in file options and in path-like parameters. `wham` removed from the whitelist: its list files (`-it`, `-ix`, `-if`, `-ip`) name files the wrapper never sees, outside the sandbox check, and `.gz` entries go through the shell. Include lines the checker cannot parse are now refused (fail closed); GROMACS 2026.4 itself rejects the loose spellings tried. Tests: `test_command_wrapper.py::TestNoShell::test_shell_active_file_names_are_refused`, `test_viewer_option_is_refused`, `test_children_get_no_display`, `test_wham_is_not_whitelisted`, `TestSandbox::test_unreadable_include_lines_are_refused` (each fails with its guard removed).
- [x] **14.6 File names in parameters were recognized by their looks (security, third automated review).** Only values with a slash or a known suffix were checked, so `-f2 'x$(...)'` passed. Now position decides: the file options of each tool come from its `gmx help` (`[<.ext>]` and `<file>` types, read once per tool), their values in `parameters` must be plain names (letters, digits, `. _ + -`, so library files like `spc216.gro` still work), and paths go through `input_files`/`output_files`, which take a list for options with several files. Tests: `test_command_wrapper.py::TestNoShell::test_file_options_in_parameters_take_plain_names_only`, `test_several_files_for_one_option`, `test_gmx_integration.py::TestRunCommand::test_file_option_value_without_a_suffix_is_checked` (fails without the check), `test_trjcat_joins_several_inputs`.
- [x] **14.7 The position check of 14.6 failed open (security, fourth automated review).** Any token starting with `-` ended a file option's value, but gmx reads `-`, `--` and `-.5` as values, so `-n - 'x$(...)'` slipped through; and a help lookup that failed gave no file options and was cached. Now only a real option name of the tool (from its help) ends the value, a file option in parameters takes exactly one plain name, and an unreadable help refuses the call without being cached. Tests: `test_command_wrapper.py::TestNoShell::test_only_real_option_names_end_a_file_value`, `test_unreadable_help_refuses_and_is_not_cached` (both fail without the fix). The fake gmx of the tests answers `help <tool>` with the real `gmx help rdf` text.
