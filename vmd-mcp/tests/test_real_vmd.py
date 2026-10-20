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
