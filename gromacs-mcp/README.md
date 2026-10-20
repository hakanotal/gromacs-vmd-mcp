# GROMACS MCP Server

An MCP (Model Context Protocol) server that lets an AI assistant run GROMACS molecular dynamics: prepare a system, run simulations as tracked background jobs, and analyze the results as numbers rather than file paths.

It provides 32 tools in six groups:

| Group | Tools |
|---|---|
| Prepare | `create_mdp`, `validate_mdp`, `run_grompp`, `run_minimization`, `extend_simulation`, `list_force_fields`, `run_full_md_pipeline` |
| Run | `submit_md_simulation`, `submit_batch_analysis`, `run_gromacs_command` (33 whitelisted gmx tools), `run_gromacs_workflow`, `list_workflows` |
| Jobs | `get_job_status` (progress and ETA), `get_job_log`, `get_job_result`, `cancel_job`, `resume_job`, `list_jobs`, `delete_job`, `clean_jobs` |
| Analyze | `energy_summary`, `analyze_rdf`, `analyze_msd`, `trajectory_report`, `read_xvg`, `plot_xvg`, `trjconv_preset`, `analyze_tpr` |
| Info | `check_environment`, `get_server_info`, `list_available_commands`, `get_command_help` |

It also exposes job output files as MCP resources and ships three prompt templates (water box, protein equilibration, RMSD analysis). See [detail.md](detail.md) for the full reference.

## Quick start with Docker

```bash
docker pull ghcr.io/hakanotal/gromacs-vmd-mcp/gromacs-mcp:latest
claude mcp add gromacs -- docker run -i --rm --user `id -u`:`id -g` -v `pwd`:`pwd` ghcr.io/hakanotal/gromacs-vmd-mcp/gromacs-mcp:latest
```

Run this from your project directory: `-v` mounts it so the server can read and write your files, and `--user` makes the output files yours. To build the image yourself:

```bash
git clone https://github.com/hakanotal/gromacs-vmd-mcp.git && cd gromacs-vmd-mcp/gromacs-mcp
docker build -t gromacs-mcp:latest .                     # GROMACS 2025.4 from conda-forge
docker build --build-arg GROMACS_VERSION=2025.3 -t gromacs-mcp:2025.3 .
claude mcp add gromacs -- docker run -i --rm --user `id -u`:`id -g` -v `pwd`:`pwd` gromacs-mcp:latest
```

## Quick start without Docker

You need Python 3.10+ and GROMACS. If `gmx` is already installed (Homebrew, a cluster module, a system package), the setup script only creates the Python environment. Otherwise it installs GROMACS from conda-forge when conda or mamba is available.

```bash
git clone https://github.com/hakanotal/gromacs-vmd-mcp.git && cd gromacs-vmd-mcp/gromacs-mcp
bash quick_setup.sh            # add --dev for the test dependencies, --plots for matplotlib
claude mcp add gromacs -- $(pwd)/env/bin/python $(pwd)/src/server.py
```

With uv instead: `uv sync --extra dev`, then register `$(pwd)/.venv/bin/python $(pwd)/src/server.py`.

## Check that it works

Ask the assistant to run `check_environment`. It reports the GROMACS version and build, CPU and GPU, free disk space, optional packages, and anything that blocks simulations. Then try:

```
Use the example in examples/data: minimize water.gro with topol.top, make an NPT mdp for 30 ps at 300 K,
run grompp and submit the simulation. When it finishes, give me the equilibrated density and temperature,
the O-O RDF first peak and the water diffusion coefficient.
```

## Configuration

All settings are environment variables (pass them with `-e` to Docker or in the `env` block of `.mcp.json`):

| Variable | Default | Meaning |
|---|---|---|
| `GMX_MCP_GMX` | `gmx`, then `gmx_mpi` on PATH | Path to the gmx binary to use |
| `GMX_MCP_WORKDIR` | unrestricted | Only allow reading and writing below this directory |
| `GMX_MCP_HOME` | repository root; `<GMX_MCP_WORKDIR>/gromacs_mcp` when a sandbox is set; `/data` in Docker | Where `jobs/` (records), `runs/` (job outputs), `results/` and `logs/` are created |
| `GMX_MCP_MAX_JOBS` | 2 | Simulations running at once; more are queued |
| `GMX_MCP_MIN_FREE_MB` | 500 | Refuse to start jobs below this much free disk |
| `GMX_MCP_MAX_CHARS` | 20000 | Longer text fields in results are shortened |
| `GMX_MCP_SCRIPT_DIRS` | none | Extra folders jobs may run scripts from (besides `scripts/`) |

## Development

```bash
./env/bin/python -m pytest             # about 380 tests; real simulations included (about 55 s)
./env/bin/python -m pytest -m "not slow"
```

Tests that need GROMACS are skipped when `gmx` is missing. See [CLAUDE.md](CLAUDE.md) for the architecture and [TODO.md](TODO.md) for open work.

## License

MIT, see [LICENSE](../LICENSE). GROMACS itself is LGPL and installed separately.
