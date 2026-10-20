# gromacs-vmd-mcp

Two MCP (Model Context Protocol) servers that let an AI assistant run molecular dynamics with [GROMACS](https://www.gromacs.org) and look at the results with [VMD](https://www.ks.uiuc.edu/Research/vmd/).

| Server | Folder | What it does |
|---|---|---|
| `gromacs` | [`gromacs-mcp/`](gromacs-mcp) | Prepares systems, runs simulations as tracked background jobs, and analyzes them as numbers: energies with uncertainties, RDF, diffusion, trajectory checks. 32 tools |
| `vmd` | [`vmd-mcp/`](vmd-mcp) | Renders structures, trajectory frames and movies without a display, computes per-frame RMSD, radius of gyration, SASA, hydrogen bonds, contacts and secondary structure, and opens files in an interactive VMD window. 14 tools |

The two servers are independent. Each has its own dependencies, tests and documentation, and you can register either one alone. Used together, a typical session runs a simulation with `gromacs`, then renders the final frame and plots the RMSD with `vmd`.

## Setup

You need Python 3.10+, GROMACS for `gromacs-mcp` (Homebrew, conda-forge, a cluster module or the Docker image), and VMD for `vmd-mcp` (tested with VMD 2.0.0a7).

```bash
git clone https://github.com/hakanotal/gromacs-vmd-mcp.git && cd gromacs-vmd-mcp

# GROMACS server: finds or installs gmx, creates gromacs-mcp/env
(cd gromacs-mcp && bash quick_setup.sh)
claude mcp add gromacs -- $(pwd)/gromacs-mcp/env/bin/python $(pwd)/gromacs-mcp/src/server.py

# VMD server
(cd vmd-mcp && python3 -m venv env && ./env/bin/pip install -r requirements.txt pillow)
claude mcp add vmd -e VMD_MCP_VMD=/path/to/vmd -- $(pwd)/vmd-mcp/env/bin/python $(pwd)/vmd-mcp/src/server.py
```

That is the macOS and Linux path. **[SETUP.md](SETUP.md)** has the full guide per system, including Linux packages, Windows through WSL2 or Docker, and Claude Desktop configuration files. For other MCP clients, copy [`.mcp.json.example`](.mcp.json.example) to `.mcp.json` and fill in the paths. Ask the assistant to run `check_environment` (GROMACS) and `check_vmd_environment` (VMD) first; they say exactly what is missing.

Details, configuration and the security model are in each server's README: [gromacs-mcp](gromacs-mcp/README.md), [vmd-mcp](vmd-mcp/README.md).

## Layout

```
gromacs-mcp/   GROMACS server: src/ (tools, jobs), scripts/ (what jobs run), tests/, examples/data/
vmd-mcp/       VMD server: src/ (server, VMD runner, Tcl templates), tests/
.github/       CI: tests for each server (fake binaries plus real GROMACS), Docker image for gromacs-mcp
```

## Tests

```bash
(cd gromacs-mcp && ./env/bin/python -m pytest)   # about 380 tests, real simulations included when gmx is present
(cd vmd-mcp && ./env/bin/python -m pip install -r requirements-dev.txt && ./env/bin/python -m pytest)
```

## License

MIT, see [LICENSE](LICENSE). `vmd-mcp` is derived from an MIT-licensed prototype whose notice is kept in [vmd-mcp/LICENSE](vmd-mcp/LICENSE). GROMACS (LGPL) and VMD (UIUC license) are installed separately and are not part of this repository.
