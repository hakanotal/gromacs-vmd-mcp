# vmd-mcp TODO

Status marks: `[x]` done and tested here, `[~]` partly done, `[ ]` open.

## Checked against a real VMD (2.0.0a7, macOS arm64)

`tests/test_real_vmd.py` runs these whenever VMD is installed (it is skipped in CI). The GROMACS tests also need `gmx`.

- [x] **1.1 Text mode works** when the binary in `VMD.app/Contents/vmd2/lib` is run directly with `VMDDIR` set; the csh launcher in `vmd2/bin` carries the packager's home folder and is not used.
- [x] **1.2 VMD 2 prints `ERROR) Duplicate resname` lines on every start.** Only lines after the script's start marker count as errors.
- [x] **1.3 VMD silently abandons an `-e` script at its first Tcl error**, and stopped mid-script after `display resize`. Every script runs as one `catch` block that prints `VMD_MCP_ERROR:` and always reaches `quit`.
- [x] **1.4 `display resize` in text mode renders an empty image**, with either renderer.
- [x] **1.5 RMSD and contacts give the expected values** on a 4 atom molecule with known motions. `measure contacts` skips pairs VMD considers bonded.
- [x] **1.6 Image size.** VMD writes a Tachyon scene; the server runs the bundled `tachyon_*` with `-res W H` as argv. Tachyon keeps the vertical field of view, so the zoom is 0.7 x min(1, W/H). Tests: `test_requested_size_with_tachyon` (checks the TGA header and that the image is not blank), `test_tachyon_renders_at_the_requested_size`, `test_tall_images_zoom_out`.
- [x] **1.7 GROMACS formats.** `.gro` with `.xtc` and with `.trr` load and render (`test_gromacs_gro_with_trajectory`, a short run of the gromacs-mcp water box made with the local gmx). `.tpr` cannot be read by VMD 2.0.0a7; the error says to use the `.gro` (`test_gromacs_tpr_gets_a_hint`).
- [x] **1.8 `launch_vmd_gui` on macOS.** Checked by hand: the window opened (OpenGL on Apple M4 Pro), stayed open, and `close_vmd_session` closed it in 0.1 s. Found that VMD quit at once with a closed stdin; it now gets a pipe (`test_launch_list_close` fails without it).
- [ ] **1.9 VMD 1.9.4.** Check the same tests with the older layout (`Contents/vmd/vmd_MACOSX*`).
- [ ] **1.10 Linux.** Run `tests/test_real_vmd.py` with the tarball install (`/usr/local/bin/vmd`, a csh launcher that needs tcsh, tachyon in `/usr/local/lib/vmd`) and with the text-only build on a machine without a display.
- [ ] **1.11 Windows.** Native: does `vmd.exe -dispdev text -e script -args ...` run in the background and read `$argv`, and where is `tachyon_WIN*.exe`? Under WSL2 the Linux steps should apply; check `launch_vmd_gui` through WSLg.

## Features

- [x] **2.1 Movies.** `render_movie`: frames with optional rotation, a frame cap, Tachyon at any size, ffmpeg assembly (argv) or the frames when ffmpeg is missing. Checked with `ffprobe`: 6 frames at 480 x 360. Tests: `TestMovie` (fakes), `test_movie` (real).
- [x] **2.2 More analyses.** Radius of gyration, SASA (with `restrict_to`), hydrogen bonds, STRIDE secondary structure with fractions. Real tests: `test_radius_of_gyration` (a 2.5 A square: 1.768 A), `test_sasa`, `test_water_hydrogen_bonds`, `test_secondary_structure_finds_a_helix` (an ideal 14 residue helix built in the test). STRIDE needs 5 or more residues and backbone hydrogen bonds; the error says so.
- [x] **2.3 Several representations in one scene.** `representations` (up to 8) and presets `protein_ligand`, `solvated`. Tests: `TestRenderSizesAndScenes`, `test_two_representations_in_one_image`.
- [x] **2.4 Hand off from gromacs-mcp.** The prompt `visualize_md_run`; `TestPrompt` checks that the tools it names exist.
- [ ] **2.5 CI with VMD.** VMD cannot be downloaded without registration, so CI only runs the fake vmd tests.
- [ ] **2.6 Protein test data.** The secondary structure and hydrogen bond checks use a generated helix and water. A small real protein run (for example a short lysozyme simulation from gromacs-mcp) would test the protein presets visually.
- [ ] **2.7 Faster movies.** Frames are rendered one at a time (Tachyon is multithreaded itself); rendering a few at once may help long movies.
