"""Parsers, MDP templates and result formatting. No GROMACS needed: inputs are real outputs saved in tests/fixtures."""

import math
import re

import pytest

from lib import mdp_templates
from lib.gromacs_utils import (
    block_average,
    detect_equilibration,
    parse_check_output,
    parse_energy_menu,
    parse_energy_table,
    parse_grompp_messages,
    parse_mdlog_progress,
    parse_msd_diffusion,
    parse_tpr_dump_output,
    parse_xvg,
    summarize_gmx_error,
    tail_lines,
)
from utils import format_tool_result


def read(fixtures, name):
    return (fixtures / name).read_text()


class TestTprDump:
    def test_values_from_real_dump(self, fixtures):
        info = parse_tpr_dump_output(read(fixtures, "npt_tpr_dump_excerpt.txt"))
        assert info["natoms"] == 1530
        assert info["nsteps"] == 25000
        assert info["dt"] == 0.002
        assert info["integrator"] == "md"
        assert info["temperature"] == 300.0
        assert info["tau_t"] == [0.1]
        assert info["pcoupl"] == "C-rescale"
        assert info["title"] == "Water box in water"
        assert info["nmolecules"] == 510

    def test_pressure_is_not_the_matrix_size(self, fixtures):
        """The old regex read the 3 from 'ref-p (3x3)'."""
        text = read(fixtures, "npt_tpr_dump_excerpt.txt")
        assert parse_tpr_dump_output(text)["pressure"] == 1.0
        changed = text.replace("ref-p[    0]={ 1.00000e+00,", "ref-p[    0]={ 2.50000e+00,")
        assert parse_tpr_dump_output(changed)["pressure"] == 2.5

    def test_atom_count_is_header_not_line_count(self, fixtures):
        info = parse_tpr_dump_output(read(fixtures, "npt_tpr_dump_excerpt.txt"))
        assert "total_atoms" not in info
        assert info["natoms"] == 1530


class TestEnergyParsing:
    def test_table(self, fixtures):
        table = parse_energy_table(read(fixtures, "gmx_energy_stdout.txt"))
        assert set(table) == {"Potential", "Temperature", "Pressure", "Volume", "Density"}
        assert table["Density"]["average"] == pytest.approx(996.415)
        assert table["Density"]["unit"] == "kg/m^3"
        assert table["Temperature"]["tot_drift"] == pytest.approx(4.81859)

    def test_table_with_dashes_for_short_windows(self):
        text = ("Energy                      Average   Err.Est.       RMSD  Tot-Drift\n"
                "-------------------------------------------------------------------------------\n"
                "Potential                  -23697.2         --    274.558    20.9108  (kJ/mol)\n"
                "Temperature                 299.985         --         --         --  (K)\n\n")
        table = parse_energy_table(text)
        assert table["Potential"]["average"] == pytest.approx(-23697.2) and table["Potential"]["err_est"] is None
        assert table["Temperature"]["rmsd"] is None and table["Temperature"]["unit"] == "K"

    def test_menu(self, fixtures):
        menu = parse_energy_menu(read(fixtures, "gmx_energy_menu.txt"))
        assert menu[4] == "Potential"
        assert menu[14] == "Density"
        assert menu[35] == "#Surf*SurfTen"
        assert len(menu) == 37

    def test_menu_names_with_spaces(self):
        text = "-----\n  1  Pres. DC (bar)   2  Disper. corr.\n\n"
        assert parse_energy_menu(text) == {1: "Pres. DC (bar)", 2: "Disper. corr."}

    def test_menu_with_more_than_99_terms(self, fixtures):
        # real output with 9 energy groups: a 14 character name leaves one space before index 100,
        # and the layout switches between four and two columns (TODO 12.24)
        menu = parse_energy_menu(read(fixtures, "gmx_energy_menu_122_terms.txt"))
        assert sorted(menu) == list(range(1, 123))
        assert menu[99] == "Coul-SR:EE-IJK" and menu[100] == "LJ-SR:EE-IJK"
        assert menu[113] == "Coul-SR:GG-IJK" and menu[119] == "Coul-SR:IJK-IJK" and menu[122] == "Lamb-System"
        assert all("  " not in n for n in menu.values())

    def test_menu_index_glued_to_a_name(self):
        text = "-----\n 98  Coul-SR:A-B12 99  LJ-SR:A-B12\n100  ABCDEFGHIJKLMNO101  LJ-SR:C-D1102  X\n\n"
        assert parse_energy_menu(text) == {98: "Coul-SR:A-B12", 99: "LJ-SR:A-B12", 100: "ABCDEFGHIJKLMNO",
                                           101: "LJ-SR:C-D1", 102: "X"}


