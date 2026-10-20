# Setup guide

How to install and register the two servers on macOS, Linux and Windows.

| | macOS | Linux | Windows |
|---|---|---|---|
| gromacs-mcp | native | native | WSL2 (recommended) or Docker. Not native: the job manager needs POSIX |
| vmd-mcp | native | native | WSL2, or native with the Windows VMD |
| Tested | yes (Apple silicon, GROMACS 2026.4, VMD 2.0.0a7) | CI only (Ubuntu, GROMACS from conda-forge; VMD faked) | no; the steps below are expected to work but are unverified |

Every server needs Python 3.10 or newer. The snippets use `/path/to/gromacs-vmd-mcp` for the folder you cloned into.

---

## macOS

```bash
brew install gromacs                       # or let quick_setup.sh use conda
git clone https://github.com/hakanotal/gromacs-vmd-mcp.git && cd gromacs-vmd-mcp
(cd gromacs-mcp && bash quick_setup.sh)
(cd vmd-mcp && python3 -m venv env && ./env/bin/pip install -r requirements.txt pillow)
```

Optional: `brew install ffmpeg` so `render_movie` can write `.mp4` files (on Linux `sudo apt install ffmpeg`; on Windows use WSL).

Install VMD from the [VMD download page](https://www.ks.uiuc.edu/Development/Download/download.cgi?PackageName=VMD) (free, needs registration) and drag `VMD.app` to `/Applications`. vmd-mcp finds it there, both the VMD 2 layout (`Contents/vmd2/lib/vmd_MACOSX*`) and the 1.9 layout (`Contents/vmd/vmd_MACOSX*`).

Register both with Claude Code:

```bash
claude mcp add gromacs -e PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin -- \
  $(pwd)/gromacs-mcp/env/bin/python $(pwd)/gromacs-mcp/src/server.py
claude mcp add vmd -- $(pwd)/vmd-mcp/env/bin/python $(pwd)/vmd-mcp/src/server.py
```

For Claude Desktop, put the same commands in `~/Library/Application Support/Claude/claude_desktop_config.json` (see [Client configuration](#client-configuration)). Desktop apps start servers with a short `PATH`, so give `gmx` explicitly with `GMX_MCP_GMX` (or a `PATH`) as shown there.

---

## Linux

The steps are for Debian and Ubuntu; use your distribution's package manager otherwise.

### GROMACS

Choose one:

```bash
# a) conda-forge, the newest GROMACS. quick_setup.sh does this for you when gmx is missing and
#    conda or mamba is installed (Miniforge: https://github.com/conda-forge/miniforge)
(cd gromacs-mcp && bash quick_setup.sh)

# b) the distribution package (older: Ubuntu 24.04 ships GROMACS 2023). The tests were written
#    against 2025 and 2026 output, so prefer (a) when a parser misbehaves
sudo apt install gromacs
(cd gromacs-mcp && bash quick_setup.sh)

# c) a cluster module: load it before starting the server, or set GMX_MCP_GMX
module load gromacs
```

Some cluster builds only provide `gmx_mpi`. The server finds it, but runs one rank; MPI launchers are not supported yet (gromacs-mcp TODO 12.5).

### VMD

Download the Linux tarball from the VMD site. Use the OpenGL build on a desktop, or the text-only build on a server without a display. Then:

```bash
tar xzf vmd-*.tar.gz && cd vmd-*/
./configure                                # installs to /usr/local/bin and /usr/local/lib/vmd
cd src && sudo make install
sudo apt install tcsh                      # the vmd launcher is a csh script
vmd -dispdev text -eofexit < /dev/null     # should print the VMD banner and exit
```

vmd-mcp finds `vmd` on `PATH`, `/usr/local/bin/vmd` and `/opt/vmd*/bin/vmd`. Rendering uses VMD's text mode and Tachyon, so it needs no X server. `launch_vmd_gui` needs a desktop session (`DISPLAY` or Wayland).

```bash
(cd vmd-mcp && python3 -m venv env && ./env/bin/pip install -r requirements.txt pillow)
```

### Register

Claude Code:

```bash
claude mcp add gromacs -- $(pwd)/gromacs-mcp/env/bin/python $(pwd)/gromacs-mcp/src/server.py
claude mcp add vmd -- $(pwd)/vmd-mcp/env/bin/python $(pwd)/vmd-mcp/src/server.py
```

Without a desktop, add `-e VMD_MCP_WORKDIR=$HOME/md` and `-e GMX_MCP_WORKDIR=$HOME/md` to confine both servers to one folder. That is advisable on shared machines.

---

## Windows

**The recommended way is WSL2.** Run both servers inside Ubuntu on WSL2 and follow the [Linux](#linux) steps there, unchanged. WSL2 is required for gromacs-mcp: its job manager uses POSIX process groups and file locks, and started natively on Windows it exits with a message saying so.

```powershell
wsl --install -d Ubuntu      # once, then restart
wsl -d Ubuntu                # open Ubuntu and follow the Linux steps in your Linux home folder
```

Keep the repository and your simulation files in the Linux file system (`/home/<you>/...`). GROMACS on `/mnt/c/...` works but is much slower. From Windows you reach those files at `\\wsl$\Ubuntu\home\<you>`.

### Option A: Claude Code inside WSL

The simplest option. Install Claude Code in the Ubuntu shell and register the servers exactly as on Linux. GUI windows from `launch_vmd_gui` open through WSLg on Windows 11.

### Option B: Claude Desktop or Claude Code on Windows, servers in WSL

Windows clients start a WSL process per server through `wsl.exe`. Environment variables from Windows do not reach WSL, so set them with `env` inside the command, in `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "gromacs": {
      "command": "wsl.exe",
      "args": ["-d", "Ubuntu", "--", "env", "GMX_MCP_GMX=/usr/bin/gmx",
               "/home/you/gromacs-vmd-mcp/gromacs-mcp/env/bin/python",
               "/home/you/gromacs-vmd-mcp/gromacs-mcp/src/server.py"]
    },
    "vmd": {
      "command": "wsl.exe",
      "args": ["-d", "Ubuntu", "--", "env", "VMD_MCP_VMD=/usr/local/bin/vmd",
               "/home/you/gromacs-vmd-mcp/vmd-mcp/env/bin/python",
               "/home/you/gromacs-vmd-mcp/vmd-mcp/src/server.py"]
    }
  }
}
```

Use `GMX_MCP_GMX=/home/you/gromacs-vmd-mcp/gromacs-mcp/env/bin/gmx` when quick_setup.sh installed GROMACS with conda. Paths you give the tools are Linux paths (`/home/you/md/protein.pdb`; Windows drives are under `/mnt/c/`).

### Option C: gromacs-mcp in Docker

This needs Docker Desktop, which itself uses WSL2. Mount the project folder and use `/work/...` paths in the tools:

```json
{
  "mcpServers": {
    "gromacs": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "-v", "C:\\Users\\you\\md:/work", "-w", "/work",
               "ghcr.io/hakanotal/gromacs-vmd-mcp/gromacs-mcp:latest"]
    }
  }
}
```

### Option D: vmd-mcp natively on Windows (unverified)

vmd-mcp has no POSIX-only code. With the Windows VMD installer it is expected to work natively:

```powershell
git clone https://github.com/hakanotal/gromacs-vmd-mcp.git
cd gromacs-vmd-mcp\vmd-mcp
py -3 -m venv env
.\env\Scripts\pip install -r requirements.txt pillow
```

```json
{
  "mcpServers": {
    "vmd": {
      "command": "C:\\Users\\you\\gromacs-vmd-mcp\\vmd-mcp\\env\\Scripts\\python.exe",
      "args": ["C:\\Users\\you\\gromacs-vmd-mcp\\vmd-mcp\\src\\server.py"],
      "env": {"VMD_MCP_VMD": "C:\\Program Files\\University of Illinois\\VMD\\vmd.exe"}
    }
  }
}
```

The server also looks in `C:\Program Files\University of Illinois\VMD`, the same under `Program Files (x86)`, and `C:\Program Files\VMD*`. If `check_vmd_environment` reports that VMD did not run in text mode, use WSL instead: the Windows VMD may open its own console window. Please report it, because this setup has not been tried.

---

## Client configuration

Every MCP client needs the same three things per server: the Python of the server's `env`, the path to `src/server.py`, and optionally environment variables.

| Client | Where the configuration goes |
|---|---|
| Claude Code | `claude mcp add <name> [-e KEY=value] -- <python> <server.py>`, or `.mcp.json` in a project ([.mcp.json.example](.mcp.json.example)) |
| Claude Desktop, macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Claude Desktop, Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Other clients | Their MCP settings, as a stdio server with the same command and arguments |

The environment variables, for both servers:

| Variable | Server | Use |
|---|---|---|
| `GMX_MCP_GMX` | gromacs | The `gmx` binary, when the client's `PATH` does not include it |
| `GMX_MCP_WORKDIR` | gromacs | Allow only files below this folder |
| `GMX_MCP_MAX_JOBS` | gromacs | Simulations at once (default 2) |
| `VMD_MCP_VMD` | vmd | The VMD executable |
| `VMD_MCP_WORKDIR` | vmd | Allow only files below this folder |
| `VMD_MCP_ALLOW_TCL` | vmd | `1` enables arbitrary Tcl (`run_vmd_tcl`); leave it off unless needed |

The full lists are in [gromacs-mcp/README.md](gromacs-mcp/README.md) and [vmd-mcp/README.md](vmd-mcp/README.md).

## Check the installation

Ask the assistant to run `check_environment` and `check_vmd_environment`. Each lists what it found and what is missing. The common fixes:

| Symptom | Fix |
|---|---|
| `Could not find GROMACS` | Set `GMX_MCP_GMX` to the full path of `gmx` (`which gmx` in a terminal) |
| `Could not find VMD` | Set `VMD_MCP_VMD` to the VMD executable |
| VMD found but not running in text mode (Linux) | Install `tcsh`, or point `VMD_MCP_VMD` at `/usr/local/lib/vmd/vmd_LINUXAMD64` |
| gromacs-mcp exits at once on Windows | Expected: use WSL2 or Docker (above) |
| Very slow simulations under WSL | Move the files from `/mnt/c/...` into the Linux home folder |
| Images come out as `.tga` | Install Pillow in the vmd-mcp env (`pip install pillow`) |
