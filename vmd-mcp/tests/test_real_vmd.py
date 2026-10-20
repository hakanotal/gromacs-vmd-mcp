"""Against a real VMD, when one is installed (skipped otherwise, as in CI).

The input is a 4 atom molecule written as a 3 frame PDB, so the expected numbers are known:
frame 1 moves every atom by 1 A (pure translation: RMSD 0 after fitting, 1 without), and frame 2
pulls atom 4 away from the others (fewer contacts).
"""

from pathlib import Path

import pytest

import server
from vmd import VMDNotFoundError, find_vmd

try:
    find_vmd()
    HAVE_VMD = True
except VMDNotFoundError:
    HAVE_VMD = False

pytestmark = pytest.mark.skipif(not HAVE_VMD, reason="VMD not installed")

# 2.5 A apart: far enough that VMD does not guess bonds (measure contacts skips bonded pairs)
FRAMES = [
    [(0.0, 0.0, 0.0), (2.5, 0.0, 0.0), (0.0, 2.5, 0.0), (2.5, 2.5, 0.0)],
    [(1.0, 0.0, 0.0), (3.5, 0.0, 0.0), (1.0, 2.5, 0.0), (3.5, 2.5, 0.0)],
    [(0.0, 0.0, 0.0), (2.5, 0.0, 0.0), (0.0, 2.5, 0.0), (9.0, 9.0, 0.0)],
]
NAMES = ["CA", "CB", "N", "O"]


def pdb_model(coords, model=None):
    lines = [f"MODEL     {model}"] if model is not None else []
    for i, ((x, y, z), name) in enumerate(zip(coords, NAMES), start=1):
        lines.append(f"ATOM  {i:5d}  {name:<3} ALA A   1    {x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00           C")
    lines += ["ENDMDL"] if model is not None else ["END"]
    return "\n".join(lines) + "\n"


@pytest.fixture
def molecule(tmp_path):
    structure = tmp_path / "mol.pdb"
    structure.write_text(pdb_model(FRAMES[0]))
    trajectory = tmp_path / "traj.pdb"
    trajectory.write_text("".join(pdb_model(f, i + 1) for i, f in enumerate(FRAMES)) + "END\n")
    return structure, trajectory


def test_environment():
    r = server.check_vmd_environment()
    assert r["status"] == "success", r
    assert r["vmd_version"]


def test_rmsd_with_and_without_fitting(molecule, tmp_path):
    s, t = molecule
    fitted = server.rmsd_over_trajectory(str(s), str(t), selection="all", output_file=str(tmp_path / "a.dat"))
    assert fitted["status"] == "success", fitted
    # frame 0 of the structure plus the 3 trajectory frames
    assert fitted["n_frames"] == 4
    assert fitted["rmsd"][2][1] == pytest.approx(0.0, abs=1e-3)
    raw = server.rmsd_over_trajectory(str(s), str(t), selection="all", align=False, output_file=str(tmp_path / "b.dat"))
    assert raw["rmsd"][2][1] == pytest.approx(1.0, abs=1e-3)


def test_contacts(molecule):
    s, t = molecule
    # O touches CB and N (2.5 A) until the last frame, where it has moved away
    r = server.contacts_over_trajectory(str(s), str(t), "name O", "not name O", cutoff=3.0)
    assert r["status"] == "success", r
    assert [c for _, c in r["contacts"]] == [2, 2, 2, 0] and r["n_frames"] == 4


def test_render(molecule, tmp_path):
    s, t = molecule
    r = server.render_image(str(s), str(t), output_file=str(tmp_path / "img.tga"), representation="VDW")
    assert r["status"] == "success", r
    # the molecule must be in the picture: `display resize` once produced a blank image of the right size
    pixels = Path(r["output_file"]).read_bytes()[18:]
    assert len(set(pixels[i:i + 3] for i in range(0, len(pixels), 3))) > 100


def test_a_bad_selection_is_reported(molecule):
    s, t = molecule
    r = server.rmsd_over_trajectory(str(s), str(t), selection="nosuchkeyword 5")
    assert r["status"] == "error"


def test_a_file_vmd_cannot_read(tmp_path):
    bad = tmp_path / "broken.pdb"
    bad.write_text("not a pdb\n")
    r = server.render_image(str(bad), output_file=str(tmp_path / "x.tga"))
    assert r["status"] == "error"


