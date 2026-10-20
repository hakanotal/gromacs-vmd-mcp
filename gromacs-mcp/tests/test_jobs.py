"""JobManager: lifecycle, cancel, queueing, recovery after the manager is gone, housekeeping."""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jobs.manager import JobManager, _arg_flags, _pid_alive

FAKE_JOB = '''
import argparse, json, sys, time
ap = argparse.ArgumentParser()
ap.add_argument("--result-json")
ap.add_argument("--output-dir")
ap.add_argument("--sleep", type=float, default=0.0)
ap.add_argument("--fail", action="store_true")
ap.add_argument("--resume", action="store_true")
ap.add_argument("--tag", default="")
ap.add_argument("--count-file")
ap.add_argument("--stopped", action="store_true")
a, _ = ap.parse_known_args()
if a.count_file:
    open(a.count_file, "a").write("ran\\n")
time.sleep(a.sleep)
res = {"result": not a.fail, "argv": sys.argv[1:], "output_files": ["x.txt"]}
if a.fail:
    res["error"] = "scripted failure"
if a.stopped:
    res.update({"stopped": True, "stop_reason": "maxh=0.01 h", "progress": {"step": 300, "nsteps": 1000}})
if a.result_json:
    open(a.result_json, "w").write(json.dumps(res))
sys.exit(1 if a.fail else 0)
'''


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "fake_job.py"
    p.write_text(FAKE_JOB)
    return str(p)


@pytest.fixture
def md_script(tmp_path):
    """Same behavior, but named like the MD script so resume_job accepts it."""
    d = tmp_path / "scripts"
    d.mkdir()
    p = d / "md_simulation.py"
    p.write_text(FAKE_JOB)
    return str(p)


@pytest.fixture
def jm(tmp_path):
    manager = JobManager(jobs_dir=tmp_path / "jobs", max_concurrent=2)
    yield manager
    for j in manager.list_jobs()["jobs"]:
        if j["status"] in ("queued", "running"):
            manager.cancel_job(j["job_id"])
    time.sleep(0.2)


def wait_for(jm, job_id, states=("completed", "stopped", "failed", "cancelled"), timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        s = jm.get_job_status(job_id)
        if s["status"] in states:
            return s
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} stuck in {jm.get_job_status(job_id)['status']}")


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def spawn_sleeper(seconds=60):
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"], start_new_session=True)


def write_orphan(jm, job_id, status="running", pid=None, owner=None, output_dir=None, script="x.py", args=None):
    meta = {
        "job_id": job_id, "job_name": job_id, "kind": None, "script": script, "args": args or {},
        "output_dir": str(output_dir) if output_dir else None, "status": status,
        "submitted_at": datetime.now().isoformat(),
        "started_at": datetime.now().isoformat() if status == "running" else None,
        "completed_at": None, "error": None, "pid": pid,
        "owner_pid": owner if owner is not None else dead_pid(), "cancel_requested": False,
    }
    jm._save(job_id, meta)
    return meta


class TestArgFlags:
    def test_flag_building(self):
        assert _arg_flags({"a_b": 1, "c": True, "d": False, "e": None, "f": [1, 2], "g": "x y"}) == [
            "--a-b", "1", "--c", "--f", "1", "--f", "2", "--g", "x y"]

    def test_list_items_with_commas_stay_whole(self):
        assert _arg_flags({"inputs": ["/sb/a,/etc/x.tpr", "/sb/b.tpr"]}) == [
            "--inputs", "/sb/a,/etc/x.tpr", "--inputs", "/sb/b.tpr"]


