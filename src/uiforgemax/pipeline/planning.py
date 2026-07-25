"""Planning — built ONLY from graph artifacts (requirement-map, api-resolution)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def generate_understanding(
    run_dir: Path,
    requirements: dict[str, Any],
    req_map: dict[str, Any],
    api_resolution: dict[str, Any],
) -> Path:
    from uiforgemax.pipeline.target_sanitize import sanitize_requirement_map

    req_map = sanitize_requirement_map(dict(req_map))
    package = build_understanding_approval_package(run_dir, requirements, req_map, api_resolution)
    (run_dir / "plans" / "understanding-approval.json").write_text(
        json.dumps(package, indent=2), encoding="utf-8"
    )
    md = _understanding_markdown(package, req_map, requirements, api_resolution)
    out = run_dir / "plans" / "understanding.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    return out


def build_understanding_approval_package(
    run_dir: Path,
    requirements: dict[str, Any] | None = None,
    req_map: dict[str, Any] | None = None,
    api_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Human-facing Gate 2 package — topology, steps, files, risks (must be displayed)."""
    from uiforgemax.pipeline.target_sanitize import sanitize_requirement_map

    requirements = requirements or _load_json(run_dir / "requirements.normalized.json")
    req_map = sanitize_requirement_map(
        req_map or _load_json(run_dir / "graph" / "requirement-map.json")
    )
    api_resolution = api_resolution or _load_json(run_dir / "api-resolution.json") or {
        "resolution": [],
        "gate1Required": False,
    }
    from uiforgemax.pipeline.source_of_truth import collect_source_of_truth

    create = list(req_map.get("create") or [])
    modify = list(req_map.get("modify") or [])
    topology = _derive_topology(run_dir, requirements, req_map)
    sot = requirements.get("sourceOfTruth") or collect_source_of_truth(run_dir)
    requirements = {**requirements, "sourceOfTruth": sot}
    risks, blockers = _derive_risks_blockers(req_map, api_resolution, create, modify, requirements)
    scope = requirements.get("scope") or req_map.get("scope") or {}
    steps = _implementation_steps(req_map, modify, create)
    issues = list(requirements.get("conflicts") or [])
    issues.extend(c.get("question", "") for c in (req_map.get("clarifications") or []) if c.get("question"))

    return {
        "gate": 2,
        "title": "Understanding approval — review before planning",
        "summary": req_map.get("summary") or requirements.get("summary"),
        "issueKey": req_map.get("issueKey") or requirements.get("issueKey"),
        "classification": {
            "surface": req_map.get("surface") or topology.get("surface"),
            "policy": topology.get("policy")
            or (requirements.get("policy") or {}).get("resolutionPolicy"),
            "targetApp": topology.get("targetApp"),
            "targetDomain": topology.get("targetDomain"),
        },
        "topology": topology,
        "scope": {"in": scope.get("in") or [], "out": scope.get("out") or []},
        "acceptanceCriteria": [
            {
                "id": m.get("acId"),
                "text": m.get("requirement"),
                "strategy": m.get("strategy"),
            }
            for m in (req_map.get("acceptanceMappings") or [])
        ],
        "implementationSteps": steps,
        "filesToCreate": [
            {"path": f.get("path"), "purpose": f.get("purpose"), "root": f.get("root", "default")}
            for f in create
        ],
        "filesToModify": [
            {"path": f.get("path"), "purpose": f.get("purpose"), "root": f.get("root", "default")}
            for f in modify
        ],
        "executionOrder": req_map.get("executionOrder") or [f.get("path") for f in modify + create],
        "reuse": req_map.get("reuse") or [],
        "sourceOfTruth": sot,
        "risks": risks,
        "blockers": blockers,
        "issues": [i for i in issues if i],
        "api": {
            "gate1Required": bool(api_resolution.get("gate1Required")),
            "gaps": req_map.get("apiGaps") or [],
            "resolution": api_resolution.get("resolution") or [],
        },
        "assumptions": req_map.get("assumptions") or requirements.get("assumptions") or [],
        "artifacts": {
            "understandingMd": "plans/understanding.md",
            "understandingApproval": "plans/understanding-approval.json",
            "requirementMap": "graph/requirement-map.json",
            "attachmentsManifest": "inputs/attachments.json",
        },
        "displayInstruction": (
            "ALWAYS show the human before approve_understanding: topology, scope in/out, "
            "acceptance criteria, implementation steps, files to create/modify, execution order, "
            "source-of-truth attachments (HTML/wireframe/design notes), risks, blockers, and issues. "
            "Do not use a one-line summary only."
        ),
    }