class TestXvg:
    def test_energy_xvg(self, fixtures):
        x = parse_xvg(read(fixtures, "energy.xvg"))
        assert x["title"] == "GROMACS Energies"
        assert x["legends"] == ["Potential", "Temperature", "Pressure", "Volume", "Density"]
        assert x["n_rows"] == 251
        assert x["n_columns"] == 6
        assert x["stats"][0]["legend"] == "Potential"
        assert x["stats"][0]["mean"] == pytest.approx(-23697.236, rel=1e-6)

    def test_downsampling_keeps_stats_on_all_rows(self, fixtures):
        full = parse_xvg(read(fixtures, "energy.xvg"))
        small = parse_xvg(read(fixtures, "energy.xvg"), max_points=10)
        assert small["downsampled"] and len(small["data"]) <= 10
        assert small["stats"][0]["mean"] == full["stats"][0]["mean"]

    def test_msd_diffusion_legend(self, fixtures):
        fit = parse_msd_diffusion(read(fixtures, "msd_header.xvg"))
        assert fit["diffusion_coefficient"] == pytest.approx(2.4942)
        assert fit["diffusion_error"] == pytest.approx(0.03)
        assert fit["unit"] == "1e-5 cm^2/s"

    def test_msd_without_fit(self):
        assert parse_msd_diffusion('@ title "MSD"\n0 0\n') is None


class TestCheckAndLog:
    def test_check_xtc(self, fixtures):
        info = parse_check_output(read(fixtures, "gmx_check_xtc_stderr.txt"))
        assert info["natoms"] == 1530
        assert info["n_frames"] == 51
        assert info["timestep_ps"] == 1.0
        assert info["last_time_ps"] == 50.0

    def test_check_edr(self, fixtures):
        info = parse_check_output(read(fixtures, "gmx_check_edr_stderr.txt"))
        assert info["n_frames"] == 251
        assert info["timestep_ps"] == 0.2
        assert info["n_energy_terms"] == 37

    def test_md_log_finished(self, fixtures):
        p = parse_mdlog_progress(read(fixtures, "md_log_full.txt"))
        assert p["finished"] and p["complete"] and p["step"] == 25000 and p["percent"] == 100.0
        assert p["ns_per_day"] == pytest.approx(210.82)
        assert not any("LINCS" in w for w in p.get("warnings", []))

    def test_md_log_midrun(self, fixtures):
        text = read(fixtures, "md_log_full.txt")
        cut = text[: text.index("           Step           Time\n          25000")]
        cut = cut.split("Finished mdrun")[0]
        p = parse_mdlog_progress(cut)
        assert not p["finished"]
        assert 0 < p["percent"] < 100
        assert "ns_per_day" not in p

    def test_finished_after_signal_is_not_complete(self, fixtures):
        text = read(fixtures, "md_log_full.txt")
        cut = text[: text.index("           Step           Time\n          25000")] + "\nFinished mdrun on rank 0\n"
        p = parse_mdlog_progress(cut)
        assert p["finished"] and not p["complete"] and p["percent"] < 100

    def test_converged_minimization_is_complete(self, fixtures):
        # steepest descent converged at step 62 of 500: done, not stopped early
        p = parse_mdlog_progress(read(fixtures, "md_log_em_converged.txt"))
        assert p["finished"] and p["complete"] and p["step"] == 62 and p["nsteps"] == 500 and p["percent"] == 100.0

    def test_zero_steps_and_no_limit(self):
        log = "   nsteps                         = {n}\n           Step           Time\n              0        0.00000\n"
        assert parse_mdlog_progress(log.format(n=0) + "Finished mdrun on rank 0\n")["complete"] is True
        assert parse_mdlog_progress(log.format(n=-1) + "Finished mdrun on rank 0\n")["complete"] is False
        assert parse_mdlog_progress("Finished mdrun on rank 0\n")["complete"] is False  # no nsteps: unknown

    def test_nsteps_from_the_tpr_overrides_the_log(self, fixtures):
        # after extend and resume, md.log only holds the first run's nsteps (mdrun -append does not
        # print the parameters again); the job passes the value from the .tpr
        p = parse_mdlog_progress(read(fixtures, "md_log_full.txt"), nsteps=50000)
        assert p["nsteps"] == 50000 and p["step"] == 25000 and p["percent"] == 50.0 and not p["complete"]

    def test_tail_lines(self, tmp_path):
        f = tmp_path / "x.log"
        f.write_text("".join(f"line {i}\n" for i in range(1000)))
        assert [l.strip() for l in tail_lines(f, 3)] == ["line 997", "line 998", "line 999"]
        assert len(tail_lines(f, 0)) == 1000