class TestLifecycle:
    def test_completes_without_mamba_on_path(self, jm, script, monkeypatch):
        monkeypatch.setenv("PATH", "/nonexistent")
        r = jm.submit_job(script, {"sleep": 0.05, "tag": "t"}, job_name="ok")
        assert r["status"] == "submitted"
        s = wait_for(jm, r["job_id"])
        assert s["status"] == "completed", s
        res = jm.get_job_result(r["job_id"])
        assert res["status"] == "success" and res["result"]["result"] is True
        assert "--tag" in res["result"]["argv"] and "--result-json" in res["result"]["argv"]

    def test_failed_job_reports_the_script_error(self, jm, script):
        r = jm.submit_job(script, {"fail": True})
        s = wait_for(jm, r["job_id"])
        assert s["status"] == "failed" and s["error"] == "scripted failure"
        res = jm.get_job_result(r["job_id"])
        assert res["status"] == "error" and res["error"] == "scripted failure"
        assert res["result"]["error"] == "scripted failure"

    def test_run_that_stopped_early_is_stopped(self, jm, md_script, tmp_path):
        # TODO 12.27: maxh used to give "completed" here but "failed" after a server restart
        out = tmp_path / "run"
        r = jm.submit_job(md_script, {"stopped": True}, output_dir=str(out), kind="md")
        s = wait_for(jm, r["job_id"])
        assert s["status"] == "stopped" and "error" not in s
        assert "step 300 of 1000" in s["stop_reason"] and "maxh" in s["stop_reason"]
        assert jm.get_job_result(r["job_id"])["status"] == "success"
        assert r["job_id"] in [j["job_id"] for j in jm.list_jobs("stopped")["jobs"]]
        (out / "md.cpt").write_text("cpt")
        (out / "md.tpr").write_text("tpr")
        assert jm.resume_job(r["job_id"])["status"] == "submitted"

    def test_cancel_wins_over_a_stopped_result(self, jm):
        # a cancelled mdrun also ends cleanly with a checkpoint; the cancel decides the state
        stopped = json.dumps({"result": True, "stopped": True, "stop_reason": "maxh=1 h"})
        write_orphan(jm, "abcd0001", pid=dead_pid())
        (jm.jobs_dir / "abcd0001" / "output.json").write_text(stopped)
        jm._cancelled.add("abcd0001")
        jm._finalize("abcd0001", 0)
        assert jm._load("abcd0001")["status"] == "cancelled"
        write_orphan(jm, "abcd0002", pid=dead_pid())
        (jm.jobs_dir / "abcd0002" / "output.json").write_text(stopped)
        jm._update("abcd0002", cancel_requested=True)
        assert jm.get_job_status("abcd0002")["status"] == "cancelled"
        write_orphan(jm, "abcd0003", pid=dead_pid())
        (jm.jobs_dir / "abcd0003" / "output.json").write_text(stopped)
        assert jm.get_job_status("abcd0003")["status"] == "stopped"

    def test_status_fields(self, jm, script):
        r = jm.submit_job(script, {}, job_name="named")
        s = wait_for(jm, r["job_id"])
        assert s["job_name"] == "named" and s["submitted_at"] and s["completed_at"]
        assert s["duration_seconds"] is not None and s["duration_seconds"] >= 0

    def test_unknown_job(self, jm):
        for call in (jm.get_job_status, jm.get_job_result, jm.get_job_log, jm.cancel_job, jm.delete_job, jm.resume_job):
            assert call("nope0000")["status"] == "error"

    def test_result_before_completion(self, jm, script):
        r = jm.submit_job(script, {"sleep": 5})
        res = jm.get_job_result(r["job_id"])
        assert res["status"] == "error" and "not completed" in res["error"]

    def test_default_output_dir_and_explicit(self, jm, script, tmp_path):
        r = jm.submit_job(script, {}, default_output_subdir="run")
        assert r["output_dir"].endswith(os.path.join(r["job_id"], "run"))
        r2 = jm.submit_job(script, {}, output_dir=str(tmp_path / "mine"))
        assert r2["output_dir"] == str(tmp_path / "mine")
        res = wait_for(jm, r2["job_id"])
        assert res["output_dir"] == str(tmp_path / "mine")
        assert "--output-dir" in jm.get_job_result(r2["job_id"])["result"]["argv"]

    def test_refuses_when_disk_is_low(self, jm, script):
        jm.min_free_mb = 10 ** 12
        r = jm.submit_job(script, {})
        assert r["status"] == "error" and "free" in r["error"]


