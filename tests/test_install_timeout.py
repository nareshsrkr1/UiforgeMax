"""Install timeout hands control back to the human instead of hanging."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from uiforgemax.env_flags import install_timeout_seconds
from uiforgemax.pipeline.testing import (
    ack_install_wait_for_retry,
    load_install_wait,
    run_allowlisted_installs,
    run_tests,
)


def _timed_out_result(*_a, **_k) -> SimpleNamespace:
    return SimpleNamespace(returncode=-1, stdout="", stderr="", timed_out=True)


def test_install_timeout_default_is_fifteen_minutes(monkeypatch):
    monkeypatch.delenv("UIFORGEMAX_INSTALL_TIMEOUT", raising=False)
    assert install_timeout_seconds() == 900
    monkeypatch.setenv("UIFORGEMAX_INSTALL_TIMEOUT", "45")
    # Floor is 60s so tiny overrides still get a usable window.
    assert install_timeout_seconds() == 60
    monkeypatch.setenv("UIFORGEMAX_INSTALL_TIMEOUT", "1200")
    assert install_timeout_seconds() == 1200


def test_run_allowlisted_installs_stops_on_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("UIFORGEMAX_INSTALL_TIMEOUT", "30")
    roots = {"default": tmp_path}

    with patch("uiforgemax.pipeline.testing.run_with_timeout", side_effect=_timed_out_result):
        log = run_allowlisted_installs(
            roots,
            [
                {"command": "npm install", "cwd": ".", "root": "default"},
                {"command": "npm install -D vitest", "cwd": ".", "root": "default"},
            ],
        )
    assert len(log) == 1
    assert log[0]["timedOut"] is True


def test_run_tests_returns_human_install_on_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("UIFORGEMAX_INSTALL_TIMEOUT", "20")
    root = tmp_path / "app"
    root.mkdir()
    run_dir = tmp_path / "run"
    (run_dir / "tests").mkdir(parents=True)
    (run_dir / "tests" / "generated-tests.json").write_text(
        json.dumps(
            {
                "installHints": [{"command": "npm ci", "cwd": ".", "root": "default"}],
                "tests": [],
                "run": [{"command": "npx vitest run", "cwd": ".", "root": "default"}],
            }
        ),
        encoding="utf-8",
    )

    with patch("uiforgemax.pipeline.testing.run_with_timeout", side_effect=_timed_out_result):
        with patch(
            "uiforgemax.pipeline.testing.ensure_dependencies_for_tests",
            return_value=[],
        ):
            results = run_tests(root, {}, run_dir=run_dir)

    assert results["installTimedOut"] is True
    assert results["humanInstallRequired"] is True
    assert results["passed"] is False
    wait = load_install_wait(run_dir)
    assert wait["status"] == "awaiting_user_install"
    assert wait.get("resumeAction") == "advance"
    assert wait.get("shell")
    assert (run_dir / "handover" / "install-paused.md").exists()
    assert ack_install_wait_for_retry(run_dir) is True
    assert load_install_wait(run_dir)["status"] == "retrying_tests_only"


def test_awaiting_user_install_gate_points_to_advance():
    from uiforgemax.mcp_response import _gate_next
    from uiforgemax.state import RunState, Stage, Status

    state = RunState(run_id="r1", project_root="C:/app", current_stage=Stage.TEST)
    state.status = Status.AWAITING_USER_INSTALL
    next_tool, alts, _blocked = _gate_next(state)
    assert next_tool == "uiforgemax_advance"
    assert "uiforgemax_resume_run" in alts


def test_resume_checkpoint_persists_stage_and_auto_resume_flag(tmp_path):
    from uiforgemax.pipeline.resume_checkpoint import (
        load_resume_checkpoint,
        should_auto_advance_on_resume,
        write_resume_checkpoint,
    )
    from uiforgemax.state import RunState, Stage, Status

    state = RunState(run_id="r-chk", project_root=str(tmp_path / "app"), current_stage=Stage.TEST)
    state.status = Status.AWAITING_USER_INSTALL
    cp = write_resume_checkpoint(
        tmp_path,
        state,
        pause_reason="dependency_install_timeout",
        skip_on_resume={"autoInstall": True},
        shell='cd "app"\nnpm install',
    )
    assert cp["resumeStage"] == "10_test"
    assert "9_implement" in cp["completedStages"]
    assert cp["nextTool"] == "uiforgemax_advance"
    loaded = load_resume_checkpoint(tmp_path)
    assert loaded["pauseReason"] == "dependency_install_timeout"
    assert should_auto_advance_on_resume(state, tmp_path) is True
    assert (tmp_path / "resume-checkpoint.json").exists()
