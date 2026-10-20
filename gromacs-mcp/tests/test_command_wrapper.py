"""run_gromacs_command behavior, using a fake gmx that records how it was called (no GROMACS needed)."""

import os
from pathlib import Path

import pytest

from gromacs_command import COMMANDS, run_gromacs_command
from lib.gromacs_utils import GromacsNotFoundError, PathNotAllowedError, find_gromacs_command, resolve_in_workdir


@pytest.fixture
def edr(tmp_path):
    p = tmp_path / "run.edr"
    p.write_bytes(b"x")
    return p


class TestWhitelist:
    def test_unknown_command_rejected_and_nothing_runs(self, fake_gmx):
        r = run_gromacs_command("rm")
        assert r["result"] is False
        assert "not allowed" in r["error"] and "energy" in r["error"]
        assert fake_gmx.calls() == []

    @pytest.mark.parametrize("name", ["mdrun", "bash", "../gmx", "energy; ls", ""])
    def test_not_in_whitelist(self, fake_gmx, name):
        assert run_gromacs_command(name)["result"] is False
        assert fake_gmx.calls() == []

    def test_expected_commands_present(self):
        for name in ("energy", "rms", "rmsf", "rdf", "msd", "grompp", "genion", "solvate", "pdb2gmx",
                     "editconf", "trjconv", "dump", "check", "traj"):
            assert name in COMMANDS


class TestSelectionsAndStdin:
    def test_energy_terms_sent_on_stdin_with_terminator(self, fake_gmx, edr, tmp_path):
        r = run_gromacs_command("energy", {"f": edr}, {"o": tmp_path / "e.xvg"}, selections=["Potential", "Density"])
        assert r["result"] is True
        assert fake_gmx.stdin() == "Potential\nDensity\n0"

    def test_terminator_not_duplicated(self, fake_gmx, edr):
        run_gromacs_command("energy", {"f": edr}, selections=["Potential", "0"])
        assert fake_gmx.stdin() == "Potential\n0"

    def test_default_selections_for_rms(self, fake_gmx, tmp_path):
        f, s = tmp_path / "a.xtc", tmp_path / "a.tpr"
        f.write_bytes(b"x"); s.write_bytes(b"x")
        run_gromacs_command("rms", {"f": f, "s": s})
        assert fake_gmx.stdin() == "0\n0"

    def test_explicit_selections_replace_defaults(self, fake_gmx, tmp_path):
        f, s = tmp_path / "a.xtc", tmp_path / "a.tpr"
        f.write_bytes(b"x"); s.write_bytes(b"x")
        run_gromacs_command("rms", {"f": f, "s": s}, selections=["Backbone", "Protein"])
        assert fake_gmx.stdin() == "Backbone\nProtein"

    def test_energy_without_selections_asks_for_menu_and_explains(self, fake_gmx, edr, monkeypatch):
        monkeypatch.setenv("FAKE_GMX_RC", "1")
        monkeypatch.setenv("FAKE_GMX_STDOUT", "  1  LJ-(SR)   2  Potential")
        r = run_gromacs_command("energy", {"f": edr})
        assert r["result"] is False
        assert any("selections" in w for w in r["warnings"])
        assert "Potential" in r["stdout"]
        assert fake_gmx.stdin() == ""  # one empty line, which the shell reads back as empty text

    def test_selections_must_be_strings(self, fake_gmx, edr):
        r = run_gromacs_command("energy", {"f": edr}, selections=[1, 2])
        assert r["result"] is False and "strings" in r["error"]