class TestCancel:
    def test_cancel_wins_over_exit_code(self, jm, script):
        r = jm.submit_job(script, {"sleep": 30})
        wait_for(jm, r["job_id"], states=("running",))
        c = jm.cancel_job(r["job_id"])
        assert c["status"] == "success"
        s = wait_for(jm, r["job_id"], states=("cancelled", "failed", "completed"))
        assert s["status"] == "cancelled"
        time.sleep(1.0)  # the worker thread finishes after the process dies: it must not overwrite
        assert jm.get_job_status(r["job_id"])["status"] == "cancelled"

    def test_cancel_kills_the_process(self, jm, script):
        r = jm.submit_job(script, {"sleep": 30})
        s = wait_for(jm, r["job_id"], states=("running",))
        pid = jm._load(r["job_id"])["pid"]
        jm.cancel_job(r["job_id"])
        wait_for(jm, r["job_id"], states=("cancelled",))
        deadline = time.time() + 5
        while _pid_alive(pid) and time.time() < deadline:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            time.sleep(0.1)
        assert not _pid_alive(pid)

    def test_cancel_finished_job_is_an_error(self, jm, script):
        r = jm.submit_job(script, {})
        wait_for(jm, r["job_id"])
        assert jm.cancel_job(r["job_id"])["status"] == "error"

    def test_cancel_queued_job(self, tmp_path, script):
        jm = JobManager(jobs_dir=tmp_path / "jobs", max_concurrent=1)
        a = jm.submit_job(script, {"sleep": 30})
        b = jm.submit_job(script, {"sleep": 0})
        wait_for(jm, a["job_id"], states=("running",))
        s = jm.get_job_status(b["job_id"])
        assert s["status"] == "queued" and s["queue_position"] == 1
        assert jm.cancel_job(b["job_id"])["status"] == "success"
        assert jm.get_job_status(b["job_id"])["status"] == "cancelled"
        jm.cancel_job(a["job_id"])
        wait_for(jm, a["job_id"], states=("cancelled",))
        time.sleep(0.3)
        assert jm.get_job_status(b["job_id"])["status"] == "cancelled"  # never started afterwards


class TestQueue:
    def test_concurrency_cap(self, tmp_path, script):
        jm = JobManager(jobs_dir=tmp_path / "jobs", max_concurrent=2)
        ids = [jm.submit_job(script, {"sleep": 0.6})["job_id"] for _ in range(5)]
        peak = 0
        end = time.time() + 20
        while time.time() < end:
            statuses = [jm.get_job_status(i)["status"] for i in ids]
            peak = max(peak, statuses.count("running"))
            if all(s == "completed" for s in statuses):
                break
            time.sleep(0.05)
        assert all(jm.get_job_status(i)["status"] == "completed" for i in ids)
        assert peak == 2

    def test_next_job_starts_after_a_slot_frees(self, tmp_path, script):
        jm = JobManager(jobs_dir=tmp_path / "jobs", max_concurrent=1)
        a = jm.submit_job(script, {"sleep": 0.5})["job_id"]
        b = jm.submit_job(script, {})["job_id"]
        sa, sb = wait_for(jm, a), wait_for(jm, b)
        assert sb["started_at"] >= sa["completed_at"]


