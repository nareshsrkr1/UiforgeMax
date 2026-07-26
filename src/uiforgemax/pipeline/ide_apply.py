"""IDE apply mode — agent writes files; MCP verifies and continues.

After plan approval, product code is written with the host IDE's Read/Edit/Write
tools. MCP only verifies planned paths and runs visual/test gates.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from uiforgemax.pipeline.implement import resolve_root
from uiforgemax.pipeline.planning import action_is_mcp_scaffold, plan_all_mcp_writable


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def build_pre_apply_baseline(roots: dict[str, Path], plan: dict[str, Any]) -> dict[str, Any]:
    """Snapshot hashes of modify targets before the IDE agent edits."""
    files: dict[str, Any] = {}
    for action in list(plan.get("modify") or []):
        rel = str(action.get("path") or "").replace("\\", "/")
        if not rel:
            continue
        root = resolve_root(roots, action)
        target = root / rel
        files[rel] = {
            "root": action.get("root", "default"),
            "exists": target.is_file(),
            "sha256": _file_sha256(target),
            "purpose": action.get("purpose") or action.get("changeSummary"),
        }
    for action in list(plan.get("create") or []):
        rel = str(action.get("path") or "").replace("\\", "/")
        if not rel:
            continue
        files.setdefault(
            rel,
            {
                "root": action.get("root", "default"),
                "exists": False,
                "sha256": None,
                "purpose": action.get("purpose") or action.get("changeSummary"),
                "create": True,
            },
        )
    return {"files": files, "mode": "ide_apply"}


def write_pre_apply_baseline(run_dir: Path, roots: dict[str, Path], plan: dict[str, Any]) -> Path:
    path = run_dir / "plans" / "pre-apply-baseline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(build_pre_apply_baseline(roots, plan), indent=2), encoding="utf-8"
    )
    return path


def verify_ide_apply(
    roots: dict[str, Path],
    plan: dict[str, Any],
    baseline: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Check that create paths exist and modify paths changed (or were created).

    Returns (diff_summary, missing_or_unchanged_paths).
    """
    baseline_files = (baseline or {}).get("files") or {}
    changed: list[str] = []
    problems: list[str] = []
    details: list[dict[str, Any]] = []

    create_paths = {
        str(a.get("path") or "").replace("\\", "/")
        for a in (plan.get("create") or [])
        if a.get("path")
    }

    for action in list(plan.get("create") or []) + list(plan.get("modify") or []):
        rel = str(action.get("path") or "").replace("\\", "/")
        if not rel:
            continue
        root = resolve_root(roots, action)
        target = root / rel
        before = baseline_files.get(rel) or {}
        after_hash = _file_sha256(target)
        is_create = rel in create_paths or bool(before.get("create"))

        entry = {
            "path": rel,
            "root": action.get("root", "default"),
            "purpose": action.get("purpose") or action.get("changeSummary"),
            "exists": target.is_file(),
            "sha256": after_hash,
        }

        if not target.is_file():
            problems.append(rel)
            entry["status"] = "missing"
            details.append(entry)
            continue

        if is_create:
            changed.append(rel)
            entry["status"] = "created"
            details.append(entry)
            continue

        before_hash = before.get("sha256")
        if before_hash and after_hash and before_hash == after_hash:
            problems.append(rel)
            entry["status"] = "unchanged"
            details.append(entry)
            continue

        # No baseline hash (new file / missing before) counts as applied.
        changed.append(rel)
        entry["status"] = "modified" if before_hash else "written"
        details.append(entry)

    summary = {
        "branch": None,
        "gitCommits": False,
        "filesChanged": changed,
        "fileCount": len(changed),
        "skipped": [{"path": p, "reason": "missing_or_unchanged"} for p in problems],
        "source": plan.get("source"),
        "roots": {name: str(p) for name, p in roots.items()},
        "mode": "ide_apply",
        "details": details,
    }
    return summary, problems


def ide_apply_brief(plan: dict[str, Any]) -> dict[str, Any]:
    """Small wire payload telling the agent which paths to edit."""
    create = [
        {
            "path": a.get("path"),
            "purpose": a.get("purpose") or a.get("changeSummary"),
            "root": a.get("root", "default"),
        }
        for a in (plan.get("create") or [])
        if a.get("path")
    ]
    modify = [
        {
            "path": a.get("path"),
            "purpose": a.get("purpose") or a.get("changeSummary"),
            "changeSummary": a.get("changeSummary") or a.get("purpose"),
            "root": a.get("root", "default"),
        }
        for a in (plan.get("modify") or [])
        if a.get("path")
    ]
    return {
        "mode": "ide_apply",
        "instruction": (
            "Plan approved. Use the IDE Read/Edit/Write tools on the target project "
            "to implement every path below (Graphify already chose the targets). "
            "Then call uiforgemax_advance to verify and continue visual/test gates. "
            "Do NOT resubmit full file bodies through MCP mediation."
        ),
        "filesToCreate": create[:80],
        "filesToModify": modify[:80],
        "executionOrder": list(plan.get("executionOrder") or [])[:80],
        "mcpWritableScaffold": plan_all_mcp_writable(plan),
    }


def partition_mcp_writable(plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split plan into MCP-writable scaffold actions vs IDE-apply intent actions."""
    mcp: dict[str, Any] = {
        "create": [],
        "modify": [],
        "source": plan.get("source"),
        "subtaskPlan": plan.get("subtaskPlan"),
    }
    ide: dict[str, Any] = {
        "create": [],
        "modify": [],
        "source": plan.get("source"),
        "subtaskPlan": plan.get("subtaskPlan"),
        "executionOrder": plan.get("executionOrder"),
    }
    for action in plan.get("create") or []:
        (mcp["create"] if action_is_mcp_scaffold(action) else ide["create"]).append(action)
    for action in plan.get("modify") or []:
        (mcp["modify"] if action_is_mcp_scaffold(action) else ide["modify"]).append(action)
    return mcp, ide