class TestNoShell:
    def test_metacharacters_are_literal_arguments(self, fake_gmx, tmp_path):
        f = tmp_path / "t.xtc"
        f.write_bytes(b"x")
        literal = ["a b", "it's", "name OW and x < 1", "resname $X"]  # selections and values, not file names
        r = run_gromacs_command("check", {"f": f}, parameters=literal)
        assert r["result"] is True
        argv = fake_gmx.calls()[0]
        for item in literal:
            assert item in argv

    def test_shell_active_file_names_are_refused(self, fake_gmx, tmp_path):
        # gmx opens compressed files through `gunzip -c < name` in a shell (and tries name.gz when name is
        # missing), so a file name must not carry $ ` ; & | < > or a newline, nor end in .gz or .Z
        marker = tmp_path / "PWNED"
        f = tmp_path / "t.xtc"
        f.write_bytes(b"x")
        for nasty in (f"; touch {marker}", f"$(touch {marker})", f"`touch {marker}`", f"{tmp_path}/a|b.trr"):
            r = run_gromacs_command("check", {"f": f}, parameters=["-f2", nasty])
            assert r["result"] is False and "not allowed" in r["error"], nasty
        for name in ("x$(id).xtc", "x.xtc.gz", "x.trr.Z", "a;b.edr"):
            bad = tmp_path / name
            bad.write_bytes(b"x")
            assert run_gromacs_command("check", {"f": bad})["result"] is False, name
        assert run_gromacs_command("energy", {"f": f}, {"o": tmp_path / "e`x`.xvg"}, selections=["1"])["result"] is False
        assert fake_gmx.calls() == [] and not marker.exists()

    def test_file_options_in_parameters_take_plain_names_only(self, fake_gmx, fixtures, tmp_path, monkeypatch):
        # decided by position: whatever follows a file option of the tool (read from its help) is a file
        # name, with or without a slash or suffix
        f, s = tmp_path / "a.xtc", tmp_path / "a.tpr"
        f.write_bytes(b"x"); s.write_bytes(b"x")
        for params in (["-sf", "sel$(id)"], ["-n", "x;id"], ["--n", str(tmp_path / "i.ndx")], ["-n", "../i.ndx"],
                       ["-ref", "name OW", "-sf", "a b"]):
            r = run_gromacs_command("rdf", {"f": f, "s": s}, parameters=params)
            assert r["result"] is False and "plain file name" in r["error"], params
        assert fake_gmx.calls() == []
        r = run_gromacs_command("rdf", {"f": f, "s": s}, parameters=["-n", "index.ndx", "-bin", "-0.5", "-ref", "x < 1"])
        assert r["result"] is True

    def test_only_real_option_names_end_a_file_value(self, fake_gmx, tmp_path):
        # gmx reads "-", "--" and -.5 as values, so they must not end the check of -n's value
        f, s = tmp_path / "a.xtc", tmp_path / "a.tpr"
        f.write_bytes(b"x"); s.write_bytes(b"x")
        for params in (["-n", "-", "x$(id)"], ["-n", "--", "x$(id)"], ["-n", "-.5", "x$(id)"],
                       ["-n", "-bogus", "x$(id)"], ["-n", "a.ndx", "b.ndx"]):
            assert run_gromacs_command("rdf", {"f": f, "s": s}, parameters=params)["result"] is False, params
        assert fake_gmx.calls() == []

    def test_unreadable_help_refuses_and_is_not_cached(self, fake_gmx, tmp_path, monkeypatch, fixtures):
        f = tmp_path / "a.xtc"
        f.write_bytes(b"x")
        empty = tmp_path / "empty.txt"
        empty.write_text("")
        monkeypatch.setenv("FAKE_GMX_HELP", str(empty))
        r = run_gromacs_command("rdf", {"f": f}, parameters=["-ref", "name OW"])
        assert r["result"] is False and "Could not read the options" in r["error"] and fake_gmx.calls() == []
        monkeypatch.setenv("FAKE_GMX_HELP", str(fixtures / "gmx_help_rdf.txt"))
        assert run_gromacs_command("rdf", {"f": f}, parameters=["-ref", "name OW"])["result"] is True

    def test_several_files_for_one_option(self, fake_gmx, tmp_path):
        a, b = tmp_path / "a.xtc", tmp_path / "b.xtc"
        a.write_bytes(b"x"); b.write_bytes(b"x")
        assert run_gromacs_command("trjcat", {"f": [a, b]}, {"o": tmp_path / "all.xtc"})["result"] is True
        argv = fake_gmx.calls()[0]
        assert argv[argv.index("-f") + 1:argv.index("-f") + 3] == [str(a), str(b)]

    def test_every_command_is_checked_in_one_place(self, fake_gmx, tmp_path):
        # tools that run gmx directly (grompp, minimization, workflows) go through run_command_full
        from lib.gromacs_utils import find_gromacs_command, run_command_full
        r = run_command_full(find_gromacs_command() + ["grompp", "-c", str(tmp_path / "a$(id).gro")])
        assert r["success"] is False and "not allowed" in r["stderr"]
        r = run_command_full(find_gromacs_command() + ["grompp", "-c", "conf;id.gro"])
        assert r["success"] is False
        assert fake_gmx.calls() == []

    def test_prep_tools_and_workflows_refuse_shell_active_names(self, fake_gmx, tmp_path):
        from tools import prep
        from gromacs_workflow import run_gromacs_workflow
        from md_simulation import run_md_simulation
        bad = tmp_path / "x$(touch PWNED).gro"
        bad.write_text("x")
        top, mdp = tmp_path / "t.top", tmp_path / "m.mdp"
        top.write_text("x"); mdp.write_text("integrator = steep\n")
        assert prep.run_grompp(str(mdp), str(bad), str(top), output_tpr=str(tmp_path / "o.tpr"))["status"] == "error"
        assert prep.run_minimization(str(bad), str(top), output_dir=str(tmp_path / "em"))["status"] == "error"
        assert prep.validate_mdp(mdp_file=str(mdp), structure=str(bad), topology=str(top))["status"] == "error"
        wf = run_gromacs_workflow("simple_md", {"structure": bad, "topology": top, "mdp": mdp}, tmp_path / "wf")
        assert wf["result"] is False and "not allowed" in wf["error"]
        tpr = tmp_path / "r$(id).tpr"
        tpr.write_bytes(b"x")
        md = run_md_simulation(tpr, tmp_path / "run", nsteps=10)  # convert-tpr -s <input>
        assert md["result"] is False and "not allowed" in md["error"]
        assert fake_gmx.calls() == [] and not (tmp_path / "PWNED").exists()
        # without nsteps the input is copied to md.tpr first, so its name never reaches gmx
        run_md_simulation(tpr, tmp_path / "run2")
        assert fake_gmx.calls() and all("$(id)" not in " ".join(c) for c in fake_gmx.calls())

    @pytest.mark.parametrize("extra", [["--f", "x.mdp"], ["-F", "x.mdp"], ["-imd", "x.gro"], ["--maxwarn", "9"]])
    def test_run_grompp_file_options_in_any_spelling(self, fake_gmx, tmp_path, extra):
        from tools import prep
        for name in ("a.mdp", "a.gro", "a.top"):
            (tmp_path / name).write_text("x")
        r = prep.run_grompp(str(tmp_path / "a.mdp"), str(tmp_path / "a.gro"), str(tmp_path / "a.top"),
                            output_tpr=str(tmp_path / "a.tpr"), extra_parameters=extra)
        assert r["status"] == "error" and "named arguments" in r["error"]
        assert fake_gmx.calls() == []

    def test_viewer_option_is_refused(self, fake_gmx, tmp_path):
        # -w starts a viewer through the shell with the output file name; -now (do not view) is fine
        f = tmp_path / "e.edr"
        f.write_bytes(b"x")
        for spelling in ("-w", "--w", "-W"):
            r = run_gromacs_command("energy", {"f": f}, {"o": tmp_path / "e.xvg"}, [spelling], selections=["1"])
            assert r["result"] is False and "not allowed" in r["error"]
        assert fake_gmx.calls() == []
        assert run_gromacs_command("energy", {"f": f}, {"o": tmp_path / "e.xvg"}, ["-now"], selections=["1"])["result"]

    def test_children_get_no_display(self, monkeypatch):
        import json, sys
        from lib.gromacs_utils import run_command_full
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setenv("GMX_VIEW_XVG", "xmgrace")
        out = run_command_full([sys.executable, "-c", "import os, json; print(json.dumps(dict(os.environ)))"])
        env = json.loads(out["stdout"])
        assert "DISPLAY" not in env and "GMX_VIEW_XVG" not in env and "PATH" in env

    def test_wham_is_not_whitelisted(self, fake_gmx):
        # wham reads file names from list files (-it, -ix, -if, -ip) that the wrapper never sees, and
        # opens .gz entries through the shell
        assert "wham" not in COMMANDS
        assert run_gromacs_command("wham")["result"] is False and fake_gmx.calls() == []

    def test_selection_expression_stays_one_argument(self, fake_gmx, tmp_path):
        f, s = tmp_path / "a.xtc", tmp_path / "a.tpr"
        f.write_bytes(b"x"); s.write_bytes(b"x")
        run_gromacs_command("rdf", {"f": f, "s": s}, parameters=["-ref", "name OW", "-sel", "name OW"])
        argv = fake_gmx.calls()[0]
        assert argv[argv.index("-ref") + 1] == "name OW"
        assert argv[argv.index("-sel") + 1] == "name OW"

    def test_file_names_with_spaces_and_quotes(self, fake_gmx, tmp_path):
        f = tmp_path / "my traj 'v2' (final).xtc"
        f.write_bytes(b"x")
        assert run_gromacs_command("check", {"f": f})["result"] is True
        assert str(f) in fake_gmx.calls()[0]

    def test_invalid_flag_names_rejected(self, fake_gmx, tmp_path):
        f = tmp_path / "t.xtc"
        f.write_bytes(b"x")
        for flag in ("f; rm -rf /", "-f", "", "f o"):
            assert run_gromacs_command("check", {flag: f})["result"] is False
        assert fake_gmx.calls() == []

    @pytest.mark.parametrize("spelling", ["-exec", "--exec", "-EXEC", "---exec"])
    def test_options_that_run_programs_are_refused(self, fake_gmx, tmp_path, spelling):
        # trjconv -exec passes its value to the shell for every output frame (gmx accepts --exec too)
        f, s = tmp_path / "a.xtc", tmp_path / "a.tpr"
        f.write_bytes(b"x"); s.write_bytes(b"x")
        r = run_gromacs_command("trjconv", {"f": f, "s": s}, {"o": tmp_path / "o.gro"},
                                parameters=["-sep", spelling, f"touch {tmp_path / 'PWNED'}"])
        assert r["result"] is False and "not allowed" in r["error"]
        assert fake_gmx.calls() == []

    def test_exec_as_a_file_option_is_refused(self, fake_gmx, tmp_path):
        f = tmp_path / "a.xtc"
        f.write_bytes(b"x")
        assert "not allowed" in run_gromacs_command("trjconv", {"f": f, "exec": f})["error"]
        assert "not allowed" in run_gromacs_command("trjconv", {"f": f}, {"exec": tmp_path / "x"})["error"]
        assert fake_gmx.calls() == []

    def test_run_grompp_refuses_exec_in_extra_parameters(self, fake_gmx, tmp_path):
        from tools import prep
        for name in ("a.mdp", "a.gro", "a.top"):
            (tmp_path / name).write_text("x")
        r = prep.run_grompp(str(tmp_path / "a.mdp"), str(tmp_path / "a.gro"), str(tmp_path / "a.top"),
                            output_tpr=str(tmp_path / "a.tpr"), extra_parameters=["--exec", "touch x"])
        assert r["status"] == "error" and "not allowed" in r["error"]
        assert fake_gmx.calls() == []

    def test_invalid_parameters_rejected(self, fake_gmx):
        assert run_gromacs_command("check", parameters=[1, 2])["result"] is False
        assert run_gromacs_command("check", parameters=[""])["result"] is False
        assert fake_gmx.calls() == []