class TestRecovery:
    def test_finished_process_with_successful_result_becomes_completed(self, jm, tmp_path):
        write_orphan(jm, "aaaa0001", pid=dead_pid())
        (jm.jobs_dir / "aaaa0001" / "output.json").write_text(json.dumps({"result": True}))
        jm.recover_orphans()
        assert jm.get_job_status("aaaa0001")["status"] == "completed"

    def test_finished_process_with_failed_result_keeps_its_error(self, jm):
        write_orphan(jm, "aaaa0002", pid=dead_pid())
        (jm.jobs_dir / "aaaa0002" / "output.json").write_text(json.dumps({"result": False, "error": "mdrun blew up"}))
        jm.recover_orphans()
        s = jm.get_job_status("aaaa0002")
        assert s["status"] == "failed" and s["error"] == "mdrun blew up"

    def test_finished_mdrun_in_log_means_completed(self, jm, tmp_path, fixtures):
        out = tmp_path / "run"
        out.mkdir()
        (out / "md.log").write_text((fixtures / "md_log_full.txt").read_text())
        write_orphan(jm, "aaaa0003", pid=dead_pid(), output_dir=out)
        jm.recover_orphans()
        s = jm.get_job_status("aaaa0003")
        assert s["status"] == "completed"
        assert s["progress"]["finished"] is True

    def test_no_evidence_means_failed_with_explanation(self, jm):
        write_orphan(jm, "aaaa0004", pid=dead_pid())
        jm.recover_orphans()
        s = jm.get_job_status("aaaa0004")
        assert s["status"] == "failed" and "without a result" in s["error"]

    def test_alive_process_is_never_touched(self, jm):
        sleeper = spawn_sleeper()
        try:
            write_orphan(jm, "aaaa0005", pid=sleeper.pid)
            fresh = JobManager(jobs_dir=jm.jobs_dir)  # a new server instance starting up
            assert fresh.get_job_status("aaaa0005")["status"] == "running"
            assert _pid_alive(sleeper.pid)
        finally:
            sleeper.kill(); sleeper.wait()

    def test_alive_job_turns_failed_once_it_dies_without_evidence(self, jm):
        sleeper = spawn_sleeper()
        write_orphan(jm, "aaaa0006", pid=sleeper.pid)
        fresh = JobManager(jobs_dir=jm.jobs_dir)
        assert fresh.get_job_status("aaaa0006")["status"] == "running"
        sleeper.kill(); sleeper.wait()
        assert fresh.get_job_status("aaaa0006")["status"] == "failed"

    def test_alive_orphan_can_be_cancelled(self, jm):
        sleeper = spawn_sleeper()
        try:
            write_orphan(jm, "aaaa0007", pid=sleeper.pid)
            fresh = JobManager(jobs_dir=jm.jobs_dir)
            assert fresh.cancel_job("aaaa0007")["status"] == "success"
            sleeper.wait(timeout=10)
            assert fresh.get_job_status("aaaa0007")["status"] == "cancelled"
        finally:
            if sleeper.poll() is None:
                sleeper.kill(); sleeper.wait()

    def test_queued_orphan_is_requeued_and_runs(self, jm, script):
        write_orphan(jm, "aaaa0008", status="queued", script=script, args={"tag": "recovered"})
        fresh = JobManager(jobs_dir=jm.jobs_dir)
        s = wait_for(fresh, "aaaa0008")
        assert s["status"] == "completed"
        assert "recovered" in fresh.get_job_result("aaaa0008")["result"]["argv"]

    def test_job_owned_by_another_live_instance_is_left_alone(self, jm, script):
        write_orphan(jm, "aaaa0009", status="queued", script=script, owner=os.getppid())
        fresh = JobManager(jobs_dir=jm.jobs_dir)
        time.sleep(0.5)
        assert fresh.get_job_status("aaaa0009")["status"] == "queued"

    def test_corrupt_metadata_is_skipped(self, jm):
        d = jm.jobs_dir / "bbbb0001"
        d.mkdir()
        (d / "metadata.json").write_text("{not json")
        JobManager(jobs_dir=jm.jobs_dir)
        assert jm.list_jobs()["total"] == 0


class TestProgressAndLogs:
    def test_running_job_has_progress_and_eta(self, jm, tmp_path, fixtures):
        text = (fixtures / "md_log_full.txt").read_text()
        mid = text[: text.index("Finished mdrun")]
        mid = mid[: mid.index("           Step           Time\n          25000")] if "          25000       50.00000" in mid else mid
        out = tmp_path / "run"
        out.mkdir()
        (out / "md.log").write_text(mid)
        sleeper = spawn_sleeper()
        try:
            meta = write_orphan(jm, "cccc0001", pid=sleeper.pid, output_dir=out)
            jm._update("cccc0001", started_at=(datetime.now() - timedelta(seconds=100)).isoformat())
            s = jm.get_job_status("cccc0001")
            p = s["progress"]
            assert s["status"] == "running" and 0 < p["percent"] < 100
            assert p["eta_seconds"] > 0 and not p["finished"]
        finally:
            sleeper.kill(); sleeper.wait()

    def test_log_merges_job_and_md_log_tails(self, jm, script, tmp_path, fixtures):
        out = tmp_path / "run"
        out.mkdir()
        (out / "md.log").write_text("".join(f"mdlog {i}\n" for i in range(100)))
        r = jm.submit_job(script, {}, output_dir=str(out))
        wait_for(jm, r["job_id"])
        log = jm.get_job_log(r["job_id"], tail=5)
        assert log["status"] == "success" and "log_lines" in log
        assert [l.strip() for l in log["md_log_lines"]][-1] == "mdlog 99" and len(log["md_log_lines"]) == 5
        assert "md_log_lines" not in jm.get_job_log(r["job_id"], tail=5, include_md_log=False)


