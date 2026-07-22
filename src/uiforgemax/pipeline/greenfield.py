"""Greenfield scaffolding — artifacts and templates when graph is skipped."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_greenfield_requirements(
    run_dir: Path,
    policy: str,
    classification: dict[str, Any],
) -> dict[str, Any]:
    """Requirements for a new app when there is no existing repo graph."""
    prompt_path = run_dir / "inputs" / "prompt.txt"
    jira_path = run_dir / "inputs" / "jira.raw.json"
    summary = "New application"
    ac: list[dict[str, str]] = []
    issue_key = "GREENFIELD"

    from uiforgemax.pipeline.source_of_truth import collect_source_of_truth

    description = ""
    reference_attachment = None
    if jira_path.exists():
        raw = json.loads(jira_path.read_text(encoding="utf-8"))
        issue_key = raw.get("key", issue_key)
        summary = raw.get("summary", summary)
        description = (raw.get("description") or "").strip()
        ac = raw.get("acceptanceCriteria") or [{"id": "AC-1", "text": summary}]
        atts = raw.get("attachments") or []
        if atts:
            reference_attachment = atts[0].get("filename")
    elif prompt_path.exists():
        text = prompt_path.read_text(encoding="utf-8").strip()
        summary = text[:120]
        description = text
        ac = [{"id": "AC-1", "text": text}]

    targets = classification.get("targets") or {}
    apps = targets.get("apps") or ["app"]
    domains = targets.get("domains") or ["product"]
    surface = classification.get("surface", "full_stack")
    sot = collect_source_of_truth(run_dir)

    data_needs: list[dict[str, Any]] = []
    if surface in ("full_stack", "api_only", "unknown"):
        data_needs.append(
            {
                "entity": "Resource",
                "operations": ["list", "create", "update", "delete"],
                "fields": ["name", "status"],
            }
        )
    if surface in ("full_stack", "ui_only", "unknown"):
        data_needs.append(
            {"entity": "Resource", "operations": ["list"], "fields": ["name", "status"]}
        )

    scope_in = [description] if description else [summary]
    return {
        "issueKey": issue_key,
        "summary": summary,
        "scope": {"in": scope_in, "out": []},
        "acceptanceCriteria": ac,
        "compliance": {
            "matchExactly": False,
            "referenceAttachment": reference_attachment,
            "referencePath": sot.get("primaryHtml") or sot.get("primaryImage"),
            "exactTextRequirements": [],
        },
        "sourceOfTruth": sot,
        "policy": {
            "resolutionPolicy": policy,
            "targetApp": apps[0],
            "targetDomain": domains[0],
        },
        "overrides": [],
        "dataNeeds": data_needs,
        "assumptions": ["Greenfield scaffold — no existing codebase graph."],
        "conflicts": [],
        "greenfield": True,
    }


def build_greenfield_requirement_map(
    requirements: dict[str, Any],
    classification: dict[str, Any],
) -> dict[str, Any]:
    """Plan file creates for a greenfield app (no graph reuse)."""
    surface = classification.get("surface", "full_stack")
    policy = requirements.get("policy", {})
    target_app = policy.get("targetApp", "app")

    create: list[dict[str, str]] = []
    modify: list[dict[str, str]] = []

    if surface in ("full_stack", "api_only", "unknown", "infra"):
        create.extend(
            [
                {"path": "backend/requirements.txt", "purpose": "Python deps", "templateId": "greenfield.backend_requirements"},
                {"path": "backend/app/__init__.py", "purpose": "Package marker", "templateId": "greenfield.backend_init"},
                {"path": "backend/app/store.py", "purpose": "In-memory store", "templateId": "greenfield.backend_store"},
                {"path": "backend/app/main.py", "purpose": "FastAPI CRUD API", "templateId": "greenfield.backend_main"},
                {"path": "backend/README.md", "purpose": "Backend docs", "templateId": "greenfield.backend_readme"},
            ]
        )

    if surface in ("full_stack", "ui_only", "unknown"):
        create.extend(
            [
                {"path": "ui/index.html", "purpose": "CRUD UI entry point", "templateId": "greenfield.ui_index"},
                {"path": "ui/styles.css", "purpose": "UI styles", "templateId": "greenfield.ui_styles"},
                {"path": "ui/serve.py", "purpose": "Static UI server", "templateId": "greenfield.ui_serve"},
                {"path": "ui/README.md", "purpose": "UI docs", "templateId": "greenfield.ui_readme"},
            ]
        )

    create.extend(
        [
            {"path": "README.md", "purpose": "Project overview", "templateId": "greenfield.readme"},
            {"path": "start.ps1", "purpose": "Launch script", "templateId": "greenfield.start_ps1"},
        ]
    )

    ac_mappings = [
        {
            "acId": ac.get("id", f"AC-{i + 1}"),
            "requirement": ac.get("text", ""),
            "strategy": "Greenfield scaffold + IDE plan refinement",
            "graphEvidence": [],
        }
        for i, ac in enumerate(requirements.get("acceptanceCriteria", []))
    ]

    execution_order = [f["path"] for f in create]

    return {
        "issueKey": requirements.get("issueKey"),
        "summary": requirements.get("summary"),
        "targetApp": target_app,
        "targetDomain": policy.get("targetDomain", "product"),
        "reuse": [],
        "apiExists": [],
        "apiGaps": [{"operation": "crud", "status": "missing"}] if surface != "ui_only" else [],
        "clients": [],
        "acceptanceMappings": ac_mappings,
        "create": create,
        "modify": modify,
        "executionOrder": execution_order,
        "clarifications": [],
        "scope": requirements.get("scope", {}),
        "assumptions": requirements.get("assumptions", []),
        "overrides": [],
        "source": "greenfield_scaffold",
        "stats": {
            "reuseCount": 0,
            "createCount": len(create),
            "modifyCount": 0,
            "acCount": len(requirements.get("acceptanceCriteria", [])),
            "acMapped": len(ac_mappings),
            "clarificationCount": 0,
        },
    }


def build_greenfield_api_resolution(
    requirements: dict[str, Any],
    classification: dict[str, Any],
) -> dict[str, Any]:
    surface = classification.get("surface", "full_stack")
    if surface == "ui_only":
        return {"resolution": [], "gate1Required": False, "source": "greenfield_skipped"}

    resolution = [
        {
            "need": "crud_api",
            "status": "missing",
            "suggestedEndpoint": "/api/items",
            "action": "create",
        }
    ]
    # Greenfield scaffold already commits to new APIs — Gate 1 is redundant.
    return {"resolution": resolution, "gate1Required": False, "source": "greenfield_scaffold"}


def write_greenfield_graph_stubs(run_dir: Path, req_map: dict[str, Any]) -> None:
    """Minimal graph folder so downstream stages find expected paths."""
    graph_dir = run_dir / "graph"
    graph_dir.mkdir(parents=True, exist_ok=True)
    stub = {"source": "greenfield_scaffold", "skipped": True, "reason": "No existing repo to index"}
    for name in ("index.json", "queries.json", "query-results.json"):
        (graph_dir / name).write_text(json.dumps(stub, indent=2), encoding="utf-8")
    (graph_dir / "requirement-map.json").write_text(json.dumps(req_map, indent=2), encoding="utf-8")
    pack = {
        "architecture": {"primary": "greenfield", "projectCount": 0},
        "targetApp": req_map.get("targetApp"),
        "targetDomain": req_map.get("targetDomain"),
        "reuse": [],
        "apiGaps": req_map.get("apiGaps", []),
        "apiExists": [],
        "source": "greenfield_scaffold",
    }
    (graph_dir / "context-pack.json").write_text(json.dumps(pack, indent=2), encoding="utf-8")
