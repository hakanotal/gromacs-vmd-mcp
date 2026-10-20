# VMD MCP Server

An MCP server that lets an AI assistant use [VMD](https://www.ks.uiuc.edu/Research/vmd/) (Visual Molecular Dynamics): render structures and trajectory frames to images without a display, compute per-frame RMSD and contacts, and open files in an interactive VMD window.

It pairs with the GROMACS server in [`../gromacs-mcp`](../gromacs-mcp): GROMACS runs the simulation, VMD shows it.

| Tool | What it does |
|---|---|
| `check_vmd_environment` | VMD path and version (text mode), workspace, PNG support. Run this first |
| `list_templates` | Render styles, representations, colorings and the scripted operations |
| `render_image` | Structure or a trajectory frame to a 512 x 512 `.png` (with Pillow) or `.tga`, headless with Tachyon |
| `rmsd_over_trajectory` | RMSD per frame against a reference frame, after fitting |
| `contacts_over_trajectory` | Atom pairs within a cutoff between two selections, per frame |
| `launch_vmd_gui` | Open files in an interactive VMD window (needs a display) |
| `list_vmd_sessions`, `close_vmd_session` | Sessions started by this server |
| `run_vmd_tcl` | Your own Tcl. Off unless `VMD_MCP_ALLOW_TCL=1`, because Tcl can run shell commands |

## Setup

You need Python 3.10+ and VMD (tested with 2.0.0a7 on macOS; 1.9.x uses the same commands). VMD is a free download from the [VMD site](https://www.ks.uiuc.edu/Development/Download/download.cgi?PackageName=VMD) after registration.

```bash
git clone https://github.com/hakanotal/gromacs-vmd-mcp.git && cd gromacs-vmd-mcp/vmd-mcp
python3 -m venv env && ./env/bin/pip install -r requirements.txt pillow   # pillow for .png output
claude mcp add vmd -- $(pwd)/env/bin/python $(pwd)/src/server.py
```

The server looks for VMD in this order: `$VMD_MCP_VMD`, `vmd` on `PATH`, `/Applications/VMD*.app/Contents/vmd2/lib/vmd_MACOSX*` (VMD 2), `/Applications/VMD*.app/Contents/vmd/vmd_MACOSX*` (VMD 1.9), `/usr/local/bin/vmd`. MCP clients often start servers with a reduced `PATH`, so setting `VMD_MCP_VMD` is the reliable way.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `VMD_MCP_VMD` | search as above | The vmd executable |
| `VMD_MCP_WORKDIR` | unrestricted | Only allow reading and writing below this directory |
| `VMD_MCP_HOME` | this folder; `<VMD_MCP_WORKDIR>/vmd_mcp` with a sandbox | Where `results/` (default outputs) go |
| `VMD_MCP_ALLOW_TCL` | off | `1` enables `run_vmd_tcl` |
| `VMD_MCP_TIMEOUT` | 600 | Default seconds before a VMD run is stopped |

## How it stays safe

- VMD runs as an argument list (`vmd -dispdev text -eofexit -e script.tcl -args ...`), never through a shell.
- The Tcl scripts are fixed text. File names, selections and numbers reach them only as `$argv` values, so they are never parsed as Tcl. Atom selections are also limited to the characters the selection language needs.
- Representations and colorings come from a fixed list.
- Paths are resolved, symlinks included, and with `VMD_MCP_WORKDIR` set they must stay inside it.
- `close_vmd_session` only signals processes this server started.

## Development

```bash
./env/bin/pip install -r requirements-dev.txt
./env/bin/python -m pytest        # a fake vmd records each call; tests/test_real_vmd.py also runs when VMD is installed
```

See [TODO.md](TODO.md) for what still needs checking against a real VMD, and [CHANGELOG.md](CHANGELOG.md) for where this server came from.

## License

MIT (see [LICENSE](LICENSE)). It is derived from an MIT-licensed prototype whose notice is kept in that file.
