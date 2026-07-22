"""Tests for dynamic flow routing."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from uiforgemax.config import Config
from uiforgemax.pipeline.flow_router import build_flow_plan, is_stage_active, load_flow
from uiforgemax.state import Stage, Status
from uiforgemax.tools import ToolContext, approvals, inputs, lifecycle, pipeline, preflight


def _ctx() -> ToolContext:
    runs = tempfile.mkdtemp()
    cfg = Config(runs_root=runs)
    os.environ["UIFORGEMAX_SKIP_MEDIATION"] = "1"
    return ToolContext.from_config(cfg)


def _parse_run_id(response: str) -> str:
    return json.loads(response)["runId"]


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text('"""App."""\n')
    return project


def test_flow_plan_ui_only_skips_api():
    flow = build_flow_plan(
        {"requestType": "enhancement", "surface": "ui_only", "recommendedPolicy": "frontend_first"},
        {"inputModes": ["prompt", "image"], "imageCount": 1, "projectRoot": {"empty": False}},
        type("S", (), {"inputs": {"modes": ["prompt", "image"]}, "architecture": {}})(),
    )
    assert not is_stage_active(Stage.API_RESOLVE, flow)
    assert not is_stage_active(Stage.GATE_API, flow)
    assert is_stage_active(Stage.GRAPHIFY_UPDATE, flow)


def test_flow_plan_greenfield_skips_graph():
    flow = build_flow_plan(
        {
            "requestType": "greenfield",
            "surface": "full_stack",
            "recommendedPolicy": "full_stack",
            "greenfieldScaffold": True,
            "useGraph": False,
        },
        {"inputModes": ["prompt"], "projectRoot": {"empty": True}, "architecture": {"primary": "greenfield"}},
        type("S", (), {"inputs": {"modes": ["prompt"]}, "architecture": {"primary": "greenfield", "empty": True}})(),
    )
    assert not is_stage_active(Stage.GRAPHIFY_UPDATE, flow)
    assert not is_stage_active(Stage.REQUIREMENT_MAP, flow)
    assert is_stage_active(Stage.IMPLEMENT, flow)
    assert flow["flags"]["greenfieldScaffold"] is True


def test_greenfield_e2e_scaffold(tmp_path):
    ctx = _ctx()
    empty = tmp_path / "greenfield-app"
    empty.mkdir()
    preflight.preflight(ctx, project_root=str(empty))
    run_id = _parse_run_id(lifecycle.start_run(ctx, project_root=str(empty)))
    inputs.add_prompt(ctx, run_id, "Build a greenfield app from scratch with API and UI")

    pipeline.advance(ctx, run_id)
    state = ctx.store.load(run_id)
    assert state.status == Status.AWAITING_PLAN_APPROVAL
    assert state.approvals.understanding.by == "system-auto"

    flow = load_flow(ctx.store.run_dir(run_id))
    assert flow is not None
    assert "3_graphify_update" in flow.get("skippedStages", {})

    run_dir = ctx.store.run_dir(run_id)
    assert (run_dir / "graph" / "requirement-map.json").exists()
    assert json.loads((run_dir / "graph" / "requirement-map.json").read_text())["source"] == "greenfield_scaffold"

    approvals.approve_plan(ctx, run_id)
    pipeline.advance(ctx, run_id)

    state = ctx.store.load(run_id)
    assert state.status == Status.COMPLETED
    assert (empty / "backend" / "app" / "main.py").exists()
    assert (empty / "ui" / "index.html").exists()


def test_ui_only_skips_api_stages(tmp_path):
    ctx = _ctx()
    project = _make_project(tmp_path)
    preflight.preflight(ctx, project_root=str(project))
    run_id = _parse_run_id(lifecycle.start_run(ctx, project_root=str(project)))
    img = tmp_path / "wire.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    inputs.add_prompt(ctx, run_id, "Update page styling only — UI changes")
    inputs.add_image(ctx, run_id, str(img), role="wireframe")

    pipeline.advance(ctx, run_id)
    flow = load_flow(ctx.store.run_dir(run_id))
    assert flow["surface"] == "ui_only"
    assert "5_api_resolve" in flow.get("skippedStages", {})

    state = ctx.store.load(run_id)
    assert state.approvals.api.required is False
    assert state.approvals.api.approved is True
