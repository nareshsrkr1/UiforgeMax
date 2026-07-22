"""Dynamic pipeline routing from request classification.

The canonical stage list in :class:`Stage` is fixed; this module decides which
stages *run* for a given request. Classification (IDE-mediated or heuristic)
plus repo signals produce a persisted ``run-flow.json`` that ``advance`` and
stage handlers consult — so ui-only work skips API resolution, api-only skips
visual stages, and greenfield / empty repos skip graph intelligence entirely.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from uiforgemax.pipeline.classify import is_greenfield
from uiforgemax.state import RunState, Stage

_FLOW_VERSION = 1

# Stages always executed (intake through classify, then plan → handover).
_ALWAYS = {
    Stage.INTAKE,
    Stage.ARCH_DETECT,
    Stage.CLASSIFY,
    Stage.NORMALIZE,
    Stage.DECOMPOSE,
    Stage.UNDERSTANDING,
    Stage.GATE_UNDERSTANDING,
    Stage.PLAN,
    Stage.PLAN_REVIEW,
    Stage.GATE_PLAN,
    Stage.IMPLEMENT,
    Stage.TEST,
    Stage.HANDOVER,
}

_GRAPH_STAGES = {
    Stage.GRAPHIFY_UPDATE,
    Stage.GRAPH_MERGE,
    Stage.GRAPH_QUERY_PLAN,
    Stage.GRAPH_QUERY_EXEC,
    Stage.REQUIREMENT_MAP,
}

_API_STAGES = {Stage.API_RESOLVE, Stage.GATE_API}

_VISUAL_STAGES = {Stage.IMAGE_CONVERT}

_VISUAL_VALIDATE_STAGES = {Stage.VISUAL_VALIDATE}


def flow_path(run_dir: Path) -> Path:
    return run_dir / "run-flow.json"


def load_flow(run_dir: Path) -> dict[str, Any] | None:
    path = flow_path(run_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_flow(run_dir: Path, flow: dict[str, Any]) -> Path:
    path = flow_path(run_dir)
    path.write_text(json.dumps(flow, indent=2), encoding="utf-8")
    return path


def build_flow_plan(
    classification: dict[str, Any],
    signals: dict[str, Any],
    state: RunState,
) -> dict[str, Any]:
    """Derive which stages run from classification + repo signals."""
    surface = classification.get("surface", "unknown")
    request_type = classification.get("requestType", "enhancement")
    modes = list(signals.get("inputModes") or state.inputs.get("modes", []))
    has_images = bool(signals.get("imageCount")) or "image" in modes
    root = signals.get("projectRoot", {})
    root_empty = bool(root.get("empty"))
    architecture = signals.get("architecture") or state.architecture or {}

    # IDE model may override; otherwise infer from signals.
    use_graph = classification.get("useGraph")
    if use_graph is None:
        use_graph = not (
            request_type == "greenfield"
            or root_empty
            or architecture.get("primary") == "greenfield"
        )

    run_visual = classification.get("runVisual")
    if run_visual is None:
        run_visual = surface != "api_only" or has_images

    run_api = classification.get("runApi")
    if run_api is None:
        run_api = surface in ("api_only", "full_stack", "unknown", "infra") and surface != "ui_only"

    # is_greenfield() covers the explicit flag + requestType fallback; an empty
    # repo with no flag/type set at all is still treated as greenfield.
    greenfield_scaffold = is_greenfield(classification) or (
        classification.get("greenfieldScaffold") is None and root_empty
    )

    skipped: dict[str, str] = {}
    active: list[str] = []

    for stage in Stage.ordered():
        if stage in _ALWAYS:
            active.append(stage.value)
            continue
        if stage in _VISUAL_STAGES:
            if run_visual and has_images:
                active.append(stage.value)
            else:
                skipped[stage.value] = _skip_reason("visual", surface, has_images)
            continue
        if stage in _GRAPH_STAGES:
            if use_graph:
                active.append(stage.value)
            else:
                skipped[stage.value] = _skip_reason("graph", request_type, root_empty)
            continue
        if stage in _API_STAGES:
            if run_api:
                active.append(stage.value)
            else:
                skipped[stage.value] = _skip_reason("api", surface)
            continue
        if stage in _VISUAL_VALIDATE_STAGES:
            if run_visual and has_images:
                active.append(stage.value)
            else:
                skipped[stage.value] = _skip_reason("visual_validate", surface, has_images)
            continue
        active.append(stage.value)

    return {
        "version": _FLOW_VERSION,
        "requestType": request_type,
        "surface": surface,
        "platform": classification.get("platform", ["web"]),
        "activeStages": active,
        "skippedStages": skipped,
        "flags": {
            "useGraph": use_graph,
            "runVisual": run_visual,
            "runApi": run_api,
            "greenfieldScaffold": greenfield_scaffold,
        },
        "source": classification.get("source", "classification"),
    }


def apply_flow_to_state(state: RunState, flow: dict[str, Any]) -> None:
    state.flow = flow
    state.artifacts["runFlow"] = "run-flow.json"
    state.artifacts["activeStages"] = flow.get("activeStages", [])
    flags = flow.get("flags", {})
    if not flags.get("runApi", True):
        state.approvals.api.required = False
        state.approvals.api.approved = True


def resolve_flow(run_dir: Path, state: RunState) -> dict[str, Any]:
    """Load persisted flow or rebuild from classification when missing."""
    existing = load_flow(run_dir)
    if existing:
        apply_flow_to_state(state, existing)
        return existing

    cls_path = run_dir / "request-classification.json"
    sig_path = run_dir / "classification-signals.json"
    if not cls_path.exists():
        return _default_flow()

    classification = json.loads(cls_path.read_text(encoding="utf-8"))
    signals = (
        json.loads(sig_path.read_text(encoding="utf-8"))
        if sig_path.exists()
        else {"inputModes": state.inputs.get("modes", [])}
    )
    flow = build_flow_plan(classification, signals, state)
    save_flow(run_dir, flow)
    apply_flow_to_state(state, flow)
    return flow


def is_stage_active(stage: Stage, flow: dict[str, Any]) -> bool:
    active = flow.get("activeStages")
    if active is not None:
        return stage.value in active
    return True


def next_active_stage(current: Stage, flow: dict[str, Any]) -> Stage | None:
    order = Stage.ordered()
    idx = order.index(current)
    for nxt in order[idx + 1 :]:
        if is_stage_active(nxt, flow):
            return nxt
    return None


def flow_flags(run_dir: Path, state: RunState | None = None) -> dict[str, Any]:
    flow = load_flow(run_dir)
    if flow:
        return flow.get("flags", {})
    if state and state.flow:
        return state.flow.get("flags", {})
    return {}


def _default_flow() -> dict[str, Any]:
    return {
        "version": _FLOW_VERSION,
        "activeStages": [s.value for s in Stage.ordered()],
        "skippedStages": {},
        "flags": {
            "useGraph": True,
            "runVisual": True,
            "runApi": True,
            "greenfieldScaffold": False,
        },
    }


def _skip_reason(kind: str, *parts: Any) -> str:
    if kind == "graph":
        return f"graph skipped (greenfield={parts[0]!r}, emptyRepo={parts[1]!r})"
    if kind == "visual":
        return f"visual skipped (surface={parts[0]!r}, hasImages={parts[1]!r})"
    if kind == "api":
        return f"api skipped (surface={parts[0]!r})"
    if kind == "visual_validate":
        return f"visual_validate skipped (surface={parts[0]!r}, hasImages={parts[1]!r})"
    return f"{kind} skipped"
