"""Structured MCP tool responses — MCP drives the agent, not markdown prompts."""

from __future__ import annotations

import json
from typing import Any

from uiforgemax.intake_hints import prefer_jira_intake
from uiforgemax.state import RunState, Status


def _pending_issue_key(state: RunState) -> str | None:
    raw = (state.inputs or {}).get("pendingIssueKey")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().upper()
    return None


def _gate_next(
    state: RunState,
    *,
    jira_configured: bool = False,
) -> tuple[str | None, list[str], list[str]]:
    """Derive next_tool / alternatives / blocked_tools from run state."""
    status = state.status
    blocked = ["uiforgemax_implement"]

    if status == Status.AWAITING_MEDIATION:
        return "uiforgemax_submit_mediation", ["uiforgemax_get_run_status"], blocked + ["uiforgemax_advance"]
    if status == Status.AWAITING_USER_INSTALL:
        # Human finishes npm/pip install; agent resumes with advance (tests-only retry).
        return "uiforgemax_advance", ["uiforgemax_resume_run", "uiforgemax_get_run_status"], blocked
    if status == Status.INTAKE:
        pending = _pending_issue_key(state)
        if prefer_jira_intake(jira_configured=jira_configured, pending_issue_key=pending):
            return "uiforgemax_add_jira", ["uiforgemax_add_prompt", "uiforgemax_add_image"], blocked
        return "uiforgemax_add_prompt", ["uiforgemax_add_jira", "uiforgemax_add_image"], blocked
    if status == Status.AWAITING_API_APPROVAL:
        return "uiforgemax_approve_api", ["uiforgemax_request_changes"], blocked
    if status in (Status.UNDERSTANDING_READY, Status.AWAITING_UNDERSTANDING_APPROVAL):
        return "uiforgemax_approve_understanding", ["uiforgemax_request_changes"], blocked
    if status in (Status.PLAN_REVIEWED, Status.AWAITING_PLAN_APPROVAL):
        from uiforgemax.env_flags import skip_plan_approval

        # Default: no nextTool — human must approve in chat; agent must not auto-approve.
        # Dev/CI: UIFORGEMAX_SKIP_PLAN_APPROVAL=1 → nextTool stays approve_plan.
        if skip_plan_approval():
            return "uiforgemax_approve_plan", ["uiforgemax_request_changes"], blocked
        return None, ["uiforgemax_approve_plan", "uiforgemax_request_changes"], blocked
    if status == Status.COMPLETED:
        return None, [], []
    if status in Status.terminal():
        return None, [], []
    return "uiforgemax_advance", ["uiforgemax_get_run_status"], blocked


def tool_response(
    state: RunState,
    message: str,
    *,
    stop: bool = False,
    extra: dict[str, Any] | None = None,
    jira_configured: bool | None = None,
) -> str:
    if jira_configured is None:
        # Lazy env check so intake can prefer add_jira when MCP has Jira credentials.
        from uiforgemax.config import Config

        jira_configured = Config.from_env().jira.is_configured
    next_tool, alternatives, blocked_tools = _gate_next(
        state, jira_configured=bool(jira_configured)
    )
    body: dict[str, Any] = {
        "runId": state.run_id,
        "status": state.status.value,
        "currentStage": state.current_stage.value,
        "message": message,
        "stop": stop,
        "nextTool": next_tool,
        "alternatives": alternatives,
        "blockedTools": blocked_tools,
        "artifactsPath": str(state.run_id),
        "approvals": {
            "api": state.approvals.api.model_dump(),
            "understanding": state.approvals.understanding.model_dump(),
            "plan": state.approvals.plan.model_dump(),
        },
    }
    pending = _pending_issue_key(state)
    if pending and state.status == Status.INTAKE:
        body["suggestedIssueKey"] = pending
    if state.status in (Status.PLAN_REVIEWED, Status.AWAITING_PLAN_APPROVAL):
        body["humanGate"] = "plan"
        body["waitForHuman"] = True
    if state.status == Status.AWAITING_USER_INSTALL:
        body["humanGate"] = "install"
        body["waitForHuman"] = True
        body["resumeHint"] = {
            "say": "resume",
            "nextTool": "uiforgemax_advance",
            "runId": state.run_id,
            "stage": "10_test",
            "note": "After local install finishes, advance/resume retries tests only — do not start a new run.",
        }
    if extra:
        body.update(extra)
    return json.dumps(body, indent=2)