def _implementation_steps(
    req_map: dict[str, Any],
    modify: list[dict[str, Any]],
    create: list[dict[str, Any]],
) -> list[str]:
    steps: list[str] = []
    for i, m in enumerate(req_map.get("acceptanceMappings") or [], 1):
        steps.append(
            f"{i}. [{m.get('acId')}] {m.get('strategy') or m.get('requirement') or 'Apply AC'}"
        )
    if not steps:
        for i, f in enumerate(modify + create, 1):
            steps.append(f"{i}. Update `{f.get('path')}` — {f.get('purpose') or 'apply change'}")
    if create and not any("create" in s.lower() for s in steps):
        for f in create:
            steps.append(f"Create `{f.get('path')}` — {f.get('purpose') or 'new file'}")
    return steps


def _understanding_markdown(
    package: dict[str, Any],
    req_map: dict[str, Any],
    requirements: dict[str, Any],
    api_resolution: dict[str, Any],
) -> str:
    topo = package.get("topology") or {}
    roots = topo.get("roots") or {}
    roots_lines = "\n".join(
        f"- `{name}`: type={info.get('type')}, nx={info.get('nx')}"
        for name, info in roots.items()
    ) or "- _(default workspace)_"
    scope = package.get("scope") or {}
    ac_rows = "\n".join(
        f"| {a.get('id')} | {a.get('text', '')} | {a.get('strategy', '')} |"
        for a in (package.get("acceptanceCriteria") or [])
    ) or "| — | — | — |"
    modify = package.get("filesToModify") or []
    create = package.get("filesToCreate") or []
    files_mod = "\n".join(
        f"- `{f.get('path')}` — {f.get('purpose') or ''}" for f in modify
    ) or "_None_"
    files_create = "\n".join(
        f"- `{f.get('path')}` — {f.get('purpose') or ''}" for f in create
    ) or "_None_"
    steps = "\n".join(f"- {s}" for s in (package.get("implementationSteps") or [])) or "_None_"
    risks = "\n".join(f"- {r}" for r in (package.get("risks") or [])) or "_None_"
    blockers = "\n".join(f"- {b}" for b in (package.get("blockers") or [])) or "_None_"
    issues = "\n".join(f"- {i}" for i in (package.get("issues") or [])) or "_None_"
    scope_in = "\n".join(f"- {x}" for x in (scope.get("in") or [])) or "_None_"
    scope_out = "\n".join(f"- {x}" for x in (scope.get("out") or [])) or "_None_"
    order = "\n".join(
        f"{i+1}. `{p}`" for i, p in enumerate(package.get("executionOrder") or [])
    ) or "_None_"

    return f"""# Understanding: {package.get('summary')} ({package.get('issueKey')})

## Topology
- Target app: `{topo.get('targetApp')}` · domain `{topo.get('targetDomain')}`
- Surface: `{topo.get('surface')}` · policy: `{topo.get('policy')}`
- Multi-root: {topo.get('monorepo')}
### Roots
{roots_lines}

## Scope in
{scope_in}

## Scope out
{scope_out}

## Acceptance criteria
| AC | Requirement | Strategy |
|----|-------------|----------|
{ac_rows}

## Implementation steps
{steps}

## Files to modify
{files_mod}

## Files to create
{files_create}

## Execution order
{order}

## Risks
{risks}

## Blockers
{blockers}

## Issues / conflicts
{issues}

## API
- Gate 1: {'REQUIRED' if (package.get('api') or {}).get('gate1Required') else 'SKIPPED'}
- Gaps: {len((package.get('api') or {}).get('gaps') or [])}

## Analysis source
- {req_map.get('source', 'graph/requirement-map.json')}
- Reuse: {req_map.get('stats', {}).get('reuseCount', 0)} · create: {len(create)} · modify: {len(modify)}
"""


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _derive_topology(run_dir: Path, requirements: dict[str, Any], req_map: dict[str, Any]) -> dict[str, Any]:
    ctx = _load_json(run_dir / "graph" / "context-pack.json")
    arch = ctx.get("architecture") or _load_json(run_dir / "architecture.json")
    roots = (arch.get("roots") if isinstance(arch, dict) else None) or {}
    policy = requirements.get("policy") or {}
    return {
        "targetApp": req_map.get("targetApp") or policy.get("targetApp"),
        "targetDomain": req_map.get("targetDomain") or policy.get("targetDomain"),
        "surface": req_map.get("surface") or ctx.get("surface"),
        "monorepo": bool(len(roots) > 1) if roots else False,
        "roots": {
            name: {
                "type": info.get("type") or info.get("primary"),
                "nx": info.get("nx", False),
                "kinds": info.get("kinds", []),
            }
            for name, info in roots.items()
            if isinstance(info, dict)
        }
        or {"default": {"type": "unknown", "nx": False, "kinds": []}},
        "policy": policy.get("resolutionPolicy"),
    }


