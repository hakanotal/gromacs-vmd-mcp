"""Server-level behavior: tool registration, the common result envelope for every tool,
input validation, resources and prompts. Uses the in-memory MCP client, so real tool schemas apply."""

import asyncio
import json

import pytest
from fastmcp import Client

import server
from gromacs_command import COMMANDS
from utils import setup_output_directory, validate_input_file

NONEXISTENT = "/nonexistent/path/file.tpr"

# One deliberately bad call per tool. Every tool must answer with the common envelope.
BAD_CALLS = {
    "analyze_tpr": {"input_file": NONEXISTENT},
    "run_gromacs_command": {"command_name": "rm"},
    "run_gromacs_workflow": {"workflow_type": "nope"},
    "run_grompp": {"mdp_file": NONEXISTENT, "structure": NONEXISTENT, "topology": NONEXISTENT},
    "list_workflows": {},
    "run_full_md_pipeline": {"structure": NONEXISTENT},
    "submit_md_simulation": {"input_file": NONEXISTENT},
    "submit_batch_analysis": {"input_files": [NONEXISTENT]},
    "get_job_status": {"job_id": "deadbeef"},
    "get_job_result": {"job_id": "deadbeef"},
    "get_job_log": {"job_id": "deadbeef"},
    "cancel_job": {"job_id": "deadbeef"},
    "list_jobs": {},
    "resume_job": {"job_id": "deadbeef"},
    "delete_job": {"job_id": "deadbeef"},
    "clean_jobs": {"older_than_days": 100000},
    "list_available_commands": {},
    "get_server_info": {},
    "check_environment": {},
    "get_command_help": {"command_name": "rm"},
    "create_mdp": {"stage": "bogus"},
    "validate_mdp": {},
    "run_minimization": {"structure": NONEXISTENT, "topology": NONEXISTENT},
    "extend_simulation": {"input_tpr": NONEXISTENT, "output_tpr": "/tmp/x.tpr", "extend_ps": 1},
    "list_force_fields": {},
    "read_xvg": {"path": NONEXISTENT},
    "energy_summary": {"edr_file": NONEXISTENT},
    "analyze_rdf": {"trajectory": NONEXISTENT, "structure": NONEXISTENT, "reference": "name OW"},
    "analyze_msd": {"trajectory": NONEXISTENT, "structure": NONEXISTENT, "selection": "name OW"},
    "trajectory_report": {"trajectory": NONEXISTENT},
    "plot_xvg": {"xvg_file": NONEXISTENT},
    "trjconv_preset": {"trajectory": NONEXISTENT, "structure": NONEXISTENT, "output_file": "/tmp/o.xtc", "preset": "whole"},
}


def list_tool_names():
    async def run():
        async with Client(server.mcp) as c:
            return {t.name for t in await c.list_tools()}
    return asyncio.run(run())



class TestRegistration:
    def test_registry_matches_what_the_client_sees(self):
        assert set(server.registry.all_tools()) == list_tool_names()

    def test_every_tool_has_a_bad_call_case(self):
        """Adding a tool without extending BAD_CALLS fails here on purpose."""
        assert set(BAD_CALLS) == set(server.registry.all_tools())

    def test_no_duplicate_registrations(self):
        names = server.registry.all_tools()
        assert len(names) == len(set(names))

    def test_every_tool_documents_itself(self):
        async def run():
            async with Client(server.mcp) as c:
                return await c.list_tools()
        for tool in asyncio.run(run()):
            assert tool.description and len(tool.description) > 30, tool.name

    def test_server_identity(self):
        assert server.SERVER_NAME == "gromacs"
        info = server.get_server_info()
        assert info["status"] == "success" and info["server_name"] == "gromacs"
        assert info["tool_counts"]["jobs"] >= 8


class TestEnvelope:
    @pytest.mark.parametrize("tool", sorted(BAD_CALLS))
    def test_bad_call_returns_the_envelope(self, tool, mcp_call):
        r = mcp_call(tool, **BAD_CALLS[tool])
        assert isinstance(r, dict), r
        assert r["status"] in ("success", "error")
        assert isinstance(r["warnings"], list)
        assert isinstance(r["output_files"], list)
        if r["status"] == "error":
            assert isinstance(r["error"], str) and r["error"] and r["error"] != "Unknown error", r
        else:
            assert "error" not in r

    @pytest.mark.parametrize("tool", [t for t in sorted(BAD_CALLS)
                                      if t not in ("list_workflows", "list_jobs", "clean_jobs", "list_available_commands",
                                                   "get_server_info", "check_environment", "list_force_fields")])
    def test_bad_input_is_an_error_not_a_crash(self, tool, mcp_call):
        assert mcp_call(tool, **BAD_CALLS[tool])["status"] == "error"

    def test_job_status_uses_job_status_field(self, mcp_call):
        r = mcp_call("list_jobs")
        assert r["status"] == "success" and isinstance(r["jobs"], list)


