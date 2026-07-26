"""Approval tools."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from uiforgemax.mcp_response import tool_response
from uiforgemax.model_mediation.service import clear_mediation_responses
from uiforgemax.state import Stage, Status
from uiforgemax.state.gates import assert_gate
from uiforgemax.tools.context import ToolContext


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _freeze_approved_plan(run_dir: Path) -> None:
    """Snapshot the plan the human approved — implement must use this file."""
    src = run_dir / "plans" / "implementation-plan.json"
    dest = run_dir / "plans" / "approved-plan.json"
    if src.exists():
        shutil.copy2(src, dest)
        meta = {
            "frozenAt": _now(),
            "source": "plans/implementation-plan.json",
            "note": "Implement uses this snapshot; plan stages must not rewrite it after approval.",
        }
        (run_dir / "plans" / "approved-plan.meta.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )


def _clear_approved_plan(run_dir: Path) -> None:
    from uiforgemax.pipeline.visual_validate import clear_visual_delta

    for name in ("approved-plan.json", "approved-plan.meta.json"):
        path = run_dir / "plans" / name
        if path.exists():
            path.unlink()
    # A stale post-implement-review.json (from a prior implement cycle) must not
    # survive a rewind to PLAN — _implement() treats an existing review with
    # passesReview != false as "already passed" and returns early WITHOUT calling
    # apply_plan() again, so delta fixes from a fresh PLAN_REFINEMENT would never
    # actually get written to disk.
    review_path = run_dir / "implementation" / "post-implement-review.json"
    if review_path.exists():
        archive = run_dir / "implementation" / "post-implement-review.superseded.json"
        try:
            review_path.replace(archive)
        except OSError:
            review_path.unlink(missing_ok=True)
    # Same for visual-delta.json — request_changes / full replan must not inherit
    # a prior visual-fidelity ST-* scope lock.
    clear_visual_delta(run_dir, reason="approved-plan-cleared")


def approve_api(ctx: ToolContext, run_id: str, by: str = "user") -> str:
    state = ctx.store.load(run_id)
    assert_gate("uiforgemax_approve_api", state)
    state.approvals.api.approved = True
    state.approvals.api.at = _now()
    state.approvals.api.by = by
    state.current_stage = Stage.GATE_API
    state.status = Status.API_RESOLVED
    state.record(Stage.GATE_API, "approved", by)
    ctx.store.save(state)
    return tool_response(state, "API gate approved. Call uiforgemax_advance.")


def approve_understanding(ctx: ToolContext, run_id: str, by: str = "user") -> str:
    """Legacy no-op path — understanding is auto-approved; sole gate is plan."""
    state = ctx.store.load(run_id)
    # Allow when still in old waiting states OR already auto-passed.
    if state.status in (Status.UNDERSTANDING_READY, Status.AWAITING_UNDERSTANDING_APPROVAL):
        assert_gate("uiforgemax_approve_understanding", state)
    state.approvals.understanding.approved = True
    state.approvals.understanding.required = False
    state.approvals.understanding.at = _now()
    state.approvals.understanding.by = by
    if state.status in (Status.UNDERSTANDING_READY, Status.AWAITING_UNDERSTANDING_APPROVAL):
        state.current_stage = Stage.GATE_UNDERSTANDING
        state.record(Stage.GATE_UNDERSTANDING, "approved", by)
    ctx.store.save(state)
    return tool_response(
        state,
        "Understanding noted (no longer a separate human gate). "
        "Sole approval before implement is uiforgemax_approve_plan. Call uiforgemax_advance.",
    )


def approve_plan(ctx: ToolContext, run_id: str, by: str = "user") -> str:
    from uiforgemax.pipeline.planning import (
        EMPTY_PLAN_BLOCKER,
        MISSING_CONTENT_BLOCKER,
        plan_actions_missing_content,
        plan_has_file_actions,
        plan_is_implementable,
    )

    state = ctx.store.load(run_id)
    assert_gate("uiforgemax_approve_plan", state)
    run_dir = ctx.store.run_dir(run_id)
    plan_path = run_dir / "plans" / "implementation-plan.json"
    plan: dict = {}
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            plan = {}
    if not plan_has_file_actions(plan):
        state.status = Status.BLOCKED
        state.record(Stage.GATE_PLAN, "blocked", EMPTY_PLAN_BLOCKER)
        ctx.store.save(state)
        return tool_response(
            state,
            f"BLOCKED: {EMPTY_PLAN_BLOCKER} "
            "Do not approve an empty plan. Use request_changes / re-run PLAN_REFINEMENT "
            "so create/modify has real implementation files, then advance again.",
            stop=True,
        )
    if not plan_is_implementable(plan):
        missing = plan_actions_missing_content(plan)
        state.status = Status.BLOCKED
        state.record(Stage.GATE_PLAN, "blocked", MISSING_CONTENT_BLOCKER)
        ctx.store.save(state)
        return tool_response(
            state,
            f"BLOCKED: {MISSING_CONTENT_BLOCKER} "
            f"Missing content for: {', '.join(missing[:12])}"
            f"{'…' if len(missing) > 12 else ''}. "
            "Do not approve a path-only plan — re-run PLAN_REFINEMENT with full file "
            "`content` for every create/modify (from graph/source-snapshots.json).",
            stop=True,
        )
    _freeze_approved_plan(run_dir)
    state.approvals.plan.approved = True
    state.approvals.plan.at = _now()
    state.approvals.plan.by = by
    state.approvals.understanding.approved = True
    state.approvals.understanding.required = False
    state.current_stage = Stage.GATE_PLAN
    state.artifacts["approvedPlan"] = "plans/approved-plan.json"
    state.record(Stage.GATE_PLAN, "approved", by)
    ctx.store.save(state)
    return tool_response(
        state,
        "Plan approved and locked (plans/approved-plan.json). "
        "Call uiforgemax_advance to implement that exact plan — no re-planning.",
    )


def request_changes(
    ctx: ToolContext,
    run_id: str,
    feedback: str,
    *,
    subtask_ids: list[str] | None = None,
) -> str:
    state = ctx.store.load(run_id)
    status = state.status
    run_dir = ctx.store.run_dir(run_id)

    if status in (Status.UNDERSTANDING_READY, Status.AWAITING_UNDERSTANDING_APPROVAL):
        state.approvals.understanding.feedback = feedback
        state.approvals.understanding.approved = False
        state.current_stage = Stage.NORMALIZE
        state.status = Status.NORMALIZED
        clear_mediation_responses(run_dir)
        target = "normalize (delta merge)"
    elif status in (Status.PLAN_READY, Status.PLAN_REVIEWED, Status.AWAITING_PLAN_APPROVAL):
        if subtask_ids:
            structured = json.dumps({
                "type": "subtask_delta",
                "subtaskIds": subtask_ids,
                "feedback": feedback,
            })
            state.approvals.plan.feedback = structured
        else:
            state.approvals.plan.feedback = feedback
        state.approvals.plan.approved = False
        _clear_approved_plan(run_dir)
        state.current_stage = Stage.PLAN
        state.status = Status.PLAN_READY
        clear_mediation_responses(run_dir)
        target = "plan (delta revise)"
        if subtask_ids:
            target += f" [subtasks: {', '.join(subtask_ids)}]"
    else:
        return tool_response(
            state,
            f"BLOCKED: request_changes not valid in status '{status.value}'.",
            stop=True,
        )

    state.record(state.current_stage, "changes_requested", feedback[:200])
    ctx.store.save(state)
    return tool_response(state, f"Feedback recorded. Delta revise → {target}. Call uiforgemax_advance.")