EMPTY_PLAN_BLOCKER = "Plan has no create/modify files — nothing to implement."
MISSING_CONTENT_BLOCKER = (
    "Plan lists create/modify paths but PLAN_REFINEMENT did not supply writable "
    "`content` (or a known greenfield templateId/patchId). Implement cannot invent "
    "product code from path+purpose alone — re-run PLAN_REFINEMENT with full file bodies."
)

# Scaffold / legacy writers that can produce bytes without mediated `content`.
_WRITABLE_WITHOUT_CONTENT_TEMPLATES = frozenset(
    {
        "api.export_route",
        "client.export_customers",
        "page.customer_list",
        "test.customer_list",
        "greenfield.backend_requirements",
        "greenfield.backend_init",
        "greenfield.backend_store",
        "greenfield.backend_main",
        "greenfield.backend_readme",
        "greenfield.ui_index",
        "greenfield.ui_styles",
        "greenfield.ui_serve",
        "greenfield.ui_readme",
        "greenfield.readme",
        "greenfield.start_ps1",
    }
)
_WRITABLE_WITHOUT_CONTENT_PATCHES = frozenset(
    {
        "app.register_export",
        "data_access.export",
        "portal.app_routing",
        "portal.green_button",
    }
)


def plan_has_file_actions(plan: dict[str, Any] | None) -> bool:
    """True when the plan has at least one create or modify file action."""
    if not plan:
        return False
    return bool(plan.get("create") or plan.get("modify"))


def plan_file_action_count(plan: dict[str, Any] | None) -> int:
    if not plan:
        return 0
    return len(plan.get("create") or []) + len(plan.get("modify") or [])


def action_has_writable_body(action: dict[str, Any] | None) -> bool:
    """True when implement can write this action without inventing product code."""
    if not action:
        return False
    content = action.get("content")
    if content is not None and str(content).strip() != "":
        return True
    tid = str(action.get("templateId") or "")
    if tid in _WRITABLE_WITHOUT_CONTENT_TEMPLATES or tid.startswith("greenfield."):
        return True
    pid = str(action.get("patchId") or "")
    if pid in _WRITABLE_WITHOUT_CONTENT_PATCHES:
        return True
    return False


def plan_actions_missing_content(plan: dict[str, Any] | None) -> list[str]:
    """Paths that would be skipped at implement (path+purpose only, no body)."""
    if not plan:
        return []
    missing: list[str] = []
    for action in list(plan.get("create") or []) + list(plan.get("modify") or []):
        if not action_has_writable_body(action):
            missing.append(str(action.get("path") or "?"))
    return missing


def plan_is_implementable(plan: dict[str, Any] | None) -> bool:
    """Non-empty create/modify AND every action has content or a known scaffold id."""
    return plan_has_file_actions(plan) and not plan_actions_missing_content(plan)


