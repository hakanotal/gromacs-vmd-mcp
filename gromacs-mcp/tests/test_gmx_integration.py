"""Tools against a real GROMACS, using the generated example data (a 2.2 nm SPC/E water box)."""

import json
import shutil
import sys
from pathlib import Path

import pytest

import server
from gromacs_command import run_gromacs_command
from gromacs_workflow import run_gromacs_workflow
from lib.gromacs_utils import parse_check_output, parse_mdlog_progress, parse_xvg
from md_simulation import extend_tpr, run_md_simulation
from tools import analysis, prep

pytestmark = pytest.mark.gmx


def set_mdp(text: str, **values) -> str:
    """Set options in .mdp text regardless of how the keys are padded."""
    import re
    for key, value in values.items():
        key = key.replace("_", "-")
        text, n = re.subn(rf"^{re.escape(key)}\s*=.*$", f"{key} = {value}", text, flags=re.M)
        if n == 0:
            text += f"{key} = {value}\n"
    return text


@pytest.fixture(scope="module")
def short_run(example_data, tmp_path_factory):
    """A 10 ps run (5000 steps) of the example system: 101 trajectory and energy frames."""
    out = tmp_path_factory.mktemp("short_run")
    res = run_md_simulation(example_data / "sample.tpr", out, nsteps=5000)
    assert res["result"], res
    return out


@pytest.fixture
def work(tmp_path, example_data):
    for name in ("water.gro", "topol.top", "em.mdp", "md.mdp"):
        shutil.copy2(example_data / name, tmp_path / name)
    return tmp_path


