"""git subprocess calls in implement.py must never block the pipeline indefinitely."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from uiforgemax.pipeline.implement import _git_run


def test_git_run_missing_binary_does_not_raise(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "uiforgemax.pipeline.implement.subprocess.Popen",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()),
    )
    _git_run(["status"], cwd=str(tmp_path))  # must not raise


def test_git_run_normal_call_completes(tmp_path: Path):
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    _git_run(["add", "f.txt"], cwd=str(tmp_path))  # must not raise or hang


def test_git_run_kills_hung_process_on_timeout(tmp_path: Path, monkeypatch):
    """Simulate a hung git process — communicate() always times out until killed."""
    calls = {"kill_called": False}

    class _HungProc:
        pid = 12345

        def communicate(self, timeout=None):
            if not calls["kill_called"]:
                raise subprocess.TimeoutExpired(cmd="git", timeout=timeout)
            return ("", "")

    monkeypatch.setattr(
        "uiforgemax.pipeline.implement.subprocess.Popen", lambda *a, **k: _HungProc()
    )

    def _fake_kill(pid):
        calls["kill_called"] = True

    monkeypatch.setattr("uiforgemax.pipeline.implement._kill_process_tree", _fake_kill)

    _git_run(["commit", "-m", "x"], cwd=str(tmp_path), timeout=1)  # must not hang/raise
    assert calls["kill_called"]
