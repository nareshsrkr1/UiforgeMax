"""Submit IDE model mediation responses."""

from __future__ import annotations

import json
from pathlib import Path

from uiforgemax.mcp_response import tool_response
from uiforgemax.model_mediation.registry import (
    MediationKind,
    attach_artifact_contents,
    build_mediation_request,
)
from uiforgemax.model_mediation.service import (
    apply_mediation,
    load_mediation_request,
    next_pending_mediation,
    save_mediation_request,
    save_mediation_response,
)
from uiforgemax.state import Stage, Status
from uiforgemax.state.gates import assert_gate
from uiforgemax.tools.context import ToolContext


def _resolve_payload(payload: str, payload_file: str | None, run_dir: Path) -> tuple[dict | None, str | None]:
    """Resolve mediation payload from string, file path, or file: prefix.

    Returns (data, error). On success error is None; on failure data is None.
    """
    # Explicit file parameter takes precedence.
    if payload_file:
        return _read_payload_file(payload_file, run_dir)

    # file: prefix — agent writes payload to disk, passes path via MCP string param.
    if payload.startswith("file:"):
        return _read_payload_file(payload[5:].strip(), run_dir)

    # Inline JSON (works for small payloads that fit in a single stdio line).
    try:
        return json.loads(payload), None
    except json.JSONDecodeError as exc:
        return None, f"payload must be valid JSON: {exc}"


def _read_payload_file(raw_path: str, run_dir: Path) -> tuple[dict | None, str | None]:
    """Read a JSON payload from a file path (absolute or relative to run dir)."""
    path = Path(raw_path)
    if not path.is_absolute():
        path = run_dir / path
    if not path.exists():
        return None, f"payload file not found: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data, None
    except json.JSONDecodeError as exc:
        return None, f"payload file is not valid JSON: {exc}"
    except OSError as exc:
        return None, f"could not read payload file: {exc}"


