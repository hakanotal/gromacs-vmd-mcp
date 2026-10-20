# vmd-mcp TODO

Status marks: `[x]` done and tested here, `[~]` partly done, `[ ]` open.

## Checked against a real VMD (2.0.0a7, macOS arm64)

`tests/test_real_vmd.py` runs these whenever VMD is installed (it is skipped in CI).

- [x] **1.1 Text mode works** when the binary in `VMD.app/Contents/vmd2/lib` is run directly with `VMDDIR` set; the csh launcher in `vmd2/bin` carries the packager's home folder and is not used.
- [x] **1.2 VMD 2 prints `ERROR) Duplicate resname` lines on every start.** Only lines after the script's start marker count as errors.
- [x] **1.3 VMD silently abandons an `-e` script at its first Tcl error**, and stopped mid-script after `display resize`. Every script now runs as one `catch` block that prints `VMD_MCP_ERROR:` and always reaches `quit`.
- [x] **1.4 `display resize` in text mode renders an empty image** of the new size. Rendering keeps VMD's 512 x 512; the test checks the image is not blank.
- [x] **1.5 RMSD and contacts give the expected values** on a 4 atom molecule with known motions (translation: RMSD 0 fitted, 1 unfitted). `measure contacts` skips pairs VMD considers bonded.
- [ ] **1.6 Image size.** Render through the external `tachyon_*` binary with `-res W H`, run by the server as an argv list (not through VMD's `render ... exec` string, which uses a shell).
- [ ] **1.7 GROMACS formats.** Check `.gro` plus `.xtc`/`.trr` and `.tpr` loading on a real run (the tests use PDB).
- [ ] **1.8 `launch_vmd_gui` on macOS.** Not run here because it opens a window; check that running the binary with `-f` gets a display.
- [ ] **1.9 VMD 1.9.4.** Check the same tests with the older layout (`Contents/vmd/vmd_MACOSX*`).

## Features

- [ ] **2.1 Movies.** The prototype's `create_movie` template (frames to images while rotating): add a tool with a frame cap and an output folder, and assemble with ffmpeg when present.
- [ ] **2.2 More analyses.** Radius of gyration, hydrogen bonds (`measure hbonds`), SASA (`measure sasa`), secondary structure per frame.
- [ ] **2.3 Scene presets with several representations** (for example protein cartoon plus ligand licorice in one image).
- [ ] **2.4 Hand off from gromacs-mcp.** A prompt that runs a GROMACS job, then renders the last frame and plots the RMSD.
- [ ] **2.5 CI with VMD.** VMD cannot be downloaded without registration, so CI only runs the fake vmd tests.