class TestHousekeeping:
    def test_list_sorted_newest_first_and_filter(self, jm, script):
        a = jm.submit_job(script, {})["job_id"]
        time.sleep(0.01)
        b = jm.submit_job(script, {"fail": True})["job_id"]
        wait_for(jm, a); wait_for(jm, b)
        jobs = jm.list_jobs()["jobs"]
        assert [j["job_id"] for j in jobs[:2]] == [b, a]
        assert [j["job_id"] for j in jm.list_jobs("failed")["jobs"]] == [b]

    def test_delete_requires_finished_and_keeps_outputs(self, jm, script, tmp_path):
        out = tmp_path / "keep"
        out.mkdir()
        (out / "result.txt").write_text("data")
        r = jm.submit_job(script, {"sleep": 5}, output_dir=str(out))
        assert jm.delete_job(r["job_id"])["status"] == "error"
        jm.cancel_job(r["job_id"])
        wait_for(jm, r["job_id"], states=("cancelled",))
        assert jm.delete_job(r["job_id"])["status"] == "success"
        assert jm.get_job_status(r["job_id"])["status"] == "error"
        assert (out / "result.txt").exists()

    def test_clean_jobs_by_age_and_keep_last(self, jm, script):
        ids = [jm.submit_job(script, {})["job_id"] for _ in range(3)]
        for i in ids:
            wait_for(jm, i)
        old = (datetime.now() - timedelta(days=30)).isoformat()
        for i in ids[:2]:
            jm._update(i, submitted_at=old)
        res = jm.clean_jobs(older_than_days=7)
        assert sorted(res["removed"]) == sorted(ids[:2])
        assert [j["job_id"] for j in jm.list_jobs()["jobs"]] == [ids[2]]
        assert jm.clean_jobs(older_than_days=None, keep_last=1)["removed"] == []

    def test_metadata_writes_are_atomic_under_threads(self, jm, script):
        r = jm.submit_job(script, {})
        wait_for(jm, r["job_id"])
        errors = []

        def hammer(n):
            try:
                for i in range(50):
                    jm._update(r["job_id"], **{f"k{n}": i})
                    assert jm._load(r["job_id"]) is not None
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
        [t.start() for t in threads]; [t.join() for t in threads]
        assert not errors
        meta = jm._load(r["job_id"])
        assert all(meta[f"k{n}"] == 49 for n in range(8))
        assert not list((jm.jobs_dir / r["job_id"]).glob("*.tmp"))


class TestResume:
    def make_finished_md_job(self, jm, md_script, tmp_path, with_checkpoint=True):
        out = tmp_path / "mdrun"
        out.mkdir()
        if with_checkpoint:
            (out / "md.cpt").write_bytes(b"cpt")
            (out / "md.tpr").write_bytes(b"tpr")
        r = jm.submit_job(md_script, {"input": "orig.tpr", "nsteps": 5000, "ntomp": 2}, output_dir=str(out), kind="md")
        wait_for(jm, r["job_id"])
        return r["job_id"], out

    def test_resume_submits_with_checkpoint_flags(self, jm, md_script, tmp_path):
        jid, out = self.make_finished_md_job(jm, md_script, tmp_path)
        r = jm.resume_job(jid)
        assert r["status"] == "submitted"
        wait_for(jm, r["job_id"])
        argv = jm.get_job_result(r["job_id"])["result"]["argv"]
        assert "--resume" in argv
        assert argv[argv.index("--input") + 1] == str(out / "md.tpr")
        assert "--ntomp" in argv
        assert "--nsteps" not in argv  # md.tpr in the run folder already has the step count
        assert argv[argv.index("--output-dir") + 1] == str(out)

    def test_resume_needs_checkpoint(self, jm, md_script, tmp_path):
        jid, _ = self.make_finished_md_job(jm, md_script, tmp_path, with_checkpoint=False)
        r = jm.resume_job(jid)
        assert r["status"] == "error" and "md.cpt" in r["error"]

    def test_resume_only_for_md_jobs(self, jm, script):
        r = jm.submit_job(script, {})
        wait_for(jm, r["job_id"])
        assert "Only MD" in jm.resume_job(r["job_id"])["error"]

    def test_resume_needs_finished_job(self, jm, md_script, tmp_path):
        r = jm.submit_job(md_script, {"sleep": 5}, output_dir=str(tmp_path / "o"))
        assert "needs a finished job" in jm.resume_job(r["job_id"])["error"]


