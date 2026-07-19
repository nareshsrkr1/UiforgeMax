"""Human plan gate: wait for approval; optional env skip."""

from __future__ import annotations

import json

from uiforgemax.env_flags import skip_plan_approval
from uiforgemax.mcp_response import tool_response
from uiforgemax.state import RunState, Stage, Status


def test_plan_gate_has_no_next_tool_until_human(monkeypatch):
    monkeypatch.delenv("UIFORGEMAX_SKIP_PLAN_APPROVAL", raising=False)
    state = RunState(
        run_id="t",
        project_root="C:/tmp",
        status=Status.AWAITING_PLAN_APPROVAL,
        current_stage=Stage.GATE_PLAN,
    )
    data = json.loads(tool_response(state, "gate"))
    assert data["nextTool"] is None
    assert data["waitForHuman"] is True
    assert "uiforgemax_approve_plan" in data["alternatives"]


def test_skip_plan_approval_env(monkeypatch):
    monkeypatch.delenv("UIFORGEMAX_SKIP_PLAN_APPROVAL", raising=False)
    assert skip_plan_approval() is False
    monkeypatch.setenv("UIFORGEMAX_SKIP_PLAN_APPROVAL", "1")
    assert skip_plan_approval() is True
    data = json.loads(
        tool_response(
            RunState(
                run_id="t",
                status=Status.AWAITING_PLAN_APPROVAL,
                current_stage=Stage.GATE_PLAN,
            ),
            "gate",
        )
    )
    assert data["nextTool"] == "uiforgemax_approve_plan"
