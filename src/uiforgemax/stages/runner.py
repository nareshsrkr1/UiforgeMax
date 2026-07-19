"""Per-stage execution — full pipeline with Graphify query engine."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from uiforgemax.graphify.engine import graphify_merge, graphify_update_multi, resolve_apis
from uiforgemax.graphify.pipeline import stage_query_exec, stage_query_plan, stage_requirement_map
from uiforgemax.graphify.source_snapshot import build_source_snapshots
from uiforgemax.graphify.stack import detect_stack
from uiforgemax.model_mediation.service import mediation_pause_result
from uiforgemax.pipeline.classify import build_classification_signals, default_classification
from uiforgemax.pipeline.flow_router import build_flow_plan, resolve_flow, save_flow, apply_flow_to_state
from uiforgemax.pipeline.greenfield import (
    build_greenfield_api_resolution,
    build_greenfield_requirement_map,
    write_greenfield_graph_stubs,
)
from uiforgemax.pipeline.handover import generate_handover
from uiforgemax.pipeline.implement import apply_plan
from uiforgemax.pipeline.normalize import normalize_run
from uiforgemax.pipeline.planning import (
    build_plan_approval_package,
    build_understanding_approval_package,
    generate_plan,
    generate_understanding,
)
from uiforgemax.pipeline.surface_scope import apply_surface_to_api_resolution, apply_surface_to_requirement_map
from uiforgemax.pipeline.testing import apply_generated_tests, run_tests
from uiforgemax.state import RunState, Stage, Status
from uiforgemax.stats import StageTimer
from uiforgemax.tools.context import ToolContext


@dataclass
class StageResult:
    stop: bool
    message: str
    mediation: dict | None = None
    extra: dict | None = None


def _maybe_pause_mediation(ctx: ToolContext, state: RunState, stage: Stage) -> StageResult | None:
    run_dir = _run_dir(ctx, state)
    paused, request = mediation_pause_result(run_dir, state, stage)
    if not paused or not request:
        return None
    return StageResult(
        stop=True,
        message=f"IDE model mediation required: {request['kind']}",
        mediation=request,
    )


def _run_dir(ctx: ToolContext, state: RunState) -> Path:
    return ctx.store.run_dir(state.run_id)


def _project_root(ctx: ToolContext, state: RunState) -> Path:
    if not state.project_root:
        raise ValueError("project_root is not set — pass it to uiforgemax_start_run")
    root = Path(state.project_root)
    if not root.exists():
        raise ValueError(f"project_root does not exist: {root}")
    return root


def _project_roots(ctx: ToolContext, state: RunState) -> dict[str, Path]:
    """All registered repo roots for this run: the primary + any added via
    ``uiforgemax_add_workspace_root``, keyed by name (primary is "default")."""
    roots = {"default": _project_root(ctx, state)}
    roots.update({name: Path(p) for name, p in state.project_roots.items()})
    return roots


def _missing_workspace_roots(actions: list[dict], state: RunState) -> list[str]:
    """Distinct non-default root names referenced by ``actions`` that aren't registered yet.

    ``actions`` is any list of create/modify/test-file dicts that may carry an
    optional ``"root"`` key (set by IDE mediation for plans spanning more
    than one repo). Unset/``"default"`` always resolves to the primary
    ``project_root`` and never blocks.
    """
    referenced = {
        a.get("root") for a in actions if isinstance(a, dict) and a.get("root") and a.get("root") != "default"
    }
    return sorted(name for name in referenced if name not in state.project_roots)


def _load_classification(run_dir: Path) -> dict:
    path = run_dir / "request-classification.json"
    return _load_json(path) if path.exists() else {}


def _ensure_analysis_artifacts(ctx: ToolContext, state: RunState, run_dir: Path) -> None:
    """Build requirement-map + api-resolution when graph stages were skipped.

    Mutates ``state`` in place; the caller's stage handler is responsible for
    persisting it (via the normal post-stage ``ctx.store.save``), consistent
    with every other stage in this module.
    """
    map_path = run_dir / "graph" / "requirement-map.json"
    if map_path.exists():
        return

    flow = resolve_flow(run_dir, state)
    flags = flow.get("flags", {})
    reqs = _load_json(run_dir / "requirements.normalized.json")
    classification = _load_classification(run_dir)
    surface = flow.get("surface") or classification.get("surface", "unknown")

    if flags.get("greenfieldScaffold"):
        req_map = build_greenfield_requirement_map(reqs, classification)
        req_map = apply_surface_to_requirement_map(req_map, surface)
        write_greenfield_graph_stubs(run_dir, req_map)
        api = build_greenfield_api_resolution(reqs, classification)
        api = apply_surface_to_api_resolution(api, surface)
    else:
        raise ValueError(
            "requirement-map missing and graph was skipped — re-classify or provide project_root"
        )

    (run_dir / "api-resolution.json").write_text(json.dumps(api, indent=2), encoding="utf-8")
    state.artifacts["requirementMap"] = "graph/requirement-map.json"
    state.artifacts["apiResolution"] = "api-resolution.json"
    state.approvals.api.required = api.get("gate1Required", False)
    state.approvals.api.approved = not state.approvals.api.required
    state.status = Status.REQUIREMENT_MAPPED
    state.record(Stage.REQUIREMENT_MAP, "ok", "greenfield_scaffold")


def _ensure_api_resolution_stub(run_dir: Path, state: RunState) -> dict:
    """Read api-resolution.json, writing a surface-filtered empty stub if absent.

    Used by stages downstream of API_RESOLVE (understanding, plan) that must
    tolerate the stage having been skipped entirely (e.g. ui_only surface).
    """
    api_path = run_dir / "api-resolution.json"
    if not api_path.exists():
        flow = resolve_flow(run_dir, state)
        surface = flow.get("surface") or "unknown"
        api = apply_surface_to_api_resolution({"resolution": [], "gate1Required": False}, surface)
        api_path.write_text(json.dumps(api, indent=2), encoding="utf-8")
    return _load_json(api_path)


def run_stage(ctx: ToolContext, state: RunState, stage: Stage) -> StageResult:
    handler = _HANDLERS.get(stage)
    if handler is None:
        return StageResult(stop=False, message=f"{stage.value}: no-op")
    return handler(ctx, state)


def _intake(ctx: ToolContext, state: RunState) -> StageResult:
    modes = state.inputs.get("modes", [])
    if not modes:
        state.status = Status.INTAKE
        return StageResult(stop=True, message="BLOCKED: no inputs yet. Add at least one input mode.")
    # Source-of-truth Jira attachments must be on disk before the pipeline advances.
    from uiforgemax.pipeline.source_of_truth import missing_sot_attachments, sot_block_message

    run_dir = ctx.store.run_dir(state.run_id)
    missing = missing_sot_attachments(run_dir)
    if missing:
        state.status = Status.INTAKE
        state.record(Stage.INTAKE, "blocked", f"missing SoT attachments: {missing}")
        return StageResult(stop=True, message=sot_block_message(missing))
    state.status = Status.INTAKE
    state.record(Stage.INTAKE, "ok", f"modes={modes}")
    return StageResult(stop=False, message=f"Intake ok (modes={modes}).")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _repo_is_empty(root: Path) -> bool:
    try:
        return not any(root.iterdir())
    except OSError:
        return True


def _arch_detect(ctx: ToolContext, state: RunState) -> StageResult:
    roots = _project_roots(ctx, state)
    per_root: dict[str, dict] = {}
    for name, root in roots.items():
        stack = detect_stack(root)
        per_root[name] = {
            "path": str(root),
            "primary": stack["primary"],
            "empty": stack["empty"],
            "nx": stack["nx"],
            "kinds": stack["kinds"],
            "complexity": "enterprise" if stack["nx"] else ("greenfield" if stack["empty"] else "standard"),
        }

    default = per_root["default"]
    state.architecture = {
        "primary": default["primary"],
        "overlays": [],
        "complexity": default["complexity"],
        "empty": default["empty"],
        "nx": default["nx"],
        "roots": per_root,
    }
    state.status = Status.ARCH_DETECTED
    detail = ", ".join(f"{name}={info['primary']}" for name, info in per_root.items())
    state.record(Stage.ARCH_DETECT, "ok", detail)
    return StageResult(stop=False, message=f"Workspace: {detail}")


_VALID_POLICIES = {"frontend_first", "full_stack", "extend_existing", "contract_driven"}


def _classify(ctx: ToolContext, state: RunState) -> StageResult:
    run_dir = _run_dir(ctx, state)
    cls_path = run_dir / "request-classification.json"

    if not cls_path.exists():
        signals = build_classification_signals(state, run_dir)
        (run_dir / "classification-signals.json").write_text(
            json.dumps(signals, indent=2), encoding="utf-8"
        )
        default = default_classification(signals)
        default["signals"] = signals
        cls_path.write_text(json.dumps(default, indent=2), encoding="utf-8")

    state.artifacts["requestClassification"] = "request-classification.json"

    pause = _maybe_pause_mediation(ctx, state, Stage.CLASSIFY)
    if pause:
        return pause

    classification = _load_json(cls_path)
    recommended = classification.get("recommendedPolicy")
    if recommended in _VALID_POLICIES:
        state.policy = recommended
    state.artifacts["requestType"] = classification.get("requestType")
    state.artifacts["surface"] = classification.get("surface")

    signals = classification.get("signals") or build_classification_signals(state, run_dir)
    flow = build_flow_plan(classification, signals, state)
    save_flow(run_dir, flow)
    apply_flow_to_state(state, flow)

    state.status = Status.CLASSIFIED
    detail = f"{classification.get('requestType')}/{classification.get('surface')}"
    skipped = len(flow.get("skippedStages", {}))
    state.record(Stage.CLASSIFY, "ok", detail)
    return StageResult(
        stop=False,
        message=(
            f"Classified: {detail} (policy={state.policy}). "
            f"Flow: {len(flow.get('activeStages', []))} active stages, {skipped} skipped."
        ),
    )


def _image_convert(ctx: ToolContext, state: RunState) -> StageResult:
    if "image" not in state.inputs.get("modes", []):
        state.record(Stage.IMAGE_CONVERT, "skipped", "no image")
        return StageResult(stop=False, message="Image convert skipped.")
    state.artifacts["visualSpec"] = "visual-spec.json"
    state.status = Status.IMAGE_CONVERTED
    state.record(Stage.IMAGE_CONVERT, "ok", "via normalize")
    return StageResult(stop=False, message="Image handled in normalize stage.")


def _normalize(ctx: ToolContext, state: RunState) -> StageResult:
    run_dir = _run_dir(ctx, state)
    feedback = state.approvals.understanding.feedback or state.approvals.plan.feedback
    req_path = run_dir / "requirements.normalized.json"
    if not req_path.exists() or feedback:
        with StageTimer(run_dir, Stage.NORMALIZE.value) as t:
            normalize_run(run_dir, state.policy, feedback)
            t.stats = {"artifact": "requirements.normalized.json"}
    state.artifacts["requirements"] = "requirements.normalized.json"
    state.artifacts["visualSpec"] = "visual-spec.json"
    pause = _maybe_pause_mediation(ctx, state, Stage.NORMALIZE)
    if pause:
        return pause
    state.status = Status.NORMALIZED
    state.record(Stage.NORMALIZE, "ok", "normalized")
    return StageResult(stop=False, message="Normalized requirements + visual-spec.")


def _graphify_update(ctx: ToolContext, state: RunState) -> StageResult:
    """Per-repo real Graphify: ``python -m graphify update <root>`` → ``graphify-out/``."""
    roots = _project_roots(ctx, state)
    run_dir = _run_dir(ctx, state)
    with StageTimer(run_dir, Stage.GRAPHIFY_UPDATE.value) as t:
        per_root = graphify_update_multi(roots)
        graph_dir = run_dir / "graph"
        graph_dir.mkdir(parents=True, exist_ok=True)
        project_paths: dict[str, str] = {}
        for name, meta in per_root.items():
            root_dir = graph_dir / "by-root" / name
            root_dir.mkdir(parents=True, exist_ok=True)
            (root_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            # Pointer copy of graph.json into the run dir for mediation convenience
            src = Path(meta.get("graphJson") or "")
            if src.exists():
                shutil.copy2(src, root_dir / "graph.json")
            project_paths[name] = meta.get("graphJson") or str(roots[name] / "graphify-out" / "graph.json")
        stub = {
            "pendingMerge": True,
            "engine": "graphify-cli",
            "roots": per_root,
        }
        (graph_dir / "per-root.json").write_text(json.dumps({"roots": project_paths}, indent=2), encoding="utf-8")
        (graph_dir / "index.json").write_text(json.dumps(stub, indent=2), encoding="utf-8")
        t.stats = {
            "roots": sorted(roots.keys()),
            "nodes": sum(int(m.get("nodeCount") or 0) for m in per_root.values()),
            "projectGraphPaths": project_paths,
        }
    state.artifacts["graphIndexByRoot"] = {name: f"graph/by-root/{name}/graph.json" for name in roots}
    state.artifacts["projectGraphPaths"] = project_paths
    state.status = Status.GRAPH_READY
    state.record(Stage.GRAPHIFY_UPDATE, "ok", f"graphify update on {len(roots)} root(s)")
    return StageResult(
        stop=False,
        message=(
            f"Real Graphify update complete for {len(roots)} root(s) "
            f"({', '.join(sorted(roots.keys()))}) → each <repo>/graphify-out/. Merge phase next."
        ),
    )


def _graph_merge(ctx: ToolContext, state: RunState) -> StageResult:
    """Merge multi-root graphs via ``graphify merge-graphs`` when needed."""
    roots = _project_roots(ctx, state)
    run_dir = _run_dir(ctx, state)
    graph_dir = run_dir / "graph"
    per_root: dict[str, dict] = {}
    for name in roots:
        meta_path = graph_dir / "by-root" / name / "meta.json"
        if meta_path.exists():
            per_root[name] = _load_json(meta_path)

    if not per_root:
        per_root = graphify_update_multi(roots)

    classification = _load_classification(run_dir)
    surface = classification.get("surface") or state.artifacts.get("surface")

    with StageTimer(run_dir, Stage.GRAPH_MERGE.value) as t:
        merged = graphify_merge(
            per_root,
            surface=surface,
            default_root=_project_root(ctx, state),
            run_graph_dir=graph_dir,
        )
        integration = merged.get("integration", {})
        t.stats = {
            "merged": bool(merged.get("merged")),
            "uiAndApi": bool(integration.get("uiAndApi")),
            "nodes": merged.get("nodeCount", 0),
            "roots": integration.get("roots", []),
        }

    state.artifacts["graphIndex"] = "graph/index.json"
    state.artifacts["graphMerged"] = "graph/merged.json"
    state.status = Status.GRAPH_MERGED
    detail = (
        f"merged={merged.get('merged')} roots={integration.get('rootCount')} "
        f"nodes={merged.get('nodeCount', 0)}"
    )
    state.record(Stage.GRAPH_MERGE, "ok", detail)
    return StageResult(
        stop=False,
        message=f"Graph merge ready ({detail}). Query stages use real graphify query next.",
    )


def _graph_query_plan(ctx: ToolContext, state: RunState) -> StageResult:
    run_dir = _run_dir(ctx, state)
    reqs = _load_json(run_dir / "requirements.normalized.json")
    with StageTimer(run_dir, Stage.GRAPH_QUERY_PLAN.value) as t:
        plan = stage_query_plan(run_dir, reqs)
        t.stats = {"queryCount": plan["queryCount"]}
    state.artifacts["graphQueries"] = "graph/queries.json"
    state.status = Status.GRAPH_QUERIED
    state.record(Stage.GRAPH_QUERY_PLAN, "ok", f"{plan['queryCount']} queries")
    return StageResult(stop=False, message=f"Planned {plan['queryCount']} graph queries → graph/queries.json")


def _graph_query_exec(ctx: ToolContext, state: RunState) -> StageResult:
    run_dir = _run_dir(ctx, state)
    index = _load_json(run_dir / "graph" / "index.json")
    with StageTimer(run_dir, Stage.GRAPH_QUERY_EXEC.value) as t:
        results = stage_query_exec(run_dir, index)
        empty = sum(1 for r in results.get("results", []) if r.get("status") in ("empty", "missing_graph"))
        t.stats = {"executed": results["executed"], "empty": empty, "engine": "graphify-cli"}
    state.artifacts["graphQueryResults"] = "graph/query-results.json"
    state.record(Stage.GRAPH_QUERY_EXEC, "ok", f"executed={results['executed']} empty={empty}")
    return StageResult(
        stop=False,
        message=(
            f"Executed {results['executed']} real graphify queries "
            f"({empty} empty) → graph/query-results.json"
        ),
    )


def _requirement_map(ctx: ToolContext, state: RunState) -> StageResult:
    run_dir = _run_dir(ctx, state)
    map_path = run_dir / "graph" / "requirement-map.json"
    if not map_path.exists():
        reqs = _load_json(run_dir / "requirements.normalized.json")
        index = _load_json(run_dir / "graph" / "index.json")
        with StageTimer(run_dir, Stage.REQUIREMENT_MAP.value) as t:
            req_map = stage_requirement_map(run_dir, reqs, index)
            t.stats = req_map.get("stats", {})
    else:
        req_map = _load_json(map_path)

    flow = resolve_flow(run_dir, state)
    surface = flow.get("surface") or state.artifacts.get("surface") or "unknown"
    req_map = apply_surface_to_requirement_map(req_map, surface)
    (map_path).write_text(json.dumps(req_map, indent=2), encoding="utf-8")

    state.artifacts["requirementMap"] = "graph/requirement-map.json"
    state.artifacts["contextPack"] = "graph/context-pack.json"
    pause = _maybe_pause_mediation(ctx, state, Stage.REQUIREMENT_MAP)
    if pause:
        return pause
    state.status = Status.REQUIREMENT_MAPPED
    state.record(Stage.REQUIREMENT_MAP, "ok", "mapped")

    if req_map.get("clarifications"):
        return StageResult(
            stop=False,
            message=f"Requirement map saved. {len(req_map['clarifications'])} clarification(s) — IDE agent may ask human via uiforgemax_answer_clarifications.",
        )
    return StageResult(stop=False, message="Requirement map + context pack saved.")


def _api_resolve(ctx: ToolContext, state: RunState) -> StageResult:
    run_dir = _run_dir(ctx, state)
    _ensure_analysis_artifacts(ctx, state, run_dir)
    reqs = _load_json(run_dir / "requirements.normalized.json")
    index_path = run_dir / "graph" / "index.json"
    flow = resolve_flow(run_dir, state)
    surface = flow.get("surface") or "unknown"

    if index_path.exists() and not _load_json(index_path).get("skipped"):
        index = _load_json(index_path)
        api_resolution = resolve_apis(reqs, index)
    else:
        classification = _load_classification(run_dir)
        api_resolution = build_greenfield_api_resolution(reqs, classification)

    api_resolution = apply_surface_to_api_resolution(api_resolution, surface)
    (run_dir / "api-resolution.json").write_text(json.dumps(api_resolution, indent=2), encoding="utf-8")
    state.artifacts["apiResolution"] = "api-resolution.json"
    state.approvals.api.required = api_resolution.get("gate1Required", False)
    state.approvals.api.approved = not state.approvals.api.required
    state.status = Status.API_RESOLVED
    state.record(Stage.API_RESOLVE, "ok", f"gate1={state.approvals.api.required}")
    return StageResult(
        stop=False,
        message=f"API resolved. Gate 1 {'required' if state.approvals.api.required else 'skipped'}.",
    )


def _gate_api(ctx: ToolContext, state: RunState) -> StageResult:
    if state.approvals.api.required and not state.approvals.api.approved:
        state.status = Status.AWAITING_API_APPROVAL
        return StageResult(stop=True, message="GATE 1: uiforgemax_approve_api or request_changes.")
    state.record(Stage.GATE_API, "ok", "passed")
    return StageResult(stop=False, message="Gate 1 passed.")


def _understanding(ctx: ToolContext, state: RunState) -> StageResult:
    run_dir = _run_dir(ctx, state)
    _ensure_analysis_artifacts(ctx, state, run_dir)
    reqs = _load_json(run_dir / "requirements.normalized.json")
    req_map = _load_json(run_dir / "graph" / "requirement-map.json")
    api = _ensure_api_resolution_stub(run_dir, state)
    generate_understanding(run_dir, reqs, req_map, api)
    state.artifacts["understanding"] = "plans/understanding.md"
    state.artifacts["understandingApproval"] = "plans/understanding-approval.json"
    state.status = Status.UNDERSTANDING_READY
    state.record(Stage.UNDERSTANDING, "ok", "understanding.md + understanding-approval.json")
    return StageResult(stop=False, message="Understanding from requirement-map → plans/understanding.md")


def _gate_understanding(ctx: ToolContext, state: RunState) -> StageResult:
    """Auto-pass — single human approval is plan gate only.

    Still writes ``understanding-approval.json`` so the plan package can fold
    scope/ACs into the one review surface.
    """
    from datetime import datetime, timezone

    run_dir = _run_dir(ctx, state)
    package = build_understanding_approval_package(run_dir)
    (run_dir / "plans" / "understanding-approval.json").write_text(
        json.dumps(package, indent=2), encoding="utf-8"
    )
    state.artifacts["understandingApproval"] = "plans/understanding-approval.json"
    state.approvals.understanding.required = False
    if not state.approvals.understanding.approved:
        state.approvals.understanding.approved = True
        state.approvals.understanding.by = "system-auto"
        state.approvals.understanding.at = datetime.now(timezone.utc).isoformat()
    state.record(Stage.GATE_UNDERSTANDING, "ok", "auto-passed; sole human gate is plan")
    return StageResult(
        stop=False,
        message=(
            "Understanding recorded (no separate human gate). "
            "Sole approval before implement is plan approval."
        ),
    )


def _plan(ctx: ToolContext, state: RunState) -> StageResult:
    run_dir = _run_dir(ctx, state)
    _ensure_analysis_artifacts(ctx, state, run_dir)
    plan_path = run_dir / "plans" / "implementation-plan.json"
    approved_path = run_dir / "plans" / "approved-plan.json"
    # After sole human approval, never regenerate — implement uses the freeze.
    if state.approvals.plan.approved and approved_path.exists():
        state.artifacts["plan"] = "plans/approved-plan.json"
        state.status = Status.PLAN_READY
        state.record(Stage.PLAN, "ok", "using locked approved-plan.json")
        return StageResult(stop=False, message="Using locked approved plan (no re-plan).")

    feedback = state.approvals.plan.feedback
    if not plan_path.exists() or feedback:
        reqs = _load_json(run_dir / "requirements.normalized.json")
        req_map = _load_json(run_dir / "graph" / "requirement-map.json")
        api = _ensure_api_resolution_stub(run_dir, state)
        generate_plan(run_dir, reqs, req_map, api, feedback)
        state.approvals.plan.feedback = None
    state.artifacts["plan"] = "plans/implementation-plan.json"

    # MCP itself (not the driving agent) reads literal current content for every
    # candidate modify/reuse/create file so PLAN_REFINEMENT can safely return full
    # file bodies without guessing structure it was never allowed to see.
    draft_plan = _load_json(plan_path)
    snapshots = build_source_snapshots(_project_roots(ctx, state), draft_plan)
    (run_dir / "graph" / "source-snapshots.json").write_text(
        json.dumps(snapshots, indent=2), encoding="utf-8"
    )
    state.artifacts["sourceSnapshots"] = "graph/source-snapshots.json"

    pause = _maybe_pause_mediation(ctx, state, Stage.PLAN)
    if pause:
        return pause

    plan = _load_json(plan_path)
    missing = _missing_workspace_roots(plan.get("create", []) + plan.get("modify", []), state)
    if missing:
        state.status = Status.BLOCKED
        state.record(Stage.PLAN, "blocked", f"missing workspace roots: {missing}")
        return StageResult(
            stop=True,
            message=(
                f"BLOCKED: plan references repo root(s) not yet part of this run: {missing}. "
                "Ask the human for each folder's path, then call "
                "uiforgemax_add_workspace_root(run_id, name, path) for each — using the same "
                "name shown here — then uiforgemax_advance again."
            ),
        )

    state.status = Status.PLAN_READY
    state.record(Stage.PLAN, "ok", "plan from requirement-map")
    return StageResult(stop=False, message="Plan built from requirement-map artifacts.")


def _plan_review(ctx: ToolContext, state: RunState) -> StageResult:
    run_dir = _run_dir(ctx, state)
    review = _load_json(run_dir / "plans" / "plan-review.json")
    state.artifacts["planReview"] = review
    if review.get("verdict") != "pass":
        state.status = Status.BLOCKED
        return StageResult(stop=True, message=f"Plan review blocked: {review.get('requiredPlanChanges')}")
    state.status = Status.PLAN_REVIEWED
    state.record(Stage.PLAN_REVIEW, "ok", "pass")
    return StageResult(stop=False, message="Plan review passed.")


def _gate_plan(ctx: ToolContext, state: RunState) -> StageResult:
    from uiforgemax.env_flags import skip_plan_approval

    run_dir = _run_dir(ctx, state)

    if not state.approvals.plan.approved and skip_plan_approval():
        from uiforgemax.tools.approvals import _freeze_approved_plan, _now

        _freeze_approved_plan(run_dir)
        state.approvals.plan.approved = True
        state.approvals.plan.at = _now()
        state.approvals.plan.by = "env:UIFORGEMAX_SKIP_PLAN_APPROVAL"
        state.approvals.understanding.approved = True
        state.approvals.understanding.required = False
        state.artifacts["approvedPlan"] = "plans/approved-plan.json"
        state.record(Stage.GATE_PLAN, "approved", "env skip")
        return StageResult(
            stop=False,
            message="Plan auto-approved (UIFORGEMAX_SKIP_PLAN_APPROVAL=1).",
        )

    if not state.approvals.plan.approved:
        state.status = Status.AWAITING_PLAN_APPROVAL
        package = build_plan_approval_package(run_dir)
        (run_dir / "plans" / "plan-approval.json").write_text(
            json.dumps(package, indent=2), encoding="utf-8"
        )
        state.artifacts["planApproval"] = "plans/plan-approval.json"
        return StageResult(
            stop=True,
            message=(
                "HUMAN PLAN GATE — show planApproval, then wait. "
                "Do NOT call approve_plan until the human explicitly approves. "
                "Then: uiforgemax_approve_plan or request_changes. "
                "Dev bypass: UIFORGEMAX_SKIP_PLAN_APPROVAL=1."
            ),
            extra={
                "planApproval": package,
                "humanGate": "plan",
                "waitForHuman": True,
            },
        )
    return StageResult(stop=False, message="Plan approved — implementing locked plan.")


def _implement(ctx: ToolContext, state: RunState) -> StageResult:
    roots = _project_roots(ctx, state)
    run_dir = _run_dir(ctx, state)
    # Prefer the frozen snapshot from approve_plan so mediation cannot drift the plan.
    approved_path = run_dir / "plans" / "approved-plan.json"
    plan_path = approved_path if approved_path.exists() else run_dir / "plans" / "implementation-plan.json"
    plan = _load_json(plan_path)
    expected = len(plan.get("create") or []) + len(plan.get("modify") or [])
    with StageTimer(run_dir, Stage.IMPLEMENT.value) as t:
        summary = apply_plan(roots, plan)
        t.stats = {
            "filesChanged": summary.get("fileCount", 0),
            "skipped": len(summary.get("skipped") or []),
            "planSource": plan_path.name,
        }
    (run_dir / "implementation" / "diff-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    state.artifacts["diffSummary"] = "implementation/diff-summary.json"
    state.artifacts["approvedPlan"] = (
        "plans/approved-plan.json" if approved_path.exists() else "plans/implementation-plan.json"
    )
    count = int(summary.get("fileCount") or 0)
    skipped = summary.get("skipped") or []
    if expected > 0 and count == 0:
        state.status = Status.FAILED
        state.record(Stage.IMPLEMENT, "failed", f"0/{expected} files; skipped={skipped}")
        return StageResult(
            stop=True,
            message=(
                f"IMPLEMENT FAILED: plan had {expected} file action(s) but wrote 0. "
                f"skipped={skipped}. Fix implement writers or plan templateId/patchId/content."
            ),
            extra={"diffSummary": summary},
        )
    state.status = Status.IMPLEMENTING
    state.record(Stage.IMPLEMENT, "ok", f"{count} files from {plan_path.name}")
    msg = f"Implemented {count} files from approved plan."
    if skipped:
        msg += f" ({len(skipped)} skipped: {skipped})"
    return StageResult(stop=False, message=msg)


def _test(ctx: ToolContext, state: RunState) -> StageResult:
    from uiforgemax.env_flags import install_timeout_seconds
    from uiforgemax.pipeline.testing import (
        ack_install_wait_for_retry,
        can_request_env_recovery,
        clear_install_wait,
        load_generated_tests,
        load_install_wait,
        results_need_env_recovery,
        write_env_gap,
    )

    roots = _project_roots(ctx, state)
    run_dir = _run_dir(ctx, state)
    # Resuming after human install — leave pause status so mediation / tests can run.
    if state.status == Status.AWAITING_USER_INSTALL:
        state.status = Status.TESTING

    # TEST_GENERATION first; TEST_ENV_RECOVERY when env-gap.json says needs_recovery
    pause = _maybe_pause_mediation(ctx, state, Stage.TEST)
    if pause:
        return pause

    approved = run_dir / "plans" / "approved-plan.json"
    plan_path = approved if approved.exists() else run_dir / "plans" / "implementation-plan.json"
    plan = _load_json(plan_path)
    written = apply_generated_tests(roots, run_dir)

    # After an install timeout, next advance skips auto-install and re-runs tests only.
    skip_auto_install = ack_install_wait_for_retry(run_dir)

    with StageTimer(run_dir, Stage.TEST.value) as t:
        results = run_tests(
            roots,
            plan,
            run_dir=run_dir,
            skip_auto_install=skip_auto_install,
        )
        t.stats = {
            "passed": results.get("passed"),
            "coverage": results.get("coverage"),
            "mediated": results.get("mediated"),
            "stack": results.get("stack"),
            "testsWritten": written,
            "skippedTests": results.get("skippedTests"),
            "installTimedOut": results.get("installTimedOut"),
        }
    (run_dir / "tests" / "unit-results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    state.artifacts["unitResults"] = "tests/unit-results.json"

    if results.get("installTimedOut") or results.get("humanInstallRequired"):
        from uiforgemax.pipeline.resume_checkpoint import write_resume_checkpoint

        wait = load_install_wait(run_dir)
        state.status = Status.AWAITING_USER_INSTALL
        state.current_stage = Stage.TEST
        state.artifacts["installWait"] = "tests/install-wait.json"
        state.artifacts["installPausedHandover"] = "handover/install-paused.md"
        shell = (wait or {}).get("shell") or ""
        mins = (wait or {}).get("timeoutMinutes") or max(1, install_timeout_seconds() // 60)
        checkpoint = write_resume_checkpoint(
            run_dir,
            state,
            pause_reason="dependency_install_timeout",
            next_tool="uiforgemax_advance",
            resume_action="advance",
            skip_on_resume={"autoInstall": True, "replan": True, "reimplement": True},
            human_commands=(wait or {}).get("commands") or [],
            shell=shell,
            notes=(
                "Implementation finished. After local install, resume/continue advances "
                "from 10_test (tests + handover) only."
            ),
        )
        state.record(Stage.TEST, "awaiting_user_install", str(results.get("failures"))[:200])
        return StageResult(
            stop=True,
            message=(
                f"INSTALL PAUSED after ~{mins} min — implementation is done; only deps/tests remain.\n"
                f"Run these commands in a terminal, then say **resume** or **continue**:\n"
                f"```\n{shell}\n```\n"
                f"Checkpoint: resume-checkpoint.json (stage={checkpoint.get('resumeStage')}). "
                f"MCP continues from where it left off — no new run."
            ),
            extra={
                "humanInstallRequired": True,
                "waitForHuman": True,
                "humanGate": "install",
                "installWait": wait,
                "resumeCheckpoint": checkpoint,
                "resumeHint": {
                    "say": "resume",
                    "nextTool": "uiforgemax_advance",
                    "runId": state.run_id,
                    "stage": checkpoint.get("resumeStage"),
                    "status": checkpoint.get("status"),
                    "completedStages": checkpoint.get("completedStages"),
                    "note": "After npm install finishes, resume/continue retries tests only.",
                },
                "failures": results.get("failures"),
            },
        )

    if not results.get("passed"):
        if results_need_env_recovery(results) and can_request_env_recovery(run_dir):
            gap = write_env_gap(
                run_dir,
                results,
                load_generated_tests(run_dir),
                roots=roots,
            )
            state.artifacts["envGap"] = "tests/env-gap.json"
            state.artifacts["toolchainFacts"] = "tests/toolchain-facts.json"
            pause = _maybe_pause_mediation(ctx, state, Stage.TEST)
            if pause:
                pause.message = (
                    "IDE model mediation required: TEST_ENV_RECOVERY — "
                    f"tooling/env gap (attempt {gap.get('attempt')}). "
                    "Read tests/toolchain-facts.json and decide installHints[] + run[] "
                    "(install is required when needInstallAny=true), then submit_mediation + advance."
                )
                return pause
        state.status = Status.FAILED
        state.record(Stage.TEST, "failed", str(results.get("failures")))
        return StageResult(stop=True, message=f"Tests failed: {results.get('failures')}")

    clear_install_wait(run_dir)
    from uiforgemax.pipeline.resume_checkpoint import clear_resume_checkpoint

    clear_resume_checkpoint(run_dir)
    cov = results.get("coverage") or {}
    cov_note = f" coverage={cov.get('lines')}%" if cov.get("lines") is not None else ""
    skip_note = " (commands skipped per IDE)" if results.get("skippedTests") else ""
    state.status = Status.VALIDATING
    state.record(Stage.TEST, "ok", f"passed{cov_note}{skip_note}")
    return StageResult(stop=False, message=f"Tests passed.{cov_note}{skip_note}")


def _handover(ctx: ToolContext, state: RunState) -> StageResult:
    run_dir = _run_dir(ctx, state)
    diff = _load_json(run_dir / "implementation" / "diff-summary.json")
    tests = _load_json(run_dir / "tests" / "unit-results.json")
    generate_handover(run_dir, diff, tests)
    state.artifacts["deliveryReport"] = "handover/delivery-report.md"
    state.status = Status.COMPLETED
    state.record(Stage.HANDOVER, "ok", "delivered")
    return StageResult(stop=True, message="Handover complete.")


_HANDLERS = {
    Stage.INTAKE: _intake,
    Stage.ARCH_DETECT: _arch_detect,
    Stage.CLASSIFY: _classify,
    Stage.IMAGE_CONVERT: _image_convert,
    Stage.NORMALIZE: _normalize,
    Stage.GRAPHIFY_UPDATE: _graphify_update,
    Stage.GRAPH_MERGE: _graph_merge,
    Stage.GRAPH_QUERY_PLAN: _graph_query_plan,
    Stage.GRAPH_QUERY_EXEC: _graph_query_exec,
    Stage.REQUIREMENT_MAP: _requirement_map,
    Stage.API_RESOLVE: _api_resolve,
    Stage.GATE_API: _gate_api,
    Stage.UNDERSTANDING: _understanding,
    Stage.GATE_UNDERSTANDING: _gate_understanding,
    Stage.PLAN: _plan,
    Stage.PLAN_REVIEW: _plan_review,
    Stage.GATE_PLAN: _gate_plan,
    Stage.IMPLEMENT: _implement,
    Stage.TEST: _test,
    Stage.HANDOVER: _handover,
}
