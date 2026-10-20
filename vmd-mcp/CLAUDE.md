# CLAUDE.md

Guidance for working on vmd-mcp.

## What this is

An MCP server (FastMCP 4) for VMD: headless rendering and movies, per-frame analysis, interactive sessions. 14 tools and 1 prompt. It runs from a checkout: `./env/bin/python src/server.py`.

## Run and test

```bash
python3 -m venv env && ./env/bin/pip install -r requirements-dev.txt
./env/bin/python -m pytest          # about 5 s; a fake vmd (tests/conftest.py) records argv and scripts;
                                    # tests/test_real_vmd.py runs against VMD when it is installed
```

## Layout

- `src/server.py`: the tools and `main()`.
- `src/vmd.py`: `find_vmd`, `run_vmd_script` (text mode, `-args`), the sandbox (`resolve_in_workdir`), input checks (`check_selection`, `check_choice`), `parse_series`, and the result envelope (`result`).
- `src/templates.py`: the Tcl scripts (render, movie, RMSD, contacts, radius of gyration, SASA, hydrogen bonds, secondary structure) and the style presets.

## Conventions

- Never run VMD through a shell. Build an argv list.
- Scripts go through `run_vmd_script`, which wraps them in one `catch` block and appends `quit` (VMD 2 drops a script silently at its first error). Do not add `quit` to templates.
- Check new templates against a real VMD: text mode differs from the GUI (`display resize` renders blank).
- Images: VMD writes a Tachyon scene (`render Tachyon file`, never the `exec` form, which uses a shell) and the server runs `tachyon` with `-res` (`run_tachyon`). Representations reach the scene as `$argv` triples (`check_representations`).
- The GUI keeps a stdin pipe open: VMD quits at the end of its console input.
- Tcl scripts are fixed text. Pass every client value as an `-args` element and read it from `$argv`; never format values into Tcl. Check selections with `check_selection`, and enumerated options with `check_choice`.
- Every tool returns `result(...)`: `status`, `warnings`, `output_files` (absolute paths), and `error` on failure.
- Resolve user paths with `resolve_in_workdir` (or `existing_file` and `output_path`) so `VMD_MCP_WORKDIR` holds.
- New tools need an entry in `tests/test_server.py::BAD_CALLS` (a test fails otherwise).
- Nothing may print to stdout: it carries the MCP protocol. Logging goes to stderr.
- When a test guards a fix, check that it fails with the fix removed.
