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
    "render_movie": {"structure": NONEXISTENT, "trajectory": NONEXISTENT},
    "radius_of_gyration_over_trajectory": {"structure": NONEXISTENT, "trajectory": NONEXISTENT},
    "sasa_over_trajectory": {"structure": NONEXISTENT, "trajectory": NONEXISTENT},
    "hbonds_over_trajectory": {"structure": NONEXISTENT, "trajectory": NONEXISTENT},
    "secondary_structure_over_trajectory": {"structure": NONEXISTENT, "trajectory": NONEXISTENT},
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

    @pytest.mark.parametrize("installed", ["C:/Program Files/University of Illinois/VMD/vmd.exe",
                                           "/opt/vmd-1.9.4/bin/vmd", "/usr/local/bin/vmd"])
    def test_standard_install_locations(self, monkeypatch, tmp_path, installed):
        monkeypatch.setenv("VMD_MCP_VMD", str(tmp_path / "nope"))
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setattr("vmd.glob.glob", lambda pattern: [installed] if pattern.rstrip("*") in installed
                            or pattern.replace("*", "-1.9.4") == installed else [])
        monkeypatch.setattr("vmd.os.path.isfile", lambda p: p == installed)
        monkeypatch.setattr("vmd.os.access", lambda p, mode: p == installed)
        assert find_vmd() == [installed]

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
        # no tachyon here: VMD's internal renderer at 512 x 512, with a warning
        assert (r["width"], r["height"]) == (512, 512) and any("512 x 512" in w for w in r["warnings"])
        assert fake_vmd.calls()[0]["argv"][6:] == [str(s), str(t), "3", "internal", "0.7", str(tmp_path / "img.tga"),
                                                   "protein", "NewCartoon", "Structure"]

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
        r = server.launch_vmd_gui(str(s), str(t))
        assert r["status"] == "success"
        for _ in range(50):
            if fake_vmd.calls():
                break
            time.sleep(0.1)
        assert fake_vmd.calls()[0]["argv"] == ["-f", str(s), str(t)]
        time.sleep(0.5)
        # a closed stdin made the real VMD quit at once; the session must still be running
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


class TestRenderSizesAndScenes:
    def test_tachyon_renders_at_the_requested_size(self, fake_vmd, fake_tachyon, files, tmp_path):
        s, t = files
        out = tmp_path / "wide.tga"
        r = server.render_image(str(s), str(t), output_file=str(out), width=640, height=360)
        assert r["status"] == "success" and (r["width"], r["height"]) == (640, 360) and r["warnings"] == []
        vmd_args = fake_vmd.calls()[0]["argv"][6:]
        assert vmd_args[3:6] == ["scene", "0.7", str(out.with_suffix(".scene.dat"))]
        tach = fake_tachyon.calls()[0]
        assert tach[1:4] == ["-res", "640", "360"] and tach[tach.index("-o") + 1] == str(out)
        assert not out.with_suffix(".scene.dat").exists()  # the scene file is removed

    def test_tall_images_zoom_out(self, fake_vmd, fake_tachyon, files, tmp_path):
        # tachyon keeps the vertical field of view, so a tall image needs a smaller zoom to fit
        s, _ = files
        server.render_image(str(s), output_file=str(tmp_path / "tall.tga"), width=300, height=600)
        assert fake_vmd.calls()[0]["argv"][6 + 4] == "0.35"

    def test_several_representations(self, fake_vmd, files, tmp_path):
        s, _ = files
        reps = [{"selection": "protein", "representation": "NewCartoon", "coloring": "Structure"},
                {"selection": "resname LIG", "representation": "Licorice"}]
        r = server.render_image(str(s), output_file=str(tmp_path / "x.tga"), representations=reps)
        assert r["status"] == "success"
        assert fake_vmd.calls()[0]["argv"][12:] == ["protein", "NewCartoon", "Structure",
                                                    "resname LIG", "Licorice", "Name"]

    def test_style_with_two_representations(self, fake_vmd, files, tmp_path):
        s, _ = files
        server.render_image(str(s), output_file=str(tmp_path / "x.tga"), style="protein_ligand")
        assert fake_vmd.calls()[0]["argv"][12:15] == ["protein", "NewCartoon", "Structure"]
        assert len(fake_vmd.calls()[0]["argv"][12:]) == 6

    @pytest.mark.parametrize("reps, message", [
        ([{"selection": "a[exec id]"}], "not a plain VMD atom selection"),
        ([{"representation": "Lines;quit"}], "representation must be one of"),
        ([{"selection": "all", "color": "Name"}], "unknown keys"),
        ([{"selection": "all"}] * 9, "At most 8"),
        (["all"], "must be an object"),
    ])
    def test_representation_lists_are_checked(self, fake_vmd, files, reps, message):
        s, _ = files
        r = server.render_image(str(s), representations=reps)
        assert r["status"] == "error" and message in r["error"]
        assert fake_vmd.calls() == []

    def test_style_and_representations_together_is_an_error(self, fake_vmd, files):
        s, _ = files
        r = server.render_image(str(s), style="all_lines", representations=[{"selection": "all"}])
        assert "either style or representations" in r["error"]

    def test_size_limits(self, fake_vmd, files):
        s, _ = files
        assert "between 16 and 4096" in server.render_image(str(s), width=5)["error"]