class TestAnalyzeTpr:
    def test_values_are_right(self, example_data):
        r = server.analyze_tpr(str(example_data / "sample.tpr"))
        assert r["status"] == "success"
        info = r["tpr_info"]
        assert info["natoms"] > 600 and info["nmolecules"] * 3 == info["natoms"]
        assert info["dt"] == 0.002 and info["nsteps"] == 500
        assert info["temperature"] == 300.0
        assert info["integrator"] == "md" and info["coulombtype"] == "PME"

    def test_summary_is_small_and_raw_dump_goes_to_a_file(self, example_data, tmp_path):
        r = server.analyze_tpr(str(example_data / "sample.tpr"), include_raw=True, output_file=str(tmp_path / "a.json"),
                               output_format="json")
        assert len(str(r)) < 5000
        dump = Path(r["raw_dump_file"])
        assert dump.exists() and dump.stat().st_size > 20_000
        assert "inputrec" in dump.read_text()
        assert "inputrec:" not in str(r)

    def test_not_a_tpr(self, tmp_path):
        bad = tmp_path / "bad.tpr"
        bad.write_bytes(b"not a tpr")
        r = server.analyze_tpr(str(bad))
        assert r["status"] == "error" and r["error"]

    def test_saved_files_are_listed_as_absolute_paths(self, example_data, tmp_path):
        r = server.analyze_tpr(str(example_data / "sample.tpr"), output_file=str(tmp_path / "s.txt"), include_raw=True)
        assert r["output_files"] == [str(tmp_path / "s.txt"), r["raw_dump_file"]]

    def test_batch_reports_a_missing_file_and_analyzes_the_rest(self, example_data, tmp_path):
        # TODO 13.6: one missing path used to refuse the whole batch
        import time
        missing = str(tmp_path / "gone.tpr")
        r = server.submit_batch_analysis([str(example_data / "sample.tpr"), missing], output_dir=str(tmp_path / "b"))
        assert r["status"] == "success" and any("gone.tpr" in w for w in r["warnings"])
        for _ in range(200):
            if server.job_manager.get_job_status(r["job_id"])["status"] in ("completed", "failed"):
                break
            time.sleep(0.1)
        res = server.get_job_result(r["job_id"])
        assert res["status"] == "success", res
        batch = res["job_result"]
        assert (batch["succeeded"], batch["failed"]) == (1, 1)
        assert batch["results"][1]["input"] == missing and "not found" in batch["results"][1]["error"].lower()
        assert res["output_files"] == [str(tmp_path / "b" / "sample.json")]
        assert any("1 of 2 files failed" in w for w in res["warnings"])

    def test_batch_refuses_bad_paths_up_front(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GMX_MCP_WORKDIR", str(tmp_path))
        assert "outside" in server.submit_batch_analysis(["/etc/x.tpr"])["error"]
        assert ".tpr" in server.submit_batch_analysis([str(tmp_path / "a.gro")])["error"]
        assert "None of the files exist" in server.submit_batch_analysis([str(tmp_path / "a.tpr")])["error"]


class TestRunCommand:
    def test_energy_with_selections_writes_xvg(self, short_run, tmp_path):
        r = run_gromacs_command("energy", {"f": short_run / "md.edr"}, {"o": tmp_path / "e.xvg"},
                                selections=["Potential", "Temperature"])
        assert r["result"] is True
        x = parse_xvg((tmp_path / "e.xvg").read_text())
        assert x["legends"] == ["Potential", "Temperature"] and x["n_rows"] == 101

    def test_energy_without_selections_lists_the_terms(self, short_run):
        r = run_gromacs_command("energy", {"f": short_run / "md.edr"})
        assert r["result"] is False
        assert "Potential" in r["stdout"] + r["stderr"]
        assert any("selections" in w for w in r["warnings"])

    def test_check_returns_the_report(self, short_run):
        r = run_gromacs_command("check", {"f": short_run / "md.xtc"})
        info = parse_check_output(r["stderr"])
        assert r["result"] and info["n_frames"] == 101 and info["natoms"] > 600

    def test_dump_to_file(self, example_data, tmp_path):
        r = run_gromacs_command("dump", {"s": example_data / "sample.tpr"}, {"o": tmp_path / "d.txt"})
        assert r["result"] and "inputrec" in (tmp_path / "d.txt").read_text()

    def test_real_error_message_not_unknown(self, short_run):
        r = run_gromacs_command("check", {"f": short_run / "md.xtc"}, parameters=["--bogus"])
        assert r["result"] is False and "Unknown command-line option" in r["error"]

    def test_trjconv_exec_never_runs(self, short_run, tmp_path):
        # with the real gmx, -exec would run this for every frame written with -sep
        marker = tmp_path / "PWNED"
        for spelling in ("-exec", "--exec"):
            r = run_gromacs_command("trjconv", {"f": short_run / "md.xtc", "s": short_run / "md.tpr"},
                                    {"o": tmp_path / "frame.gro"}, ["-sep", "-e", "0.2", spelling, f"touch {marker}"],
                                    selections=["0"])
            assert r["result"] is False and "not allowed" in r["error"]
        assert not marker.exists()

    def test_file_option_value_without_a_suffix_is_checked(self, short_run, tmp_path):
        # no slash and no suffix, but it follows a file option (-f2), so gmx would treat it as a file name
        r = run_gromacs_command("check", {"f": short_run / "md.xtc"}, parameters=["-f2", "x$(touch PWNED)"])
        assert r["result"] is False and "plain file name" in r["error"]
        assert not (short_run / "PWNED").exists()

    def test_trjcat_joins_several_inputs(self, short_run, tmp_path):
        r = run_gromacs_command("trjcat", {"f": [short_run / "md.xtc", short_run / "md.xtc"]},
                                {"o": tmp_path / "joined.xtc"}, ["-cat"])
        assert r["result"] is True, r.get("error")
        assert (tmp_path / "joined.xtc").stat().st_size > 1.9 * (short_run / "md.xtc").stat().st_size

    def test_unknown_option_gets_suggestions(self, short_run, tmp_path):
        # TODO 12.3 (partly): name the closest valid options instead of only the rejected one
        r = run_gromacs_command("rdf", {"f": short_run / "md.xtc", "s": short_run / "md.tpr"},
                                {"o": tmp_path / "r.xvg"}, ["-refx", "name OW", "-sel", "name OW"])
        assert r["result"] is False
        assert "-refx: did you mean -ref" in r["error"] and "Valid options for gmx rdf" in r["error"]
        assert "sel" in r["valid_options"] and "rmax" in r["valid_options"]

    def test_selection_expression_rdf(self, short_run, tmp_path):
        r = run_gromacs_command("rdf", {"f": short_run / "md.xtc", "s": short_run / "md.tpr"}, {"o": tmp_path / "rdf.xvg"},
                                ["-ref", "name OW", "-sel", "name OW"])
        assert r["result"] and (tmp_path / "rdf.xvg").exists()

    def test_injection_attempt_runs_nothing(self, short_run, tmp_path):
        marker = tmp_path / "PWNED"
        r = run_gromacs_command("check", {"f": short_run / "md.xtc"}, parameters=[f"; touch {marker}"])
        assert r["result"] is False and not marker.exists()


class TestGrompp:
    def test_grompp_ok(self, work):
        r = prep.run_grompp(str(work / "md.mdp"), str(work / "water.gro"), str(work / "topol.top"),
                            output_tpr=str(work / "o.tpr"))
        assert r["status"] == "success" and Path(r["output_tpr"]).exists()
        assert r["grompp_errors"] == [] and r["output_files"] == [r["output_tpr"]]

    def test_grompp_error_is_specific(self, work):
        (work / "bad.mdp").write_text(set_mdp((work / "em.mdp").read_text(), rvdw=1.6, rcoulomb=1.6))
        r = prep.run_grompp(str(work / "bad.mdp"), str(work / "water.gro"), str(work / "topol.top"),
                            output_tpr=str(work / "o.tpr"))
        assert r["status"] == "error"
        assert "cut-off" in r["error"] and r["grompp_errors"]

    @pytest.fixture
    def charged(self, work):
        """A system with net charge +1: grompp warns and stops unless maxwarn allows it."""
        from lib.gromacs_utils import run_command_full, find_gromacs_command
        gmx = find_gromacs_command()
        top = work / "topol.top"
        top.write_text(top.read_text().replace("[ system ]", '#include "oplsaa.ff/ions.itp"\n\n[ system ]'))
        run_command_full(gmx + ["grompp", "-f", "em.mdp", "-c", "water.gro", "-p", "topol.top", "-o", "i.tpr"], working_dir=work)
        res = run_command_full(gmx + ["genion", "-s", "i.tpr", "-o", "charged.gro", "-p", "topol.top",
                                      "-pname", "NA", "-np", "1", "-nn", "0"], working_dir=work, stdin_text="SOL\n")
        assert res["success"], res["stderr"][-400:]
        return work

    def test_warning_stops_grompp_and_is_reported(self, charged):
        r = prep.run_grompp(str(charged / "em.mdp"), str(charged / "charged.gro"), str(charged / "topol.top"),
                            output_tpr=str(charged / "c.tpr"))
        assert r["status"] == "error"
        assert r["grompp_warnings"] and any("charge" in w.lower() for w in r["grompp_warnings"])
        assert "maxwarn" in r["error"] and r["output_tpr"] is None

    def test_maxwarn_is_explicit_and_noted(self, charged):
        r = prep.run_grompp(str(charged / "em.mdp"), str(charged / "charged.gro"), str(charged / "topol.top"),
                            output_tpr=str(charged / "c.tpr"), maxwarn=1)
        assert r["status"] == "success"
        assert any("maxwarn=1 allowed" in w for w in r["warnings"])

    def test_negative_maxwarn_rejected(self, work):
        r = prep.run_grompp(str(work / "md.mdp"), str(work / "water.gro"), str(work / "topol.top"), maxwarn=-1)
        assert r["status"] == "error"


class TestValidateMdp:
    def test_valid_mdp_with_grompp_check(self, work):
        r = prep.validate_mdp(mdp_file=str(work / "md.mdp"), structure=str(work / "water.gro"),
                              topology=str(work / "topol.top"))
        assert r["status"] == "success" and r["grompp_ok"] is True

    def test_grompp_failure_makes_validation_fail(self, work):
        text = set_mdp((work / "md.mdp").read_text(), rvdw=1.6, rcoulomb=1.6)
        r = prep.validate_mdp(mdp_text=text, structure=str(work / "water.gro"), topology=str(work / "topol.top"))
        assert r["status"] == "error" and r["grompp_ok"] is False and r["grompp_errors"]


class TestTemplatesAgainstGrompp:
    """Every template must be accepted by grompp, not only by the lint rules."""

    @pytest.mark.parametrize("stage,overrides", [
        ("em", {}), ("ions", {}), ("nvt", {}), ("npt", {}), ("nve", {}),
        ("free_energy", {"couple-moltype": "SOL"}),
        ("npt", {"tc-grps": "SOL"}),
    ])
    def test_template_passes_grompp(self, work, stage, overrides):
        r = prep.create_mdp(stage, output_file=str(work / f"{stage}.mdp"), temperature=310, overrides=overrides)
        assert r["status"] == "success", r.get("issues")
        g = prep.run_grompp(str(work / f"{stage}.mdp"), str(work / "water.gro"), str(work / "topol.top"),
                            output_tpr=str(work / f"{stage}.tpr"))
        assert g["status"] == "success", (g.get("error"), g.get("grompp_warnings"))
        assert g["grompp_warnings"] == []


class TestMinimization:
    def test_converges_and_reports(self, work):
        r = prep.run_minimization(str(work / "water.gro"), str(work / "topol.top"), output_dir=str(work / "min"),
                                  emtol=200.0, nsteps=2000)
        assert r["status"] == "success", r
        assert r["converged"] is True and r["steps"] > 0
        assert r["max_force"] < 200.0 and r["potential_energy_kj_mol"] < 0
        assert (work / "min" / "em.gro").exists() and r["output_gro"].endswith("em.gro")

    def test_not_converging_is_a_warning(self, work):
        r = prep.run_minimization(str(work / "water.gro"), str(work / "topol.top"), output_dir=str(work / "min2"),
                                  emtol=0.001, nsteps=3)
        assert r["status"] == "success" and r["converged"] is False
        assert any("did not reach" in w for w in r["warnings"])

    def test_grompp_failure_is_reported(self, work):
        bad = work / "bad.mdp"
        bad.write_text("integrator = steep\nrvdw = 1.6\nrcoulomb = 1.6\ncutoff-scheme = Verlet\n")
        r = prep.run_minimization(str(work / "water.gro"), str(work / "topol.top"), output_dir=str(work / "min3"),
                                  mdp_file=str(bad))
        assert r["status"] == "error" and "cut-off" in r["error"]


class TestMdRun:
    def test_uses_tpr_steps_when_nsteps_not_given(self, example_data, tmp_path):
        res = run_md_simulation(example_data / "sample.tpr", tmp_path / "r")
        assert res["result"], res
        p = parse_mdlog_progress((tmp_path / "r" / "md.log").read_text())
        assert p["nsteps"] == 500 and p["finished"]
        assert res["metadata"]["ntmpi"] == 1
        assert str(tmp_path / "r" / "md.log") in res["output_files"]
        assert all(Path(f).is_absolute() and res["file_sizes"][f] > 0 for f in res["output_files"])

    @pytest.mark.parametrize("limit", [{"maxh": 0.00002}, {"timeout": 3}])
    def test_run_stopped_by_a_limit_is_stopped_not_failed(self, example_data, tmp_path, limit):
        # maxh or the wall clock limit: mdrun checkpoints and exits cleanly before nsteps (TODO 12.27)
        res = run_md_simulation(example_data / "sample.tpr", tmp_path / "r", nsteps=5_000_000, ntomp=2, **limit)
        assert res["result"] is True and res["stopped"] is True and res["complete"] is False, res
        assert ("maxh" if "maxh" in limit else "time limit") in res["stop_reason"]
        assert (tmp_path / "r" / "md.cpt").exists()
        assert any("resume continues" in w for w in res["warnings"])

    def test_relative_input_path_with_nsteps(self, example_data, tmp_path, monkeypatch):
        """convert-tpr runs inside the output folder, so a relative input must be resolved first."""
        monkeypatch.chdir(example_data.parent)
        res = run_md_simulation(Path("data") / "sample.tpr", tmp_path / "rel", nsteps=20)
        assert res["result"], res.get("error")

    def test_failure_returns_gromacs_error(self, tmp_path):
        bad = tmp_path / "bad.tpr"
        bad.write_bytes(b"garbage")
        res = run_md_simulation(bad, tmp_path / "r")
        assert res["result"] is False and res["error"] and res["mdrun_output_tail"]

    def test_extend_then_resume_continues_from_checkpoint(self, example_data, tmp_path):
        out = tmp_path / "r"
        assert run_md_simulation(example_data / "sample.tpr", out)["result"]
        longer = tmp_path / "longer.tpr"
        assert extend_tpr(example_data / "sample.tpr", longer, extend_ps=1.0)["result"]
        info = server.analyze_tpr(str(longer))["tpr_info"]
        assert info["nsteps"] == 1000
        res = run_md_simulation(longer, out, resume=True)
        assert res["result"], res
        p = parse_mdlog_progress((out / "md.log").read_text())
        assert p["finished"] and p["time_ps"] == pytest.approx(2.0, abs=0.01)
        assert res["complete"] and not res["stopped"] and res["progress"]["nsteps"] == 1000

    def test_extended_run_stopped_by_maxh_is_not_complete(self, example_data, tmp_path):
        # md.log keeps the first run's nsteps (500) after extend and resume; a run stopped past step 500
        # used to count as complete (TODO 14.1). The wrapper and the job manager read nsteps from the .tpr
        from lib.gromacs_utils import read_tpr_nsteps
        out = tmp_path / "r"
        assert run_md_simulation(example_data / "sample.tpr", out, ntomp=2)["result"]
        longer = tmp_path / "longer.tpr"
        assert extend_tpr(example_data / "sample.tpr", longer, extend_ps=10000.0)["result"]
        assert read_tpr_nsteps(longer) == 5_000_500
        res = run_md_simulation(longer, out, resume=True, maxh=0.00002, ntomp=2)
        assert res["stopped"] is True and res["complete"] is False, res
        assert res["progress"]["nsteps"] == 5_000_500 and res["progress"]["step"] > 500
        assert parse_mdlog_progress((out / "md.log").read_text())["nsteps"] == 500  # what the log alone says
        meta = {"script": "scripts/md_simulation.py", "args": {"input": str(longer)}, "output_dir": str(out)}
        progress = server.job_manager._progress(meta)
        assert progress["nsteps"] == 5_000_500 and progress["percent"] < 1 and not progress["complete"]

    def test_nsteps_zero_is_respected(self, example_data, tmp_path):
        from lib.gromacs_utils import run_command_full
        script = Path(__file__).parent.parent / "scripts" / "md_simulation.py"
        res = run_command_full([sys.executable, str(script), "--input", str(example_data / "sample.tpr"),
                                "--output-dir", str(tmp_path / "zero"), "--nsteps", "0",
                                "--result-json", str(tmp_path / "r.json")], timeout=120)
        assert res["success"], res["stdout"] + res["stderr"]
        assert parse_mdlog_progress((tmp_path / "zero" / "md.log").read_text())["nsteps"] == 0
        result = json.loads((tmp_path / "r.json").read_text())
        assert result["complete"] is True and result["stopped"] is False  # zero steps asked, zero done

    def test_minimization_as_a_job_is_complete_when_converged(self, work):
        from lib.gromacs_utils import find_gromacs_command, run_command_full
        assert run_command_full(find_gromacs_command() + ["grompp", "-f", "em.mdp", "-c", "water.gro", "-p", "topol.top",
                                                          "-o", "em.tpr"], working_dir=work)["success"]
        res = run_md_simulation(work / "em.tpr", work / "em", ntomp=2)
        assert res["result"] and res["complete"] is True and res["stopped"] is False, res

    def test_resume_without_checkpoint(self, example_data, tmp_path):
        res = run_md_simulation(example_data / "sample.tpr", tmp_path / "empty", resume=True)
        assert res["result"] is False and "md.cpt" in res["error"]

    def test_extend_requires_exactly_one_option(self, example_data, tmp_path):
        assert extend_tpr(example_data / "sample.tpr", tmp_path / "x.tpr")["result"] is False
        assert extend_tpr(example_data / "sample.tpr", tmp_path / "x.tpr", extend_ps=1, until_ps=2)["result"] is False
        assert prep.extend_simulation(str(example_data / "sample.tpr"), str(tmp_path / "x.tpr"), until_ps=3)["status"] == "success"


class TestSandboxedDefaults:
    def test_tools_work_with_default_outputs_inside_a_sandbox(self, tmp_path, monkeypatch, short_run, example_data):
        box = tmp_path / "sandbox"
        box.mkdir()
        for name in ("md.edr", "md.xtc", "md.tpr"):
            shutil.copy2(short_run / name, box / name)
        shutil.copy2(example_data / "sample.tpr", box / "sample.tpr")
        monkeypatch.delenv("GMX_MCP_HOME", raising=False)
        monkeypatch.setenv("GMX_MCP_WORKDIR", str(box))
        r = server.analyze_tpr(str(box / "sample.tpr"), include_raw=True)
        assert r["status"] == "success" and box.resolve() in Path(r["raw_dump_file"]).resolve().parents
        e = analysis.energy_summary(str(box / "md.edr"))
        assert e["status"] == "success" and box.resolve() in Path(e["output_xvg"]).resolve().parents
        rep = analysis.trajectory_report(str(box / "md.xtc"), str(box / "md.tpr"), str(box / "md.edr"))
        assert rep["status"] == "success" and rep["box"] is not None
        assert not [i for i in rep["issues"] if "Box analysis skipped" in i]


class TestIncludesWithRealForceField:
    def test_workflow_with_a_multi_file_topology_in_a_sandbox(self, work, tmp_path, monkeypatch):
        """Sibling .itp files are staged before the include check, so they are found and accepted."""
        top = (work / "topol.top").read_text().replace('#include "oplsaa.ff/spce.itp"', '#include "mywater.itp"')
        (work / "topol.top").write_text(top)
        (work / "mywater.itp").write_text('#include "oplsaa.ff/spce.itp"\n')
        monkeypatch.delenv("GMX_MCP_HOME", raising=False)
        monkeypatch.setenv("GMX_MCP_WORKDIR", str(work))
        r = run_gromacs_workflow("full_md", {"structure": work / "water.gro", "topology": work / "topol.top"},
                                 work / "wf", set_box=False, solvate=False, ions=False, stages=["em"], em_steps=10)
        assert r["result"], r.get("error")

    def test_force_field_includes_are_allowed_in_a_sandbox(self, work, monkeypatch):
        monkeypatch.delenv("GMX_MCP_HOME", raising=False)
        monkeypatch.setenv("GMX_MCP_WORKDIR", str(work))
        r = prep.run_grompp(str(work / "md.mdp"), str(work / "water.gro"), str(work / "topol.top"),
                            output_tpr=str(work / "o.tpr"))
        assert r["status"] == "success", r.get("error")

    def test_comma_in_batch_paths_cannot_escape(self, work, example_data, monkeypatch, tmp_path):
        import time
        outside = tmp_path / "outside.tpr"
        shutil.copy2(example_data / "sample.tpr", outside)
        tricky = work / "a,"
        (tricky / str(outside.parent).lstrip("/")).mkdir(parents=True)
        shutil.copy2(example_data / "sample.tpr", tricky / str(outside.parent).lstrip("/") / "outside.tpr")
        monkeypatch.delenv("GMX_MCP_HOME", raising=False)
        monkeypatch.setenv("GMX_MCP_WORKDIR", str(work))
        path = f"{work}/a,{outside}"
        r = server.submit_batch_analysis([path], output_dir=str(work / "batch"))
        assert r["status"] == "success", r
        for _ in range(100):
            s = server.job_manager.get_job_status(r["job_id"])
            if s["status"] in ("completed", "failed"):
                break
            time.sleep(0.1)
        res = server.job_manager.get_job_result(r["job_id"])
        analysed = [e["input"] for e in res["result"]["results"]]
        assert analysed == [str(Path(path).resolve())]


class TestAnalysisTools:
    def test_default_outputs_are_unique(self, short_run):
        a = analysis.energy_summary(str(short_run / "md.edr"), terms=["Potential"])
        b = analysis.energy_summary(str(short_run / "md.edr"), terms=["Potential"])
        assert a["output_xvg"] != b["output_xvg"]
        assert not list(Path(a["output_xvg"]).parent.glob("#*#"))

    def test_energy_summary(self, short_run, tmp_path):
        r = analysis.energy_summary(str(short_run / "md.edr"), output_xvg=str(tmp_path / "e.xvg"))
        assert r["status"] == "success"
        assert set(r["terms_used"]) | set(r["terms_missing"]) == {"Potential", "Temperature", "Pressure", "Density", "Volume"}
        t = r["stats"]["Temperature"]
        assert 250 < t["full"]["mean"] < 350 and t["equilibrated"]["n"] <= t["full"]["n"]
        full = t["full"]
        assert full["statistical_inefficiency"] >= 1 and 0 < full["n_independent"] <= full["n"]
        assert full["sem"] >= full["std"] / full["n"] ** 0.5
        assert r["gmx_table"]["Temperature"]["unit"] == "K"
        assert r["n_frames"] == 101 and r["time_range_ps"] == [0.0, 10.0]

    def test_energy_summary_short_series(self, short_run, tmp_path):
        # 21 frames: detection runs (it used to need 40); 11 frames: reported as unknown, never as 0 ps
        r = analysis.energy_summary(str(short_run / "md.edr"), begin_ps=8, output_xvg=str(tmp_path / "a.xvg"))
        assert r["n_frames"] == 21 and r["equilibration_time_ps"] is not None
        assert "equilibrated" in r["stats"]["Potential"]
        r = analysis.energy_summary(str(short_run / "md.edr"), begin_ps=9, output_xvg=str(tmp_path / "b.xvg"))
        assert r["status"] == "success" and r["n_frames"] == 11
        assert r["equilibration_time_ps"] is None and "equilibrated" not in r["stats"]["Potential"]
        assert any("too few" in w for w in r["warnings"])

    def test_energy_summary_window_and_missing_terms(self, short_run, tmp_path):
        r = analysis.energy_summary(str(short_run / "md.edr"), terms=["Potential", "NoSuchTerm"], begin_ps=5,
                                    output_xvg=str(tmp_path / "e.xvg"))
        assert r["status"] == "success" and r["terms_missing"] == ["NoSuchTerm"] and r["n_frames"] == 51
        assert any("NoSuchTerm" in w for w in r["warnings"])

    def test_energy_summary_prefix_matching(self, short_run, tmp_path):
        r = analysis.energy_summary(str(short_run / "md.edr"), terms=["potent"], output_xvg=str(tmp_path / "e.xvg"))
        assert r["terms_used"] == ["Potential"]

    def test_energy_summary_no_valid_terms(self, short_run, tmp_path):
        r = analysis.energy_summary(str(short_run / "md.edr"), terms=["Nope"], output_xvg=str(tmp_path / "e.xvg"))
        assert r["status"] == "error" and "available_terms" in r

    def test_rdf(self, short_run, tmp_path):
        r = analysis.analyze_rdf(str(short_run / "md.xtc"), str(short_run / "md.tpr"), reference="name OW",
                                 begin_ps=2, output_xvg=str(tmp_path / "rdf.xvg"))
        assert r["status"] == "success", r
        assert 0.26 < r["first_peak_r_nm"] < 0.30 and r["first_peak_g"] > 2.0
        assert r["first_minimum_r_nm"] > r["first_peak_r_nm"]
        assert 3.0 < r["coordination_number"] < 6.0
        assert len(r["output_files"]) == 2

    def test_rdf_bad_selection(self, short_run, tmp_path):
        r = analysis.analyze_rdf(str(short_run / "md.xtc"), str(short_run / "md.tpr"), reference="name NOPE",
                                 output_xvg=str(tmp_path / "rdf.xvg"))
        assert r["status"] == "error" and r["error"]

    def test_msd(self, short_run, tmp_path):
        r = analysis.analyze_msd(str(short_run / "md.xtc"), str(short_run / "md.tpr"), selection="name OW", begin_ps=1,
                                 trestart_ps=1, output_xvg=str(tmp_path / "msd.xvg"), temperature_k=300,
                                 viscosity_mpas=0.7, box_length_nm=2.2)
        assert r["status"] == "success", r
        assert 0.5 < r["diffusion_coefficient"] < 6.0 and r["unit"].startswith("1e-5")
        assert r["diffusion_corrected"] > r["diffusion_coefficient"]
        assert r["finite_size_correction"] == pytest.approx(0.36, abs=0.05)

    def test_msd_partial_correction_inputs_warn(self, short_run, tmp_path):
        r = analysis.analyze_msd(str(short_run / "md.xtc"), str(short_run / "md.tpr"), selection="name OW", begin_ps=1,
                                 trestart_ps=1, output_xvg=str(tmp_path / "m.xvg"), temperature_k=300)
        assert "diffusion_corrected" not in r and any("together" in w for w in r["warnings"])

    def test_msd_input_error_is_explained(self, short_run, tmp_path):
        r = analysis.analyze_msd(str(short_run / "md.xtc"), str(short_run / "md.tpr"), selection="name OW",
                                 trestart_ps=0.01, output_xvg=str(tmp_path / "m.xvg"))
        assert r["status"] == "error" and "trestart" in r["error"]

    def test_trajectory_report(self, short_run):
        from utils import get_results_dir
        results = get_results_dir()
        before = sorted(p for p in results.rglob("*") if p.is_file()) if results.exists() else []
        r = analysis.trajectory_report(str(short_run / "md.xtc"), str(short_run / "md.tpr"), str(short_run / "md.edr"))
        # its intermediate files (box and energy series) are temporary and leave nothing behind
        assert sorted(p for p in results.rglob("*") if p.is_file()) == before
        assert r["status"] == "success"
        assert r["n_frames"] == 101 and r["last_time_ps"] == 10.0 and r["natoms"] > 600
        assert set(r["box"]["change_percent"]) == {"x", "y", "z"}
        assert r["non_finite"] == 0 and r["issues"] == []
        assert "Potential" in r["energy"]

    def test_read_xvg_tool(self, short_run, tmp_path):
        out = tmp_path / "e.xvg"
        run_gromacs_command("energy", {"f": short_run / "md.edr"}, {"o": out}, selections=["Temperature"])
        r = analysis.read_xvg(str(out), max_points=10)
        assert r["status"] == "success" and r["n_rows"] == 101 and len(r["data"]) <= 10
        assert analysis.read_xvg(str(out), begin=5)["n_rows"] == 51
        assert analysis.read_xvg(str(out), begin=500)["status"] == "error"

    def test_trjconv_presets(self, short_run, tmp_path):
        for preset in ("whole", "nojump", "mol"):
            out = tmp_path / f"{preset}.xtc"
            r = analysis.trjconv_preset(str(short_run / "md.xtc"), str(short_run / "md.tpr"), str(out), preset)
            assert r["status"] == "success" and out.exists(), (preset, r)
        r = analysis.trjconv_preset(str(short_run / "md.xtc"), str(short_run / "md.tpr"), str(tmp_path / "w.xtc"),
                                    "window", begin_ps=2, end_ps=4)
        assert r["status"] == "success"
        assert run_gromacs_command("check", {"f": tmp_path / "w.xtc"})["stderr"].count("Last frame") == 1
        pdb = tmp_path / "f.pdb"
        assert analysis.trjconv_preset(str(short_run / "md.xtc"), str(short_run / "md.tpr"), str(pdb), "pdb",
                                       dump_ps=5)["status"] == "success"
        assert pdb.read_text().startswith(("TITLE", "REMARK", "CRYST1", "MODEL"))

    def test_trjconv_preset_validation(self, short_run, tmp_path):
        args = (str(short_run / "md.xtc"), str(short_run / "md.tpr"))
        assert "Unknown preset" in analysis.trjconv_preset(*args, str(tmp_path / "x.xtc"), "spin")["error"]
        assert ".pdb" in analysis.trjconv_preset(*args, str(tmp_path / "x.xtc"), "pdb")["error"]

    def test_plot_xvg(self, short_run, tmp_path):
        pytest.importorskip("matplotlib")
        out = tmp_path / "e.xvg"
        run_gromacs_command("energy", {"f": short_run / "md.edr"}, {"o": out}, selections=["Temperature", "Potential"])
        r = analysis.plot_xvg(str(out), str(tmp_path / "p.png"))
        assert r["status"] == "success" and (tmp_path / "p.png").stat().st_size > 1000
        assert analysis.plot_xvg(str(out), columns=[9])["status"] == "error"

    def test_plot_xvg_without_matplotlib_is_a_clear_error(self, short_run, tmp_path, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def no_mpl(name, *a, **k):
            if name.startswith("matplotlib"):
                raise ImportError("no matplotlib")
            return real_import(name, *a, **k)
        monkeypatch.setattr(builtins, "__import__", no_mpl)
        r = analysis.plot_xvg(str(tmp_path / "x.xvg"))
        assert r["status"] == "error" and "pip install matplotlib" in r["error"]


class TestWorkflows:
    def test_full_md_end_to_end_and_resume(self, work, tmp_path):
        opts = dict(set_box=False, solvate=False, em_steps=200, nvt_ps=1, npt_ps=1, production_ps=2)
        out = tmp_path / "wf"
        r = run_gromacs_workflow("full_md", {"structure": work / "water.gro", "topology": work / "topol.top"}, out, **opts)
        assert r["result"] is True, r.get("error")
        assert r["completed_steps"] == 9 and r["skipped_steps"] == 0
        assert Path(r["final_tpr"]).exists() and Path(r["equilibrated_gro"]).exists()
        assert "ions.itp" in (out / "topol.top").read_text()
        final_top = Path(r["final_topology"])
        assert final_top.name == "ions.top" and "NA" in final_top.read_text()
        assert not list(out.glob("#*.top.*#"))  # genion edits a fresh copy; no backup of it is kept (TODO 13.10)
        assert "NA" not in (out / "topol.top").read_text().split("[ molecules ]")[1]
        # second run: everything already done
        r2 = run_gromacs_workflow("full_md", {"structure": work / "water.gro", "topology": work / "topol.top"}, out, **opts)
        assert r2["result"] and r2["skipped_steps"] == 9 and r2["completed_steps"] == 0
        # a changed input restarts the workflow
        gro = work / "water2.gro"
        gro.write_text((work / "water.gro").read_text())
        r3 = run_gromacs_workflow("full_md", {"structure": gro, "topology": work / "topol.top"}, out, **opts)
        assert r3["result"] and r3["completed_steps"] == 9

    def test_changed_option_reruns_the_affected_steps(self, work, tmp_path):
        inputs = {"structure": work / "water.gro", "topology": work / "topol.top"}
        base = dict(set_box=False, solvate=False, ions=False, stages=["em"], em_steps=30)
        out = tmp_path / "wf"
        assert run_gromacs_workflow("full_md", inputs, out, **base)["result"]
        r = run_gromacs_workflow("full_md", inputs, out, **{**base, "em_steps": 60})
        ran = {s["name"]: s["status"] for s in r["step_results"]}
        assert ran == {"em_grompp": "completed", "em": "completed"}
        assert server.analyze_tpr(str(out / "em.tpr"))["tpr_info"]["nsteps"] == 60

    def test_upstream_change_invalidates_downstream(self, work, tmp_path):
        inputs = {"structure": work / "water.gro", "topology": work / "topol.top"}
        base = dict(set_box=True, solvate=False, ions=True, salt_concentration=0.1, stages=["em"], em_steps=20,
                    box_distance=0.3)
        out = tmp_path / "wf"
        assert run_gromacs_workflow("full_md", inputs, out, **base)["result"]
        r = run_gromacs_workflow("full_md", inputs, out, **{**base, "box_distance": 0.6})
        assert r["result"], r.get("error")
        assert r["skipped_steps"] == 0, [(s["name"], s["status"]) for s in r["step_results"]]

    def test_repeated_fresh_runs_do_not_corrupt_the_topology(self, work, tmp_path):
        inputs = {"structure": work / "water.gro", "topology": work / "topol.top"}
        opts = dict(set_box=True, box_distance=0.5, solvate=True, ions=True, salt_concentration=0.1,
                    stages=["em"], em_steps=20)
        out = tmp_path / "wf"
        for _ in range(3):
            r = run_gromacs_workflow("full_md", inputs, out, resume=False, **opts)
            assert r["result"], r.get("error")
        molecules = (out / "solv.top").read_text().split("[ molecules ]")[1]
        assert len([l for l in molecules.splitlines() if l.strip().startswith("SOL")]) == 2  # input + solvate
        assert not list(out.glob("#*.top.*#"))  # solvate and genion edit fresh copies; no backups (TODO 13.10)
        r = run_gromacs_workflow("full_md", inputs, out, **opts)
        assert r["result"] and r["completed_steps"] == 0

    def test_failed_step_stops_and_reports(self, work, tmp_path):
        (work / "topol.top").write_text("this is not a topology")
        r = run_gromacs_workflow("full_md", {"structure": work / "water.gro", "topology": work / "topol.top"},
                                 tmp_path / "wf", set_box=False, solvate=False, ions=False, em_steps=10)
        assert r["result"] is False and "em_grompp" in r["error"]
        assert r["failed_steps"] == 1
        assert [s["status"] for s in r["step_results"]][-1] == "failed"
        assert r["step_results"][-1]["stderr_tail"]

    def test_stages_subset(self, work, tmp_path):
        r = run_gromacs_workflow("full_md", {"structure": work / "water.gro", "topology": work / "topol.top"},
                                 tmp_path / "wf", set_box=False, solvate=False, ions=False, em_steps=50,
                                 stages=["em"])
        assert r["result"] and (tmp_path / "wf" / "em.gro").exists() and not (tmp_path / "wf" / "nvt.tpr").exists()

    def test_simple_md_uses_given_nsteps_only_when_asked(self, work, tmp_path):
        inputs = {"structure": work / "water.gro", "topology": work / "topol.top", "mdp": work / "md.mdp"}
        r = run_gromacs_workflow("simple_md", inputs, tmp_path / "s1")
        assert r["result"], r.get("error")
        assert parse_mdlog_progress((tmp_path / "s1" / "md.log").read_text())["nsteps"] == 500
        r = run_gromacs_workflow("simple_md", inputs, tmp_path / "s2", nsteps=20)
        assert parse_mdlog_progress((tmp_path / "s2" / "md.log").read_text())["nsteps"] == 20

    def test_analysis_workflow(self, short_run, tmp_path):
        r = run_gromacs_workflow("analysis", {"structure": short_run / "md.tpr", "trajectory": short_run / "md.xtc",
                                              "energy": short_run / "md.edr"}, tmp_path / "an")
        assert r["result"], r.get("error")
        for f in ("rms.xvg", "rmsf.xvg", "energy.xvg"):
            assert (tmp_path / "an" / f).exists()
            assert str(tmp_path / "an" / f) in r["output_files"]

    def test_demo_workflow_is_a_self_test(self, tmp_path):
        r = run_gromacs_workflow("demo", output_file=tmp_path / "d")
        assert r["result"] and [s["name"] for s in r["step_results"]] == ["gmx_version", "gmx_help"]

    def test_missing_required_input(self, tmp_path):
        r = run_gromacs_workflow("simple_md", {"structure": "x.gro"}, tmp_path / "m")
        assert r["result"] is False and "not found" in r["error"] or "Required input" in r["error"]

    def test_preprocessing_from_pdb(self, work, tmp_path):
        from lib.gromacs_utils import run_command_full, find_gromacs_command
        pdb = work / "water.pdb"
        assert run_command_full(find_gromacs_command() + ["editconf", "-f", "water.gro", "-o", "water.pdb"],
                                working_dir=work)["success"]
        r = run_gromacs_workflow("preprocessing", {"structure": pdb}, tmp_path / "pre", box_distance=1.2,
                                 force_field="oplsaa", water_model="spce", solvate=False)
        # pdb2gmx on bare water PDB produces SOL molecules; this checks the chain runs and fails or succeeds clearly
        assert isinstance(r["result"], bool)
        assert r["step_results"][0]["name"] == "pdb2gmx"


class TestForceFields:
    def test_list(self):
        r = prep.list_force_fields()
        assert r["status"] == "success" and r["total"] >= 5
        ff = {f["name"]: f for f in r["force_fields"]}
        assert "oplsaa" in ff and "spce" in ff["oplsaa"]["water_models"]

    def test_command_help_text(self):
        from tools.env_tools import get_command_help
        r = get_command_help("rdf")
        assert r["status"] == "success" and "-ref" in r["help"]