class TestStdin:
    def test_child_stdin_is_closed_when_no_answers(self):
        import sys
        from lib.gromacs_utils import run_command_full
        r = run_command_full([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], timeout=10)
        assert r["stdout"].strip() == "''"


class TestErrors:
    def test_grompp_error_block(self, fixtures):
        text = read(fixtures, "grompp_cutoff_error_stderr.txt")
        err = summarize_gmx_error(text)
        assert "cut-off length is longer than half" in err
        assert "GROMACS reminds you" not in err

    def test_user_input_inconsistency(self, fixtures):
        err = summarize_gmx_error(read(fixtures, "gmx_msd_inconsistency_stderr.txt"))
        assert "-dt cannot be larger than -trestart" in err
        assert "For more information" not in err

    def test_generic_tail(self):
        assert summarize_gmx_error("a\nb\nc", max_lines=2) == "b\nc"
        assert summarize_gmx_error("") == ""

    def test_grompp_messages(self):
        out = parse_grompp_messages(
            "NOTE 1 [file md.mdp]:\n  odd\n\nWARNING 1 [file topol.top, line 5]:\n  net charge\n  of 2\n\n"
            "ERROR 1 [file x, line 1]:\n  broken\n\n")
        assert out["notes"] == ["[file md.mdp] odd"]
        assert out["warnings"] == ["[file topol.top, line 5] net charge of 2"]
        assert out["errors"] == ["[file x, line 1] broken"]


class TestStatistics:
    def test_equilibration_detected(self):
        import random
        random.seed(1)
        series = [10 - 0.2 * i for i in range(40)] + [random.gauss(0, 0.05) for _ in range(300)]
        idx = detect_equilibration(series)
        assert 20 <= idx <= 120

    def test_flat_series_is_equilibrated_from_start(self):
        import random
        random.seed(2)
        assert detect_equilibration([random.gauss(5, 0.1) for _ in range(300)]) <= 40

    def test_short_series_is_unknown_not_equilibrated(self):
        assert detect_equilibration([1.0, 2.0, 3.0]) is None
        assert detect_equilibration([1.0] * 19) is None
        assert detect_equilibration([1.0] * 20) == 0

    def test_short_run_with_a_relaxing_start_is_truncated(self):
        # 21 frames like a 20 ps NVT run writing energies every 1 ps: high start, then a plateau
        # (TODO 13.1: a 40 frame gate used to report this as equilibrated from 0 ps)
        from tools.analysis import _equilibration_index
        pot = [-13648, -15200, -15800, -16000, -16100] + [-16180 + (-1) ** i * 120 for i in range(16)]
        idx, why_not = _equilibration_index({"Potential": pot})
        assert why_not is None and idx >= 5

    def test_too_short_for_detection_says_so(self):
        from tools.analysis import _equilibration_index
        idx, why_not = _equilibration_index({"Potential": [1.0] * 11, "Temperature": [300.0] * 11})
        assert idx is None and "11 energy frames" in why_not
        idx, why_not = _equilibration_index({"Pressure": [1.0] * 50})
        assert idx is None and "needs one of" in why_not

    @pytest.mark.parametrize("phi", [0.0, 0.9])
    def test_statistical_inefficiency_of_ar1_series(self, phi):
        # an AR(1) series has g = (1 + phi) / (1 - phi): 1 for white noise, 19 for phi = 0.9 (TODO 12.17)
        import random
        from lib.gromacs_utils import statistical_inefficiency
        rng = random.Random(7)
        x = [0.0]
        for _ in range(20000):
            x.append(phi * x[-1] + rng.gauss(0, 1))
        expected = (1 + phi) / (1 - phi)
        assert statistical_inefficiency(x) == pytest.approx(expected, rel=0.25)
        # thinned when long: still close, in units of the original frames
        assert statistical_inefficiency(x * 3) == pytest.approx(expected, rel=0.35)

    def test_statistical_inefficiency_degenerate(self):
        from lib.gromacs_utils import statistical_inefficiency
        assert statistical_inefficiency([1.0] * 50) is None
        assert statistical_inefficiency([1.0, 2.0, 3.0]) is None

    def test_block_average(self):
        r = block_average([1.0] * 100)
        assert r["mean"] == 1.0 and r["sem"] == 0.0
        assert math.isnan(block_average([])["mean"])