class TestReviewRegressions:
    """Defects found in review; each test reproduces the original failure."""

    def test_delete_never_removes_outputs(self, jm, script):
        r = jm.submit_job(script, {}, default_output_subdir="run")
        out = Path(r["output_dir"])
        assert jm.jobs_dir not in out.parents  # outputs live outside the record folder
        wait_for(jm, r["job_id"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "md.xtc").write_bytes(b"trajectory")
        assert jm.delete_job(r["job_id"])["status"] == "success"
        assert (out / "md.xtc").exists()

    def test_delete_keeps_outputs_of_old_jobs_inside_the_record(self, jm, script):
        r = jm.submit_job(script, {})
        wait_for(jm, r["job_id"])
        legacy = jm.jobs_dir / r["job_id"] / "run"
        legacy.mkdir()
        (legacy / "md.xtc").write_bytes(b"trajectory")
        res = jm.delete_job(r["job_id"])
        assert res["status"] == "success" and "kept" in res["message"]
        assert (legacy / "md.xtc").exists() and not (jm.jobs_dir / r["job_id"] / "metadata.json").exists()

    def test_output_dir_used_by_an_unfinished_job_is_refused(self, jm, script, tmp_path):
        out = tmp_path / "shared"
        a = jm.submit_job(script, {"sleep": 30}, output_dir=str(out))
        b = jm.submit_job(script, {}, output_dir=str(out))
        assert b["status"] == "error" and a["job_id"] in b["error"]
        jm.cancel_job(a["job_id"])
        wait_for(jm, a["job_id"], states=("cancelled",))
        assert jm.submit_job(script, {}, output_dir=str(out))["status"] == "submitted"

    def test_relative_output_dir_is_made_absolute(self, jm, script, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        r = jm.submit_job(script, {}, output_dir="rel_run")
        assert r["output_dir"] == str((tmp_path / "rel_run").resolve())

    def test_orphaned_queued_job_runs_once_across_processes(self, tmp_path, script):
        jobs = tmp_path / "jobs"
        seed = JobManager(jobs_dir=jobs)
        counter = tmp_path / "runs.txt"
        write_orphan(seed, "dddd0001", status="queued", script=script,
                     args={"sleep": 0.5, "tag": "once", "count_file": str(counter)})
        code = (
            "import sys, time; sys.path[:0] = [%r, %r]\n"
            "from jobs.manager import JobManager\n"
            "m = JobManager(jobs_dir=%r)\n"
            "time.sleep(3)\n" % (str(Path(__file__).parent.parent / "src"), str(Path(__file__).parent.parent / "scripts"),
                                 str(jobs)))
        procs = [subprocess.Popen([sys.executable, "-c", code]) for _ in range(3)]
        for p in procs:
            p.wait(timeout=60)
        assert counter.read_text().count("ran") == 1
        meta = seed._load("dddd0001")
        assert meta["owner_pid"] in [p.pid for p in procs]
        assert meta["status"] == "completed"

    def test_reused_pid_is_not_mistaken_for_the_job(self, jm):
        sleeper = spawn_sleeper()
        try:
            meta = write_orphan(jm, "eeee0001", pid=sleeper.pid)
            jm._update("eeee0001", pid_start="Thu Jan  1 00:00:00 1970")  # a different process had this pid
            assert jm.get_job_status("eeee0001")["status"] == "failed"
            assert sleeper.poll() is None  # and it was not signalled
        finally:
            sleeper.kill(); sleeper.wait()

    def test_stopped_early_is_not_completed(self, jm, tmp_path, fixtures):
        text = (fixtures / "md_log_full.txt").read_text()
        cut = text[: text.index("           Step           Time\n          25000")]
        out = tmp_path / "run"
        out.mkdir()
        (out / "md.log").write_text(cut + "\nFinished mdrun on rank 0 Sun Oct 18 21:56:42 2026\n")
        write_orphan(jm, "ffff0001", pid=dead_pid(), output_dir=out)
        s = jm.get_job_status("ffff0001")
        # mdrun exited cleanly before its last step: stopped (resumable), the same state a watched job gets
        assert s["status"] == "stopped" and "resume_job" in s["stop_reason"] and "error" not in s
        assert s["progress"]["finished"] and not s["progress"]["complete"] and s["progress"]["percent"] < 100
        res = jm.get_job_result("ffff0001")
        assert res["status"] == "success" and res["result"]["stopped"] is True

    def test_child_processes_get_no_stdin(self, jm, tmp_path):
        reader = tmp_path / "reader.py"
        reader.write_text("import sys, json\nimport argparse\nap=argparse.ArgumentParser()\n"
                          "ap.add_argument('--result-json')\na,_=ap.parse_known_args()\n"
                          "data=sys.stdin.read()\nopen(a.result_json,'w').write(json.dumps({'result': True, 'stdin': data}))\n")
        r = jm.submit_job(str(reader), {})
        wait_for(jm, r["job_id"], timeout=10)
        assert jm.get_job_result(r["job_id"])["result"]["stdin"] == ""


@pytest.mark.gmx
def test_cancel_real_mdrun_writes_checkpoint_and_waits_for_it(tmp_path, example_data):
    """SIGTERM reaches mdrun and the wrapper; the wrapper must outlive mdrun, report the stop, and
    the slot must stay busy until mdrun has exited."""
    jm = JobManager(jobs_dir=tmp_path / "jobs", max_concurrent=1)
    md_script = Path(__file__).parent.parent / "scripts" / "md_simulation.py"
    out = tmp_path / "run"
    a = jm.submit_job(str(md_script), {"input": str(example_data / "sample.tpr"), "nsteps": 5_000_000, "ntomp": 2},
                      output_dir=str(out), kind="md")
    b = jm.submit_job(str(md_script), {"input": str(example_data / "sample.tpr"), "nsteps": 10},
                      output_dir=str(tmp_path / "next"), kind="md")
    for _ in range(200):
        s = jm.get_job_status(a["job_id"])
        if s.get("progress", {}).get("step", 0) > 0:
            break
        time.sleep(0.1)
    assert jm.cancel_job(a["job_id"])["status"] == "success"
    s = wait_for(jm, a["job_id"], states=("cancelled",), timeout=60)
    assert (out / "md.cpt").exists()
    result = json.loads((jm.jobs_dir / a["job_id"] / "output.json").read_text())
    assert result["result"] is False and "signal" in result["error"]
    assert s["progress"]["complete"] is False and s["progress"]["percent"] < 100
    pgid = jm._load(a["job_id"])["pgid"]
    from jobs.manager import _group_alive
    assert not _group_alive(pgid)
    assert wait_for(jm, b["job_id"], timeout=60)["status"] == "completed"


class TestJobIdsAndScripts:
    @pytest.mark.parametrize("bad", ["../../etc", "..", "abc", "DEADBEEF", "deadbeef/..", "", "deadbeef0"])
    def test_malformed_job_ids_are_never_looked_up(self, jm, tmp_path, bad):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "metadata.json").write_text(json.dumps({"job_id": "x", "status": "completed", "script": "x"}))
        for call in (jm.get_job_status, jm.get_job_result, jm.get_job_log, jm.cancel_job, jm.resume_job, jm.delete_job):
            assert call(bad)["status"] == "error"
        assert jm._load(bad) is None

    def test_script_outside_allowed_folders_is_refused(self, jm, monkeypatch, tmp_path):
        monkeypatch.setenv("GMX_MCP_SCRIPT_DIRS", "")
        rogue = tmp_path / "rogue.py"
        rogue.write_text("print('should not run')")
        r = jm.submit_job(str(rogue), {})
        assert r["status"] == "error" and "allowed scripts folder" in r["error"]

    def test_tampered_metadata_script_is_not_executed(self, jm, script, tmp_path, monkeypatch):
        marker = tmp_path / "ran.txt"
        rogue = tmp_path / "elsewhere" / "rogue.py"
        rogue.parent.mkdir()
        rogue.write_text(f"open({str(marker)!r}, 'w').write('x')")
        write_orphan(jm, "abab0001", status="queued", script=str(rogue))
        monkeypatch.setenv("GMX_MCP_SCRIPT_DIRS", str(Path(script).parent / "nowhere"))
        s = wait_for(jm, "abab0001")
        assert s["status"] == "failed" and "allowed scripts folder" in s["error"]
        assert not marker.exists()