class TestFilesAndOutput:
    def test_missing_input_is_an_error_and_not_silently_dropped(self, fake_gmx, tmp_path):
        r = run_gromacs_command("check", {"f": tmp_path / "nope.xtc"})
        assert r["result"] is False and "not found" in r["error"]
        assert fake_gmx.calls() == []

    def test_overwrite_warning(self, fake_gmx, edr, tmp_path):
        out = tmp_path / "e.xvg"
        out.write_text("old")
        r = run_gromacs_command("energy", {"f": edr}, {"o": out}, selections=["Potential"])
        assert any("overwritten" in w for w in r["warnings"])

    def test_dump_writes_stdout_to_file_and_has_no_o_flag(self, fake_gmx, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_GMX_STDOUT", "inputrec: dumped text")
        tpr = tmp_path / "a.tpr"
        tpr.write_bytes(b"x")
        out = tmp_path / "dump.txt"
        r = run_gromacs_command("dump", {"s": tpr}, {"o": out})
        assert r["result"] is True
        assert "dumped text" in out.read_text()
        assert "-o" not in fake_gmx.calls()[0]
        assert r["output_files"] == [str(out)] and r["file_sizes"][str(out)] > 0

    def test_dump_into_a_folder_that_does_not_exist_yet(self, fake_gmx, tmp_path, monkeypatch):
        # gmx runs inside the output folder, so it must exist before the launch (TODO 13.2)
        monkeypatch.setenv("FAKE_GMX_STDOUT", "inputrec: dumped text")
        tpr = tmp_path / "a.tpr"
        tpr.write_bytes(b"x")
        out = tmp_path / "new" / "deeper" / "dump.txt"
        r = run_gromacs_command("dump", {"s": tpr}, {"o": out})
        assert r["result"] is True, r.get("error")
        assert "dumped text" in out.read_text()

    def test_missing_working_directory_is_a_clear_error(self, fake_gmx, tmp_path):
        r = run_gromacs_command("check", {"f": "a.xtc"}, working_directory=str(tmp_path / "nope"))
        assert r["result"] is False and "working_directory does not exist" in r["error"]
        assert fake_gmx.calls() == []

    def test_runs_in_folder_of_first_output(self, fake_gmx, edr, tmp_path):
        out_dir = tmp_path / "results" / "deep"
        run_gromacs_command("energy", {"f": edr}, {"o": out_dir / "e.xvg"}, selections=["Potential"])
        assert Path(fake_gmx.cwds()[0]).resolve() == out_dir.resolve()

    def test_explicit_working_directory(self, fake_gmx, tmp_path):
        wd = tmp_path / "work"
        wd.mkdir()
        (wd / "a.xtc").write_bytes(b"x")
        r = run_gromacs_command("check", {"f": "a.xtc"}, working_directory=str(wd))
        assert r["result"] is True
        assert Path(fake_gmx.cwds()[0]).resolve() == wd.resolve()
        assert str((wd / "a.xtc").resolve()) in fake_gmx.calls()[0]


class TestErrorPropagation:
    def test_gromacs_error_text_returned_without_banner(self, fake_gmx, edr, monkeypatch):
        monkeypatch.setenv("FAKE_GMX_RC", "1")
        monkeypatch.setenv("FAKE_GMX_STDERR",
                           ":-) GROMACS - gmx energy (-:\n\nCommand line:\n  gmx energy\n\n"
                           "Fatal error:\nNo such term 'Foo'\n\nFor more information and tips for troubleshooting, see...")
        r = run_gromacs_command("energy", {"f": edr}, selections=["Foo"])
        assert r["result"] is False
        assert r["error"].startswith("Fatal error:") and "No such term 'Foo'" in r["error"]
        assert "Command line" not in r["error"]
        assert "GROMACS - gmx energy" in r["stderr"]  # the full text is still available
        assert r["returncode"] == 1 and r["command"].endswith("-f " + str(edr))

    def test_menu_probe_is_not_logged_as_an_error(self, fake_gmx, edr, monkeypatch, caplog):
        # energy_summary asks for the term menu on purpose; that is not a failure worth an ERROR (TODO 13.8)
        import logging
        monkeypatch.setenv("FAKE_GMX_RC", "1")
        monkeypatch.setenv("FAKE_GMX_STDERR", "Fatal error:\nNo energy terms selected")
        with caplog.at_level(logging.DEBUG):
            assert run_gromacs_command("energy", {"f": edr})["result"] is False
        assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
        with caplog.at_level(logging.DEBUG):
            run_gromacs_command("energy", {"f": edr}, selections=["Potential"])
        assert [rec for rec in caplog.records if rec.levelno >= logging.ERROR]

    def test_stdout_and_stderr_returned_separately(self, fake_gmx, edr, monkeypatch):
        monkeypatch.setenv("FAKE_GMX_STDOUT", "to stdout")
        monkeypatch.setenv("FAKE_GMX_STDERR", "to stderr")
        r = run_gromacs_command("check", {"e": edr})
        assert r["stdout"].strip() == "to stdout" and r["stderr"].strip() == "to stderr"
        assert "output" not in r  # it used to repeat stdout in every response

    def test_tool_envelope_keeps_failure_details(self, fake_gmx, edr, monkeypatch):
        import server
        monkeypatch.setenv("FAKE_GMX_RC", "2")
        monkeypatch.setenv("FAKE_GMX_STDERR", "Fatal error:\nbad thing")
        r = server.run_gromacs_command("check", {"e": str(edr)})
        assert r["status"] == "error"
        assert "bad thing" in r["error"]
        assert "bad thing" in r["stderr"] and r["command"]


class TestSandbox:
    @pytest.fixture
    def sandbox(self, tmp_path, monkeypatch):
        root = tmp_path / "allowed"
        root.mkdir()
        monkeypatch.setenv("GMX_MCP_WORKDIR", str(root))
        return root

    def test_inside_is_fine(self, fake_gmx, sandbox):
        f = sandbox / "a.xtc"
        f.write_bytes(b"x")
        assert run_gromacs_command("check", {"f": f})["result"] is True

    def test_outside_input_rejected(self, fake_gmx, sandbox, tmp_path):
        outside = tmp_path / "secret.xtc"
        outside.write_bytes(b"x")
        r = run_gromacs_command("check", {"f": outside})
        assert r["result"] is False and "outside the allowed directory" in r["error"]
        assert fake_gmx.calls() == []

    def test_outside_output_rejected(self, fake_gmx, sandbox, tmp_path):
        f = sandbox / "a.edr"
        f.write_bytes(b"x")
        r = run_gromacs_command("energy", {"f": f}, {"o": tmp_path / "leak.xvg"}, selections=["Potential"])
        assert r["result"] is False and "outside" in r["error"]

    def test_paths_inside_parameters_are_checked(self, fake_gmx, sandbox, tmp_path):
        f = sandbox / "a.edr"
        f.write_bytes(b"x")
        for leak in ([ "-o", str(tmp_path / "leak.xvg")], ["-o", "../leak.xvg"], ["-od", "../../x.dat"]):
            r = run_gromacs_command("energy", {"f": f}, parameters=leak, selections=["Potential"])
            assert r["result"] is False and "outside" in r["error"], leak
        assert fake_gmx.calls() == []
        ok = run_gromacs_command("energy", {"f": f}, parameters=["-o", "inside.xvg", "-b", "-5"],
                                 selections=["Potential"])
        assert ok["result"] is True
        sel = run_gromacs_command("rdf", {"f": f, "s": f}, parameters=["-sel", "name OW and x < 2"])
        assert sel["result"] is True

    def test_grompp_extra_parameters_are_sandboxed(self, fake_gmx, sandbox, tmp_path):
        from tools.prep import run_grompp
        for name in ("a.mdp", "a.gro", "a.top"):
            (sandbox / name).write_text("x")
        args = (str(sandbox / "a.mdp"), str(sandbox / "a.gro"), str(sandbox / "a.top"))
        r = run_grompp(*args, output_tpr=str(sandbox / "o.tpr"), extra_parameters=["-pp", str(tmp_path / "leak.top")])
        assert r["status"] == "error" and "named arguments" in r["error"]  # grompp file options never pass
        r = run_grompp(*args, output_tpr=str(sandbox / "o.tpr"), extra_parameters=["-sel", str(tmp_path / "leak.dat")])
        assert r["status"] == "error" and "outside" in r["error"]
        r = run_grompp(*args, output_tpr=str(sandbox / "o.tpr"), extra_parameters=["-o", str(sandbox / "x.tpr")])
        assert r["status"] == "error" and "named arguments" in r["error"]
        assert fake_gmx.calls() == []

    def test_multidir_is_sandboxed(self, sandbox, tmp_path):
        import server
        tpr = sandbox / "a.tpr"
        tpr.write_bytes(b"x")
        r = server.submit_md_simulation(str(tpr), multidir=["../../escape"], output_dir=str(sandbox / "multi"))
        assert r["status"] == "error" and "outside" in r["error"]
        r = server.submit_md_simulation(str(tpr), multidir=["r1"])
        assert r["status"] == "error" and "requires output_dir" in r["error"]

    def test_relative_parameter_paths_checked_against_the_run_folder(self, fake_gmx, sandbox, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # the server's own folder differs from where gmx will run
        (tmp_path / "x.ndx").write_text("x")
        r = run_gromacs_command("select", parameters=["-select", "name OW", "-on", "../escape.ndx"])
        assert r["result"] is False and "outside" in r["error"]
        ok = run_gromacs_command("select", parameters=["-select", "name OW", "-on", "inside.ndx"])
        assert ok["result"] is True and Path(fake_gmx.cwds()[-1]).resolve() == sandbox.resolve()

    def test_commands_without_files_run_inside_the_sandbox(self, fake_gmx, sandbox):
        assert run_gromacs_command("select", parameters=["-select", "name OW"])["result"] is True
        assert Path(fake_gmx.cwds()[0]).resolve() == sandbox.resolve()

    def test_default_folders_follow_the_sandbox(self, sandbox, monkeypatch):
        from lib.gromacs_utils import get_home, get_results_dir, get_runs_dir
        monkeypatch.delenv("GMX_MCP_HOME", raising=False)
        for d in (get_home(), get_results_dir(), get_runs_dir()):
            assert sandbox.resolve() in d.resolve().parents or d.resolve() == sandbox.resolve()

    def test_grompp_includes_outside_the_sandbox_are_refused(self, fake_gmx, sandbox, tmp_path):
        from tools.prep import run_grompp, validate_mdp
        secret = tmp_path / "secret.itp"
        secret.write_text("[ moleculetype ]\n")
        (sandbox / "a.gro").write_text("x")
        (sandbox / "ok.mdp").write_text("integrator = steep\n")
        cases = {
            "abs.top": f'#include "{secret}"\n',
            "rel.top": '#include "../secret.itp"\n',
            "nested.top": '#include "inner.itp"\n',
        }
        (sandbox / "inner.itp").write_text('#include "../secret.itp"\n')
        for name, text in cases.items():
            (sandbox / name).write_text(text)
            r = run_grompp(str(sandbox / "ok.mdp"), str(sandbox / "a.gro"), str(sandbox / name),
                           output_tpr=str(sandbox / "o.tpr"))
            assert r["status"] == "error" and "outside" in r["error"], name
            r = run_gromacs_command("grompp", {"f": sandbox / "ok.mdp", "c": sandbox / "a.gro", "p": sandbox / name})
            assert r["result"] is False and "outside" in r["error"], name
        (sandbox / "inc.mdp").write_text(f"integrator = steep\ninclude = -I{tmp_path}\n")
        (sandbox / "fine.top").write_text('#include "inner2.itp"\n')
        (sandbox / "inner2.itp").write_text("; nothing\n")
        r = validate_mdp(mdp_file=str(sandbox / "inc.mdp"), structure=str(sandbox / "a.gro"),
                         topology=str(sandbox / "fine.top"))
        assert r["status"] == "error" and "include directory" in r["error"]
        ok = run_gromacs_command("grompp", {"f": sandbox / "ok.mdp", "c": sandbox / "a.gro", "p": sandbox / "fine.top"})
        assert ok["result"] is True
        assert len(fake_gmx.calls()) == 1

    @pytest.mark.parametrize("flag", ["-p", "--p", "-P", "-f", "-n", "-r", "-t", "-e", "-po", "-pp", "-o", "-imd", "-qmi"])
    def test_grompp_files_given_as_parameters_are_refused(self, fake_gmx, sandbox, flag):
        (sandbox / "a.gro").write_text("x")
        (sandbox / "bad.top").write_text('#include "../secret.itp"\n')
        r = run_gromacs_command("grompp", {"c": sandbox / "a.gro"}, parameters=[flag, str(sandbox / "bad.top")])
        assert r["result"] is False and "input_files" in r["error"]
        assert fake_gmx.calls() == []

    def test_unreadable_include_lines_are_refused(self, sandbox):
        # what the checker cannot parse it cannot verify; gmxcpp of another version might accept it
        from lib.gromacs_utils import check_grompp_includes
        (sandbox / "inc.itp").write_text("")
        for line in ('#include junk"../secret.itp"', "#include ../secret.itp", "#include \\"):
            (sandbox / "t.top").write_text(line + "\n")
            with pytest.raises(PathNotAllowedError, match="cannot verify"):
                check_grompp_includes(topology=sandbox / "t.top", cwd=sandbox)
        for line in ('#include "inc.itp"', '\t#\tinclude\t<inc.itp"', "#include <inc.itp>"):
            (sandbox / "t.top").write_text(line + "\n")
            check_grompp_includes(topology=sandbox / "t.top", cwd=sandbox)

    def test_mdp_include_key_spelling_variants(self, sandbox, tmp_path):
        from lib.gromacs_utils import check_grompp_includes
        for key in ("include", "INCLUDE", "In_clude", "in-clude"):
            (sandbox / "x.mdp").write_text(f"{key} = -I{tmp_path}\n")
            with pytest.raises(PathNotAllowedError):
                check_grompp_includes(mdp=sandbox / "x.mdp", cwd=sandbox)

    def test_every_candidate_include_is_followed(self, sandbox, tmp_path):
        """inner.itp exists both next to the topology and in the run folder; the second one escapes."""
        from lib.gromacs_utils import check_grompp_includes
        (tmp_path / "secret.itp").write_text("x")
        sub = sandbox / "sub"
        sub.mkdir()
        (sub / "t.top").write_text('#include "inner.itp"\n')
        (sub / "inner.itp").write_text("; harmless\n")
        (sandbox / "inner.itp").write_text('#include "../secret.itp"\n')
        with pytest.raises(PathNotAllowedError):
            check_grompp_includes(topology=sub / "t.top", cwd=sandbox)

    def test_include_nesting_limit_fails_closed(self, sandbox):
        from lib.gromacs_utils import check_grompp_includes
        for i in range(20):
            (sandbox / f"l{i}.itp").write_text(f'#include "l{i + 1}.itp"\n')
        (sandbox / "l20.itp").write_text("; end\n")
        with pytest.raises(PathNotAllowedError, match="nested"):
            check_grompp_includes(topology=sandbox / "l0.itp", cwd=sandbox)

    def test_workflow_rejects_path_like_force_field(self, sandbox):
        from gromacs_workflow import run_gromacs_workflow
        (sandbox / "p.pdb").write_text("ATOM\n")
        for ff in ("../../etc/evil", "/abs/evil", "oplsaa/../x"):
            r = run_gromacs_workflow("preprocessing", {"structure": sandbox / "p.pdb"}, sandbox / "wf", force_field=ff)
            assert r["result"] is False and "plain name" in r["error"], ff

    def test_grompp_default_topology_in_the_run_folder_is_checked(self, fake_gmx, sandbox, tmp_path):
        (tmp_path / "secret.itp").write_text("x")
        (sandbox / "a.gro").write_text("x")
        (sandbox / "topol.top").write_text('#include "../secret.itp"\n')
        r = run_gromacs_command("grompp", {"c": sandbox / "a.gro"}, working_directory=str(sandbox))
        assert r["result"] is False and "outside" in r["error"]
        assert fake_gmx.calls() == []

    def test_relative_include_dirs_resolve_against_the_run_folder(self, fake_gmx, sandbox, tmp_path):
        from tools.prep import run_grompp
        deep = sandbox / "a" / "b"
        deep.mkdir(parents=True)
        # relative to the .mdp this is sandbox/a (inside); grompp runs in sandbox, where it is outside
        (deep / "inc.mdp").write_text("integrator = steep\ninclude = -I..\n")
        (sandbox / "a.gro").write_text("x")
        (sandbox / "fine.top").write_text("; nothing\n")
        r = run_grompp(str(deep / "inc.mdp"), str(sandbox / "a.gro"), str(sandbox / "fine.top"),
                       output_tpr=str(sandbox / "o.tpr"))
        assert r["status"] == "error" and "include directory" in r["error"]

    def test_unlocatable_include_is_refused(self, fake_gmx, sandbox):
        (sandbox / "a.gro").write_text("x")
        (sandbox / "ok.mdp").write_text("integrator = steep\n")
        (sandbox / "ghost.top").write_text('#include "nowhere/to/be/found.itp"\n')
        r = run_gromacs_command("grompp", {"f": sandbox / "ok.mdp", "c": sandbox / "a.gro", "p": sandbox / "ghost.top"})
        assert r["result"] is False and "could not be located" in r["error"]

    def test_dotdot_traversal_rejected(self, sandbox, tmp_path):
        (tmp_path / "x.xtc").write_bytes(b"x")
        with pytest.raises(PathNotAllowedError):
            resolve_in_workdir(sandbox / ".." / "x.xtc")

    def test_symlink_escape_rejected(self, sandbox, tmp_path):
        target = tmp_path / "outside.xtc"
        target.write_bytes(b"x")
        link = sandbox / "link.xtc"
        link.symlink_to(target)
        with pytest.raises(PathNotAllowedError):
            resolve_in_workdir(link)

    def test_tools_enforce_sandbox(self, sandbox, tmp_path):
        import server
        outside = tmp_path / "o.tpr"
        outside.write_bytes(b"x")
        r = server.analyze_tpr(str(outside))
        assert r["status"] == "error" and "outside" in r["error"]
        r = server.submit_md_simulation(str(sandbox / "none.tpr"), output_dir=str(tmp_path / "out"))
        assert r["status"] == "error"


class TestFindGromacs:
    def test_explicit_env_var_wins(self, fake_gmx):
        assert find_gromacs_command() == [os.environ["GMX_MCP_GMX"]]

    def test_clear_error_when_nothing_found(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GMX_MCP_GMX", raising=False)
        monkeypatch.setenv("PATH", str(tmp_path))
        with pytest.raises(GromacsNotFoundError) as e:
            find_gromacs_command()
        assert "gmx on PATH" in str(e.value) and "GMX_MCP_GMX" in str(e.value)

    def test_command_reports_missing_gromacs_instead_of_crashing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GMX_MCP_GMX", raising=False)
        monkeypatch.setenv("PATH", str(tmp_path))
        f = tmp_path / "a.xtc"
        f.write_bytes(b"x")
        r = run_gromacs_command("check", {"f": f})
        assert r["result"] is False and "Could not find GROMACS" in r["error"]


class TestCompactForClient:
    """What the MCP tool returns on success is shortened (TODO 13.4); library callers get everything."""

    def test_energy_stderr_keeps_only_what_matters(self, fixtures):
        from gromacs_command import strip_gmx_noise
        raw = (fixtures / "gmx_energy_success_stderr.txt").read_text()
        out = strip_gmx_noise(raw, drop_term_menu=True)
        for noise in (":-) GROMACS", "Executable:", "Command line:", "Reading energy frame", "GROMACS reminds you",
                      "Select the terms", "LJ-(SR)", "Lamb-System"):
            assert noise not in out, noise
        assert "Opened md.edr" in out and "Last energy frame read 10" in out
        assert len(out) < len(raw) / 4

    def test_menu_is_kept_unless_it_was_answered(self, fixtures):
        from gromacs_command import strip_gmx_noise
        raw = (fixtures / "gmx_energy_success_stderr.txt").read_text()
        assert "Lamb-System" in strip_gmx_noise(raw, drop_term_menu=False)

    def test_check_report_on_stderr_survives(self, fixtures):
        from gromacs_command import strip_gmx_noise
        out = strip_gmx_noise((fixtures / "gmx_check_xtc_stderr.txt").read_text())
        assert "Reading frame" not in out and "GROMACS reminds" not in out
        assert "# Atoms  1530" in out and "Last frame         50" in out and "Coords          51" in out

    def test_dump_saved_to_a_file_returns_a_preview(self, tmp_path):
        from gromacs_command import STDOUT_PREVIEW_LINES, compact_for_client
        saved = str(tmp_path / "d.txt")
        text = "\n".join(f"line {i}" for i in range(500))
        r = compact_for_client({"result": True, "stdout": text, "stderr": "", "output_files": [saved]}, "dump", False)
        lines = r["stdout"].splitlines()
        assert len(lines) == STDOUT_PREVIEW_LINES + 1 and saved in lines[-1] and "460 more lines" in lines[-1]
        # without a file to point to, nothing is cut
        r = compact_for_client({"result": True, "stdout": text, "stderr": "", "output_files": []}, "dump", False)
        assert r["stdout"] == text

    def test_failures_are_returned_whole(self, fixtures):
        from gromacs_command import compact_for_client
        raw = (fixtures / "gmx_energy_success_stderr.txt").read_text()
        failed = {"result": False, "stdout": "", "stderr": raw, "output_files": []}
        assert compact_for_client(failed, "energy", True)["stderr"] == raw

    def test_tool_response_is_compact(self, fake_gmx, edr, fixtures, monkeypatch):
        import server
        monkeypatch.setenv("FAKE_GMX_STDERR", (fixtures / "gmx_energy_success_stderr.txt").read_text())
        r = server.run_gromacs_command("energy", {"f": str(edr)}, selections=["Potential"])
        assert r["status"] == "success" and "output" not in r
        assert "Reading energy frame" not in r["stderr"] and "Last energy frame read" in r["stderr"]
