"""Tools, safety rules and the result envelope, against a fake vmd."""

import asyncio
import os
import re
import sys
import time
from pathlib import Path

import pytest
from fastmcp import Client

import server
from vmd import PathNotAllowedError, check_selection, find_vmd, parse_series, resolve_in_workdir

NONEXISTENT = "/nonexistent/conf.gro"

BAD_CALLS = {
    "check_vmd_environment": {},
    "list_templates": {},
    "render_image": {"structure": NONEXISTENT},
    "rmsd_over_trajectory": {"structure": NONEXISTENT, "trajectory": NONEXISTENT},
    "contacts_over_trajectory": {"structure": NONEXISTENT, "trajectory": NONEXISTENT,
                                 "selection1": "protein", "selection2": "water"},
    "launch_vmd_gui": {"structure": NONEXISTENT},
    "list_vmd_sessions": {},
    "close_vmd_session": {"pid": 1},
    "run_vmd_tcl": {"script": "puts hi"},
}


def call(name, **args):
    async def run():
        async with Client(server.mcp) as c:
            return (await c.call_tool(name, args, raise_on_error=False)).structured_content
    return asyncio.run(run())


def tool_names():
    async def run():
        async with Client(server.mcp) as c:
            return {t.name for t in await c.list_tools()}
    return asyncio.run(run())


class TestRegistration:
    def test_every_tool_has_a_bad_call_case(self):
        assert tool_names() == set(BAD_CALLS)

    def test_tool_names_are_accepted_by_clients(self):
        # the prototype used Chinese tool names, which Claude rejects
        assert all(re.match(r"^[a-zA-Z0-9_-]{1,64}$", n) for n in tool_names())

    @pytest.mark.parametrize("tool", sorted(BAD_CALLS))
    def test_every_tool_returns_the_envelope(self, tool, monkeypatch, tmp_path):
        monkeypatch.setenv("VMD_MCP_VMD", str(tmp_path / "no_vmd_here"))
        monkeypatch.setenv("PATH", "/nonexistent")
        r = call(tool, **BAD_CALLS[tool])
        assert r["status"] in ("success", "error")
        assert isinstance(r["warnings"], list) and isinstance(r["output_files"], list)
        if r["status"] == "error":
            assert r["error"]