@pytest.fixture(autouse=True)
def real_helpers(monkeypatch):
    """Use the tachyon and ffmpeg found on this machine (the fake tests block them by default)."""
    monkeypatch.delenv("VMD_MCP_TACHYON", raising=False)
    monkeypatch.delenv("VMD_MCP_FFMPEG", raising=False)


def _distinct_colors(path: Path) -> int:
    pixels = path.read_bytes()[18:]
    return len(set(pixels[i:i + 3] for i in range(0, len(pixels), 3)))


def test_requested_size_with_tachyon(molecule, tmp_path):
    s, t = molecule
    r = server.render_image(str(s), str(t), output_file=str(tmp_path / "wide.tga"), style="all_vdw",
                            width=640, height=360)
    assert r["status"] == "success" and (r["width"], r["height"]) == (640, 360), r
    data = Path(r["output_file"]).read_bytes()
    assert int.from_bytes(data[12:14], "little") == 640 and int.from_bytes(data[14:16], "little") == 360
    assert _distinct_colors(Path(r["output_file"])) > 100


def test_two_representations_in_one_image(molecule, tmp_path):
    s, _ = molecule
    reps = [{"selection": "name CA CB", "representation": "VDW", "coloring": "Name"},
            {"selection": "name N O", "representation": "Licorice", "coloring": "Element"}]
    r = server.render_image(str(s), output_file=str(tmp_path / "two.tga"), representations=reps, width=300, height=500)
    assert r["status"] == "success", r
    assert _distinct_colors(Path(r["output_file"])) > 100


def test_movie(molecule, tmp_path):
    from vmd import find_ffmpeg
    s, t = molecule
    r = server.render_movie(str(s), str(t), output_file=str(tmp_path / "m.mp4"), style="all_vdw",
                            rotate_degrees=10, width=320, height=240, fps=4)
    assert r["status"] == "success", r
    assert r["n_frames"] == 4
    frames = sorted(Path(r["frames_dir"]).glob("frame_*.tga"))
    assert all(_distinct_colors(f) > 50 for f in (frames[0], frames[-1]))
    if find_ffmpeg():
        assert Path(r["output_file"]).suffix == ".mp4" and Path(r["output_file"]).stat().st_size > 1000


def test_radius_of_gyration(molecule):
    s, t = molecule
    r = server.radius_of_gyration_over_trajectory(str(s), str(t), selection="all")
    assert r["status"] == "success", r
    # a 2.5 A square: every atom 2.5/sqrt(2) from the center (unweighted)
    assert r["rgyr"][0][1] == pytest.approx(2.5 / 2 ** 0.5, abs=0.01)
    assert r["rgyr"][-1][1] > r["rgyr"][0][1]  # atom 4 moved away


def test_sasa(molecule):
    s, t = molecule
    r = server.sasa_over_trajectory(str(s), str(t), selection="all")
    assert r["status"] == "success" and r["min"] > 0, r
    part = server.sasa_over_trajectory(str(s), str(t), selection="all", restrict_to="name O")
    assert part["status"] == "success" and part["sasa"][0][1] < r["sasa"][0][1]


def _helix_pdb(n_res: int) -> str:
    """An ideal alpha helix (phi -57, psi -47) of alanine backbone atoms, built atom by atom (NeRF)."""
    import math

    def place(a, b, c, bond, angle, torsion):
        ang, tor = math.radians(angle), math.radians(torsion)
        bc = [c[i] - b[i] for i in range(3)]
        nbc = math.sqrt(sum(x * x for x in bc))
        bc = [x / nbc for x in bc]
        ab = [b[i] - a[i] for i in range(3)]
        n = [ab[1] * bc[2] - ab[2] * bc[1], ab[2] * bc[0] - ab[0] * bc[2], ab[0] * bc[1] - ab[1] * bc[0]]
        nn = math.sqrt(sum(x * x for x in n))
        n = [x / nn for x in n]
        m = [n[1] * bc[2] - n[2] * bc[1], n[2] * bc[0] - n[0] * bc[2], n[0] * bc[1] - n[1] * bc[0]]
        d = [-bond * math.cos(ang), bond * math.sin(ang) * math.cos(tor), bond * math.sin(ang) * math.sin(tor)]
        return [c[i] + d[0] * bc[i] + d[1] * m[i] + d[2] * n[i] for i in range(3)]

    N, CA, C = [-0.5, 1.4, 0.0], [0.0, 0.0, 0.0], [1.525, 0.0, 0.0]
    atoms = []
    for i in range(n_res):
        if i:
            N = place(prev_N, prev_CA, prev_C, 1.329, 116.2, -47.0)
            CA = place(prev_CA, prev_C, N, 1.458, 121.7, 180.0)
            C = place(prev_C, N, CA, 1.525, 111.2, -57.0)
        nxt = place(N, CA, C, 1.329, 116.2, -47.0)
        O = place(nxt, CA, C, 1.231, 120.5, 180.0)
        for name, xyz in (("N", N), ("CA", CA), ("C", C), ("O", O)):
            atoms.append((i + 1, name, xyz))
        prev_N, prev_CA, prev_C = N, CA, C
    lines = [f"ATOM  {k:5d}  {name:<3} ALA A{res:4d}    {x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00           "
             f"{name[0]}" for k, (res, name, (x, y, z)) in enumerate(atoms, start=1)]
    return "\n".join(lines) + "\nEND\n"


