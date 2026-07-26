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
    """Intent-only file skeleton for a greenfield app (no graph reuse).

    Does NOT hardcode FastAPI/HTML templateIds — PLAN_REFINEMENT + IDE apply
    choose the real stack (Python/Go/Java/.NET/Node/…). Optional greenfield.*
    templateIds remain available if mediation explicitly opts into a scaffold.
    """
    surface = classification.get("surface", "full_stack")
    policy = requirements.get("policy", {})
    target_app = policy.get("targetApp", "app")
    platforms = [str(p).lower() for p in (classification.get("platform") or [])]
    plat = " ".join(platforms)
    # Soft entry paths only — PLAN_REFINEMENT should replace with the real stack.
    # No templateId → IDE apply writes (not MCP FastAPI/HTML scaffolds).
    api_path = "src/api/main.py"
    ui_path = "src/ui/index.html"
    if "go" in plat:
        api_path = "cmd/server/main.go"
    elif "java" in plat or "jvm" in plat:
        api_path = "src/main/java/App.java"
    elif ".net" in plat or "csharp" in plat or "c#" in plat:
        api_path = "src/App/Program.cs"
    elif "node" in plat or "javascript" in plat or "typescript" in plat:
        api_path = "src/server/index.ts"
        ui_path = "src/ui/App.tsx"
    elif "python" in plat:
        api_path = "src/api/main.py"

    create: list[dict[str, str]] = []
    modify: list[dict[str, str]] = []

    if surface in ("full_stack", "api_only", "unknown", "infra"):
        create.append(
            {
                "path": api_path,
                "purpose": "API/service entrypoint (refine path for chosen stack)",
                "changeSummary": (
                    "Create the real service entry for this requirement; "
                    "PLAN_REFINEMENT may replace this path for Go/Java/.NET/Node/etc."
                ),
            }
        )

    if surface in ("full_stack", "ui_only", "unknown"):
        create.append(
            {
                "path": ui_path,
                "purpose": "UI entrypoint (refine path for chosen stack)",
                "changeSummary": (
                    "Create the real UI entry; replace with React/Vue/.razor/etc. as needed."
                ),
            }
        )

    ac_mappings = [
        {
            "acId": ac.get("id", f"AC-{i + 1}"),
            "requirement": ac.get("text", ""),
            "strategy": "Greenfield intent + IDE apply (stack-agnostic)",
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
