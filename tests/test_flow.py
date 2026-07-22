"""Smoke and integration tests for the MCP pipeline."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from uiforgemax.config import Config
from uiforgemax.state import Status
from uiforgemax.state.gates import GateError
from uiforgemax.tools import ToolContext, approvals, inputs, lifecycle, pipeline, preflight


_JIRA_FIXTURE = {
    "key": "PROJ-1234",
    "summary": "Customer List Page with search, pagination, and CSV export",
    "labels": ["frontend", "customer-domain"],
    "description": (
        "Build a customer list page.\n\n"
        "The page must show a paginated table of customers.\n"
        "Users must be able to search by name or email.\n"
        "Users must be able to export results to CSV."
    ),
    "acceptanceCriteria": [
        {"id": "AC-1", "text": "A paginated customer table shows Name, Email, Status, Company."},
        {"id": "AC-2", "text": "Searching by name or email filters the table."},
        {"id": "AC-3", "text": "Clicking Export CSV downloads the filtered rows."},
    ],
    "comments": [],
    "attachments": [],
    "customFields": {},
}

_FIXTURES_ROOT: str | None = None


def _fixtures_root() -> str:
    global _FIXTURES_ROOT
    if _FIXTURES_ROOT is None:
        root = Path(tempfile.mkdtemp()) / "fixtures"
        (root / "jira").mkdir(parents=True, exist_ok=True)
        (root / "jira" / "PROJ-1234.json").write_text(
            json.dumps(_JIRA_FIXTURE, indent=2), encoding="utf-8"
        )
        _FIXTURES_ROOT = str(root)
    return _FIXTURES_ROOT


def _make_project(tmp_path: Path) -> Path:
    """Create a minimal project dir that passes preflight."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text('"""App entry."""\n')
    return project


def _parse_run_id(response: str) -> str:
    data = json.loads(response)
    return data["runId"]


def _ctx(tmp_runs: str | None = None) -> ToolContext:
    runs = tmp_runs or tempfile.mkdtemp()
    cfg = Config(runs_root=runs)
    os.environ["UIFORGEMAX_USE_FIXTURES"] = "1"
    os.environ["UIFORGEMAX_FIXTURES_ROOT"] = _fixtures_root()
    os.environ["UIFORGEMAX_SKIP_MEDIATION"] = "1"
    return ToolContext.from_config(cfg)


def test_runs_root_not_in_project(tmp_path):
    ctx = _ctx(str(tmp_path / "appdata-runs"))
    project = _make_project(tmp_path)
    preflight.preflight(ctx, project_root=str(project))
    resp = lifecycle.start_run(ctx, project_root=str(project))
    run_id = _parse_run_id(resp)
    run_dir = ctx.store.run_dir(run_id)
    assert str(tmp_path) in str(run_dir)
    assert ".uiforgemax" not in str(run_dir)


def test_cannot_approve_plan_before_plan_ready(tmp_path):
    ctx = _ctx()
    project = _make_project(tmp_path)
    preflight.preflight(ctx, project_root=str(project))
    run_id = _parse_run_id(lifecycle.start_run(ctx, project_root=str(project)))
    with pytest.raises(GateError):
        approvals.approve_plan(ctx, run_id)


def test_cannot_add_input_after_pipeline_started(tmp_path):
    ctx = _ctx()
    project = _make_project(tmp_path)
    preflight.preflight(ctx, project_root=str(project))
    run_id = _parse_run_id(lifecycle.start_run(ctx, project_root=str(project)))
    inputs.add_prompt(ctx, run_id, "x")
    pipeline.advance(ctx, run_id)
    with pytest.raises(GateError):
        inputs.add_prompt(ctx, run_id, "sneak in a change")


def test_advance_blocks_without_inputs(tmp_path):
    ctx = _ctx()
    project = _make_project(tmp_path)
    preflight.preflight(ctx, project_root=str(project))
    run_id = _parse_run_id(lifecycle.start_run(ctx, project_root=str(project)))
    out = pipeline.advance(ctx, run_id)
    data = json.loads(out)
    assert "no inputs" in data["message"].lower()


def test_mcp_response_includes_next_tool(tmp_path):
    ctx = _ctx()
    project = _make_project(tmp_path)
    preflight.preflight(ctx, project_root=str(project))
    resp = lifecycle.start_run(ctx, project_root=str(project))
    data = json.loads(resp)
    assert data["nextTool"] in ("uiforgemax_add_prompt", "uiforgemax_add_jira")
    assert "uiforgemax_implement" in data["blockedTools"]


def test_start_run_blocks_without_workspace():
    ctx = _ctx()
    from uiforgemax.session import session_path

    sp = session_path()
    if sp.exists():
        sp.unlink()
    out = lifecycle.start_run(ctx)
    data = json.loads(out)
    assert data.get("ok") is False or "BLOCKED" in data.get("message", "")
    assert "preflight" in data.get("nextTool", "").lower() or "preflight" in data.get("message", "").lower()


def test_preflight_passes(tmp_path):
    ctx = _ctx()
    project = _make_project(tmp_path)
    out = preflight.preflight(ctx, project_root=str(project))
    data = json.loads(out)
    assert data["ok"] is True
    assert data["session"]["graphifyReady"] is True
    assert "pythonExecutable" in data["session"]


def test_add_image_records_role(tmp_path):
    ctx = _ctx()
    project = _make_project(tmp_path)
    preflight.preflight(ctx, project_root=str(project))
    run_id = _parse_run_id(lifecycle.start_run(ctx, project_root=str(project)))
    img = tmp_path / "before.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    inputs.add_image(ctx, run_id, str(img), role="before")
    manifest = json.loads(
        (ctx.store.run_dir(run_id) / "inputs" / "images.json").read_text(encoding="utf-8")
    )
    assert manifest["images"][0]["role"] == "before"


def test_request_changes_delta_rewinds(tmp_path):
    ctx = _ctx()
    empty = tmp_path / "greenfield-app"
    empty.mkdir()
    preflight.preflight(ctx, project_root=str(empty))
    run_id = _parse_run_id(lifecycle.start_run(ctx, project_root=str(empty)))
    inputs.add_prompt(ctx, run_id, "Build a new app with API and UI")
    pipeline.advance(ctx, run_id)
    state = ctx.store.load(run_id)
    assert state.status == Status.AWAITING_PLAN_APPROVAL
    approvals.request_changes(ctx, run_id, "use a side panel, not a detail page")
    state = ctx.store.load(run_id)
    assert state.approvals.plan.feedback is not None
    assert state.current_stage.value == "7_plan"


def test_mediation_pauses_at_classify_first(monkeypatch, tmp_path):
    ctx = _ctx()
    monkeypatch.setenv("UIFORGEMAX_SKIP_MEDIATION", "0")
    project = _make_project(tmp_path)
    preflight.preflight(ctx, project_root=str(project))
    run_id = _parse_run_id(lifecycle.start_run(ctx, project_root=str(project)))
    inputs.add_jira(ctx, run_id, "PROJ-1234")
    out = pipeline.advance(ctx, run_id)
    data = json.loads(out)
    assert data["status"] == Status.AWAITING_MEDIATION.value
    assert data["nextTool"] == "uiforgemax_submit_mediation"
    assert "modelMediation" in data
    assert data["modelMediation"]["kind"] == "REQUEST_CLASSIFICATION"

    run_dir = ctx.store.run_dir(run_id)
    assert (run_dir / "classification-signals.json").exists()
    default = json.loads((run_dir / "request-classification.json").read_text(encoding="utf-8"))
    assert default["requestType"]
    assert default["surface"]


def test_classify_default_when_mediation_skipped(tmp_path):
    ctx = _ctx()
    project = _make_project(tmp_path)
    preflight.preflight(ctx, project_root=str(project))
    run_id = _parse_run_id(lifecycle.start_run(ctx, project_root=str(project)))
    inputs.add_jira(ctx, run_id, "PROJ-1234")
    pipeline.advance(ctx, run_id)
    state = ctx.store.load(run_id)
    assert state.artifacts.get("requestClassification") == "request-classification.json"
    cls = json.loads(
        (ctx.store.run_dir(run_id) / "request-classification.json").read_text(encoding="utf-8")
    )
    assert cls["source"] == "heuristic_default"