def test_secondary_structure_finds_a_helix(tmp_path):
    pdb = tmp_path / "helix.pdb"
    pdb.write_text(_helix_pdb(14))
    r = server.secondary_structure_over_trajectory(str(pdb), str(pdb))
    assert r["status"] == "success", r
    assert r["n_residues"] == 14 and r["mean_fractions"]["helix"] > 0.5, r["final"]


def test_secondary_structure_explains_short_chains(tmp_path):
    pdb = tmp_path / "ala1.pdb"
    pdb.write_text("ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00\n"
                   "ATOM      2  CA  ALA A   1       1.460   0.300   0.000  1.00  0.00\nEND\n")
    r = server.secondary_structure_over_trajectory(str(pdb), str(pdb))
    assert r["status"] == "error" and "shorter than 5 residues" in r["error"]


# --- GROMACS output, made with the local gmx from the gromacs-mcp example water box ---

GMX_EXAMPLE = Path(__file__).resolve().parent.parent.parent / "gromacs-mcp" / "examples" / "data"


@pytest.fixture(scope="module")
def gmx_run(tmp_path_factory):
    import shutil
    import subprocess
    gmx = shutil.which("gmx")
    if not gmx or not GMX_EXAMPLE.is_dir():
        pytest.skip("gmx or the gromacs-mcp example data is not available")
    d = tmp_path_factory.mktemp("gmx_run")
    for name in ("water.gro", "topol.top", "md.mdp"):
        shutil.copy2(GMX_EXAMPLE / name, d / name)
    mdp = (d / "md.mdp").read_text().replace("nsteps               = 500", "nsteps               = 200")
    (d / "md.mdp").write_text(mdp + "nstxout = 50\n")
    for cmd in (["grompp", "-f", "md.mdp", "-c", "water.gro", "-p", "topol.top", "-o", "md.tpr"],
                ["mdrun", "-deffnm", "md", "-ntmpi", "1", "-ntomp", "2"]):
        res = subprocess.run([gmx] + cmd, cwd=d, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if res.returncode != 0:
            pytest.skip(f"gmx {cmd[0]} failed: {res.stderr[-300:]}")
    return d


@pytest.mark.parametrize("trajectory", ["md.xtc", "md.trr"])
def test_gromacs_gro_with_trajectory(gmx_run, trajectory, tmp_path):
    r = server.rmsd_over_trajectory(str(gmx_run / "md.gro"), str(gmx_run / trajectory), selection="name OW")
    assert r["status"] == "success" and r["n_frames"] == 6, r  # the .gro frame plus 5 trajectory frames
    img = server.render_image(str(gmx_run / "md.gro"), str(gmx_run / trajectory), style="water_points",
                              output_file=str(tmp_path / "w.tga"), width=400, height=300)
    assert img["status"] == "success" and _distinct_colors(Path(img["output_file"])) > 20


def test_gromacs_tpr_gets_a_hint(gmx_run):
    r = server.rmsd_over_trajectory(str(gmx_run / "md.tpr"), str(gmx_run / "md.xtc"), selection="name OW")
    if r["status"] == "error":
        assert "use the .gro" in r["error"]


def test_water_hydrogen_bonds(gmx_run):
    r = server.hbonds_over_trajectory(str(gmx_run / "md.gro"), str(gmx_run / "md.xtc"), "water", "water")
    assert r["status"] == "success" and r["min"] > 20, r
