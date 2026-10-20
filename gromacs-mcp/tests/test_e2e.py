"""The water box demo end to end through MCP tools only: minimize, write an NPT mdp, grompp,
run as a background job, then analyze. Checks the physics lands in sensible ranges."""

import time

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.gmx]


def test_water_box_through_tools(mcp_call, example_data, tmp_path):
    r = mcp_call("check_environment")
    assert r["ok"], r["problems"]

    r = mcp_call("run_minimization", structure=str(example_data / "water.gro"),
                 topology=str(example_data / "topol.top"), output_dir=str(tmp_path / "min"), emtol=200.0)
    assert r["status"] == "success" and r["converged"], r

    r = mcp_call("create_mdp", stage="npt", output_file=str(tmp_path / "npt.mdp"), time_ps=30,
                 overrides={"nstxout-compressed": 250, "nstenergy": 100, "continuation": "no",
                            "gen-vel": "yes", "gen-temp": 300, "gen-seed": 7})
    assert r["status"] == "success", r

    r = mcp_call("run_grompp", mdp_file=str(tmp_path / "npt.mdp"), structure=r_min_gro(tmp_path),
                 topology=str(example_data / "topol.top"), output_tpr=str(tmp_path / "npt.tpr"))
    assert r["status"] == "success", r

    r = mcp_call("submit_md_simulation", input_file=str(tmp_path / "npt.tpr"), output_dir=str(tmp_path / "run"),
                 job_name="e2e water")
    assert r["status"] == "success"
    job = r["job_id"]
    seen_running = False
    for _ in range(600):
        s = mcp_call("get_job_status", job_id=job)
        seen_running |= s["job_status"] == "running"
        if s["job_status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.5)
    assert s["job_status"] == "completed", s
    assert s["progress"]["percent"] == 100.0 and s["progress"]["ns_per_day"] > 0

    res = mcp_call("get_job_result", job_id=job)
    assert res["status"] == "success" and any("md.xtc" in f for f in res["output_files"])

    run = tmp_path / "run"
    e = mcp_call("energy_summary", edr_file=str(run / "md.edr"), output_xvg=str(tmp_path / "e.xvg"))
    assert e["status"] == "success"
    eq = {k: v["equilibrated"]["mean"] for k, v in e["stats"].items()}
    assert 290 < eq["Temperature"] < 310
    assert 950 < eq["Density"] < 1040  # SPC/E is about 995 kg/m^3; short run, small box

    rdf = mcp_call("analyze_rdf", trajectory=str(run / "md.xtc"), structure=str(run / "md.tpr"),
                   reference="name OW", begin_ps=10, output_xvg=str(tmp_path / "rdf.xvg"))
    assert rdf["status"] == "success"
    assert 0.27 <= rdf["first_peak_r_nm"] <= 0.285 and 2.5 < rdf["first_peak_g"] < 4.0
    assert 3.5 < rdf["coordination_number"] < 5.5

    msd = mcp_call("analyze_msd", trajectory=str(run / "md.xtc"), structure=str(run / "md.tpr"),
                   selection="name OW", begin_ps=5, trestart_ps=1, output_xvg=str(tmp_path / "msd.xvg"))
    assert msd["status"] == "success"
    assert 1.0 < msd["diffusion_coefficient"] < 5.0

    rep = mcp_call("trajectory_report", trajectory=str(run / "md.xtc"), structure=str(run / "md.tpr"),
                   energy_file=str(run / "md.edr"))
    assert rep["status"] == "success" and rep["n_frames"] == 61 and rep["non_finite"] == 0

    log = mcp_call("get_job_log", job_id=job, tail=5)
    assert log["status"] == "success" and log["md_log_lines"]


def r_min_gro(tmp_path):
    return str(tmp_path / "min" / "em.gro")


def build_peptide_pdb(path, sequence="ALA ALA ALA"):
    """Write an extended peptide (heavy atoms only) from ideal internal coordinates."""
    import math

    def place(a, b, c, bond, angle, torsion):
        # NeRF: put d so that |cd| = bond, angle(b,c,d) = angle, dihedral(a,b,c,d) = torsion
        ang, tor = math.radians(angle), math.radians(torsion)
        bc = [c[i] - b[i] for i in range(3)]
        n = math.sqrt(sum(x * x for x in bc)); bc = [x / n for x in bc]
        ab = [b[i] - a[i] for i in range(3)]
        nv = [ab[1] * bc[2] - ab[2] * bc[1], ab[2] * bc[0] - ab[0] * bc[2], ab[0] * bc[1] - ab[1] * bc[0]]
        nn = math.sqrt(sum(x * x for x in nv)); nv = [x / nn for x in nv]
        m2 = [nv[1] * bc[2] - nv[2] * bc[1], nv[2] * bc[0] - nv[0] * bc[2], nv[0] * bc[1] - nv[1] * bc[0]]
        d2 = [-bond * math.cos(ang), bond * math.sin(ang) * math.cos(tor), bond * math.sin(ang) * math.sin(tor)]
        return [c[i] + d2[0] * bc[i] + d2[1] * m2[i] + d2[2] * nv[i] for i in range(3)]

    residues = sequence.split()
    n, ca = [0.0, 1.0, 0.0], [1.458, 1.0, 0.0]
    c = place([0.0, 0.0, 0.0], n, ca, 1.525, 111.0, -120.0)
    atoms = []
    for i, res in enumerate(residues):
        o = place(n, ca, c, 1.231, 120.5, 300.0 if i % 2 else 120.0 + 180.0)
        cb = place(c, n, ca, 1.53, 110.5, -122.5)
        atoms += [("N", n), ("CA", ca), ("C", c), ("O", o), ("CB", cb)]
        if i + 1 < len(residues):
            n_next = place(n, ca, c, 1.329, 116.2, 120.0)
            ca_next = place(ca, c, n_next, 1.458, 121.7, 180.0)
            c_next = place(c, n_next, ca_next, 1.525, 111.0, -120.0)
            n, ca, c = n_next, ca_next, c_next
    lines, serial = [], 1
    for idx, (name, xyz) in enumerate(atoms):
        resnum = idx // 5 + 1
        lines.append(f"ATOM  {serial:5d}  {name:<3s} {residues[resnum - 1]:>3s} A{resnum:4d}    "
                     f"{xyz[0]:8.3f}{xyz[1]:8.3f}{xyz[2]:8.3f}  1.00  0.00           {name[0]}")
        serial += 1
    path.write_text("\n".join(lines) + "\nTER\nEND\n")
    return path


def test_protein_pipeline_with_position_restraints(mcp_call, tmp_path):
    """pdb2gmx path: position restraints during NVT and NPT. NPT with restraints used to fail grompp
    (pressure coupling with absolute restraints needs refcoord-scaling)."""
    pdb = build_peptide_pdb(tmp_path / "ala3.pdb")
    r = mcp_call("run_full_md_pipeline", structure=str(pdb), output_dir=str(tmp_path / "prep"),
                 force_field="oplsaa", water_model="spce", box_distance_nm=0.8, salt_concentration_molar=0.0,
                 em_steps=500, nvt_ps=2, npt_ps=2, production_ps=4)
    assert r["status"] == "success", (r.get("error"), r.get("step_results", [])[-1:])
    names = [s["name"] for s in r["step_results"]]
    assert names[0] == "pdb2gmx" and "npt" in names
    assert not any("refcoord" in w.lower() for w in r["warnings"])
    assert "define" in (tmp_path / "prep" / "npt.mdp").read_text()
    assert r["final_tpr"].endswith("md.tpr")