def build_plan_review(
    plan: dict[str, Any],
    *,
    requirements: dict[str, Any] | None = None,
    req_map: dict[str, Any] | None = None,
    api_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build plan-review.json from the current plan.

    Soft risks (e.g. README-only) stay advisory. Hard blockers — including an
    empty create/modify list or paths without ``content`` — always force
    ``verdict: revise`` so we never open the human plan gate for a plan that
    would write 0 product files after approval.
    """
    requirements = requirements or {}
    req_map = req_map or {}
    api_resolution = api_resolution or {}
    create = list(plan.get("create") or [])
    modify = list(plan.get("modify") or [])
    risks, blockers = _derive_risks_blockers(
        req_map, api_resolution, create, modify, requirements
    )
    for b in plan.get("blockers") or []:
        if b not in blockers:
            blockers.append(b)
    for r in plan.get("risks") or []:
        if r not in risks:
            risks.append(r)

    missing_bodies = plan_actions_missing_content(plan)
    if missing_bodies and MISSING_CONTENT_BLOCKER not in blockers:
        blockers.append(
            f"{MISSING_CONTENT_BLOCKER} Missing content for: {', '.join(missing_bodies[:12])}"
            + ("…" if len(missing_bodies) > 12 else "")
        )

    ac_total = len(requirements.get("acceptanceCriteria", []))
    mapped = len(plan.get("acceptanceMappings") or req_map.get("acceptanceMappings") or [])
    unmapped = [] if (not ac_total or mapped >= ac_total) else [f"Unmapped AC count: {ac_total - mapped}"]
    required = unmapped + list(blockers)
    return {
        "verdict": "revise" if required else "pass",
        "coverage": {"acTotal": ac_total, "acCovered": mapped, "gaps": unmapped},
        "risks": risks,
        "blockers": blockers,
        "requiredPlanChanges": required,
        "missingContentPaths": missing_bodies,
        "validationPlan": plan.get("validationPlan") or {},
    }


def _derive_risks_blockers(
    req_map: dict[str, Any],
    api_resolution: dict[str, Any],
    create: list[dict[str, Any]],
    modify: list[dict[str, Any]],
    requirements: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    risks: list[str] = []
    blockers: list[str] = []

    if req_map.get("weakGraph"):
        risks.append("Graphify evidence is thin — file targets may need human confirmation.")
    if not modify and not create:
        blockers.append(EMPTY_PLAN_BLOCKER)
    readme_only = modify and all(Path(f.get("path", "")).name.lower() == "readme.md" for f in modify)
    if readme_only and req_map.get("surface") in (None, "ui_only"):
        risks.append(
            "Modify list is README-only; dark-mode / UI work likely needs styles/HTML/components — refine before implement."
        )
    api_gaps = req_map.get("apiGaps") or []
    if api_gaps:
        risks.append(f"API gaps remaining: {len(api_gaps)}")
    if api_resolution.get("gate1Required") and not api_resolution.get("approved"):
        blockers.append("API gate still required but not approved.")
    for c in req_map.get("clarifications") or []:
        if not c.get("resolved"):
            blockers.append(f"Unresolved clarification: {c.get('id', '?')} — {c.get('question', '')}")
    sot = (requirements or {}).get("sourceOfTruth") or {}
    missing = sot.get("missing") or []
    if missing:
        blockers.append(
            "Source-of-truth attachment(s) not stored in run inputs: "
            + ", ".join(str(m) for m in missing)
            + ". Re-fetch Jira attachments or add_html/add_image before implement."
        )
    elif sot.get("primaryHtml") or sot.get("primaryImage") or sot.get("designNotes"):
        risks.append(
            "Visual/HTML/design-note SoT is present — plan must reference those artifacts "
            "(inputs/attachments/, inputs/page.html) for layout and tokens."
        )
    # Do not invent a fake "API gaps: 0" risk — empty means clean.
    return risks, blockers


def _derive_validation_plan(
    requirements: dict[str, Any],
    req_map: dict[str, Any],
    create: list[dict[str, Any]],
    modify: list[dict[str, Any]],
) -> dict[str, Any]:
    """Static/manual checks only — test framework + paths come from TEST_GENERATION.

    MCP must not invent ``*.test.tsx`` / pytest / junit paths here. The IDE model
    inspects the real repo and returns idiomatic tests + ``run[]`` commands.
    """
    files = [f.get("path") for f in (create + modify) if f.get("path")]
    acs = requirements.get("acceptanceCriteria") or req_map.get("acceptanceMappings") or []
    compliance = requirements.get("compliance") or {}
    static_checks = [
        "Created files from plan exist on disk",
        "Template sanity markers present (when templateId known)",
        "TEST_GENERATION mediation supplied stack-appropriate tests + run commands",
    ]
    if compliance.get("exactTextRequirements"):
        static_checks.append("Exact-text compliance strings found in touched files")
    manual = [
        f"Verify AC {m.get('acId') or m.get('id')}: {m.get('requirement') or m.get('text')}"
        for m in acs
    ]
    automated = {
        "staticFileChecks": True,
        "unitTests": True,
        "codeCoverage": True,
        "coverageThreshold": {"lines": 70, "soft": True},
        "drivenBy": "TEST_GENERATION mediation only — MCP never chooses pytest/vitest/junit",
        "generatedTests": (
            "IDE decides stack (pytest, vitest, junit, go test, …), file paths, and run[]; "
            "MCP only writes those files and executes those commands"
        ),
        "complianceExactText": bool(compliance.get("exactTextRequirements")),
    }
    return {
        "filesUnderTest": files,
        "automated": automated,
        "staticChecks": static_checks,
        "manualChecks": manual
        or [
            "Visually confirm acceptance criteria on the running app",
            "Confirm layout/structure unchanged where required",
        ],
        # Filled later by TEST_GENERATION — leave empty so Gate 3 does not imply a stack.
        "unit": [],
        "unitSpecs": [{"covers": p, "type": "unit", "path": "(mediation decides)"} for p in files],
        "compliance": compliance,
    }


def build_plan_approval_package(run_dir: Path, plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """Sole human-facing approval package before implement."""
    plan = plan or _load_json(run_dir / "plans" / "implementation-plan.json")
    review = _load_json(run_dir / "plans" / "plan-review.json")
    reqs = _load_json(run_dir / "requirements.normalized.json")
    understanding = _load_json(run_dir / "plans" / "understanding-approval.json")
    create = plan.get("create") or []
    modify = plan.get("modify") or []
    validation = plan.get("validationPlan") or _derive_validation_plan(reqs, {}, create, modify)
    tests = plan.get("tests") or {}
    if not tests.get("unit") and validation.get("unit"):
        tests = {
            **tests,
            "unit": validation.get("unit") or [],
            "staticChecks": validation.get("staticChecks") or tests.get("staticChecks") or [],
            "manualChecks": validation.get("manualChecks") or tests.get("manualChecks") or [],
            "automated": validation.get("automated") or tests.get("automated") or {},
        }
    sot = plan.get("sourceOfTruth") or reqs.get("sourceOfTruth") or understanding.get("sourceOfTruth") or {}
    visual = plan.get("visualCompliance") or {}

    subtask_plan = plan.get("subtaskPlan")
    subtask_summary: list[dict[str, Any]] = []
    if subtask_plan:
        for st in subtask_plan.get("subtasks", []):
            subtask_summary.append({
                "subtaskId": st.get("subtaskId"),
                "title": st.get("title"),
                "surfaceHint": st.get("surfaceHint"),
                "linkedAcIds": st.get("linkedAcIds", []),
                "dependencies": st.get("dependencies", []),
                "complexity": st.get("complexity"),
                "fileCount": len(st.get("modify", [])) + len(st.get("create", [])),
            })

    package = {
        "gate": 1,
        "title": "Plan approval — sole review before implement",
        "summary": plan.get("summary") or reqs.get("summary"),
        "issueKey": plan.get("issueKey") or reqs.get("issueKey"),
        "topology": plan.get("topology") or understanding.get("topology") or {},
        "scope": understanding.get("scope") or {},
        "acceptanceCriteria": understanding.get("acceptanceCriteria") or [],
        "filesToCreate": [
            {
                "path": f.get("path"),
                "purpose": f.get("purpose"),
                "root": f.get("root", "default"),
                "hasContent": action_has_writable_body(f),
            }
            for f in create
        ],
        "filesToModify": [
            {
                "path": f.get("path"),
                "purpose": f.get("purpose"),
                "root": f.get("root", "default"),
                "hasContent": action_has_writable_body(f),
            }
            for f in modify
        ],
        "executionOrder": plan.get("executionOrder") or [],
        "acceptanceMappings": plan.get("acceptanceMappings") or [],
        "sourceOfTruth": sot,
        "visualCompliance": visual,
        "risks": plan.get("risks") or review.get("risks") or [],
        "blockers": plan.get("blockers") or [],
        "missingContentPaths": plan_actions_missing_content(plan),
        "implementable": plan_is_implementable(plan),
        "validationPlan": validation,
        "tests": tests,
        "review": {
            "verdict": review.get("verdict"),
            "acceptanceCoverage": review.get("coverage"),
            "codeCoverageRequired": True,
            "unitTestsRequired": True,
        },
        "artifacts": {
            "planJson": "plans/implementation-plan.json",
            "planMd": "plans/implementation-plan.md",
            "planReview": "plans/plan-review.json",
            "understandingMd": "plans/understanding.md",
            "attachmentsManifest": "inputs/attachments.json",
            "referenceHtml": sot.get("primaryHtml") or visual.get("referenceHtml"),
            "referenceImage": sot.get("primaryImage") or visual.get("referenceImage"),
        },
        "displayInstruction": (
            "SOLE human gate before implement. Show: topology, scope, ACs, files to "
            "create/modify, execution order, source-of-truth references "
            "(HTML / wireframe / mockup / design notes under inputs/attachments/), "
            "risks, blockers, unit tests to generate, coverage expectation, validation plan. "
            "Then approve_plan or request_changes. After approve_plan the same locked plan "
            "is implemented — no second approval."
        ),
    }
    if subtask_summary:
        package["subtaskBreakdown"] = {
            "strategy": subtask_plan.get("decompositionStrategy", "unknown"),
            "subtaskCount": subtask_plan.get("subtaskCount", 0),
            "dependencyOrder": subtask_plan.get("dependencyOrder", []),
            "parallelGroups": subtask_plan.get("parallelGroups", []),
            "subtasks": subtask_summary,
        }
        package["displayInstruction"] = (
            "SOLE human gate before implement. Show: topology, scope, ACs, "
            "SUB-TASK BREAKDOWN (strategy, dependency order, per-sub-task files and ACs), "
            "files to create/modify, execution order, source-of-truth references "
            "(HTML / wireframe / mockup / design notes under inputs/attachments/), "
            "risks, blockers, unit tests to generate, coverage expectation, validation plan. "
            "Then approve_plan or request_changes. After approve_plan the same locked plan "
            "is implemented — no second approval."
        )
    return package


def generate_plan(
    run_dir: Path,
    requirements: dict[str, Any],
    req_map: dict[str, Any],
    api_resolution: dict[str, Any],
    feedback: str | None = None,
) -> tuple[dict, dict]:
    from uiforgemax.pipeline.decompose import load_subtasks
    from uiforgemax.pipeline.target_sanitize import sanitize_plan_targets, sanitize_requirement_map

    req_map = sanitize_requirement_map(dict(req_map))
    create = list(req_map.get("create", []))
    modify = list(req_map.get("modify", []))
    if feedback and "side panel" in feedback.lower():
        create = [f for f in create if "detail" not in f.get("path", "").lower()]

    # Prefer mediated AC strategies when graph modify list is weak (e.g. README-only).
    for review in req_map.get("acceptanceMappingsReview") or []:
        ac_id = review.get("acId")
        for m in req_map.get("acceptanceMappings") or []:
            if m.get("acId") == ac_id and review.get("strategy"):
                m["strategy"] = review["strategy"]

    from uiforgemax.pipeline.source_of_truth import collect_source_of_truth

    topology = _derive_topology(run_dir, requirements, req_map)
    sot = requirements.get("sourceOfTruth") or collect_source_of_truth(run_dir)
    requirements = {**requirements, "sourceOfTruth": sot}
    risks, blockers = _derive_risks_blockers(req_map, api_resolution, create, modify, requirements)
    validation = _derive_validation_plan(requirements, req_map, create, modify)
    visual_compliance = {
        "referenceHtml": sot.get("primaryHtml"),
        "referenceImage": sot.get("primaryImage"),
        "designNotes": sot.get("designNotes") or [],
        "referenceArtifacts": [
            {
                "filename": a.get("filename"),
                "role": a.get("role"),
                "path": a.get("path"),
            }
            for a in (sot.get("attachments") or [])
            if a.get("path")
        ],
        "checks": [
            "Side-by-side review against reference HTML/wireframe before handover",
        ]
        if sot.get("primaryHtml") or sot.get("primaryImage")
        else [],
    }

    subtasks = load_subtasks(run_dir)
    subtask_plan = None
    if subtasks and subtasks.get("subtaskCount", 0) > 1:
        subtask_plan = _build_subtask_plan(
            subtasks, req_map, create, modify,
        )
        create = _tag_actions_with_subtask(create, subtask_plan)
        modify = _tag_actions_with_subtask(modify, subtask_plan)

    plan = {
        "source": req_map.get("source", "graph/requirement-map.json"),
        "issueKey": req_map.get("issueKey") or requirements.get("issueKey"),
        "summary": req_map.get("summary") or requirements.get("summary"),
        "topology": topology,
        "reuse": req_map.get("reuse", []),
        "create": create,
        "modify": modify,
        "executionOrder": _build_execution_order(req_map, subtask_plan),
        "acceptanceMappings": req_map.get("acceptanceMappings", []),
        "sourceOfTruth": sot,
        "visualCompliance": visual_compliance,
        "risks": risks,
        "blockers": blockers,
        "validationPlan": validation,
        "tests": {
            "unit": validation.get("unit") or [],
            "unitSpecs": validation.get("unitSpecs") or [],
            "staticChecks": validation.get("staticChecks") or [],
            "manualChecks": validation.get("manualChecks") or [],
            "automated": validation.get("automated") or {},
            "compliance": requirements.get("compliance", {}),
        },
    }
    if subtask_plan:
        plan["subtaskPlan"] = subtask_plan
    plan = sanitize_plan_targets(plan)
    # Sanitize can drop all create/modify targets — rebuild review from the
    # final lists so empty plans cannot force-pass and reach human approval.
    review = build_plan_review(
        plan,
        requirements=requirements,
        req_map=req_map,
        api_resolution=api_resolution,
    )
    plan["risks"] = review["risks"]
    plan["blockers"] = review["blockers"]

    (run_dir / "plans" / "implementation-plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    md = _plan_markdown(plan)
    (run_dir / "plans" / "implementation-plan.md").write_text(md, encoding="utf-8")
    (run_dir / "plans" / "plan-review.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
    approval = build_plan_approval_package(run_dir, plan)
    (run_dir / "plans" / "plan-approval.json").write_text(json.dumps(approval, indent=2), encoding="utf-8")
    return plan, review


def _build_subtask_plan(
    subtasks: dict[str, Any],
    req_map: dict[str, Any],
    create: list[dict[str, Any]],
    modify: list[dict[str, Any]],
) -> dict[str, Any]:
    st_list = subtasks.get("subtasks", [])
    dep_order = subtasks.get("dependencyOrder", [st["id"] for st in st_list])
    parallel_groups = subtasks.get("parallelGroups", [])
    req_map_subtasks = req_map.get("subtasks") or {}

    ordered_subtasks: list[dict[str, Any]] = []
    for st in st_list:
        st_id = st["id"]
        rm_section = req_map_subtasks.get(st_id, {})
        st_create = rm_section.get("create", [])
        st_modify = rm_section.get("modify", [])
        st_evidence = rm_section.get("evidenceFiles", [])

        ordered_subtasks.append({
            "subtaskId": st_id,
            "title": st.get("title", ""),
            "summary": st.get("summary", ""),
            "surfaceHint": st.get("surfaceHint", "unknown"),
            "linkedAcIds": st.get("linkedAcIds", []),
            "dependencies": st.get("dependencies", []),
            "complexity": st.get("complexity", "medium"),
            "order": st.get("order", 0),
            "create": st_create,
            "modify": st_modify,
            "evidenceFiles": st_evidence,
            "executionOrder": [m.get("path", "") for m in st_modify] + [c.get("path", "") for c in st_create],
        })

    return {
        "version": 1,
        "decompositionStrategy": subtasks.get("decompositionStrategy", "ac_grouping"),
        "subtaskCount": len(st_list),
        "subtasks": ordered_subtasks,
        "dependencyOrder": dep_order,
        "parallelGroups": parallel_groups,
    }


def _tag_actions_with_subtask(
    actions: list[dict[str, Any]],
    subtask_plan: dict[str, Any],
) -> list[dict[str, Any]]:
    path_to_subtask: dict[str, str] = {}
    for st in subtask_plan.get("subtasks", []):
        st_id = st["subtaskId"]
        for item in st.get("modify", []) + st.get("create", []):
            p = item.get("path", "")
            if p:
                path_to_subtask.setdefault(p, st_id)

    for action in actions:
        p = action.get("path", "")
        if p in path_to_subtask and "subtaskId" not in action:
            action["subtaskId"] = path_to_subtask[p]
    return actions


def _build_execution_order(
    req_map: dict[str, Any],
    subtask_plan: dict[str, Any] | None,
) -> list[str]:
    if not subtask_plan:
        return req_map.get("executionOrder", [])

    ordered: list[str] = []
    dep_order = subtask_plan.get("dependencyOrder", [])
    st_by_id = {st["subtaskId"]: st for st in subtask_plan.get("subtasks", [])}

    for st_id in dep_order:
        st = st_by_id.get(st_id)
        if not st:
            continue
        for path in st.get("executionOrder", []):
            if path and path not in ordered:
                ordered.append(path)

    flat = req_map.get("executionOrder", [])
    for path in flat:
        if path and path not in ordered:
            ordered.append(path)

    return ordered


def _plan_markdown(plan: dict[str, Any]) -> str:
    topo = plan.get("topology") or {}
    roots = topo.get("roots") or {}
    roots_lines = "\n".join(
        f"- `{name}`: type={info.get('type')}, nx={info.get('nx')}"
        for name, info in roots.items()
    ) or "- _(none)_"
    create = plan.get("create") or []
    modify = plan.get("modify") or []
    risks = plan.get("risks") or []
    blockers = plan.get("blockers") or []
    validation = plan.get("validationPlan") or {}
    tests = plan.get("tests") or {}

    def _files(items: list[dict[str, Any]]) -> str:
        if not items:
            return "_None_"
        return "\n".join(
            f"- `{f.get('path')}` — {f.get('purpose', '')} "
            f"(root=`{f.get('root', 'default')}`, `{f.get('templateId', f.get('patchId', 'patch'))}`)"
            for f in items
        )

    manual = "\n".join(f"- {c}" for c in (validation.get("manualChecks") or [])) or "_None_"
    static = "\n".join(f"- {c}" for c in (validation.get("staticChecks") or tests.get("staticChecks") or [])) or "_None_"
    auto = validation.get("automated") or tests.get("automated") or {}
    sot = plan.get("sourceOfTruth") or {}
    visual = plan.get("visualCompliance") or {}
    sot_lines = []
    if sot.get("primaryHtml") or visual.get("referenceHtml"):
        sot_lines.append(f"- HTML: `{sot.get('primaryHtml') or visual.get('referenceHtml')}`")
    if sot.get("primaryImage") or visual.get("referenceImage"):
        sot_lines.append(f"- Image/wireframe: `{sot.get('primaryImage') or visual.get('referenceImage')}`")
    for note in sot.get("designNotes") or visual.get("designNotes") or []:
        sot_lines.append(f"- Design notes: `{note}`")
    for a in sot.get("attachments") or visual.get("referenceArtifacts") or []:
        path = a.get("path") if isinstance(a, dict) else None
        role = a.get("role") if isinstance(a, dict) else None
        name = a.get("filename") if isinstance(a, dict) else None
        if path:
            sot_lines.append(f"- `{role or 'artifact'}`: `{path}` ({name or ''})")
    sot_block = "\n".join(dict.fromkeys(sot_lines)) or "_None — no HTML/wireframe/design-note SoT_"

    return f"""# Implementation Plan — {plan.get('issueKey') or ''}

{plan.get('summary') or ''}

## Topology
- Target app: `{topo.get('targetApp')}` · domain `{topo.get('targetDomain')}`
- Surface: `{topo.get('surface')}` · policy: `{topo.get('policy')}`
- Multi-root: {topo.get('monorepo')}
### Roots
{roots_lines}

## Source of truth (run inputs)
{sot_block}

## Files to create
{_files(create)}

## Files to modify
{_files(modify)}

## Execution order
{chr(10).join(f'{i+1}. `{p}`' for i, p in enumerate(plan.get('executionOrder') or [])) or '_None_'}

{_subtask_plan_markdown(plan.get('subtaskPlan'))}
## Risks
{chr(10).join(f'- {r}' for r in risks) or '_None_'}

## Blockers
{chr(10).join(f'- {b}' for b in blockers) or '_None_'}

## Validation plan
### Automated
- Static file checks: {auto.get('staticFileChecks')}
- npm test: {auto.get('npmTest')}
- Generated tests: {auto.get('generatedTests')}
- Exact-text compliance: {auto.get('complianceExactText')}

### Static checks
{static}

### Manual checks (human / visual)
{manual}

### Unit / generated test paths
{chr(10).join(f'- `{u}`' for u in (tests.get('unit') or [])) or '_None planned_'}
"""


def _subtask_plan_markdown(subtask_plan: dict[str, Any] | None) -> str:
    if not subtask_plan or not subtask_plan.get("subtasks"):
        return ""
    lines = [
        f"## Sub-task decomposition (strategy: `{subtask_plan.get('decompositionStrategy', 'unknown')}`)",
        f"- Sub-task count: {subtask_plan.get('subtaskCount', 0)}",
        f"- Dependency order: {' → '.join(subtask_plan.get('dependencyOrder', []))}",
        "",
    ]
    for st in subtask_plan.get("subtasks", []):
        lines.append(f"### {st.get('subtaskId', '?')}: {st.get('title', '')}")
        lines.append(f"- Surface: `{st.get('surfaceHint', 'unknown')}` · Complexity: `{st.get('complexity', '?')}`")
        lines.append(f"- Linked ACs: {', '.join(st.get('linkedAcIds', []))}")
        deps = st.get("dependencies", [])
        if deps:
            lines.append(f"- Depends on: {', '.join(deps)}")
        for m in st.get("modify", []):
            lines.append(f"  - modify: `{m.get('path', '')}`")
        for c in st.get("create", []):
            lines.append(f"  - create: `{c.get('path', '')}`")
        lines.append("")
    return "\n".join(lines)