class TestRdfFeatures:
    def test_peak_and_minimum_on_real_water_rdf(self, fixtures):
        from tools.analysis import _first_peak_and_minimum
        d = parse_xvg(read(fixtures, "rdf_oo.xvg"))["data"]
        f = _first_peak_and_minimum([x[0] for x in d], [x[1] for x in d])
        assert f["first_peak_r_nm"] == pytest.approx(0.278, abs=0.004)
        assert f["first_peak_g"] == pytest.approx(3.1, abs=0.2)
        assert f["first_minimum_r_nm"] == pytest.approx(0.33, abs=0.02)
        assert f["first_minimum_g"] < 1.0

    def test_flat_rdf_has_no_peak(self):
        from tools.analysis import _first_peak_and_minimum
        assert _first_peak_and_minimum([i * 0.01 for i in range(50)], [0.5] * 50) == {}


class TestMdpTemplates:
    def test_all_stages_render_and_parse_back(self):
        for stage in mdp_templates.STAGES:
            text, params = mdp_templates.render_stage(stage)
            parsed = mdp_templates.parse_mdp(text)
            assert set(parsed) == set(params)

    def test_time_sets_nsteps(self):
        _, p = mdp_templates.render_stage("nvt", time_ps=100)
        assert p["nsteps"] == 50000
        _, p = mdp_templates.render_stage("nvt", time_ps=10, overrides={"dt": 0.001})
        assert p["nsteps"] == 10000

    def test_underscore_overrides_and_removal(self):
        _, p = mdp_templates.render_stage("npt", overrides={"tau_p": 5, "pcoupl": None})
        assert p["tau-p"] == 5
        assert "pcoupl" not in p

    def test_temperature_convenience(self):
        _, p = mdp_templates.render_stage("nvt", temperature=310)
        assert str(p["ref-t"]) == "310" and p["gen-temp"] == 310

    def test_temperature_fills_every_coupling_group(self):
        _, p = mdp_templates.render_stage("nvt", temperature=310, overrides={"tc_grps": "Protein Non-Protein"})
        assert p["ref-t"] == "310 310" and p["tau-t"] == "0.1 0.1"
        assert not [i for i in mdp_templates.lint_params(p) if i["level"] == "error"]

    def test_temperature_without_coupling_groups_adds_nothing(self):
        _, p = mdp_templates.render_stage("nve", temperature=310)
        assert "ref-t" not in p and "tc-grps" not in p

    def test_npt_has_refcoord_scaling(self):
        for stage in ("npt", "free_energy", "umbrella"):
            assert mdp_templates.build_params(stage)["refcoord-scaling"] == "com"

    def test_free_energy_switches_charges_before_vdw(self):
        p = mdp_templates.build_params("free_energy")
        coul = [float(x) for x in p["coul-lambdas"].split()]
        vdw = [float(x) for x in p["vdw-lambdas"].split()]
        assert len(coul) == len(vdw)
        assert all(v == 0.0 for c, v in zip(coul, vdw) if c < 1.0)
        assert p["tc-grps"] and p["tau-t"]

    def test_unknown_stage(self):
        with pytest.raises(ValueError):
            mdp_templates.render_stage("production")

    def test_templates_have_no_errors(self):
        for stage in ("em", "nvt", "npt", "nve", "ions"):
            _, p = mdp_templates.render_stage(stage)
            assert not [i for i in mdp_templates.lint_params(p) if i["level"] == "error"], stage

    @pytest.mark.parametrize("params,key,level", [
        ({"integrator": "steep", "pcoupl": "C-rescale"}, "pcoupl", "warning"),
        ({"integrator": "md", "tcoupl": "berendsen", "tc-grps": "System", "tau-t": 0.1, "ref-t": 300}, "tcoupl", "warning"),
        ({"integrator": "md", "pcoupl": "berendsen", "tcoupl": "no"}, "pcoupl", "warning"),
        ({"integrator": "md", "pcoupl": "C-rescale", "tau-p": 0.5, "tcoupl": "V-rescale", "tc-grps": "System", "tau-t": 0.1, "ref-t": 300}, "tau-p", "warning"),
        ({"integrator": "md", "tcoupl": "V-rescale"}, "tc-grps", "error"),
        ({"integrator": "md", "constraints": "none", "dt": 0.002}, "dt", "warning"),
        ({"cutoff-scheme": "group"}, "cutoff-scheme", "error"),
        ({"integrator": "md", "gen-vel": "yes", "continuation": "yes"}, "gen-vel", "warning"),
        ({"integrator": "md", "nstxout-compressed": 0, "nstxout": 0}, "nstxout-compressed", "warning"),
        ({"integrator": "md", "nstenergy": 150, "nstcalcenergy": 100}, "nstenergy", "warning"),
        ({"integrator": "md", "free-energy": "yes"}, "couple-moltype", "error"),
        ({"integrator": "sd"}, "tc-grps", "error"),
        ({"integrator": "md", "tcoupl": "V-rescale", "tc-grps": "Protein Non-Protein", "tau-t": 0.1, "ref-t": "300 300"},
         "tau-t", "error"),
        ({"integrator": "md", "define": "-DPOSRES", "pcoupl": "C-rescale", "tcoupl": "V-rescale", "tc-grps": "System",
          "tau-t": 0.1, "ref-t": 300}, "refcoord-scaling", "warning"),
    ])
    def test_lint_rules(self, params, key, level):
        issues = mdp_templates.lint_params(params)
        assert any(i["key"] == key and i["level"] == level for i in issues), issues

    def test_simulated_time_note(self):
        issues = mdp_templates.lint_params({"integrator": "md", "dt": 0.002, "nsteps": 5000})
        assert any("10.000 ps" in i["message"] for i in issues)


