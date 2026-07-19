"""Graph analysis pipeline — plan NL queries, execute via CLI, build requirement map."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from uiforgemax.graphify.query_executor import execute_queries
from uiforgemax.graphify.query_planner import plan_queries
from uiforgemax.graphify.requirement_map import build_requirement_map


def run_graph_analysis(
    run_dir: Path,
    requirements: dict[str, Any],
    graph_index: dict[str, Any],
    visual_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    graph_dir = run_dir / "graph"
    graph_dir.mkdir(parents=True, exist_ok=True)

    classification = _load_classification(run_dir)
    stack = _stack_from_index(graph_index)
    graphs = _graphs_by_root(graph_index, graph_dir)

    query_plan = plan_queries(requirements, visual_spec, classification=classification, stack=stack)
    (graph_dir / "queries.json").write_text(json.dumps(query_plan, indent=2), encoding="utf-8")

    query_results = execute_queries(
        query_plan, graphs_by_root=graphs, requirements=requirements
    )
    (graph_dir / "query-results.json").write_text(json.dumps(query_results, indent=2), encoding="utf-8")

    req_map = build_requirement_map(
        requirements, query_results, visual_spec, classification=classification
    )
    (graph_dir / "requirement-map.json").write_text(json.dumps(req_map, indent=2), encoding="utf-8")

    pack = _slim_context_pack(req_map, graph_index)
    (graph_dir / "context-pack.json").write_text(json.dumps(pack, indent=2), encoding="utf-8")

    return {
        "queryPlan": query_plan,
        "queryResults": query_results,
        "requirementMap": req_map,
        "contextPack": pack,
        "stats": req_map.get("stats", {}),
    }


def _slim_context_pack(req_map: dict[str, Any], graph_index: dict[str, Any]) -> dict[str, Any]:
    return {
        "engine": "graphify-cli",
        "architecture": graph_index.get("architecture") or {
            "roots": {
                name: (meta.get("architecture") if isinstance(meta, dict) else None)
                for name, meta in (graph_index.get("roots") or {}).items()
            }
        },
        "surface": req_map.get("surface"),
        "targetApp": req_map.get("targetApp"),
        "targetDomain": req_map.get("targetDomain"),
        "reuse": req_map.get("reuse", [])[:20],
        "apiGaps": req_map.get("apiGaps", []),
        "apiExists": req_map.get("apiExists", []),
        "weakGraph": req_map.get("weakGraph", False),
        "modify": req_map.get("modify", [])[:20],
        "create": req_map.get("create", [])[:20],
    }


def load_visual_spec(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "visual-spec.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def stage_query_plan(
    run_dir: Path,
    requirements: dict[str, Any],
    *,
    classification: dict[str, Any] | None = None,
    stack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    visual = load_visual_spec(run_dir)
    classification = classification or _load_classification(run_dir)
    stack = stack or _stack_from_run(run_dir)
    plan = plan_queries(requirements, visual, classification=classification, stack=stack)
    graph_dir = run_dir / "graph"
    graph_dir.mkdir(parents=True, exist_ok=True)
    (graph_dir / "queries.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return plan


def stage_query_exec(
    run_dir: Path,
    graph_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    graph_dir = run_dir / "graph"
    plan = json.loads((graph_dir / "queries.json").read_text(encoding="utf-8"))
    index = graph_index or {}
    if not index and (graph_dir / "index.json").exists():
        index = json.loads((graph_dir / "index.json").read_text(encoding="utf-8"))
    graphs = _graphs_by_root(index, graph_dir)
    reqs: dict[str, Any] = {}
    req_path = run_dir / "requirements.normalized.json"
    if req_path.exists():
        reqs = json.loads(req_path.read_text(encoding="utf-8"))
    results = execute_queries(plan, graphs_by_root=graphs, requirements=reqs)
    (graph_dir / "query-results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


def stage_requirement_map(
    run_dir: Path,
    requirements: dict[str, Any],
    graph_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    visual = load_visual_spec(run_dir)
    results = json.loads((run_dir / "graph" / "query-results.json").read_text(encoding="utf-8"))
    classification = _load_classification(run_dir)
    req_map = build_requirement_map(
        requirements, results, visual, classification=classification
    )
    (run_dir / "graph" / "requirement-map.json").write_text(json.dumps(req_map, indent=2), encoding="utf-8")
    pack = _slim_context_pack(req_map, graph_index or {})
    (run_dir / "graph" / "context-pack.json").write_text(json.dumps(pack, indent=2), encoding="utf-8")
    return req_map


def _load_classification(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "request-classification.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _stack_from_index(graph_index: dict[str, Any]) -> dict[str, Any]:
    roots = graph_index.get("roots") or {}
    if not roots:
        return graph_index.get("stack") or {}
    # Prefer default root stack, else first
    meta = roots.get("default") or next(iter(roots.values()))
    if isinstance(meta, dict):
        return meta.get("stack") or meta.get("architecture") or {}
    return {}


def _stack_from_run(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "graph" / "index.json"
    if not path.exists():
        return {}
    return _stack_from_index(json.loads(path.read_text(encoding="utf-8")))


def _graphs_by_root(graph_index: dict[str, Any], graph_dir: Path) -> dict[str, Path]:
    """Resolve graph.json paths per root from merge meta or by-root snapshots."""
    out: dict[str, Path] = {}
    roots = graph_index.get("roots") or {}
    for name, meta in roots.items():
        if isinstance(meta, dict) and meta.get("graphJson"):
            p = Path(meta["graphJson"])
            if p.exists():
                out[name] = p
                continue
        # Fallback: copied pointer under run dir
        candidate = graph_dir / "by-root" / name / "graph.json"
        if candidate.exists():
            out[name] = candidate

    if not out:
        # Single merged / copied graph
        for candidate in (graph_dir / "graph.json", graph_dir / "merged-graph.json"):
            if candidate.exists():
                out["default"] = candidate
                break
    return out
