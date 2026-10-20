#!/usr/bin/env python3
"""Job management for long-running GROMACS tasks.

Jobs run the scripts in ../../scripts as detached subprocesses (own session and process
group, started with the interpreter that runs this server).

Where things live:
  jobs/<job_id>/   the job record: metadata.json (atomic writes), job.log, output.json
  runs/<job_id>/   default output folder (outside the record, so deleting a record never
                   deletes simulation data)

Lifecycle: queued -> running -> completed | stopped | failed | cancelled
  stopped: mdrun ended cleanly before its last step (maxh, the wall clock limit, or mdrun stopped
  from outside, for example found after a restart) and wrote a checkpoint; resume_job continues it.

Design notes:
  * At most GMX_MCP_MAX_JOBS jobs run at once per server (default 2); the rest wait in order.
  * Cancel sends SIGTERM to the job's process group so mdrun writes a checkpoint. The slot is
    only freed once every process of the group has exited; after a grace period the group
    gets SIGKILL. A cancel request always wins over the exit code.
  * Jobs outlive the server. A running job whose processes are gone is finalized from
    evidence: its result file, or md.log showing the last step reached nsteps. mdrun prints
    "Finished mdrun" after SIGTERM too, so that line alone does not mean completed.
  * Several server instances may share the jobs folder. Each process is identified by pid
    plus start time (pids get reused); queued jobs of a dead server are claimed under a
    file lock so exactly one instance re-queues them; a job whose processes are alive is
    never touched; an output folder can only be used by one unfinished job at a time.
"""

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from loguru import logger

_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from lib.gromacs_utils import (  # noqa: E402
    PathNotAllowedError,
    free_disk_mb,
    get_home,
    get_runs_dir,
    parse_mdlog_progress,
    read_tpr_nsteps,
    resolve_in_workdir,
    tail_lines,
    write_json_atomic,
)

JOB_ID_RE = re.compile(r"^[0-9a-f]{8}$")
CANCEL_GRACE_SECONDS = 30
HEAD_BYTES = 40_000
TAIL_BYTES = 40_000
RECORD_FILES = ("metadata.json", "job.log", "output.json", ".claim.lock")


class JobStatus(Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL = {JobStatus.COMPLETED.value, JobStatus.STOPPED.value, JobStatus.FAILED.value,
             JobStatus.CANCELLED.value}
_ACTIVE = {JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.PENDING.value}


# ----------------------------------------------------------------------
# Process identity
# ----------------------------------------------------------------------
def _pid_alive(pid: Optional[int]) -> bool:
    """True if some process has this pid (it may be a different process than we started)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _proc_start(pid: Optional[int]) -> Optional[str]:
    """Start time of a process as printed by ps, used to detect pid reuse. None if unknown."""
    if not pid:
        return None
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5, stdin=subprocess.DEVNULL)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _same_process_alive(pid: Optional[int], start: Optional[str]) -> bool:
    """Alive and, when a start time was recorded, still the same process (not a reused pid)."""
    if not _pid_alive(pid):
        return False
    if start:
        current = _proc_start(pid)
        if current is not None and current != start:
            return False
    return True


def _group_alive(pgid: Optional[int]) -> bool:
    if not pgid:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: Optional[int], sig: int) -> None:
    if not pgid:
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _job_processes_alive(meta: Dict[str, Any]) -> bool:
    """The job's wrapper is alive (same process), or another member of its group (mdrun) is."""
    pid, start = meta.get("pid"), meta.get("pid_start")
    pgid = meta.get("pgid") or pid
    if pid and _pid_alive(pid):
        if start:
            current = _proc_start(pid)
            if current is not None and current != start:
                return False  # pid reused by an unrelated process; its group is not ours either
        return True
    return _group_alive(pgid)


def _stop_note(result: Dict[str, Any]) -> str:
    """One line for a stopped run: where it stopped, why, and what to do."""
    progress = result.get("progress") or {}
    where = f"at step {progress.get('step')} of {progress.get('nsteps')} " if progress.get("step") is not None else ""
    return (f"mdrun stopped {where}({result.get('stop_reason') or 'before the last step'}); "
            "resume_job continues from the checkpoint")


def _now() -> str:
    return datetime.now().isoformat()


def _duration(meta: Dict[str, Any]) -> Optional[float]:
    start = meta.get("started_at")
    if not start:
        return None
    end = meta.get("completed_at") or _now()
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 1)
    except ValueError:
        return None


