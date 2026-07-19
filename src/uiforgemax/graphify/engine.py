"""Graphify engine — real CLI update / merge / API resolution helpers."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from uiforgemax.graphify.cli_runner import GraphifyCliError, run_merge_graphs, run_update
from uiforgemax.graphify.stack import detect_stack


def graphify_update(project_root: Path) -> dict[str, Any]:
    """Update one repo via real ``graphify update``; return a small meta index."""
    root = Path(project_root)
    stack = detect_stack(root)
    update = run_update(root)
    return {
        "generatedBy": "graphify-cli",
        "projectRoot": str(root.resolve()),
        "stack": stack,
        "graphifyOut": update["graphifyOut"],
        "graphJson": update["graphJson"],
        "architecture": {
            "type": stack["primary"],
            "projectCount": 1 if not stack["empty"] else 0,
            "kinds": stack["kinds"],
            "nx": stack["nx"],
        },
        "nodeCount": update["nodeCount"],
        "edgeCount": update["edgeCount"],
        "sourceFiles": update["sourceFiles"],
        "stdout": update.get("stdout"),
    }


def graphify_update_multi(roots: dict[str, Path]) -> dict[str, dict[str, Any]]:
    """Run real Graphify update on every registered root."""
    return {name: graphify_update(path) for name, path in roots.items()}


def graphify_merge(
    per_root: dict[str, dict[str, Any]],
    *,
    surface: str | None = None,
    default_root: Path | None = None,
    run_graph_dir: Path | None = None,
) -> dict[str, Any]:
    """Merge multi-root graphs with real ``graphify merge-graphs`` when needed."""
    graph_paths = []
    for name, meta in per_root.items():
        path = Path(meta.get("graphJson") or "")
        if path.exists():
            graph_paths.append((name, path))

    out_dir = Path(run_graph_dir) if run_graph_dir else (
        Path(default_root) / "graphify-out" if default_root else Path(".")
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    if len(graph_paths) >= 2:
        merged_path = out_dir / "merged-graph.json"
        merge_info = run_merge_graphs([p for _, p in graph_paths], merged_path)
        # Also copy into default root's graphify-out when present
        if default_root is not None:
            dest = Path(default_root) / "graphify-out" / "merged-graph.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(merged_path, dest)
        merged = {
            "generatedBy": "graphify-cli",
            "merged": True,
            "surface": surface,
            "roots": {name: meta for name, meta in per_root.items()},
            "mergedGraph": str(merged_path),
            "nodeCount": merge_info.get("nodeCount", 0),
            "edgeCount": merge_info.get("edgeCount", 0),
            "integration": {
                "uiAndApi": surface in ("full_stack", "unknown") or len(per_root) > 1,
                "rootCount": len(per_root),
                "roots": sorted(per_root.keys()),
            },
        }
    else:
        name, meta = next(iter(per_root.items()))
        merged = {
            "generatedBy": "graphify-cli",
            "merged": False,
            "surface": surface,
            "roots": {name: meta},
            "mergedGraph": meta.get("graphJson"),
            "nodeCount": meta.get("nodeCount", 0),
            "edgeCount": meta.get("edgeCount", 0),
            "integration": {
                "uiAndApi": False,
                "rootCount": 1,
                "roots": [name],
            },
        }
        # Convenience copy as index pointer
        if meta.get("graphJson") and run_graph_dir:
            src = Path(meta["graphJson"])
            if src.exists():
                shutil.copy2(src, Path(run_graph_dir) / "graph.json")

    if run_graph_dir:
        (Path(run_graph_dir) / "merged.json").write_text(json.dumps(merged, indent=2), encoding="utf-8")
        (Path(run_graph_dir) / "index.json").write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return merged


def resolve_apis(requirements: dict[str, Any], graph_index: dict[str, Any]) -> dict[str, Any]:
    """Generic API gap hints from requirements + graph meta (no Nx hardcodes).

    Prefer query-stage evidence; this remains a light Gate-1 helper.
    """
    surface = requirements.get("surface") or graph_index.get("surface")
    if surface == "ui_only":
        return {"resolution": [], "gate1Required": False, "source": "ui_only_skip"}

    policy = requirements.get("policy", {}).get("resolutionPolicy", "pending")
    source_files = set()
    for meta in (graph_index.get("roots") or {}).values():
        if isinstance(meta, dict):
            source_files.update(meta.get("sourceFiles") or [])
    # Also accept flattened list
    source_files.update(graph_index.get("sourceFiles") or [])

    resolution: list[dict[str, Any]] = []
    for need in requirements.get("dataNeeds", []):
        entity = need.get("entity", "Resource")
        for operation in need.get("operations", []):
            hint = f"{operation} {entity}".lower()
            hit_file = next((f for f in source_files if entity.lower().rstrip("s") in f.lower()), None)
            if hit_file:
                resolution.append(
                    {
                        "need": f"{operation}{entity}",
                        "status": "exists",
                        "file": hit_file,
                        "action": "reuse",
                    }
                )
            else:
                action = "create"
                if policy == "contract_driven":
                    action = "block"
                elif policy == "frontend_first":
                    action = "mock_or_create"
                resolution.append(
                    {
                        "need": f"{operation}{entity}",
                        "status": "missing",
                        "suggestedEndpoint": f"/api/{entity.lower().rstrip('s')}s",
                        "action": action,
                        "hint": hint,
                    }
                )

    gate1 = any(r["status"] == "missing" and r["action"] == "block" for r in resolution)
    return {"resolution": resolution, "gate1Required": gate1, "source": "generic"}


# Backward-compat aliases used by older imports / tests
def build_context_pack_from_map(run_graph_dir: Path, req_map: dict[str, Any], graph_index: dict[str, Any]) -> dict[str, Any]:
    from uiforgemax.graphify.pipeline import _slim_context_pack

    pack = _slim_context_pack(req_map, graph_index)
    run_graph_dir.mkdir(parents=True, exist_ok=True)
    (run_graph_dir / "context-pack.json").write_text(json.dumps(pack, indent=2), encoding="utf-8")
    return pack


def build_context_pack(
    project_root: Path,
    requirements: dict[str, Any],
    graph_index: dict[str, Any],
    run_graph_dir: Path,
) -> dict[str, Any]:
    from uiforgemax.graphify.pipeline import run_graph_analysis, load_visual_spec

    return run_graph_analysis(run_graph_dir.parent, requirements, graph_index, load_visual_spec(run_graph_dir.parent))[
        "contextPack"
    ]


__all__ = [
    "GraphifyCliError",
    "build_context_pack",
    "build_context_pack_from_map",
    "graphify_merge",
    "graphify_update",
    "graphify_update_multi",
    "resolve_apis",
]