class TestResultEnvelope:
    def test_success_envelope(self):
        r = format_tool_result(True, {"result": True, "x": 1}, output_files=["a"])
        assert r == {"status": "success", "x": 1, "warnings": [], "output_files": ["a"]}

    def test_failure_keeps_data_and_takes_error_from_data(self):
        r = format_tool_result(False, {"result": False, "error": "boom", "stderr": "details", "command": "gmx x"})
        assert r["status"] == "error" and r["error"] == "boom"
        assert r["stderr"] == "details" and r["command"] == "gmx x"
        assert r["warnings"] == [] and r["output_files"] == []

    def test_error_message_wins_and_default(self):
        assert format_tool_result(False, {"error": "a"}, error_message="b")["error"] == "b"
        assert format_tool_result(False)["error"] == "Unknown error"

    def test_warnings_merge(self):
        r = format_tool_result(True, {"warnings": ["a"]}, warnings=["b"])
        assert r["warnings"] == ["a", "b"]

    def test_long_strings_truncated_and_flagged(self):
        r = format_tool_result(True, {"stdout": "x" * 50000}, max_chars=1000)
        assert len(r["stdout"]) < 1200
        assert "stdout" in r["truncated"]
        assert any("shortened" in w for w in r["warnings"])

    def test_long_lists_truncated(self):
        r = format_tool_result(True, {"rows": list(range(10000))})
        assert len(r["rows"]) <= 2000 and "rows" in r["truncated"]


class TestPlotLabels:
    def test_y_label_keeps_the_units_of_plotted_columns(self):
        # TODO 13.7: one column used to be labelled with every unit in the file
        from tools.analysis import _y_label
        legends = ["Potential", "Temperature", "Pressure", "Density"]
        units = "(kJ/mol), (K), (bar), (kg/m^3)"
        assert _y_label(units, legends, [2]) == "(K)"
        assert _y_label(units, legends, [1, 3]) == "(kJ/mol), (bar)"
        assert _y_label("(kJ/mol), (kJ/mol)", ["LJ", "Coulomb"], [1, 2]) == "(kJ/mol)"
        assert _y_label("g(r)", ["O-O"], [1]) == "g(r)"
        assert _y_label("(nm), (nm)", ["a", "b", "c"], [1]) == "(nm), (nm)"  # no one to one match: unchanged


class TestExampleData:
    def test_example_mdps_pass_the_servers_own_lint(self):
        # TODO 13.9: md.mdp used to warn about nstenergy and nstcalcenergy
        from pathlib import Path
        files = sorted((Path(__file__).resolve().parent.parent / "examples" / "data").glob("*.mdp"))
        assert {f.name for f in files} >= {"em.mdp", "md.mdp"}
        for f in files:
            issues = mdp_templates.lint_params(mdp_templates.parse_mdp(f.read_text()))
            assert not [i for i in issues if i["level"] in ("warning", "error")], (f.name, issues)


class TestHelpOptions:
    def test_options_from_real_help_text(self, fixtures):
        from gromacs_command import parse_help_options
        opts = parse_help_options(read(fixtures, "gmx_help_rdf.txt"))
        assert opts[:5] == ["f", "s", "n", "o", "cn"]
        for name in ("ref", "sel", "bin", "rmax", "rmpbc", "normpbc", "xy", "noxy", "selrpos"):
            assert name in opts
        assert not [o for o in opts if o.startswith("[")]