def _arg_flags(args: Dict[str, Any]) -> List[str]:
    """Turn {key: value} into CLI flags. None and False are skipped, True is a bare flag, and a
    list repeats the flag once per item (never joined into one string: paths may contain commas)."""
    flags: List[str] = []
    for key, value in args.items():
        if value is None or value is False:
            continue
        flag = "--" + key.replace("_", "-")
        if value is True:
            flags.append(flag)
        elif isinstance(value, (list, tuple)):
            for item in value:
                flags.extend([flag, str(item)])
        else:
            flags.extend([flag, str(value)])
    return flags


def _valid_job_id(job_id: Any) -> bool:
    return isinstance(job_id, str) and bool(JOB_ID_RE.match(job_id))


def _allowed_script_dirs() -> List[Path]:
    """Jobs may only run scripts from these folders: scripts/ plus GMX_MCP_SCRIPT_DIRS (os.pathsep separated)."""
    dirs = [_SCRIPTS.resolve()]
    for extra in os.environ.get("GMX_MCP_SCRIPT_DIRS", "").split(os.pathsep):
        if extra:
            dirs.append(Path(extra).expanduser().resolve())
    return dirs


def _script_allowed(script: str) -> bool:
    path = Path(script).resolve()
    return path.is_file() and any(d == path.parent or d in path.parents for d in _allowed_script_dirs())