class TestMovie:
    def test_frames_rendered_and_assembled(self, fake_vmd, fake_tachyon, fake_ffmpeg, files, tmp_path):
        s, t = files
        out = tmp_path / "m" / "run.mp4"
        r = server.render_movie(str(s), str(t), output_file=str(out), style="all_lines", stride=2,
                                rotate_degrees=2, width=321, height=240, fps=10)
        assert r["status"] == "success", r
        assert r["n_frames"] == 3 and r["output_file"] == str(out) and out.exists()
        vmd_args = fake_vmd.calls()[0]["argv"][6:]
        assert vmd_args[2:8] == ["0", "-1", "2", "2.0", "300", "scene"]
        assert len(fake_tachyon.calls()) == 3 and fake_tachyon.calls()[0][2:4] == ["322", "240"]  # even width
        ff = fake_ffmpeg.calls()[0]
        assert ff[ff.index("-framerate") + 1] == "10" and "frame_%05d.tga" in ff and ff[-1] == str(out)
        assert not list(Path(r["frames_dir"]).glob("*.dat"))

    def test_without_ffmpeg_the_frames_are_returned(self, fake_vmd, fake_tachyon, files, tmp_path):
        s, t = files
        r = server.render_movie(str(s), str(t), output_file=str(tmp_path / "m.mp4"))
        assert r["status"] == "success" and r["output_file"] == r["frames_dir"] and len(r["output_files"]) == 3
        assert any("ffmpeg not found" in w for w in r["warnings"])

    def test_frame_cap_is_reported(self, fake_vmd, fake_tachyon, files, tmp_path, monkeypatch):
        s, t = files
        monkeypatch.setenv("FAKE_VMD_TRUNCATED", "1")
        r = server.render_movie(str(s), str(t), output_file=str(tmp_path / "m.mp4"), max_frames=3)
        assert any("max_frames=3" in w for w in r["warnings"])

    @pytest.mark.parametrize("kwargs, message", [
        ({"max_frames": 5000}, "max_frames must be between 1 and 2000"),
        ({"stride": 0}, "stride must be 1 or more"),
        ({"fps": 0}, "fps must be between"),
        ({"output_file": "/tmp/x.gif"}, "must end in .mp4"),
    ])
    def test_inputs_are_checked(self, fake_vmd, files, kwargs, message):
        s, t = files
        r = server.render_movie(str(s), str(t), **kwargs)
        assert r["status"] == "error" and message in r["error"]
        assert fake_vmd.calls() == []


class TestMoreAnalyses:
    def test_radius_of_gyration(self, fake_vmd, files):
        s, t = files
        r = server.radius_of_gyration_over_trajectory(str(s), str(t), selection="protein and backbone")
        assert r["status"] == "success" and r["rgyr"] == [[0.0, 10.5], [1.0, 11.0]] and r["final"] == 11.0
        assert fake_vmd.calls()[0]["argv"][8] == "protein and backbone"

    def test_sasa_with_and_without_restriction(self, fake_vmd, files):
        s, t = files
        r = server.sasa_over_trajectory(str(s), str(t))
        assert r["status"] == "success" and r["mean"] == 1190.0 and r["unit"] == "Angstrom^2"
        assert fake_vmd.calls()[0]["argv"][9:11] == ["1.4", "none"]
        server.sasa_over_trajectory(str(s), str(t), restrict_to="resid 1 to 5", probe_radius=1.2)
        assert fake_vmd.calls()[1]["argv"][9:11] == ["1.2", "resid 1 to 5"]
        assert "restrict_to" in server.sasa_over_trajectory(str(s), str(t), restrict_to="x[1]")["error"]

    def test_hbonds(self, fake_vmd, files):
        s, t = files
        r = server.hbonds_over_trajectory(str(s), str(t), "protein", "water", distance=3.5, angle=30)
        assert r["status"] == "success" and r["hbonds"] == [[0.0, 3.0], [1.0, 4.0]]
        assert fake_vmd.calls()[0]["argv"][8:12] == ["protein", "water", "3.5", "30.0"]
        assert "distance must be" in server.hbonds_over_trajectory(str(s), str(t), distance=9)["error"]

    def test_secondary_structure(self, fake_vmd, files):
        s, t = files
        r = server.secondary_structure_over_trajectory(str(s), str(t))
        assert r["status"] == "success" and r["n_frames"] == 2 and r["n_residues"] == 6
        assert r["per_frame"][1] == [1, "CHHHEC"] and r["final"] == "CHHHEC"
        assert r["fractions"][0] == {"helix": 0.6667, "sheet": 0.0, "turn": 0.0, "coil": 0.3333}
        assert r["mean_fractions"]["sheet"] == pytest.approx(0.0833, abs=1e-4)


class TestPrompt:
    def test_prompt_names_only_real_tools(self):
        text = server.visualize_md_run("a.gro", "a.xtc")
        named = set(re.findall(r"\b([a-z]+(?:_[a-z]+)+)\b", text))
        tools = tool_names()
        assert {"render_image", "render_movie", "rmsd_over_trajectory"} <= named
        assert {n for n in named if n.endswith(("_trajectory", "_image", "_movie", "_environment"))} <= tools


class TestZoom:
    def test_zoom_multiplies_the_fit(self, fake_vmd, files, tmp_path):
        s, _ = files
        server.render_image(str(s), output_file=str(tmp_path / "x.tga"), zoom=2)
        assert fake_vmd.calls()[0]["argv"][6 + 4] == "1.4"
        assert "zoom must be between" in server.render_image(str(s), zoom=50)["error"]
