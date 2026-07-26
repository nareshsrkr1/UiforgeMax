"""Pipeline driver."""

from __future__ import annotations

from uiforgemax.mcp_response import tool_response
from uiforgemax.pipeline.flow_router import is_stage_active, load_flow, next_active_stage, resolve_flow
from uiforgemax.pipeline.ide_apply import ide_apply_brief
from uiforgemax.stages import run_stage
from uiforgemax.state import Stage, Status
from uiforgemax.tools.context import ToolContext
from uiforgemax.tools.mediation import mediation_extra

_MAX_STEPS = 32


def advance(ctx: ToolContext, run_id: str) -> str:
    state = ctx.store.load(run_id)
    if state.status == Status.AWAITING_MEDIATION:
        return tool_response(
            state,
            "BLOCKED: IDE model mediation pending. Use mediationBrief "
            "(or uiforgemax_get_run_status) then uiforgemax_submit_mediation.",
            stop=True,
            extra=mediation_extra(ctx, state, full=False),
        )
    if state.status == Status.AWAITING_IDE_APPLY:
        # Jump straight to implement verify — never re-run earlier stages blind.
        state.current_stage = Stage.IMPLEMENT
        ctx.store.save(state)
    if state.status in Status.terminal():
        # Recover FAILED partial-implement into IDE apply instead of dead-ending.
        if state.status == Status.FAILED and state.current_stage == Stage.IMPLEMENT:
            run_dir = ctx.store.run_dir(run_id)
            plan_path = run_dir / "plans" / "approved-plan.json"
            if not plan_path.exists():
                plan_path = run_dir / "plans" / "implementation-plan.json"
            if plan_path.exists():
                import json

                try:
                    plan = json.loads(plan_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    plan = {}
                state.status = Status.AWAITING_IDE_APPLY
                state.artifacts["ideApply"] = True
                state.current_stage = Stage.IMPLEMENT
                ctx.store.save(state)
                return tool_response(
                    state,
                    "Recovered from FAILED implement — IDE apply required. "
                    "Edit ideApplyBrief paths with IDE tools, then call uiforgemax_advance once.",
                    stop=True,
                    extra={
                        "ideApplyBrief": ide_apply_brief(plan),
                        "waitForIdeApply": True,
                        "doNotAdvanceUntilEdited": True,
                    },
                )
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
            # Brief-only on advance — full mediation/plan packages blow IDE MCP
            # size limits and become content.json pointers that break the flow.
            extra = mediation_extra(ctx, state, full=False)
            if result.extra:
                # Keep small diagnostic keys; drop bulky nested packages.
                for k, v in result.extra.items():
                    if k in {"planApproval", "understandingApproval", "modelMediation", "runFlow"}:
                        continue
                    extra[k] = v
            if state.flow:
                flags = (state.flow or {}).get("flags") or {}
                extra["runFlowBrief"] = {
                    "surface": state.flow.get("surface"),
                    "requestType": state.flow.get("requestType"),
                    "flags": flags,
                }
            if state.status == Status.AWAITING_PLAN_APPROVAL:
                from uiforgemax.pipeline.planning import build_plan_approval_package

                pkg = build_plan_approval_package(run_dir)
                # Write full package for human display via get_run_status / file.
                (run_dir / "plans" / "plan-approval.json").write_text(
                    __import__("json").dumps(pkg, indent=2), encoding="utf-8"
                )
                extra["planApprovalBrief"] = {
                    "summary": pkg.get("summary"),
                    "issueKey": pkg.get("issueKey"),
                    "filesToCreate": [
                        (f.get("path") if isinstance(f, dict) else f)
                        for f in (pkg.get("filesToCreate") or [])
                    ][:80],
                    "filesToModify": [
                        (f.get("path") if isinstance(f, dict) else f)
                        for f in (pkg.get("filesToModify") or [])
                    ][:80],
                    "planApprovalFile": "plans/plan-approval.json",
                }
                extra["planApprovalFile"] = "plans/plan-approval.json"
            # Truncate stage log — long multi-stage advances were a spill source.
            msg = "advance log:\n" + "\n".join(log[-12:])
            return tool_response(state, msg, stop=True, extra=extra)

        flow = load_flow(run_dir) or state.flow or flow
        nxt = next_active_stage(stage, flow)
        if nxt is None:
            return tool_response(state, "\n".join(log), stop=True)
        state.current_stage = nxt

    return tool_response(state, "\n".join(log) + "\n(step limit reached)", stop=True)