class JobManager:
    """Manages asynchronous job execution."""

    def __init__(self, jobs_dir: Optional[Path] = None, max_concurrent: Optional[int] = None,
                 runs_dir: Optional[Path] = None):
        env_dir = os.environ.get("GMX_MCP_JOBS_DIR")
        self.jobs_dir = Path(jobs_dir or env_dir or get_home() / "jobs")
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        if runs_dir is not None:
            self.runs_dir = Path(runs_dir)
        elif jobs_dir is not None:
            self.runs_dir = Path(jobs_dir).parent / "runs"
        else:
            self.runs_dir = get_runs_dir()
        self.max_concurrent = int(max_concurrent or os.environ.get("GMX_MCP_MAX_JOBS", 2))
        self.min_free_mb = float(os.environ.get("GMX_MCP_MIN_FREE_MB", 500))
        self.instance_id = uuid.uuid4().hex
        self.owner_pid = os.getpid()
        self.owner_start = _proc_start(self.owner_pid)
        self._lock = threading.RLock()
        self._procs: Dict[str, subprocess.Popen] = {}
        self._active: set = set()
        self._cancelled: set = set()
        self._queue: Deque[str] = deque()
        self.recover_orphans()

    # ------------------------------------------------------------------
    # Submit and run
    # ------------------------------------------------------------------
    def _owner_fields(self) -> Dict[str, Any]:
        return {"owner_pid": self.owner_pid, "owner_start": self.owner_start, "owner_instance": self.instance_id}

    def _owner_alive(self, meta: Dict[str, Any]) -> bool:
        if meta.get("owner_instance") == self.instance_id:
            return True
        if meta.get("owner_pid") == self.owner_pid:
            return True  # another manager in this same process
        return _same_process_alive(meta.get("owner_pid"), meta.get("owner_start"))

    def _output_dir_in_use(self, output_dir: Path, exclude: Optional[str] = None) -> Optional[Dict[str, Any]]:
        for job_dir in self.jobs_dir.iterdir():
            if not job_dir.is_dir() or job_dir.name == exclude:
                continue
            meta = self._reconcile(job_dir.name)
            if meta and meta["status"] in _ACTIVE and meta.get("output_dir"):
                if Path(meta["output_dir"]).resolve() == output_dir.resolve():
                    return meta
        return None

    def submit_job(
        self,
        script_path: str,
        args: Dict[str, Any],
        job_name: Optional[str] = None,
        output_dir: Optional[str] = None,
        default_output_subdir: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Queue a script for background execution.

        Args:
            script_path: script to run with the server's Python interpreter
            args: {name: value}; name becomes --name (underscores to hyphens)
            job_name: label
            output_dir: artifacts directory, passed to the script as --output-dir (made absolute)
            default_output_subdir: when output_dir is None, use runs/<job id>/<this>
            kind: free text such as "md" or "analysis"
        """
        if not _script_allowed(script_path):
            return {"status": "error", "error": f"Refusing to run {script_path}: not in an allowed scripts folder"}
        free = free_disk_mb(self.jobs_dir)
        if free < self.min_free_mb:
            return {"status": "error",
                    "error": f"Only {free:.0f} MB free in {self.jobs_dir}; need at least {self.min_free_mb:.0f} MB"}

        job_id = uuid.uuid4().hex[:8]
        if output_dir is None and default_output_subdir:
            out_path: Optional[Path] = self.runs_dir / job_id / default_output_subdir
        else:
            out_path = Path(output_dir).expanduser().resolve() if output_dir else None

        if out_path is not None:
            try:
                resolve_in_workdir(out_path)
            except PathNotAllowedError as e:
                return {"status": "error",
                        "error": f"{e}. The default job folder comes from GMX_MCP_HOME or GMX_MCP_RUNS_DIR; "
                                 "set it inside GMX_MCP_WORKDIR, or pass output_dir"}
            busy = self._output_dir_in_use(out_path)
            if busy:
                return {"status": "error",
                        "error": f"{out_path} is used by job {busy['job_id']} ({busy['status']}); wait for it, "
                                 "cancel it, or choose another output_dir"}

        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        args = dict(args)
        if out_path is not None:
            args["output_dir"] = str(out_path)

        metadata = {
            "job_id": job_id,
            "job_name": job_name or f"job_{job_id}",
            "kind": kind,
            "script": script_path,
            "args": args,
            "output_dir": str(out_path) if out_path else None,
            "status": JobStatus.QUEUED.value,
            "submitted_at": _now(),
            "started_at": None,
            "completed_at": None,
            "error": None,
            "pid": None,
            "pgid": None,
            "pid_start": None,
            "cancel_requested": False,
            **self._owner_fields(),
        }
        with self._lock:
            self._save(job_id, metadata)
            self._queue.append(job_id)
        self._dispatch()
        status = self._load(job_id)["status"]
        return {
            "status": "submitted",
            "job_id": job_id,
            "job_status": status,
            "output_dir": metadata["output_dir"],
            "message": f"Job {status}. Use get_job_status('{job_id}') to check progress.",
        }

    def _dispatch(self) -> None:
        """Start queued jobs while slots are free."""
        with self._lock:
            while self._queue and len(self._active) < self.max_concurrent:
                job_id = self._queue.popleft()
                meta = self._load(job_id)
                if not meta or meta["status"] != JobStatus.QUEUED.value:
                    continue
                self._active.add(job_id)
                threading.Thread(target=self._run_job, args=(job_id,), daemon=True,
                                 name=f"job-{job_id}").start()

    def _run_job(self, job_id: str) -> None:
        job_dir = self.jobs_dir / job_id
        meta = self._load(job_id)
        try:
            if not _script_allowed(meta["script"]):
                raise RuntimeError(f"Refusing to run {meta['script']}: not in an allowed scripts folder")
            if meta.get("output_dir"):
                Path(meta["output_dir"]).mkdir(parents=True, exist_ok=True)
            result_json = job_dir / "output.json"
            cmd = [sys.executable, meta["script"]] + _arg_flags(meta["args"]) + ["--result-json", str(result_json)]
            log_path = job_dir / "job.log"
            with open(log_path, "w") as log:
                process = subprocess.Popen(
                    cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    cwd=str(_ROOT), start_new_session=True,
                )
                pgid = process.pid  # start_new_session makes the child its own group leader
                with self._lock:
                    self._procs[job_id] = process
                    self._update(job_id, status=JobStatus.RUNNING.value, started_at=_now(), pid=process.pid,
                                 pgid=pgid, pid_start=_proc_start(process.pid))
                    if job_id in self._cancelled:
                        _signal_group(pgid, signal.SIGTERM)
                process.wait()
            self._wait_for_group(job_id, pgid)
            self._finalize(job_id, process.returncode)
        except Exception as e:
            logger.error(f"Job {job_id} failed to run: {e}")
            self._finalize(job_id, None, error=str(e))
        finally:
            with self._lock:
                self._procs.pop(job_id, None)
                self._active.discard(job_id)
            self._dispatch()

    def _wait_for_group(self, job_id: str, pgid: int) -> None:
        """Do not free the slot while a member of the job's group (mdrun) is still running.

        After a cancel, wait up to the grace period for mdrun to write its checkpoint, then kill.
        """
        if not _group_alive(pgid):
            return
        deadline = time.time() + CANCEL_GRACE_SECONDS
        while _group_alive(pgid) and time.time() < deadline:
            time.sleep(0.2)
        if _group_alive(pgid):
            logger.warning(f"Job {job_id}: processes still running after {CANCEL_GRACE_SECONDS}s; sending SIGKILL")
            _signal_group(pgid, signal.SIGKILL)

    def _finalize(self, job_id: str, returncode: Optional[int], error: Optional[str] = None) -> None:
        """Single place that decides the final status of a job this manager ran."""
        with self._lock:
            meta = self._load(job_id) or {}
            if job_id in self._cancelled or meta.get("cancel_requested"):
                self._update(job_id, status=JobStatus.CANCELLED.value, completed_at=_now())
                return
            data = self._result_data(job_id) or {}
            if error is None and data.get("stopped"):
                self._update(job_id, status=JobStatus.STOPPED.value, completed_at=_now(),
                             stop_reason=_stop_note(data))
                return
            if error is None and returncode == 0:
                self._update(job_id, status=JobStatus.COMPLETED.value, completed_at=_now())
                return
            msg = error or data.get("error") or f"Process exited with code {returncode}"
            self._update(job_id, status=JobStatus.FAILED.value, completed_at=_now(), error=msg)

    def _result_data(self, job_id: str) -> Optional[Dict[str, Any]]:
        """The job script's result file (output.json), or None when missing or unreadable."""
        out = self.jobs_dir / job_id / "output.json"
        if out.exists():
            try:
                data = json.loads(out.read_text())
                return data if isinstance(data, dict) else None
            except (OSError, ValueError):
                return None
        return None

    # ------------------------------------------------------------------
    # Recovery of jobs whose manager is gone
    # ------------------------------------------------------------------
    def _claim(self, job_id: str) -> bool:
        """Take ownership of a queued job whose owner is gone. A file lock makes this exclusive
        across processes, so exactly one server re-queues it."""
        job_dir = self.jobs_dir / job_id
        try:
            fh = open(job_dir / ".claim.lock", "a+")
        except OSError:
            return False
        with fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                meta = self._load(job_id)
                if not meta or meta["status"] not in (JobStatus.QUEUED.value, JobStatus.PENDING.value) or meta.get("pid"):
                    return False
                if meta.get("owner_instance") == self.instance_id:
                    return True
                if self._owner_alive(meta):
                    return False
                self._update(job_id, status=JobStatus.QUEUED.value, **self._owner_fields())
                return True
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def recover_orphans(self) -> None:
        """Re-queue jobs whose server died before starting them, and finalize finished orphans."""
        for job_dir in sorted(self.jobs_dir.iterdir()) if self.jobs_dir.exists() else []:
            if job_dir.is_dir():
                self._reconcile(job_dir.name)
        self._dispatch()

    def _reconcile(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Bring a job's recorded state in line with reality. Safe to call often."""
        meta = self._load(job_id)
        if not meta:
            return meta

        if meta["status"] in (JobStatus.QUEUED.value, JobStatus.PENDING.value) and not meta.get("pid"):
            with self._lock:
                ours = job_id in self._queue or job_id in self._active
            if not ours and meta.get("owner_instance") != self.instance_id and self._claim(job_id):
                with self._lock:
                    if job_id not in self._queue and job_id not in self._active:
                        self._queue.append(job_id)
                self._dispatch()
            return self._load(job_id)

        if meta["status"] != JobStatus.RUNNING.value:
            return meta
        with self._lock:
            if job_id in self._procs or job_id in self._active:
                return meta
        if _job_processes_alive(meta):
            return meta
        if meta.get("cancel_requested"):
            return self._update(job_id, status=JobStatus.CANCELLED.value, completed_at=_now())

        if (self.jobs_dir / job_id / "output.json").exists():
            data = self._result_data(job_id) or {}
            if data.get("stopped"):
                return self._update(job_id, status=JobStatus.STOPPED.value, completed_at=_now(),
                                    stop_reason=_stop_note(data))
            if data.get("result"):
                return self._update(job_id, status=JobStatus.COMPLETED.value, completed_at=_now())
            return self._update(job_id, status=JobStatus.FAILED.value, completed_at=_now(),
                                error=data.get("error") or "Job reported failure")

        md_log = Path(meta["output_dir"]) / "md.log" if meta.get("output_dir") else None
        if md_log and md_log.exists():
            progress = parse_mdlog_progress(md_log.read_text(errors="replace"), nsteps=self._target_nsteps(meta))
            if progress.get("complete"):
                return self._update(job_id, status=JobStatus.COMPLETED.value, completed_at=_now())
            if progress.get("finished"):
                # mdrun exited cleanly with a checkpoint while no server watched (time limit or a signal)
                return self._update(job_id, status=JobStatus.STOPPED.value, completed_at=_now(),
                                    stop_reason=_stop_note({"progress": progress,
                                                            "stop_reason": "signal or time limit"}))
        return self._update(job_id, status=JobStatus.FAILED.value, completed_at=_now(),
                            error="Process ended without a result (server restart or crash)")

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def get_job_status(self, job_id: str) -> Dict[str, Any]:
        meta = self._reconcile(job_id)
        if not meta:
            return {"status": "error", "error": f"Job {job_id} not found"}

        result = {
            "job_id": job_id,
            "job_name": meta.get("job_name"),
            "kind": meta.get("kind"),
            "status": meta["status"],
            "submitted_at": meta.get("submitted_at"),
            "started_at": meta.get("started_at"),
            "completed_at": meta.get("completed_at"),
            "duration_seconds": _duration(meta),
            "output_dir": meta.get("output_dir"),
        }
        if meta["status"] == JobStatus.QUEUED.value:
            with self._lock:
                if job_id in self._queue:
                    result["queue_position"] = list(self._queue).index(job_id) + 1
        if meta["status"] == JobStatus.FAILED.value:
            result["error"] = meta.get("error")
        if meta["status"] == JobStatus.STOPPED.value:
            result["stop_reason"] = meta.get("stop_reason")

        progress = self._progress(meta)
        if progress:
            if meta["status"] == JobStatus.RUNNING.value and progress.get("percent"):
                elapsed = _duration(meta) or 0
                if progress["percent"] > 0:
                    progress["eta_seconds"] = round(elapsed * (100 - progress["percent"]) / progress["percent"])
            result["progress"] = progress
        return result

    def _progress(self, meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        out = meta.get("output_dir")
        if not out:
            return None
        md_log = Path(out) / "md.log"
        if not md_log.exists():
            return None
        try:
            size = md_log.stat().st_size
            with open(md_log, "rb") as f:
                head = f.read(HEAD_BYTES)
                tail = b""
                if size > HEAD_BYTES:
                    f.seek(max(HEAD_BYTES, size - TAIL_BYTES))
                    tail = f.read()
            return parse_mdlog_progress((head + b"\n" + tail).decode(errors="replace"),
                                        nsteps=self._target_nsteps(meta))
        except OSError:
            return None

    def _target_nsteps(self, meta: Dict[str, Any]) -> Optional[int]:
        """The step count an MD job runs to: its nsteps argument, or the value in its .tpr, read once and
        kept in the record. md.log is not enough: after extend and resume it still shows the first run's."""
        if "md_simulation" not in str(meta.get("script")):
            return None
        if "target_nsteps" in meta:
            return meta["target_nsteps"]
        args = meta.get("args") or {}
        value = args.get("nsteps")
        if value is None and args.get("input") and not args.get("multidir"):
            value = read_tpr_nsteps(args["input"])
        if meta.get("job_id"):
            self._update(meta["job_id"], target_nsteps=value)
        return value

    def get_job_result(self, job_id: str) -> Dict[str, Any]:
        meta = self._reconcile(job_id)
        if not meta:
            return {"status": "error", "error": f"Job {job_id} not found"}

        output_file = self.jobs_dir / job_id / "output.json"
        data = None
        if output_file.exists():
            try:
                data = json.loads(output_file.read_text())
            except (OSError, ValueError):
                data = None

        if meta["status"] in (JobStatus.COMPLETED.value, JobStatus.STOPPED.value):
            if data is not None:
                return {"status": "success", "result": data}
            fallback = {"result": True, "output_dir": meta.get("output_dir"),
                        "note": "The job finished while no server was watching it; outputs are in output_dir"}
            if meta["status"] == JobStatus.STOPPED.value:
                fallback.update({"stopped": True, "warnings": [meta.get("stop_reason") or "Stopped early"]})
            return {"status": "success", "result": fallback}
        if meta["status"] in (JobStatus.FAILED.value, JobStatus.CANCELLED.value):
            out = {"status": "error", "error": meta.get("error") or f"Job {meta['status']}"}
            if data is not None:
                out["result"] = data
            return out
        return {"status": "error", "error": f"Job not completed. Current status: {meta['status']}"}

    def get_job_log(self, job_id: str, tail: int = 50, include_md_log: bool = True) -> Dict[str, Any]:
        """Tail of the job's stdout log, plus the tail of md.log when it exists."""
        meta = self._load(job_id)
        if not meta:
            return {"status": "error", "error": f"Job {job_id} not found"}
        log_file = self.jobs_dir / job_id / "job.log"
        if not log_file.exists():
            return {"status": "error", "error": f"Log not found for job {job_id} (status: {meta['status']})"}

        with open(log_file, "rb") as f:
            total = sum(1 for _ in f)
        out = {
            "status": "success",
            "job_id": job_id,
            "log_lines": tail_lines(log_file, tail),
            "total_lines": total,
        }
        if include_md_log and meta.get("output_dir"):
            md_log = Path(meta["output_dir"]) / "md.log"
            if md_log.exists():
                out["md_log_lines"] = tail_lines(md_log, tail)
        return out

    def list_jobs(self, status: Optional[str] = None) -> Dict[str, Any]:
        jobs = []
        for job_dir in self.jobs_dir.iterdir():
            if not job_dir.is_dir():
                continue
            meta = self._reconcile(job_dir.name)
            if not meta:
                continue
            if status is None or meta["status"] == status:
                jobs.append({
                    "job_id": meta["job_id"],
                    "job_name": meta.get("job_name"),
                    "kind": meta.get("kind"),
                    "status": meta["status"],
                    "submitted_at": meta.get("submitted_at"),
                    "duration_seconds": _duration(meta),
                    "output_dir": meta.get("output_dir"),
                })
        jobs.sort(key=lambda j: j["submitted_at"] or "", reverse=True)
        return {"status": "success", "jobs": jobs, "total": len(jobs)}

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------
    def cancel_job(self, job_id: str) -> Dict[str, Any]:
        """Cancel a queued or running job. Running jobs get SIGTERM (checkpoint), then SIGKILL."""
        orphan_pgid = None
        with self._lock:
            meta = self._load(job_id)
            if not meta:
                return {"status": "error", "error": f"Job {job_id} not found"}
            if meta["status"] in _TERMINAL:
                return {"status": "error", "error": f"Job {job_id} already {meta['status']}"}

            if job_id in self._queue:
                self._queue.remove(job_id)
                self._update(job_id, status=JobStatus.CANCELLED.value, completed_at=_now(), cancel_requested=True)
                return {"status": "success", "message": f"Job {job_id} cancelled (was queued)"}

            if meta["status"] == JobStatus.QUEUED.value:
                if job_id in self._active:
                    # Popped from the queue, process not up yet: _run_job honors the flag.
                    self._cancelled.add(job_id)
                    self._update(job_id, cancel_requested=True)
                    return {"status": "success", "message": f"Job {job_id} cancelling (was starting)"}
                if not self._owner_alive(meta) or self._claim(job_id):
                    self._update(job_id, status=JobStatus.CANCELLED.value, completed_at=_now(), cancel_requested=True)
                    return {"status": "success", "message": f"Job {job_id} cancelled (was queued)"}
                return {"status": "error", "error": f"Job {job_id} is queued by another server instance"}

            if meta["status"] != JobStatus.RUNNING.value or not _job_processes_alive(meta):
                return {"status": "error", "error": f"Job {job_id} not running"}

            pgid = meta.get("pgid") or meta.get("pid")
            self._cancelled.add(job_id)
            self._update(job_id, cancel_requested=True)
            _signal_group(pgid, signal.SIGTERM)
            ours = job_id in self._procs
            if not ours:
                orphan_pgid = pgid

        if orphan_pgid:
            # Started by another server instance: nobody here waits on it, so escalate on a timer.
            timer = threading.Timer(CANCEL_GRACE_SECONDS, self._force_kill, args=(job_id, orphan_pgid))
            timer.daemon = True
            timer.start()
        return {"status": "success",
                "message": f"Job {job_id} cancelling (SIGTERM sent; SIGKILL after {CANCEL_GRACE_SECONDS}s if needed)"}

    def _force_kill(self, job_id: str, pgid: int) -> None:
        meta = self._load(job_id)
        if meta and meta["status"] == JobStatus.RUNNING.value and _group_alive(pgid):
            logger.warning(f"Job {job_id} did not exit after SIGTERM; sending SIGKILL")
            _signal_group(pgid, signal.SIGKILL)
        self._reconcile(job_id)

    def resume_job(self, job_id: str) -> Dict[str, Any]:
        """Submit a new job that continues an MD job from its checkpoint (md.cpt)."""
        meta = self._reconcile(job_id)
        if not meta:
            return {"status": "error", "error": f"Job {job_id} not found"}
        if meta["status"] not in _TERMINAL:
            return {"status": "error", "error": f"Job {job_id} is {meta['status']}; resume needs a finished job"}
        out = meta.get("output_dir")
        if not out or "md_simulation" not in meta["script"]:
            return {"status": "error", "error": "Only MD simulation jobs can be resumed"}
        out_dir = Path(out)
        if not (out_dir / "md.cpt").exists() or not (out_dir / "md.tpr").exists():
            return {"status": "error", "error": f"No checkpoint (md.cpt) and md.tpr found in {out_dir}"}
        args = {k: v for k, v in meta["args"].items() if k not in ("input", "output_dir", "resume", "nsteps")}
        args.update({"input": str(out_dir / "md.tpr"), "resume": True})
        return self.submit_job(meta["script"], args, job_name=f"{meta.get('job_name')}_resume",
                               output_dir=str(out_dir), kind=meta.get("kind"))

    def delete_job(self, job_id: str) -> Dict[str, Any]:
        """Remove a finished job's record files. Output folders are never deleted, even if they
        sit inside the record folder (older jobs did that)."""
        meta = self._reconcile(job_id)
        if not meta:
            return {"status": "error", "error": f"Job {job_id} not found"}
        if meta["status"] not in _TERMINAL:
            return {"status": "error", "error": f"Job {job_id} is {meta['status']}; cancel it first"}
        job_dir = self.jobs_dir / job_id
        for name in RECORD_FILES:
            try:
                (job_dir / name).unlink()
            except FileNotFoundError:
                pass
        for tmp in job_dir.glob(".metadata.json.*.tmp"):
            tmp.unlink(missing_ok=True)
        kept = ""
        try:
            job_dir.rmdir()
        except OSError:
            kept = f"; {job_dir} still holds output files and was kept"
        return {"status": "success", "message": f"Job {job_id} record deleted (output files kept{kept})"}

    def clean_jobs(self, older_than_days: Optional[float] = 7, keep_last: Optional[int] = None,
                   statuses: Optional[List[str]] = None) -> Dict[str, Any]:
        """Delete finished job records older than N days, always keeping the newest `keep_last`."""
        wanted = set(statuses or _TERMINAL)
        finished = [j for j in self.list_jobs()["jobs"] if j["status"] in wanted and j["status"] in _TERMINAL]
        protected = {j["job_id"] for j in finished[:keep_last]} if keep_last else set()
        cutoff = datetime.now() - timedelta(days=older_than_days) if older_than_days is not None else None
        removed = []
        for j in finished:
            if j["job_id"] in protected:
                continue
            if cutoff and j["submitted_at"] and datetime.fromisoformat(j["submitted_at"]) > cutoff:
                continue
            if self.delete_job(j["job_id"])["status"] == "success":
                removed.append(j["job_id"])
        return {"status": "success", "removed": removed, "total_removed": len(removed)}

    # ------------------------------------------------------------------
    # Metadata persistence (atomic, locked)
    # ------------------------------------------------------------------
    def _save(self, job_id: str, metadata: Dict[str, Any]) -> None:
        if not _valid_job_id(job_id):
            raise ValueError(f"Invalid job id {job_id!r}")
        with self._lock:
            write_json_atomic(self.jobs_dir / job_id / "metadata.json", metadata)

    def _load(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Metadata of a job, or None. Ids that are not 8 hex characters are never looked up, so a
        crafted id such as ../../x cannot point outside the jobs folder."""
        if not _valid_job_id(job_id):
            return None
        meta_file = self.jobs_dir / job_id / "metadata.json"
        try:
            with open(meta_file) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _update(self, job_id: str, **fields) -> Optional[Dict[str, Any]]:
        with self._lock:
            meta = self._load(job_id)
            if meta is None:
                return None
            meta.update(fields)
            self._save(job_id, meta)
            return meta


# Global job manager instance
job_manager = JobManager()