class TestFindVmd:
    def test_explicit_path(self, fake_vmd):
        assert find_vmd() == [str(fake_vmd.path)]

    def test_not_found_lists_what_was_tried(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VMD_MCP_VMD", str(tmp_path / "nope"))
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setattr("vmd.glob.glob", lambda pattern: [])
        monkeypatch.setattr("vmd.os.access", lambda p, mode: False)
        with pytest.raises(Exception, match="Tried: .*VMD_MCP_VMD.*vmd on PATH"):
            find_vmd()

    def test_environment_check(self, fake_vmd):
        r = server.check_vmd_environment()
        assert r["status"] == "success" and r["vmd_version"] == "1.9.4a57" and r["vmd"] == str(fake_vmd.path)


class TestNoCodeFromValues:
    def test_argv_shape_and_values_only_in_args(self, fake_vmd, files):
        s, t = files
        r = server.rmsd_over_trajectory(str(s), str(t), selection="backbone and resid 1 to 10")
        assert r["status"] == "success", r
        argv = fake_vmd.calls()[0]["argv"]
        assert argv[:4] == ["-dispdev", "text", "-eofexit", "-e"] and argv[5] == "-args"
        assert argv[6:] == [str(s), str(t), "backbone and resid 1 to 10", "0", "1", r["output_file"]]
        script = fake_vmd.calls()[0]["script"]
        assert str(s) not in script and "backbone" not in script  # the script is fixed text

    @pytest.mark.parametrize("bad", ['protein"; exec touch /tmp/x; "', "[exec id]", "resid $x", "a } b",
                                     "a \\ b", "a;b", "a\nb", ""])
    def test_selections_with_tcl_syntax_are_refused(self, fake_vmd, files, bad):
        s, t = files
        r = server.rmsd_over_trajectory(str(s), str(t), selection=bad)
        assert r["status"] == "error" and "not a plain VMD atom selection" in r["error"]
        assert fake_vmd.calls() == []

    def test_selection_language_still_works(self):
        for ok in ("protein and name CA", "resname SOL and within 5 of protein", "x < 10 and y >= -2.5",
                   "name 'C1*'", "not (water or ions)", "index 1 to 20"):
            assert check_selection(ok) == ok

    def test_file_names_with_shell_and_tcl_characters_stay_data(self, fake_vmd, tmp_path):
        d = tmp_path / "a $(id) [exec ls] {x}"
        d.mkdir()
        s, t = d / "c;b.gro", d / "t`x`.xtc"
        s.write_text("x"); t.write_bytes(b"x")
        r = server.rmsd_over_trajectory(str(s), str(t))
        assert r["status"] == "success"
        call0 = fake_vmd.calls()[0]
        assert call0["argv"][6:8] == [str(s), str(t)] and "exec ls" not in call0["script"]

    def test_representation_and_coloring_are_from_a_list(self, fake_vmd, files):
        s, _ = files
        assert "representation must be one of" in server.render_image(str(s), representation="Lines; quit")["error"]
        assert "coloring must be one of" in server.render_image(str(s), coloring="ColorID 1")["error"]
        assert "Unknown style" in server.render_image(str(s), style="nope")["error"]
        assert fake_vmd.calls() == []


class TestSandbox:
    def test_paths_outside_are_refused(self, fake_vmd, files, tmp_path, monkeypatch):
        s, t = files
        box = tmp_path / "box"
        box.mkdir()
        monkeypatch.setenv("VMD_MCP_WORKDIR", str(box))
        r = server.rmsd_over_trajectory(str(s), str(t))
        assert r["status"] == "error" and "outside the allowed directory" in r["error"]
        inside = box / "c.gro"
        inside.write_text("x")
        assert "outside" in server.render_image(str(inside), output_file=str(tmp_path / "o.tga"))["error"]
        assert "outside" in server.launch_vmd_gui(str(s))["error"]
        assert fake_vmd.calls() == []

    def test_symlink_escape(self, tmp_path, monkeypatch):
        box = tmp_path / "box"
        box.mkdir()
        (box / "link").symlink_to(tmp_path)
        monkeypatch.setenv("VMD_MCP_WORKDIR", str(box))
        with pytest.raises(PathNotAllowedError):
            resolve_in_workdir(box / "link" / "x.gro")

    def test_default_outputs_follow_the_sandbox(self, fake_vmd, tmp_path, monkeypatch):
        box = tmp_path / "box"
        box.mkdir()
        (box / "c.gro").write_text("x"); (box / "t.xtc").write_bytes(b"x")
        monkeypatch.setenv("VMD_MCP_WORKDIR", str(box))
        monkeypatch.delenv("VMD_MCP_HOME")
        r = server.contacts_over_trajectory(str(box / "c.gro"), str(box / "t.xtc"), "protein", "water")
        assert r["status"] == "success", r
        assert Path(r["output_file"]).resolve().is_relative_to(box.resolve())


class TestAnalysis:
    def test_rmsd_numbers(self, fake_vmd, files, tmp_path):
        s, t = files
        r = server.rmsd_over_trajectory(str(s), str(t), output_file=str(tmp_path / "r.dat"), align=False)
        assert r["status"] == "success" and r["n_frames"] == 3 and r["max"] == 1.75 and r["final"] == 1.75
        assert r["rmsd"][1] == [1.0, 1.25] and r["output_files"] == [str(tmp_path / "r.dat")]
        assert fake_vmd.calls()[0]["argv"][-2] == "0"  # align off

    def test_contacts_numbers(self, fake_vmd, files):
        s, t = files
        r = server.contacts_over_trajectory(str(s), str(t), "protein", "resname LIG", cutoff=4.0)
        assert r["status"] == "success" and r["contacts"] == [[0.0, 4.0], [1.0, 6.0], [2.0, 5.0]] and r["max"] == 6
        assert "4.0" in fake_vmd.calls()[0]["argv"]

    def test_vmd_errors_are_reported(self, fake_vmd, files, monkeypatch):
        s, t = files
        monkeypatch.setenv("FAKE_VMD_WRITE", "0")
        monkeypatch.setenv("FAKE_VMD_STDOUT", "ERROR) Unable to load file conf.gro")
        r = server.rmsd_over_trajectory(str(s), str(t))
        assert r["status"] == "error" and "Unable to load file" in r["error"]

    def test_start_up_noise_is_not_an_error(self, fake_vmd, files, monkeypatch):
        # VMD 2.0 prints these from its own start-up files on every run (seen with 2.0.0a7)
        s, t = files
        monkeypatch.setenv("FAKE_VMD_PRESTART", 'ERROR) Duplicate resname "4YS"')
        assert server.rmsd_over_trajectory(str(s), str(t))["status"] == "success"
        monkeypatch.setenv("FAKE_VMD_STDOUT", "ERROR) Unable to load file")
        assert server.rmsd_over_trajectory(str(s), str(t))["status"] == "error"

    def test_parse_series(self):
        assert parse_series("# c\n0 1.5\nbad\n1 2\n") == [[0.0, 1.5], [1.0, 2.0]]


class TestRender:
    def test_tga_and_style(self, fake_vmd, files, tmp_path):
        s, t = files
        r = server.render_image(str(s), str(t), output_file=str(tmp_path / "img.tga"), style="protein_cartoon",
                                frame=3)
        assert r["status"] == "success" and r["format"] == "tga" and Path(r["output_file"]).exists()
        assert fake_vmd.calls()[0]["argv"][6:] == [str(s), str(t), "3", "protein", "NewCartoon", "Structure",
                                                   str(tmp_path / "img.tga")]

    def test_png_needs_pillow(self, fake_vmd, files, tmp_path):
        s, _ = files
        r = server.render_image(str(s), output_file=str(tmp_path / "img.png"))
        assert r["status"] == "success"
        try:
            import PIL  # noqa: F401
            assert r["format"] == "png" and (tmp_path / "img.png").exists() and not (tmp_path / "img.tga").exists()
        except ImportError:
            assert r["format"] == "tga" and any("Pillow" in w for w in r["warnings"])

    def test_bad_suffix(self, fake_vmd, files, tmp_path):
        s, _ = files
        assert ".png or .tga" in server.render_image(str(s), output_file=str(tmp_path / "x.jpg"))["error"]


class TestSessionsAndTcl:
    def test_launch_list_close(self, fake_vmd, files, monkeypatch):
        s, t = files
        monkeypatch.setenv("FAKE_VMD_SLEEP", "30")
        r = server.launch_vmd_gui(str(s), str(t))
        assert r["status"] == "success"
        for _ in range(50):
            if fake_vmd.calls():
                break
            time.sleep(0.1)
        assert fake_vmd.calls()[0]["argv"] == ["-f", str(s), str(t)]
        listed = server.list_vmd_sessions()["sessions"]
        assert [x["pid"] for x in listed if x["running"]] == [r["pid"]]
        assert server.close_vmd_session(r["pid"])["status"] == "success"
        assert server.list_vmd_sessions()["total"] == 0

    def test_foreign_pids_are_never_signalled(self):
        r = server.close_vmd_session(os.getpid())
        assert r["status"] == "error" and "was started by this server" in r["error"]

    def test_tcl_is_off_by_default(self, fake_vmd):
        r = server.run_vmd_tcl("exec touch /tmp/should_not_exist")
        assert r["status"] == "error" and "disabled" in r["error"] and fake_vmd.calls() == []

    def test_tcl_when_enabled(self, fake_vmd, monkeypatch):
        monkeypatch.setenv("VMD_MCP_ALLOW_TCL", "1")
        r = server.run_vmd_tcl("puts hello")
        assert r["status"] == "success" and "puts hello" in fake_vmd.calls()[0]["script"]