def submit_mediation(
    ctx: ToolContext,
    run_id: str,
    mediation_key: str,
    payload: str,
    payload_file: str | None = None,
) -> str:
    state = ctx.store.load(run_id)
    assert_gate("uiforgemax_submit_mediation", state)
    run_dir = ctx.store.run_dir(run_id)
    pending_key = state.artifacts.get("pendingMediation")
    if pending_key and pending_key != mediation_key:
        return tool_response(
            state,
            f"BLOCKED: expected mediationKey '{pending_key}', got '{mediation_key}'.",
            stop=True,
        )

    data, error = _resolve_payload(payload, payload_file, run_dir)
    if error:
        return tool_response(state, f"BLOCKED: {error}", stop=True)

    kind_str = mediation_key.split("::")[-1]
    kind = MediationKind(kind_str)
    if kind == MediationKind.TEST_GENERATION:
        from uiforgemax.pipeline.testing import validate_test_generation

        ok, reason = validate_test_generation(run_dir, data)
        if not ok:
            return tool_response(
                state,
                f"BLOCKED: TEST_GENERATION rejected — {reason}. "
                "Re-read approved plan + inputs/page.html / visual-spec.json and resubmit "
                "with real tests[] + run[] (or justified skipTests).",
                stop=True,
                extra={"runsDir": str(run_dir), "modelMediation": state.mediation.get("pending")},
            )
    if kind == MediationKind.TEST_ENV_RECOVERY:
        from uiforgemax.pipeline.testing import validate_test_env_recovery

        ok, reason = validate_test_env_recovery(run_dir, data)
        if not ok:
            return tool_response(
                state,
                f"BLOCKED: TEST_ENV_RECOVERY rejected — {reason}. "
                "Re-read tests/toolchain-facts.json and resubmit with installHints[] / corrected run[].",
                stop=True,
                extra={"runsDir": str(run_dir), "modelMediation": state.mediation.get("pending")},
            )
    if kind == MediationKind.PLAN_REFINEMENT:
        from uiforgemax.pipeline.planning import (
            plan_actions_missing_content,
            plan_actions_noop_modifies,
            plan_actions_outside_delta_scope,
            plan_actions_placeholder_creates,
        )

        # Merge-shaped payload: validate the create/modify it would write.
        probe = {
            "create": data.get("create") if data.get("create") is not None else [],
            "modify": data.get("modify") if data.get("modify") is not None else [],
        }
        # If mediation omitted both keys, keep existing plan check after apply —
        # but when it sends path lists, every entry must be writable.
        if probe["create"] or probe["modify"]:
            missing = plan_actions_missing_content(probe)
            if missing:
                return tool_response(
                    state,
                    "BLOCKED: PLAN_REFINEMENT rejected — create/modify entries need full "
                    f"`content` (or known greenfield templateId/patchId). Missing for: "
                    f"{', '.join(missing[:12])}{'…' if len(missing) > 12 else ''}. "
                    "Re-read graph/source-snapshots.json, edit each file, and resubmit "
                    "with content= the FULL file body.",
                    stop=True,
                    extra={"runsDir": str(run_dir), "modelMediation": state.mediation.get("pending")},
                )

            snapshots_path = run_dir / "graph" / "source-snapshots.json"
            snapshots = None
            if snapshots_path.exists():
                try:
                    snapshots = json.loads(snapshots_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    snapshots = None
            noop = plan_actions_noop_modifies(probe, snapshots)
            if noop:
                return tool_response(
                    state,
                    "BLOCKED: PLAN_REFINEMENT rejected — these 'modify' actions return content "
                    "IDENTICAL to the original file (no actual change): "
                    f"{', '.join(noop[:12])}{'…' if len(noop) > 12 else ''}. "
                    "A modify action must apply the requirement's real change. Re-read "
                    "graph/source-snapshots.json, make the actual edit, and resubmit with the "
                    "genuinely modified full file body — or move the path to 'create' only if "
                    "it doesn't really need editing.",
                    stop=True,
                    extra={"runsDir": str(run_dir), "modelMediation": state.mediation.get("pending")},
                )

            placeholders = plan_actions_placeholder_creates(probe)
            if placeholders:
                return tool_response(
                    state,
                    "BLOCKED: PLAN_REFINEMENT rejected — these 'create' actions are trivial "
                    "placeholders (TODO/stub/near-empty), not a real implementation: "
                    f"{', '.join(placeholders[:12])}{'…' if len(placeholders) > 12 else ''}. "
                    "Write the actual full file body implementing the requirement.",
                    stop=True,
                    extra={"runsDir": str(run_dir), "modelMediation": state.mediation.get("pending")},
                )

            delta_path = run_dir / "plans" / "visual-delta.json"
            if delta_path.exists():
                try:
                    visual_delta = json.loads(delta_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    visual_delta = None
                out_of_scope = plan_actions_outside_delta_scope(probe, visual_delta)
                if out_of_scope:
                    failed_ids = sorted(
                        {
                            str(st.get("subtaskId"))
                            for st in ((visual_delta or {}).get("failedSubtasks") or [])
                            if st.get("subtaskId")
                        }
                    )
                    return tool_response(
                        state,
                        "BLOCKED: PLAN_REFINEMENT rejected — this is a visual-fidelity DELTA "
                        f"re-plan scoped to sub-tasks {failed_ids}, but these actions are "
                        "missing subtaskId or tag a sub-task that already passed: "
                        f"{', '.join(out_of_scope[:12])}{'…' if len(out_of_scope) > 12 else ''}. "
                        "Set subtaskId on every create/modify action to one of the failed IDs "
                        "above, and do not re-touch sub-tasks that already passed.",
                        stop=True,
                        extra={"runsDir": str(run_dir), "modelMediation": state.mediation.get("pending")},
                    )
    save_mediation_response(run_dir, mediation_key, data)
    apply_mediation(run_dir, kind, data)

    stage = Stage(mediation_key.split("::")[0])
    nxt = next_pending_mediation(run_dir, state, stage)
    if nxt:
        st, kind = nxt
        request = build_mediation_request(st, kind, run_dir, state)
        save_mediation_request(run_dir, request)
        state.artifacts["pendingMediation"] = request["mediationKey"]
        state.mediation["pending"] = request
        state.status = Status.AWAITING_MEDIATION
        state.record(stage, "mediation_partial", kind.value)
        ctx.store.save(state)
        return tool_response(
            state,
            f"Mediation saved. Next IDE mediation required: {kind.value}",
            stop=True,
            extra={
                "modelMediation": attach_artifact_contents(request, run_dir),
                "runsDir": str(run_dir),
            },
        )

    state.status = _status_after_stage(stage)
    state.artifacts.pop("pendingMediation", None)
    state.mediation.pop("pending", None)
    state.record(stage, "mediation_complete", mediation_key)
    ctx.store.save(state)
    return tool_response(
        state,
        f"Mediation complete for {mediation_key}. Call uiforgemax_advance.",
        extra={"runsDir": str(run_dir)},
    )


def mediation_extra(ctx: ToolContext, state) -> dict:
    run_dir = ctx.store.run_dir(state.run_id)
    key = state.artifacts.get("pendingMediation", "")
    request = state.mediation.get("pending") or load_mediation_request(run_dir, key)
    extra: dict = {"runsDir": str(run_dir)}
    if request:
        extra["modelMediation"] = attach_artifact_contents(request, run_dir)
    return extra


def _status_after_stage(stage: Stage) -> Status:
    mapping = {
        Stage.CLASSIFY: Status.CLASSIFIED,
        Stage.NORMALIZE: Status.NORMALIZED,
        Stage.REQUIREMENT_MAP: Status.REQUIREMENT_MAPPED,
        Stage.PLAN: Status.PLAN_READY,
        # Re-enter implement so post-review gate / skip-rewrite can run.
        Stage.IMPLEMENT: Status.IMPLEMENTING,
        # Stay on testing so advance re-runs _test after GENERATION / ENV_RECOVERY.
        Stage.TEST: Status.TESTING,
        Stage.VISUAL_VALIDATE: Status.VISUAL_VALIDATED,
    }
    return mapping.get(stage, Status.NORMALIZED)
