"""Intake prefers real Jira fetch over invented SCRUM-* prompts."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from uiforgemax.config import Config, JiraConfig
from uiforgemax.intake_hints import extract_leading_issue_key, normalize_issue_key
from uiforgemax.state.gates import GateError
from uiforgemax.tools import ToolContext, approvals, inputs, lifecycle, preflight


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text('"""App."""\n')
    return project


def _ctx(monkeypatch, tmp_path: Path, *, jira: bool = False) -> tuple[ToolContext, Path]:
    runs = tempfile.mkdtemp()
    if jira:
        monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
        monkeypatch.setenv("JIRA_API_TOKEN", "token")
        cfg = Config(
            runs_root=runs,
            jira=JiraConfig(
                base_url="https://example.atlassian.net",
                email="dev@example.com",
                api_token="token",
            ),
        )
    else:
        monkeypatch.delenv("JIRA_BASE_URL", raising=False)
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
        cfg = Config(runs_root=runs)
    monkeypatch.setenv("UIFORGEMAX_USE_FIXTURES", "1")
    ctx = ToolContext.from_config(cfg)
    project = _make_project(tmp_path)
    preflight.preflight(ctx, project_root=str(project))
    return ctx, project


def test_extract_leading_issue_key():
    assert extract_leading_issue_key("SCRUM-5") == "SCRUM-5"
    assert extract_leading_issue_key("SCRUM-5: invent a dashboard") == "SCRUM-5"
    assert extract_leading_issue_key("  proj-12 do something") == "PROJ-12"
    assert extract_leading_issue_key("Build a dashboard") is None
    assert normalize_issue_key("scrum-5") == "SCRUM-5"


def test_start_run_without_jira_prefers_prompt(monkeypatch, tmp_path):
    ctx, project = _ctx(monkeypatch, tmp_path, jira=False)
    data = json.loads(lifecycle.start_run(ctx, project_root=str(project)))
    assert data["nextTool"] == "uiforgemax_add_prompt"
    assert "uiforgemax_add_jira" in data["alternatives"]


def test_start_run_with_jira_prefers_add_jira(monkeypatch, tmp_path):
    ctx, project = _ctx(monkeypatch, tmp_path, jira=True)
    data = json.loads(lifecycle.start_run(ctx, project_root=str(project)))
    assert data["nextTool"] == "uiforgemax_add_jira"
    assert "uiforgemax_add_prompt" in data["alternatives"]
    assert data["jiraConfigured"] is True


def test_start_run_issue_key_sets_pending_and_next_tool(monkeypatch, tmp_path):
    ctx, project = _ctx(monkeypatch, tmp_path, jira=False)
    data = json.loads(
        lifecycle.start_run(ctx, project_root=str(project), issue_key="SCRUM-5")
    )
    assert data["nextTool"] == "uiforgemax_add_jira"
    assert data["suggestedIssueKey"] == "SCRUM-5"
    assert "add_jira" in data["message"]


def test_add_prompt_blocked_when_starts_with_key_and_jira_configured(monkeypatch, tmp_path):
    ctx, project = _ctx(monkeypatch, tmp_path, jira=True)
    run_id = json.loads(lifecycle.start_run(ctx, project_root=str(project)))["runId"]
    out = json.loads(
        inputs.add_prompt(
            ctx,
            run_id,
            "SCRUM-5: invent a polished dashboard with summary cards",
        )
    )
    assert out["stop"] is True
    assert out["nextTool"] == "uiforgemax_add_jira"
    assert out["suggestedIssueKey"] == "SCRUM-5"
    assert not (ctx.store.run_dir(run_id) / "inputs" / "prompt.txt").exists()


def test_add_prompt_allowed_without_jira_env(monkeypatch, tmp_path):
    ctx, project = _ctx(monkeypatch, tmp_path, jira=False)
    run_id = json.loads(lifecycle.start_run(ctx, project_root=str(project)))["runId"]
    out = json.loads(inputs.add_prompt(ctx, run_id, "SCRUM-5: free text when no jira"))
    assert out["stop"] is False
    assert (ctx.store.run_dir(run_id) / "inputs" / "prompt.txt").exists()


def test_next_tool_is_advance_after_prompt_attached(monkeypatch, tmp_path):
    """Once an input is attached, intake is satisfied — steer to advance, not add_prompt."""
    ctx, project = _ctx(monkeypatch, tmp_path, jira=False)
    run_id = json.loads(lifecycle.start_run(ctx, project_root=str(project)))["runId"]
    out = json.loads(inputs.add_prompt(ctx, run_id, "Build a settings page"))
    assert out["nextTool"] == "uiforgemax_advance"
    # Adding more inputs is still possible via alternatives, just not the default.
    assert "uiforgemax_add_prompt" in out["alternatives"]


def test_next_tool_is_advance_after_jira_attached():
    """Regression: with Jira configured AND a jira input already attached,
    nextTool must be advance — NOT add_jira again (which stalled the agent)."""
    from uiforgemax.mcp_response import tool_response
    from uiforgemax.state import RunState, Status

    state = RunState(run_id="r1", project_root="C:/tmp", status=Status.INTAKE)
    state.inputs["modes"] = ["jira"]  # jira already attached this run
    out = json.loads(tool_response(state, "jira fetched", jira_configured=True))
    assert out["nextTool"] == "uiforgemax_advance"
    assert "uiforgemax_add_jira" in out["alternatives"]


def test_next_tool_still_add_jira_before_any_input(monkeypatch, tmp_path):
    """Guard the other direction: with NO input yet + Jira configured, still add_jira."""
    ctx, project = _ctx(monkeypatch, tmp_path, jira=True)
    data = json.loads(lifecycle.start_run(ctx, project_root=str(project)))
    assert data["nextTool"] == "uiforgemax_add_jira"


def test_approve_plan_too_early_message(monkeypatch, tmp_path):
    ctx, project = _ctx(monkeypatch, tmp_path, jira=False)
    run_id = json.loads(lifecycle.start_run(ctx, project_root=str(project)))["runId"]
    try:
        approvals.approve_plan(ctx, run_id)
        raise AssertionError("expected GateError")
    except GateError as e:
        msg = str(e)
        assert "too early" in msg
        assert "intake" in msg
