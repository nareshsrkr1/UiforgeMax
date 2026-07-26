"""Pipeline driver."""

from __future__ import annotations

from uiforgemax.mcp_response import tool_response
from uiforgemax.pipeline.flow_router import is_stage_active, load_flow, next_active_stage, resolve_flow
from uiforgemax.stages import run_stage
from uiforgemax.state import Status
from uiforgemax.tools.context import ToolContext
from uiforgemax.tools.mediation import mediation_extra

_MAX_STEPS = 32


def advance(ctx: ToolContext, run_id: str) -> str:
    state = ctx.store.load(run_id)
    if state.status == Status.AWAITING_MEDIATION:
        return tool_response(
            state,
            "BLOCKED: IDE model mediation pending. Read modelMediation artifacts, then uiforgemax_submit_mediation.",
            stop=True,
            extra=mediation_extra(ctx, state),
        )
    if state.status in Status.terminal():
        return tool_response(state, f"Run terminal ({state.status.value}).", stop=True)

    run_dir = ctx.store.run_dir(run_id)
    log: list[str] = []
    steps = 0

    while steps < _MAX_STEPS:
        steps += 1
        stage = state.current_stage
        flow = load_flow(run_dir) or state.flow or resolve_flow(run_dir, state)

        if not is_stage_active(stage, flow):
            reason = flow.get("skippedStages", {}).get(stage.value, "flow branch")
            state.record(stage, "skipped", reason)
            log.append(f"[{stage.value}] skipped ({reason})")
            nxt = next_active_stage(stage, flow)
            if nxt is None:
                ctx.store.save(state)
                return tool_response(state, "\n".join(log), stop=True)
            state.current_stage = nxt
            ctx.store.save(state)
            continue

        try:
            result = run_stage(ctx, state, stage)
        except ValueError as exc:
            ctx.store.save(state)
            return tool_response(state, f"BLOCKED: {exc}", stop=True)

        log.append(f"[{stage.value}] {result.message}")
        ctx.store.save(state)

        if result.stop:
            extra = mediation_extra(ctx, state)
            if result.mediation:
                extra["modelMediation"] = result.mediation
            if result.extra:
                extra.update(result.extra)
            if state.flow:
                extra["runFlow"] = state.flow
            # Re-attach human gate packages so the agent always has detail to display.
            if state.status == Status.AWAITING_UNDERSTANDING_APPROVAL and "understandingApproval" not in extra:
                from uiforgemax.pipeline.planning import build_understanding_approval_package

                extra["understandingApproval"] = build_understanding_approval_package(run_dir)
            if state.status == Status.AWAITING_PLAN_APPROVAL and "planApproval" not in extra:
                from uiforgemax.pipeline.planning import build_plan_approval_package

                # Keep wire small: full package is also written under plans/.
                pkg = build_plan_approval_package(run_dir)
                extra["planApproval"] = pkg
                extra["planApprovalFile"] = "plans/plan-approval.json"
            return tool_response(
                state,
                "advance log:\n" + "\n".join(log),
                stop=True,
                extra=extra,
            )

        flow = load_flow(run_dir) or state.flow or flow
        nxt = next_active_stage(stage, flow)
        if nxt is None:
            return tool_response(state, "\n".join(log), stop=True)
        state.current_stage = nxt

    return tool_response(state, "\n".join(log) + "\n(step limit reached)", stop=True)