class TestValidation:
    def test_validate_input_file(self, tmp_path):
        f = tmp_path / "a.tpr"
        f.write_bytes(b"x")
        assert validate_input_file(str(f), ".tpr")["valid"] is True
        assert "not found" in validate_input_file(NONEXISTENT, ".tpr")["error"]
        assert "Expected .gro" in validate_input_file(str(f), ".gro")["error"]
        assert validate_input_file(str(tmp_path))["valid"] is False

    def test_setup_output_directory(self, tmp_path):
        r = setup_output_directory(str(tmp_path / "d"), "default")
        assert r["success"] and (tmp_path / "d").is_dir() and r["output_file"] is None
        r = setup_output_directory(str(tmp_path / "e" / "result.txt"), "default")
        assert r["success"] and r["output_file"].endswith("result.txt")

    def test_submit_validation(self, tmp_path):
        f = tmp_path / "a.tpr"
        f.write_bytes(b"x")
        assert "not found" in server.submit_md_simulation(NONEXISTENT)["error"]
        assert "pin" in server.submit_md_simulation(str(f), pin="sideways")["error"]
        assert "resume requires output_dir" in server.submit_md_simulation(str(f), resume=True)["error"]
        r = server.submit_md_simulation(str(f), resume=True, output_dir=str(tmp_path / "none"))
        assert "md.cpt not found" in r["error"]
        assert "Unsupported analysis type" in server.submit_batch_analysis([str(f)], analysis_type="trajectory")["error"]

    def test_workflow_unknown_type_lists_available(self):
        r = server.run_gromacs_workflow("nope")
        assert r["status"] == "error" and "full_md" in r["error"]

    def test_workflow_missing_inputs(self):
        r = server.run_gromacs_workflow("full_md", input_files={})
        assert r["status"] == "error" and "structure" in r["error"]

    def test_list_commands_is_generated_from_the_registry(self):
        r = server.list_available_commands()
        assert set(r["tools"]) == set(server.registry.categories)
        assert "energy" in r["gromacs_commands"] and "full_md" in r["workflow_types"]
        assert set(r["gromacs_commands_by_category"]) >= {"analysis", "prepare", "trajectory", "info"}

    def test_get_command_help_whitelist(self):
        from tools.env_tools import get_command_help
        assert get_command_help("rm")["status"] == "error"


class TestMdp:
    def test_create_mdp_returns_text_explanations_and_lint(self, tmp_path):
        from tools.prep import create_mdp
        out = tmp_path / "npt.mdp"
        r = create_mdp("npt", output_file=str(out), time_ps=10, temperature=310)
        assert r["status"] == "success" and out.exists()
        assert str(r["params"]["ref-t"]) == "310" and r["params"]["nsteps"] == 5000
        assert "tau-p" in r["explanations"] and r["mdp_text"].startswith("; npt")
        assert r["output_files"] == [str(out)]

    def test_create_mdp_reports_lint_errors(self):
        from tools.prep import create_mdp
        r = create_mdp("nvt", overrides={"tc_grps": None})
        assert r["status"] == "error" and any(i["key"] == "tc-grps" for i in r["issues"])

    def test_validate_mdp_text_only(self):
        from tools.prep import validate_mdp
        r = validate_mdp(mdp_text="integrator = steep\npcoupl = C-rescale\n")
        assert r["status"] == "success"
        assert any(i["key"] == "pcoupl" for i in r["issues"])
        assert any("pcoupl" in w for w in r["warnings"])
        assert validate_mdp(mdp_text="cutoff-scheme = group\n")["status"] == "error"


