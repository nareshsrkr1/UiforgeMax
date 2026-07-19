"""Intake prefers real Jira fetch over invented SCRUM-* prompts."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from uiforgemax.config import Config, JiraConfig
from uiforgemax.intake_hints import extract_leading_issue_key, normalize_issue_key
from uiforgemax.state.gates import GateError
from uiforgemax.tools import ToolContext, approvals, inputs, lifecycle

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM = REPO_ROOT / "platform"


def _ctx(monkeypatch, *, jira: bool = False) -> ToolContext:
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
    return ToolContext.from_config(cfg)


def test_extract_leading_issue_key():
    assert extract_leading_issue_key("SCRUM-5") == "SCRUM-5"
    assert extract_leading_issue_key("SCRUM-5: invent a dashboard") == "SCRUM-5"
    assert extract_leading_issue_key("  proj-12 do something") == "PROJ-12"
    assert extract_leading_issue_key("Build a dashboard") is None
    assert normalize_issue_key("scrum-5") == "SCRUM-5"


def test_start_run_without_jira_prefers_prompt(monkeypatch):
    ctx = _ctx(monkeypatch, jira=False)
    data = json.loads(lifecycle.start_run(ctx, project_root=str(PLATFORM)))
    assert data["nextTool"] == "uiforgemax_add_prompt"
    assert "uiforgemax_add_jira" in data["alternatives"]


def test_start_run_with_jira_prefers_add_jira(monkeypatch):
    ctx = _ctx(monkeypatch, jira=True)
    data = json.loads(lifecycle.start_run(ctx, project_root=str(PLATFORM)))
    assert data["nextTool"] == "uiforgemax_add_jira"
    assert "uiforgemax_add_prompt" in data["alternatives"]
    assert data["jiraConfigured"] is True


def test_start_run_issue_key_sets_pending_and_next_tool(monkeypatch):
    ctx = _ctx(monkeypatch, jira=False)
    data = json.loads(
        lifecycle.start_run(ctx, project_root=str(PLATFORM), issue_key="SCRUM-5")
    )
    assert data["nextTool"] == "uiforgemax_add_jira"
    assert data["suggestedIssueKey"] == "SCRUM-5"
    assert "add_jira" in data["message"]


def test_add_prompt_blocked_when_starts_with_key_and_jira_configured(monkeypatch):
    ctx = _ctx(monkeypatch, jira=True)
    run_id = json.loads(lifecycle.start_run(ctx, project_root=str(PLATFORM)))["runId"]
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


def test_add_prompt_allowed_without_jira_env(monkeypatch):
    ctx = _ctx(monkeypatch, jira=False)
    run_id = json.loads(lifecycle.start_run(ctx, project_root=str(PLATFORM)))["runId"]
    out = json.loads(inputs.add_prompt(ctx, run_id, "SCRUM-5: free text when no jira"))
    assert out["stop"] is False
    assert (ctx.store.run_dir(run_id) / "inputs" / "prompt.txt").exists()


def test_approve_plan_too_early_message(monkeypatch):
    ctx = _ctx(monkeypatch, jira=False)
    run_id = json.loads(lifecycle.start_run(ctx, project_root=str(PLATFORM)))["runId"]
    try:
        approvals.approve_plan(ctx, run_id)
        raise AssertionError("expected GateError")
    except GateError as e:
        msg = str(e)
        assert "too early" in msg
        assert "intake" in msg
