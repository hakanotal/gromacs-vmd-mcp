# Changelog

## 1.1.0 (repository v1.1)

- `render_image` takes `width` and `height` again: VMD writes a Tachyon scene and the server renders it with the `tachyon` binary VMD ships (`-res`), as an argument list. The zoom adapts to the aspect ratio so the scene fits, and `zoom` magnifies on top of it. Without Tachyon, images stay 512 x 512.
- Several representations in one image (`representations`, up to 8), and presets built from them (`protein_ligand`, `solvated`, `all_vdw`).
- `render_movie`: frames with an optional rotation, rendered by Tachyon and assembled into an `.mp4` by ffmpeg when present, with a frame cap.
- New analyses: `radius_of_gyration_over_trajectory`, `sasa_over_trajectory`, `hbonds_over_trajectory`, `secondary_structure_over_trajectory` (STRIDE, with fractions).
- The prompt `visualize_md_run` for runs from gromacs-mcp.
- `launch_vmd_gui` kept no window open: VMD reads commands from its console and quit at the end of the closed input it was given. It now gets a pipe that stays open, and `close_vmd_session` sends `quit` before signalling.
- A `.tpr` given as the structure gets an error that points to the `.gro` (VMD cannot read `.tpr`).
- Finds VMD in the Linux install locations (`/usr/local/bin/vmd`, `/opt/vmd*/bin/vmd`) and the Windows installer's folders (`C:\Program Files\University of Illinois\VMD\vmd.exe` and similar).
- SETUP.md at the repository root covers Linux and Windows.

## 1.0.0

First release, derived from an MIT-licensed prototype ("mcp-gmx-vmd") that combined VMD and GROMACS tools. The prototype did not work with Claude and was unsafe, so this version keeps its VMD ideas (the Tcl templates for loading, representations, rendering, RMSD and contacts, the style presets, and launching VMD sessions) and rebuilds the rest.

### Changed from the prototype

- Tool names are English identifiers. The prototype's tool names were Chinese strings, which Claude rejects.
- The server starts: `python src/server.py` runs it over stdio. The prototype's main file never called `run()`, and its other entry points (`server.py`, `cli.py`, `main.py`, `mcp_gmx_vmd/server.py`) were stubs or broken.
- No shell anywhere. The prototype ran VMD with `shell=True`, wrote Terminal `.sh` scripts with unquoted paths, and made a temporary folder world writable.
- No Tcl injection. The prototype pasted selections and file names into Tcl text; values now reach fixed scripts only as `$argv`, and selections are restricted to the selection language.
- Arbitrary Tcl (`run_vmd_tcl`) is off unless `VMD_MCP_ALLOW_TCL=1`.
- Paths are sandboxed with `VMD_MCP_WORKDIR`, and default outputs get unique names under `results/`.
- No hardcoded paths. The prototype used the original author's home folder and a fixed ARM64 VMD binary.
- RMSD fits each frame onto the reference before measuring (the prototype measured without fitting).

### Removed

- The GROMACS tools (command execution, simulation stages, analysis, workflow management, parameter validation). The GROMACS server in `../gromacs-mcp` covers them safely. The prototype also let a config tool point `gmx_path` at any binary.
- Workflow and config resources, `config.json`, `setup.py`, and about 50 pinned dependencies the code did not use (fastapi, uvicorn, trio, ...).

### Added

- One result envelope for every tool (`status`, `error`, `warnings`, `output_files`), like gromacs-mcp.
- Tests: 42 with a fake vmd (argv shape, values kept out of the scripts, refused selections, the sandbox, every tool's envelope, a stdio start-up check) and 6 against a real VMD that run when it is installed (checked with VMD 2.0.0a7 on macOS).
- Works with VMD 2: finds `VMD.app/Contents/vmd2/lib/vmd_MACOSX*`, ignores VMD 2's start-up `ERROR)` lines, and runs each script as one `catch` block (VMD 2 drops a script silently at its first Tcl error). Images are 512 x 512, because `display resize` renders blank in text mode.
