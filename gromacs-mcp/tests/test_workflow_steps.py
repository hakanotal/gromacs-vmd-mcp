"""Workflow step bookkeeping (resume markers), with a plain Python command instead of gmx."""

import json
import os
import sys
from pathlib import Path

import gromacs_workflow
from gromacs_workflow import WorkflowStep

UPPER = [sys.executable, "-c",
         "import pathlib; pathlib.Path('out.txt').write_text(pathlib.Path('in.txt').read_text().upper())"]


def make_step() -> WorkflowStep:
    return WorkflowStep("upper", UPPER, reads=["in.txt"], outputs=["out.txt"])


def count_hashing(monkeypatch) -> list:
    """Record the files hashed from now on."""
    calls = []
    real = gromacs_workflow._sha256
    monkeypatch.setattr(gromacs_workflow, "_sha256", lambda p: calls.append(Path(p).name) or real(p))
    return calls


class TestResumeMarkers:
    def test_unchanged_inputs_are_not_read_again(self, tmp_path, monkeypatch):
        # TODO 12.28: every resume used to hash every input again
        (tmp_path / "in.txt").write_text("abc")
        assert make_step().execute(tmp_path)
        hashed = count_hashing(monkeypatch)
        step = make_step()
        assert step.execute(tmp_path) and step.status == "skipped"
        assert hashed == []

    def test_a_touched_file_is_hashed_again_and_still_skipped(self, tmp_path, monkeypatch):
        src = tmp_path / "in.txt"
        src.write_text("abc")
        assert make_step().execute(tmp_path)
        st = src.stat()
        os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        hashed = count_hashing(monkeypatch)
        step = make_step()
        assert step.execute(tmp_path) and step.status == "skipped"
        assert hashed == ["in.txt"]

    def test_changed_content_reruns(self, tmp_path):
        (tmp_path / "in.txt").write_text("abc")
        assert make_step().execute(tmp_path)
        (tmp_path / "in.txt").write_text("abcd")
        step = make_step()
        assert step.execute(tmp_path) and step.status == "completed"
        assert (tmp_path / "out.txt").read_text() == "ABCD"

    def test_markers_from_older_versions_still_resume(self, tmp_path, monkeypatch):
        (tmp_path / "in.txt").write_text("abc")
        assert make_step().execute(tmp_path)
        marker = tmp_path / ".workflow" / "upper.json"
        data = json.loads(marker.read_text())
        data.pop("stats")  # written before sizes and times were recorded
        marker.write_text(json.dumps(data))
        hashed = count_hashing(monkeypatch)
        step = make_step()
        assert step.execute(tmp_path) and step.status == "skipped"
        assert hashed == ["in.txt"]