class TestResourcesAndPrompts:
    def test_job_files_listing_and_reading(self, mcp_call):
        from jobs.manager import job_manager
        script = server.SCRIPTS_DIR / "tpr_analysis.py"
        r = job_manager.submit_job(str(script), {"input": "/nonexistent.tpr"}, job_name="res")
        jid = r["job_id"]
        import time
        for _ in range(100):
            if job_manager.get_job_status(jid)["status"] in ("completed", "failed"):
                break
            time.sleep(0.1)

        async def run():
            async with Client(server.mcp) as c:
                listing = json.loads((await c.read_resource(f"gromacs://jobs/{jid}"))[0].text)
                log = (await c.read_resource(f"gromacs://jobs/{jid}/job.log"))[0].text
                return listing, log
        listing, log = asyncio.run(run())
        names = {f["name"] for f in listing["files"]}
        assert {"job.log", "metadata.json", "output.json"} <= names
        assert "nonexistent" in log or "not found" in log.lower()

    def test_resource_guards(self):
        from tools import resources
        with pytest.raises(ValueError):
            resources.job_file("deadbeef", "../x.log")
        with pytest.raises(ValueError):
            resources.job_file("not-a-job-id", "job.log")
        with pytest.raises(ValueError):
            resources.job_file("deadbeef", "job.log")  # unknown job
        with pytest.raises(ValueError):
            resources.example_file("../../etc/passwd")

    def test_binary_files_are_not_served(self):
        from tools import resources
        from jobs.manager import job_manager
        job_manager._save("feedface", {"job_id": "feedface", "status": "completed", "output_dir": None,
                                       "script": "x", "args": {}})
        (job_manager.jobs_dir / "feedface" / "md.xtc").write_bytes(b"\x00\x01")
        with pytest.raises(ValueError, match="text files"):
            resources.job_file("feedface", "md.xtc")
        (job_manager.jobs_dir / "feedface" / "big.log").write_text("x" * 500_000)
        assert "omitted" in resources.job_file("feedface", "big.log")

    def test_prompts_listed_and_render(self):
        async def run():
            async with Client(server.mcp) as c:
                names = {p.name for p in await c.list_prompts()}
                msg = await c.get_prompt("setup_water_box", {"box_nm": "2.5"})
                return names, msg
        names, msg = asyncio.run(run())
        assert names == {"setup_water_box", "equilibrate_protein", "analyze_trajectory_rmsd"}
        text = msg.messages[0].content.text
        assert "2.5 nm" in text and "run_minimization" in text and "energy_summary" in text

    def test_prompts_only_name_real_tools(self):
        """A prompt that tells the model to call a tool that does not exist is worse than none."""
        import re
        from tools import resources
        tools = set(server.registry.all_tools())
        known_commands = {"solvate", "rms", "rmsf"}
        for fn in (resources.setup_water_box, resources.equilibrate_protein, resources.analyze_trajectory_rmsd):
            text = fn()
            for name in re.findall(r"\b([a-z]+(?:_[a-z]+)+)\(", text):
                assert name in tools, f"{fn.__name__} mentions unknown tool {name}"
            for name in re.findall(r"run_gromacs_command\('(\w+)'", text):
                assert name in known_commands | set(COMMANDS)


class TestFailedJobsThroughTools:
    def test_failed_job_reason_reaches_the_client(self, mcp_call):
        import time
        from jobs.manager import job_manager
        r = job_manager.submit_job(str(server.SCRIPTS_DIR / "tpr_analysis.py"), {"input": "/nonexistent/x.tpr"})
        for _ in range(100):
            s = mcp_call("get_job_status", job_id=r["job_id"])
            if s["job_status"] in ("completed", "failed"):
                break
            time.sleep(0.1)
        assert s["status"] == "success" and s["job_status"] == "failed"
        assert s["job_error"] and "not found" in s["job_error"].lower()

    def test_default_job_folder_outside_the_sandbox_is_refused_at_submit(self, tmp_path, monkeypatch):
        box = tmp_path / "box"
        box.mkdir()
        tpr = box / "a.tpr"
        tpr.write_bytes(b"x")
        monkeypatch.setenv("GMX_MCP_WORKDIR", str(box))  # GMX_MCP_HOME (set by the tests) is outside it
        r = server.submit_md_simulation(str(tpr))
        assert r["status"] == "error" and "GMX_MCP_HOME" in r["error"]
        from tools.env_tools import check_environment
        env = check_environment()
        assert any("outside GMX_MCP_WORKDIR" in p for p in env["problems"])


class TestEnvironmentTool:
    def test_check_environment_shape(self, mcp_call):
        r = mcp_call("check_environment")
        assert "ok" in r and "problems" in r and "hardware" in r and "python" in r
        assert r["hardware"]["cpu_count"] >= 1
        assert r["disk"]["jobs_free_mb"] > 0
        assert r["configuration"]["max_concurrent_jobs"] >= 1

    @pytest.mark.gmx
    def test_check_environment_with_gromacs(self, mcp_call):
        r = mcp_call("check_environment")
        assert r["status"] == "success" and r["ok"] is True
        assert r["gromacs"]["runs"] is True and r["gromacs"]["version"]
        assert r["force_fields_installed"] >= 1
