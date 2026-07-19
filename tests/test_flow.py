"""Smoke and integration tests for the MCP pipeline."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from uiforgemax.config import Config
from uiforgemax.state import Status
from uiforgemax.state.gates import GateError
from uiforgemax.tools import ToolContext, approvals, inputs, lifecycle, pipeline, preflight

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM = REPO_ROOT / "platform"

# Self-contained Jira sample used only by the tests. The repo ships no sample
# fixtures; this is written to a throwaway temp dir at runtime.
_JIRA_FIXTURE = {
    "key": "PROJ-1234",
    "summary": "Customer List Page with search, pagination, and CSV export",
    "labels": ["frontend", "customer-domain", "match-exactly"],
    "description": (
        "Build a desktop customer list page in the customer portal.\n\n"
        "The page must show a paginated table of customers with Name, Email, "
        "Status, and Company columns.\nUsers must be able to search by name or "
        "email.\nUsers must be able to export the current filtered result set to "
        'CSV.\n\nThe Export CSV button label must match the wireframe exactly: '
        '"Export CSV".'
    ),
    "acceptanceCriteria": [
        {"id": "AC-1", "text": "A paginated customer table shows Name, Email, Status, Company."},
        {"id": "AC-2", "text": "Searching by name or email filters the table."},
        {"id": "AC-3", "text": "Clicking Export CSV downloads the filtered rows."},
        {"id": "AC-4", "text": "Styling and the Export CSV label match the wireframe exactly."},
    ],
    "comments": [
        {"author": "Product Owner", "created": "2026-07-15T09:00:00.000Z",
         "body": "Override: use the green primary theme for action buttons on this screen."},
        {"author": "Tech Lead", "created": "2026-07-15T11:30:00.000Z",
         "body": "Override: do not create a separate customer detail route; use a side panel."},
    ],
    "attachments": [],
    "customFields": {
        "resolutionPolicy": "frontend_first",
        "matchExactly": True,
        "targetApp": "customer-portal",
        "targetDomain": "customer",
    },
}

_FIXTURES_ROOT: str | None = None


def _fixtures_root() -> str:
    """Create (once) a temp fixtures dir holding the test Jira sample."""
    global _FIXTURES_ROOT
    if _FIXTURES_ROOT is None:
        root = Path(tempfile.mkdtemp()) / "fixtures"
        (root / "jira").mkdir(parents=True, exist_ok=True)
        (root / "jira" / "PROJ-1234.json").write_text(
            json.dumps(_JIRA_FIXTURE, indent=2), encoding="utf-8"
        )
        _FIXTURES_ROOT = str(root)
    return _FIXTURES_ROOT


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


def _workspace_copy() -> Path:
    dest = Path(tempfile.mkdtemp()) / "platform"
    shutil.copytree(PLATFORM, dest, ignore=shutil.ignore_patterns("node_modules", "dist"))
    return dest


def test_happy_path_stops_at_each_gate():
    ctx = _ctx()
    workspace = _workspace_copy()
    resp = lifecycle.start_run(ctx, project_root=str(workspace))
    run_id = _parse_run_id(resp)
    inputs.add_jira(ctx, run_id, "PROJ-1234")

    pipeline.advance(ctx, run_id)
    state = ctx.store.load(run_id)
    # Understanding auto-passes — sole human gate is plan approval.
    assert state.status == Status.AWAITING_PLAN_APPROVAL
    assert state.approvals.understanding.approved
    assert state.approvals.understanding.by == "system-auto"

    approvals.approve_plan(ctx, run_id)
    run_dir = ctx.store.run_dir(run_id)
    assert (run_dir / "plans" / "approved-plan.json").exists()
    pipeline.advance(ctx, run_id)
    state = ctx.store.load(run_id)
    # Real Graphify path: plan is evidence-based (no hardcoded CustomerListPage scaffold).
    # Implement/test may still FAIL when the ticket needs new files the graph didn't
    # invent — that's correct. Gates + graphify-out are what this test proves.
    assert state.status in {Status.COMPLETED, Status.FAILED, Status.TESTING}
    run_dir = ctx.store.run_dir(run_id)
    assert (workspace / "graphify-out" / "graph.json").exists()
    req_map = json.loads((run_dir / "graph" / "requirement-map.json").read_text(encoding="utf-8"))
    assert req_map.get("source") == "graphify-cli"
    assert "CustomerListPage.tsx" not in json.dumps(req_map.get("create", []))


def test_runs_root_not_in_project(tmp_path):
    ctx = _ctx(str(tmp_path / "appdata-runs"))
    resp = lifecycle.start_run(ctx, project_root=str(PLATFORM))
    run_id = _parse_run_id(resp)
    run_dir = ctx.store.run_dir(run_id)
    assert str(tmp_path) in str(run_dir)
    assert ".uiforgemax" not in str(run_dir)


def test_cannot_approve_plan_before_plan_ready():
    ctx = _ctx()
    run_id = _parse_run_id(lifecycle.start_run(ctx, project_root=str(PLATFORM)))
    # Fresh intake — plan gate not open yet.
    with pytest.raises(GateError):
        approvals.approve_plan(ctx, run_id)


def test_cannot_add_input_after_pipeline_started():
    ctx = _ctx()
    run_id = _parse_run_id(lifecycle.start_run(ctx, project_root=str(PLATFORM)))
    inputs.add_prompt(ctx, run_id, "x")
    pipeline.advance(ctx, run_id)
    with pytest.raises(GateError):
        inputs.add_prompt(ctx, run_id, "sneak in a change")


def test_advance_blocks_without_inputs():
    ctx = _ctx()
    preflight.preflight(ctx, project_root=str(PLATFORM))
    run_id = _parse_run_id(lifecycle.start_run(ctx, project_root=str(PLATFORM)))
    out = pipeline.advance(ctx, run_id)
    data = json.loads(out)
    assert "no inputs" in data["message"].lower()


def test_request_changes_delta_rewinds():
    ctx = _ctx()
    run_id = _parse_run_id(lifecycle.start_run(ctx, project_root=str(PLATFORM)))
    inputs.add_jira(ctx, run_id, "PROJ-1234")
    pipeline.advance(ctx, run_id)
    state = ctx.store.load(run_id)
    assert state.status == Status.AWAITING_PLAN_APPROVAL
    approvals.request_changes(ctx, run_id, "use a side panel, not a detail page")
    state = ctx.store.load(run_id)
    assert state.approvals.plan.feedback is not None
    assert state.current_stage.value == "7_plan"


def test_mcp_response_includes_next_tool():
    ctx = _ctx()
    preflight.preflight(ctx, project_root=str(PLATFORM))
    resp = lifecycle.start_run(ctx, project_root=str(PLATFORM))
    data = json.loads(resp)
    assert data["nextTool"] == "uiforgemax_add_prompt"
    assert "uiforgemax_implement" in data["blockedTools"]


def test_mediation_pauses_at_classify_first(monkeypatch):
    ctx = _ctx()
    monkeypatch.setenv("UIFORGEMAX_SKIP_MEDIATION", "0")
    workspace = _workspace_copy()
    run_id = _parse_run_id(lifecycle.start_run(ctx, project_root=str(workspace)))
    inputs.add_jira(ctx, run_id, "PROJ-1234")
    out = pipeline.advance(ctx, run_id)
    data = json.loads(out)
    assert data["status"] == Status.AWAITING_MEDIATION.value
    assert data["nextTool"] == "uiforgemax_submit_mediation"
    assert "modelMediation" in data
    assert data["modelMediation"]["kind"] == "REQUEST_CLASSIFICATION"

    # Deterministic signals + default classification are written before the pause.
    run_dir = ctx.store.run_dir(run_id)
    assert (run_dir / "classification-signals.json").exists()
    default = json.loads((run_dir / "request-classification.json").read_text(encoding="utf-8"))
    assert default["requestType"]
    assert default["surface"]


def test_classify_default_when_mediation_skipped():
    ctx = _ctx()  # skip mediation on
    workspace = _workspace_copy()
    run_id = _parse_run_id(lifecycle.start_run(ctx, project_root=str(workspace)))
    inputs.add_jira(ctx, run_id, "PROJ-1234")
    pipeline.advance(ctx, run_id)
    state = ctx.store.load(run_id)
    assert state.artifacts.get("requestClassification") == "request-classification.json"
    cls = json.loads(
        (ctx.store.run_dir(run_id) / "request-classification.json").read_text(encoding="utf-8")
    )
    assert cls["source"] == "heuristic_default"


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


def test_preflight_passes_with_platform():
    ctx = _ctx()
    out = preflight.preflight(ctx, project_root=str(PLATFORM))
    data = json.loads(out)
    assert data["ok"] is True
    assert data["session"]["graphifyReady"] is True
    assert "pythonExecutable" in data["session"]


def test_add_image_records_role():
    ctx = _ctx()
    preflight.preflight(ctx, project_root=str(PLATFORM))
    run_id = _parse_run_id(lifecycle.start_run(ctx, project_root=str(PLATFORM)))
    img = Path(tempfile.mkdtemp()) / "before.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    inputs.add_image(ctx, run_id, str(img), role="before")
    manifest = json.loads(
        (ctx.store.run_dir(run_id) / "inputs" / "images.json").read_text(encoding="utf-8")
    )
    assert manifest["images"][0]["role"] == "before"
