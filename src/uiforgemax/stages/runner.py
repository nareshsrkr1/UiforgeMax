"""Per-stage execution — full pipeline with Graphify query engine."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from uiforgemax.graphify.engine import graphify_merge, graphify_update_multi, resolve_apis
from uiforgemax.graphify.pipeline import stage_query_exec, stage_query_plan, stage_requirement_map
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
from uiforgemax.pipeline.ide_apply import (
    ide_apply_brief,
    partition_mcp_writable,
    verify_ide_apply,
    write_pre_apply_baseline,
)
from uiforgemax.pipeline.implement import apply_plan, PartialImplementError
from uiforgemax.pipeline.normalize import normalize_run
from uiforgemax.pipeline.planning import (
    build_plan_approval_package,
    build_understanding_approval_package,
    generate_plan,
    generate_understanding,
    load_locked_plan,
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
    flow = build_flow_plan(classification, signals, state, run_dir=run_dir)
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


def _decompose(ctx: ToolContext, state: RunState) -> StageResult:
    from uiforgemax.pipeline.decompose import (
        auto_decompose,
        load_subtasks,
        should_auto_decompose,
    )

    run_dir = _run_dir(ctx, state)
    subtasks_path = run_dir / "plans" / "subtasks.json"

    if subtasks_path.exists():
        subtasks = _load_json(subtasks_path)
        state.artifacts["subtasks"] = "plans/subtasks.json"
        state.status = Status.DECOMPOSED
        state.record(Stage.DECOMPOSE, "ok", f"{subtasks.get('subtaskCount', 1)} subtask(s)")
        return StageResult(stop=False, message=f"Subtasks already exist ({subtasks.get('subtaskCount', 1)}).")

    reqs_path = run_dir / "requirements.normalized.json"
    reqs = _load_json(reqs_path) if reqs_path.exists() else {}
    classification = _load_classification(run_dir)
    visual_path = run_dir / "visual-spec.json"
    visual = _load_json(visual_path) if visual_path.exists() else None

    if should_auto_decompose(reqs, classification):
        subtasks = auto_decompose(reqs, classification, visual)
        subtasks_path.parent.mkdir(parents=True, exist_ok=True)
        subtasks_path.write_text(json.dumps(subtasks, indent=2), encoding="utf-8")
        state.artifacts["subtasks"] = "plans/subtasks.json"
        state.status = Status.DECOMPOSED
        state.record(Stage.DECOMPOSE, "ok", "auto (simple request)")
        return StageResult(stop=False, message="Simple request — auto-wrapped as single sub-task.")

    pause = _maybe_pause_mediation(ctx, state, Stage.DECOMPOSE)
    if pause:
        return pause

    subtasks = load_subtasks(run_dir)
    if not subtasks:
        return StageResult(stop=True, message="BLOCKED: subtasks.json missing after mediation.")

    state.artifacts["subtasks"] = "plans/subtasks.json"
    state.status = Status.DECOMPOSED
    state.record(Stage.DECOMPOSE, "ok", f"{subtasks.get('subtaskCount', 1)} subtask(s) via IDE mediation")
    return StageResult(stop=False, message=f"Decomposed into {subtasks.get('subtaskCount', 1)} sub-task(s).")


def _graphify_update(ctx: ToolContext, state: RunState) -> StageResult:
    """Per-repo real Graphify: ``python -m graphify update <root>`` → ``graphify-out/``."""
    roots = _project_roots(ctx, state)
    run_dir = _run_dir(ctx, state)
    with StageTimer(run_dir, Stage.GRAPHIFY_UPDATE.value) as t:
        per_root = graphify_update_multi(roots, run_dir=run_dir)
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
        per_root = graphify_update_multi(roots, run_dir=run_dir)

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

    pause = _maybe_pause_mediation(ctx, state, Stage.GRAPH_QUERY_PLAN)
    if pause:
        return pause

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
    """Auto-pass — sole human gate is plan approval only."""
    from datetime import datetime, timezone

    if state.approvals.api.required and not state.approvals.api.approved:
        state.approvals.api.approved = True
        state.approvals.api.by = "system-auto"
        state.approvals.api.at = datetime.now(timezone.utc).isoformat()
    state.record(Stage.GATE_API, "ok", "auto-passed; sole human gate is plan")
    return StageResult(
        stop=False,
        message="API gate auto-passed (sole human gate is plan approval).",
    )


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
    # Intent-only PLAN_REFINEMENT: no source-snapshots content bus. The IDE reads
    # target files after plan approval and writes them with native tools.

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
    from uiforgemax.pipeline.planning import (
        EMPTY_PLAN_BLOCKER,
        MISSING_INTENT_BLOCKER,
        plan_actions_missing_intent,
        plan_has_file_actions,
    )

    run_dir = _run_dir(ctx, state)
    review = _load_json(run_dir / "plans" / "plan-review.json")
    plan = _load_json(run_dir / "plans" / "implementation-plan.json")
    state.artifacts["planReview"] = review
    blockers = list(review.get("blockers") or [])
    if not plan_has_file_actions(plan) and EMPTY_PLAN_BLOCKER not in blockers:
        blockers.append(EMPTY_PLAN_BLOCKER)
    missing_intent = plan_actions_missing_intent(plan)
    if missing_intent and not any(MISSING_INTENT_BLOCKER[:40] in str(b) for b in blockers):
        blockers.append(
            f"{MISSING_INTENT_BLOCKER} Missing intent for: {', '.join(missing_intent[:12])}"
        )
    if review.get("verdict") != "pass" or blockers:
        changes = review.get("requiredPlanChanges") or blockers
        state.status = Status.BLOCKED
        state.record(Stage.PLAN_REVIEW, "blocked", str(changes)[:200])
        return StageResult(
            stop=True,
            message=(
                f"Plan review blocked: {changes}. "
                "Empty create/modify or missing path/purpose must be fixed "
                "via PLAN_REFINEMENT / request_changes — do not open the human approval gate."
            ),
        )
    state.status = Status.PLAN_REVIEWED
    state.record(Stage.PLAN_REVIEW, "ok", "pass")
    return StageResult(stop=False, message="Plan review passed.")


def _gate_plan(ctx: ToolContext, state: RunState) -> StageResult:
    from uiforgemax.env_flags import skip_plan_approval
    from uiforgemax.pipeline.planning import (
        EMPTY_PLAN_BLOCKER,
        MISSING_INTENT_BLOCKER,
        plan_actions_missing_intent,
        plan_actions_outside_delta_scope,
        plan_all_mcp_writable,
        plan_has_file_actions,
        plan_is_implementable,
    )

    run_dir = _run_dir(ctx, state)
    plan = _load_json(run_dir / "plans" / "implementation-plan.json")
    if not plan_has_file_actions(plan):
        state.status = Status.BLOCKED
        state.record(Stage.GATE_PLAN, "blocked", EMPTY_PLAN_BLOCKER)
        return StageResult(
            stop=True,
            message=(
                f"BLOCKED: {EMPTY_PLAN_BLOCKER} "
                "Fix requirement-map / PLAN_REFINEMENT so create/modify has real "
                "implementation files, then request_changes or advance again. "
                "Do not approve an empty plan."
            ),
        )
    if not plan_is_implementable(plan):
        missing = plan_actions_missing_intent(plan)
        state.status = Status.BLOCKED
        state.record(Stage.GATE_PLAN, "blocked", MISSING_INTENT_BLOCKER)
        return StageResult(
            stop=True,
            message=(
                f"BLOCKED: {MISSING_INTENT_BLOCKER} "
                f"Missing path/purpose for: {', '.join(missing[:12])}"
                f"{'…' if len(missing) > 12 else ''}."
            ),
        )

    delta_path = run_dir / "plans" / "visual-delta.json"
    if delta_path.exists():
        visual_delta = _load_json(delta_path)
        out_of_scope = plan_actions_outside_delta_scope(plan, visual_delta)
        if out_of_scope:
            failed_ids = sorted(
                {
                    str(st.get("subtaskId"))
                    for st in (visual_delta.get("failedSubtasks") or [])
                    if st.get("subtaskId")
                }
            )
            state.status = Status.BLOCKED
            state.record(Stage.GATE_PLAN, "blocked", "delta_out_of_scope")
            return StageResult(
                stop=True,
                message=(
                    f"BLOCKED: this is a visual-fidelity DELTA re-plan scoped to "
                    f"sub-tasks {failed_ids}, but these actions are missing subtaskId "
                    f"or tag a sub-task that already passed: {', '.join(out_of_scope[:12])}"
                    f"{'…' if len(out_of_scope) > 12 else ''}. Set subtaskId on every "
                    "create/modify action to one of the failed IDs above."
                ),
            )

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
        if plan_all_mcp_writable(plan):
            return StageResult(
                stop=False,
                message="Plan auto-approved (UIFORGEMAX_SKIP_PLAN_APPROVAL=1) — MCP writable.",
            )
        # Intent-only: pause for IDE apply even when human gate is skipped.
        roots = _project_roots(ctx, state)
        write_pre_apply_baseline(run_dir, roots, plan)
        state.status = Status.AWAITING_IDE_APPLY
        state.current_stage = Stage.IMPLEMENT
        state.artifacts["ideApply"] = True
        state.artifacts["preApplyBaseline"] = "plans/pre-apply-baseline.json"
        return StageResult(
            stop=True,
            message=(
                "Plan auto-approved (UIFORGEMAX_SKIP_PLAN_APPROVAL=1). "
                "IDE-apply: edit listed paths, then uiforgemax_advance."
            ),
            extra={"ideApplyBrief": ide_apply_brief(plan), "waitForIdeApply": True},
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
                "HUMAN PLAN GATE — show planApprovalBrief / planApproval, then wait. "
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
    # Already approved — either IDE-apply pause or MCP scaffold write.
    if state.status == Status.AWAITING_IDE_APPLY:
        locked = load_locked_plan(run_dir, require_approved=True) or plan
        return StageResult(
            stop=True,
            message="Awaiting IDE apply — edit approved-plan.json paths only, then advance.",
            extra={
                "ideApplyBrief": ide_apply_brief(locked),
                "waitForIdeApply": True,
                "planSource": "plans/approved-plan.json",
                "doNotAdvanceUntilEdited": True,
            },
        )
    return StageResult(stop=False, message="Plan approved — continuing to implement/verify.")


def _implement(ctx: ToolContext, state: RunState) -> StageResult:
    from uiforgemax.pipeline.decompose import load_subtasks

    roots = _project_roots(ctx, state)
    run_dir = _run_dir(ctx, state)

    # After POST_IMPLEMENT_REVIEW: gate on passesReview, then continue without rewrite.
    review_path = run_dir / "implementation" / "post-implement-review.json"
    if review_path.exists():
        try:
            review = _load_json(review_path)
        except Exception:  # noqa: BLE001
            review = {}

        # An omitted passesReview is NOT the same as passesReview=true — the old
        # check (`review.get("passesReview") is False`) let a missing/None field
        # fall through to "already passed, continue" by default. A malformed or
        # incomplete review must never silently count as a pass.
        if "passesReview" not in review or review.get("passesReview") is None:
            review["issues"] = [
                {
                    "severity": "critical",
                    "file": "(review)",
                    "issue": (
                        "POST_IMPLEMENT_REVIEW response omitted 'passesReview' — "
                        "treating as failed pending an explicit true/false verdict."
                    ),
                }
            ] + (review.get("issues") or [])
            review["passesReview"] = False

        # A rubber-stamped passesReview=true must not override the model's own
        # critical findings — if it listed a critical issue, that IS a failure,
        # regardless of what the boolean says.
        has_critical_issue = any(
            str(i.get("severity", "")).lower() == "critical" for i in (review.get("issues") or [])
        )
        if review.get("passesReview") is True and has_critical_issue:
            review["passesReview"] = False

        # Deterministic cross-check: planCoverage/passesReview are entirely model
        # self-reported. Verify against diff-summary.json (MCP's own record of what
        # apply_plan() actually wrote) independent of what the review claims — a
        # review that says "fully covered" while a planned path was genuinely never
        # written must not be trusted just because the model said so.
        from uiforgemax.pipeline.planning import plan_paths_missing_from_diff

        approved_path = run_dir / "plans" / "approved-plan.json"
        approved_plan = _load_json(approved_path) if approved_path.exists() else {}
        diff_path = run_dir / "implementation" / "diff-summary.json"
        diff_summary = _load_json(diff_path) if diff_path.exists() else {}
        real_missing = plan_paths_missing_from_diff(approved_plan, diff_summary)
        if real_missing:
            review["passesReview"] = False
            review["realMissingPaths"] = real_missing
            review.setdefault("planCoverage", {})["missing"] = sorted(
                set(review.get("planCoverage", {}).get("missing") or []) | set(real_missing)
            )
            # Inject synthetic critical issues so the rewind message surfaces the
            # deterministically-found gap even if the model's own issues[] didn't
            # mention it (e.g. it wrongly claimed passesReview=true with no issues).
            existing_issue_files = {i.get("file") for i in (review.get("issues") or [])}
            synthetic = [
                {
                    "severity": "critical",
                    "file": path,
                    "issue": "Planned file was never written (verified against diff-summary.json)",
                }
                for path in real_missing
                if path not in existing_issue_files
            ]
            review["issues"] = (review.get("issues") or []) + synthetic

        if review.get("passesReview") is False:
            from uiforgemax.model_mediation.service import clear_mediation_responses

            issues = review.get("issues") or []
            critical = [
                i for i in issues if str(i.get("severity", "")).lower() == "critical"
            ] or issues[:5]
            feedback = {
                "type": "post_implement_review",
                "passesReview": False,
                "issues": critical,
                "planCoverage": review.get("planCoverage"),
                "message": (
                    "POST_IMPLEMENT_REVIEW failed — fix critical gaps vs plan/SoT, "
                    "then re-approve plan for delta implement."
                ),
            }
            from uiforgemax.pipeline.visual_validate import clear_visual_delta

            state.approvals.plan.feedback = json.dumps(feedback)
            state.approvals.plan.approved = False
            # Archive failed review so the next implement cycle can re-run review.
            archive = run_dir / "implementation" / "post-implement-review.failed.json"
            try:
                review_path.replace(archive)
            except OSError:
                review_path.unlink(missing_ok=True)
            approved = run_dir / "plans" / "approved-plan.json"
            if approved.exists():
                approved.unlink(missing_ok=True)
            # Drop any prior visual-delta lock — this rewind is post-implement, not
            # a visual-fidelity delta; stale ST-* scope must not constrain re-plan.
            clear_visual_delta(run_dir, reason="post-implement-rewind")
            clear_mediation_responses(run_dir)
            state.status = Status.PLAN_READY
            state.current_stage = Stage.PLAN
            state.record(Stage.IMPLEMENT, "failed", "post_implement_review failed")
            return StageResult(
                stop=True,
                message=(
                    "POST_IMPLEMENT_REVIEW failed (passesReview=false). "
                    "Rewound to PLAN — re-run PLAN_REFINEMENT with fixes for: "
                    + ", ".join(
                        str(i.get("issue") or i.get("file") or "?") for i in critical[:6]
                    )
                ),
                extra={"postImplementReview": review},
            )
        # Review already passed — do not re-apply the plan; continue pipeline.
        state.status = Status.IMPLEMENTING
        state.record(Stage.IMPLEMENT, "ok", "post_implement_review passed")
        return StageResult(
            stop=False,
            message="Post-implement review passed — continuing.",
        )

    # After approval, ONLY approved-plan.json is authoritative (never draft).
    approved_path = run_dir / "plans" / "approved-plan.json"
    if state.approvals.plan.approved:
        if not approved_path.exists():
            state.status = Status.BLOCKED
            state.record(Stage.IMPLEMENT, "blocked", "missing approved-plan.json")
            return StageResult(
                stop=True,
                message=(
                    "BLOCKED: plan is approved but plans/approved-plan.json is missing. "
                    "Re-approve the plan or request_changes — refuse to implement from a draft."
                ),
            )
        plan = load_locked_plan(run_dir, require_approved=True)
        plan_path = approved_path
    else:
        plan_path = (
            approved_path
            if approved_path.exists()
            else run_dir / "plans" / "implementation-plan.json"
        )
        plan = _load_json(plan_path)
    subtasks = load_subtasks(run_dir)
    expected = len(plan.get("create") or []) + len(plan.get("modify") or [])
    mcp_plan, ide_plan = partition_mcp_writable(plan)
    ide_expected = len(ide_plan.get("create") or []) + len(ide_plan.get("modify") or [])
    baseline_path = run_dir / "plans" / "pre-apply-baseline.json"

    # IDE-apply path: agent already edited files; verify vs baseline.
    if state.status == Status.AWAITING_IDE_APPLY or (
        ide_expected > 0 and state.artifacts.get("ideApply")
    ):
        if state.status == Status.AWAITING_IDE_APPLY and not baseline_path.exists():
            write_pre_apply_baseline(run_dir, roots, plan)
        baseline = _load_json(baseline_path) if baseline_path.exists() else {}
        # Optional: MCP still writes scaffolds that carry templateId/content.
        mcp_changed: list[str] = []
        if mcp_plan.get("create") or mcp_plan.get("modify"):
            try:
                mcp_summary = apply_plan(roots, mcp_plan, subtasks=None)
                mcp_changed = list(mcp_summary.get("filesChanged") or [])
            except PartialImplementError as exc:
                # Scaffold failure must not kill the run — continue IDE verify for the rest.
                state.record(
                    Stage.IMPLEMENT,
                    "scaffold_partial",
                    str([s.get("path") for s in exc.skipped])[:200],
                )
        summary, problems = verify_ide_apply(roots, ide_plan if ide_expected else plan, baseline)
        summary["filesChanged"] = list(dict.fromkeys(mcp_changed + list(summary.get("filesChanged") or [])))
        summary["fileCount"] = len(summary["filesChanged"])
        (run_dir / "implementation").mkdir(parents=True, exist_ok=True)
        (run_dir / "implementation" / "diff-summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        state.artifacts["diffSummary"] = "implementation/diff-summary.json"
        if problems:
            state.status = Status.AWAITING_IDE_APPLY
            state.artifacts["ideApply"] = True
            state.record(Stage.IMPLEMENT, "blocked", f"ide_apply incomplete: {problems[:8]}")
            return StageResult(
                stop=True,
                message=(
                    "IDE APPLY INCOMPLETE — do NOT advance again yet. "
                    "These planned paths are missing or unchanged on disk: "
                    f"{', '.join(problems[:12])}{'…' if len(problems) > 12 else ''}. "
                    "Use IDE Read/Edit/Write on EACH path (project_root), THEN "
                    "call uiforgemax_advance once to verify."
                ),
                extra={
                    "ideApplyBrief": ide_apply_brief(plan),
                    "waitForIdeApply": True,
                    "doNotAdvanceUntilEdited": True,
                    "incompletePaths": problems,
                    "diffSummary": summary,
                },
            )
        count = int(summary.get("fileCount") or 0)
        state.status = Status.IMPLEMENTING
        state.artifacts.pop("ideApply", None)
        state.artifacts.pop("ideApplyNoProgress", None)
        state.record(Stage.IMPLEMENT, "ok", f"ide_apply verified {count} files")
        msg = f"IDE apply verified {count} file(s)."
        pause = _maybe_pause_mediation(ctx, state, Stage.IMPLEMENT)
        if pause:
            pause.message = f"{msg} — POST_IMPLEMENT_REVIEW mediation required."
            return pause
        return StageResult(stop=False, message=msg)

    # MCP-writable path (all actions have content/templateId) — greenfield / CI scaffolds.
    with StageTimer(run_dir, Stage.IMPLEMENT.value) as t:
        try:
            summary = apply_plan(roots, plan, subtasks=subtasks)
        except PartialImplementError as exc:
            t.stats = {
                "filesChanged": len(exc.changed),
                "skipped": len(exc.skipped),
                "planSource": plan_path.name,
                "partialFail": True,
            }
            partial_summary = {
                "branch": exc.branch,
                "filesChanged": exc.changed,
                "fileCount": len(exc.changed),
                "skipped": exc.skipped,
                "source": plan.get("source"),
                "roots": {name: str(p) for name, p in roots.items()},
                "partialFail": True,
                "mode": "mcp_write",
            }
            (run_dir / "implementation" / "diff-summary.json").write_text(
                json.dumps(partial_summary, indent=2), encoding="utf-8"
            )
            state.artifacts["diffSummary"] = "implementation/diff-summary.json"
            # Never hard-FAIL on partial MCP write — always hand off to IDE apply.
            write_pre_apply_baseline(run_dir, roots, plan)
            state.status = Status.AWAITING_IDE_APPLY
            state.artifacts["ideApply"] = True
            skipped_paths = [s["path"] for s in exc.skipped]
            state.record(Stage.IMPLEMENT, "awaiting_ide_apply", str(skipped_paths)[:200])
            return StageResult(
                stop=True,
                message=(
                    "MCP could not write all planned files (expected for intent-only plans). "
                    "Do NOT loop advance/resume. Edit these paths with IDE Read/Edit/Write, "
                    f"then call uiforgemax_advance once: {', '.join(skipped_paths[:12])}"
                    f"{'…' if len(skipped_paths) > 12 else ''}."
                ),
                extra={
                    "ideApplyBrief": ide_apply_brief(plan),
                    "waitForIdeApply": True,
                    "doNotAdvanceUntilEdited": True,
                    "skippedPaths": skipped_paths,
                    "diffSummary": partial_summary,
                },
            )

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
    if expected == 0:
        state.status = Status.FAILED
        state.record(Stage.IMPLEMENT, "failed", "0 create/modify actions in approved plan")
        return StageResult(
            stop=True,
            message=(
                "IMPLEMENT FAILED: approved plan has 0 create/modify actions — nothing to write. "
                "Use request_changes / PLAN_REFINEMENT to add real file targets."
            ),
            extra={"diffSummary": summary},
        )
    if expected > 0 and count == 0:
        write_pre_apply_baseline(run_dir, roots, plan)
        state.status = Status.AWAITING_IDE_APPLY
        state.artifacts["ideApply"] = True
        state.record(Stage.IMPLEMENT, "awaiting_ide_apply", "0 mcp writes")
        return StageResult(
            stop=True,
            message=(
                f"No MCP writes for {expected} planned action(s). "
                "Implement with IDE Read/Edit/Write, then uiforgemax_advance."
            ),
            extra={"ideApplyBrief": ide_apply_brief(plan), "waitForIdeApply": True},
        )
    state.status = Status.IMPLEMENTING
    if summary.get("subtaskResults"):
        for st_id, st_res in summary["subtaskResults"].items():
            state.subtask_progress[st_id] = st_res.get("status", "completed")
    state.record(Stage.IMPLEMENT, "ok", f"{count} files from {plan_path.name}")
    msg = f"Implemented {count} files from approved plan."
    if summary.get("subtaskResults"):
        msg += f" ({len(summary['subtaskResults'])} sub-tasks)"
    if skipped:
        msg += f" ({len(skipped)} skipped: {skipped})"

    pause = _maybe_pause_mediation(ctx, state, Stage.IMPLEMENT)
    if pause:
        pause.message = f"{msg} — POST_IMPLEMENT_REVIEW mediation required."
        return pause

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

    # Do not invent tests for a noop implement — that is how empty plans reached
    # TEST_GENERATION and looked "done" with only a generated *.test.tsx.
    diff_path = run_dir / "implementation" / "diff-summary.json"
    if diff_path.exists():
        try:
            diff = _load_json(diff_path)
        except Exception:  # noqa: BLE001
            diff = {}
        file_count = int(diff.get("fileCount") or 0)
        if file_count <= 0:
            state.status = Status.FAILED
            state.record(Stage.TEST, "failed", "no implementation files changed")
            return StageResult(
                stop=True,
                message=(
                    "TEST BLOCKED: implementation/diff-summary.json shows 0 files changed. "
                    "Complete IDE apply (or MCP scaffold write), then test. "
                    "Do not generate tests for a noop implement."
                ),
                extra={"diffSummary": diff},
            )

    # TEST_GENERATION first; TEST_ENV_RECOVERY when env-gap.json says needs_recovery
    pause = _maybe_pause_mediation(ctx, state, Stage.TEST)
    if pause:
        return pause

    # Tests must cover the locked approved plan when present.
    plan = load_locked_plan(run_dir, require_approved=bool(state.approvals.plan.approved))
    if not plan:
        plan = _load_json(run_dir / "plans" / "implementation-plan.json")
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
                    "Read tests/command-logs.json + tests/toolchain-facts.json; "
                    "if Cannot find module / missingPackages → installHints for those packages "
                    "(any stack), then corrected run[]. Prefer install over skipTests. "
                    "submit_mediation + advance."
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


def _visual_validate(ctx: ToolContext, state: RunState) -> StageResult:
    from uiforgemax.env_flags import skip_mediation as is_mediation_skipped
    from uiforgemax.pipeline.visual_validate import (
        clear_visual_delta,
        prepare_delta_reimplementation,
        should_run_visual_validation,
    )

    run_dir = _run_dir(ctx, state)

    if not should_run_visual_validation(run_dir, state):
        state.record(Stage.VISUAL_VALIDATE, "skipped", "no visual input")
        return StageResult(stop=False, message="Visual validation skipped (no visual reference).")

    pause = _maybe_pause_mediation(ctx, state, Stage.VISUAL_VALIDATE)
    if pause:
        return pause

    validation_path = run_dir / "implementation" / "visual-validation.json"
    if not validation_path.exists():
        if is_mediation_skipped():
            state.record(Stage.VISUAL_VALIDATE, "skipped", "no validation result (mediation disabled)")
            return StageResult(
                stop=False, message="Visual validation skipped (mediation disabled)."
            )
        # A real visual SoT exists and mediation is NOT disabled, yet no result
        # landed — _maybe_pause_mediation should have caught this. Treat the
        # desync as a hard block instead of silently shipping unverified code.
        state.status = Status.BLOCKED
        state.record(Stage.VISUAL_VALIDATE, "blocked", "visual SoT pending, no validation result")
        return StageResult(
            stop=True,
            message=(
                "BLOCKED: a visual source-of-truth exists but VISUAL_VALIDATION never "
                "completed. Call uiforgemax_advance to retry the mediation."
            ),
        )

    validation = _load_json(validation_path)

    from uiforgemax.pipeline.visual_validate import find_cross_view_button_leaks

    roots: dict[str, Path] = {}
    if state.project_root:
        roots["default"] = Path(state.project_root)
    for name, path in (state.project_roots or {}).items():
        roots[str(name)] = Path(path)
    leaks = find_cross_view_button_leaks(run_dir, roots)
    if leaks:
        results = validation.setdefault("subtaskResults", [])
        by_id = {r.get("subtaskId"): r for r in results}
        for v in leaks:
            r = by_id.get(v["subtaskId"])
            if r is None:
                r = {"subtaskId": v["subtaskId"], "fidelity": 0.0, "deviations": []}
                results.append(r)
                by_id[v["subtaskId"]] = r
            r["needsReimplementation"] = True
            r.setdefault("deviations", []).append(
                {
                    "severity": "critical",
                    "component": "button provenance",
                    "expected": f"button only in HTML render function '{v['expectedView']}'",
                    "actual": (
                        f"'{v['buttonLabel']}' in {v['file']} belongs to "
                        f"{v['actualOwningViews']} per HTML SoT, not '{v['expectedView']}'"
                    ),
                    "fix": (
                        f"Remove '{v['buttonLabel']}' from this screen — the HTML SoT "
                        f"only renders it in {v['actualOwningViews']}."
                    ),
                }
            )
            r["fixInstructions"] = (
                (r.get("fixInstructions") or "")
                + f" Cross-view button leak: '{v['buttonLabel']}' does not belong on this screen per HTML SoT."
            ).strip()
        validation["passesVisualGate"] = False
        state.record(
            Stage.VISUAL_VALIDATE,
            "cross_view_leak",
            f"{len(leaks)} button(s) sourced from a different HTML view than their screen",
        )

    # Attempt count is owned by MCP code, not the model — it lives in
    # plans/visual-delta.json (written by the prior prepare_delta_reimplementation
    # call), never in the mediation's own response. The model was never asked
    # for (and never reliably supplies) an "_attempt" field, so reading it from
    # validation.json always defaulted to 1 and the "max 2 attempts" cap never
    # tripped — this could loop between PLAN and VISUAL_VALIDATE indefinitely.
    delta_path = run_dir / "plans" / "visual-delta.json"
    attempt = 1
    if delta_path.exists():
        try:
            attempt = int(_load_json(delta_path).get("attempt", 1))
        except (ValueError, TypeError):
            attempt = 1

    if validation.get("passesVisualGate"):
        clear_visual_delta(run_dir, reason="passed")
        state.status = Status.VISUAL_VALIDATED
        fidelity = validation.get("overallFidelity", "?")
        state.record(Stage.VISUAL_VALIDATE, "ok", f"fidelity={fidelity}")
        return StageResult(stop=False, message=f"Visual validation passed (fidelity={fidelity}).")

    if attempt >= 2:
        clear_visual_delta(run_dir, reason="max-attempts")
        state.status = Status.VISUAL_VALIDATED
        state.record(Stage.VISUAL_VALIDATE, "warn", "max attempts reached, proceeding")
        return StageResult(
            stop=False,
            message="Visual validation failed after 2 attempts — proceeding to tests with warnings.",
        )

    failed = [r for r in validation.get("subtaskResults", []) if r.get("needsReimplementation")]
    failed_ids = [r["subtaskId"] for r in failed]

    prepare_delta_reimplementation(run_dir, state, failed, attempt=attempt)

    from uiforgemax.model_mediation.service import clear_mediation_responses

    state.approvals.plan.approved = False
    approved = run_dir / "plans" / "approved-plan.json"
    if approved.exists():
        approved.unlink(missing_ok=True)
    clear_mediation_responses(run_dir)
    # Drop prior validation so the next cycle re-pauses for VISUAL_VALIDATION.
    validation_path.unlink(missing_ok=True)
    # Drop the prior POST_IMPLEMENT_REVIEW too — otherwise _implement() finds this
    # stale (already-passed) review on the next cycle and short-circuits without
    # ever calling apply_plan() again, so the delta re-implementation fixes from
    # PLAN_REFINEMENT are silently never written to disk.
    review_path = run_dir / "implementation" / "post-implement-review.json"
    if review_path.exists():
        archive = run_dir / "implementation" / "post-implement-review.superseded-by-visual.json"
        try:
            review_path.replace(archive)
        except OSError:
            review_path.unlink(missing_ok=True)

    state.status = Status.PLAN_READY
    state.current_stage = Stage.PLAN
    state.record(Stage.VISUAL_VALIDATE, "failed", f"re-impl subtasks: {failed_ids}")

    return StageResult(
        stop=True,
        message=(
            f"VISUAL VALIDATION FAILED: {len(failed)} sub-task(s) need delta re-implementation: "
            f"{failed_ids}. Pipeline rewound to PLAN stage for targeted fixes. "
            "Call uiforgemax_advance to re-plan and re-implement only affected sub-tasks."
        ),
        extra={
            "visualValidation": validation,
            "failedSubtasks": failed_ids,
            "deltaReimplementation": True,
        },
    )


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
    Stage.DECOMPOSE: _decompose,
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
    Stage.VISUAL_VALIDATE: _visual_validate,
    Stage.TEST: _test,
    Stage.HANDOVER: _handover,
}
