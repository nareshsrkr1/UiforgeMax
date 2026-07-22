"""Build requirement-map.json from real Graphify query results — no Nx boilerplate."""

from __future__ import annotations

from typing import Any

from uiforgemax.pipeline.target_sanitize import filter_impl_paths, sanitize_requirement_map


def build_requirement_map(
    requirements: dict[str, Any],
    query_results: dict[str, Any],
    visual_spec: dict[str, Any] | None,
    *,
    classification: dict[str, Any] | None = None,
    subtasks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    classification = classification or {}
    surface = classification.get("surface") or requirements.get("surface") or "unknown"
    policy = requirements.get("policy", {})

    results = query_results.get("results") or []
    raw_evidence = _collect_files(results)
    evidence_files = filter_impl_paths(raw_evidence)
    weak_graph = not evidence_files and all(
        r.get("status") in ("empty", "missing_graph", "error") for r in results
    )

    # Never invent API work for ui_only — and ignore stale dataNeeds on UI tickets.
    api_gaps: list[dict[str, Any]] = []
    api_exists: list[dict[str, Any]] = []
    data_needs = [] if surface == "ui_only" else (requirements.get("dataNeeds") or [])
    if surface != "ui_only":
        for need in data_needs:
            entity = need.get("entity", "Resource")
            related = [f for f in evidence_files if entity.lower().rstrip("s") in f.lower()]
            if related:
                api_exists.append({"entity": entity, "files": related, "status": "exists"})
            else:
                api_gaps.append({"entity": entity, "operations": need.get("operations", []), "status": "missing"})

    create: list[dict[str, Any]] = []
    modify: list[dict[str, Any]] = []
    reuse = [{"kind": "file", "path": f} for f in evidence_files]

    # Prefer modify of evidenced *implementation* files; never package.json/project.json.
    for path in evidence_files:
        modify.append(
            {
                "path": path,
                "purpose": "Touched by graph evidence for this requirement",
                "source": "graphify-query",
            }
        )

    # Build AC→subtask lookup for subtask-aware matching
    _ac_to_subtask: dict[str, str] = {}
    if subtasks:
        for st in subtasks.get("subtasks", []):
            for ac_id in st.get("linkedAcIds") or []:
                _ac_to_subtask[ac_id] = st["id"]

    ac_mappings = []
    for i, ac in enumerate(requirements.get("acceptanceCriteria") or []):
        ac_id = ac.get("id", f"AC-{i + 1}") if isinstance(ac, dict) else f"AC-{i + 1}"
        text = (ac.get("text") if isinstance(ac, dict) else str(ac)) or ""
        st_id = _ac_to_subtask.get(ac_id)
        related_q = next(
            (
                r
                for r in results
                if (r.get("params") or {}).get("subtaskId") == st_id and st_id
                or (r.get("params") or {}).get("acId") == ac_id
                or (text and text[:40].lower() in (r.get("question") or "").lower())
            ),
            None,
        )
        files = []
        if related_q:
            files = filter_impl_paths(
                sorted(
                    {
                        n.get("source_file")
                        for n in (related_q.get("nodes") or [])
                        if n.get("source_file")
                    }
                )
            )
        if files:
            strategy = f"Modify evidenced files: {', '.join(files)}"
        elif weak_graph:
            strategy = (
                "No graph evidence for this AC — identify concrete style/code files "
                "during understanding/plan (do not invent demo pages/APIs)."
            )
        else:
            strategy = "Use nearest graph evidence from related queries; refine in plan mediation."

        ac_mappings.append(
            {
                "acId": ac_id,
                "requirement": text,
                "strategy": strategy,
                "graphEvidence": files,
                "approved": False,
            }
        )

    clarifications: list[dict[str, Any]] = []
    if weak_graph:
        clarifications.append(
            {
                "id": "CL-WEAK-GRAPH",
                "question": (
                    "Graphify returned little or no evidence for this ticket. "
                    "Confirm target files (e.g. styles.css, index.html, theme tokens) in plan mediation."
                ),
            }
        )

    dropped_noise = sorted(set(raw_evidence) - set(evidence_files))
    assumptions = list(requirements.get("assumptions") or [])
    if weak_graph:
        assumptions.append(
            "Graph evidence was weak; paths will be refined in plan — no demo boilerplate applied."
        )
    if dropped_noise:
        assumptions.append(
            "Auto-excluded non-implementation graph hits: " + ", ".join(dropped_noise)
        )
    if surface == "ui_only":
        assumptions.append("ui_only surface — dataNeeds/API gaps cleared automatically.")

    req_map = {
        "issueKey": requirements.get("issueKey"),
        "summary": requirements.get("summary"),
        "surface": surface,
        "targetApp": policy.get("targetApp"),
        "targetDomain": policy.get("targetDomain"),
        "reuse": reuse,
        "apiExists": api_exists,
        "apiGaps": api_gaps if surface != "ui_only" else [],
        "clients": [],
        "acceptanceMappings": ac_mappings,
        "create": create,
        "modify": modify,
        "executionOrder": [m["path"] for m in modify] + [c["path"] for c in create],
        "clarifications": clarifications,
        "scope": requirements.get("scope", {}),
        "assumptions": assumptions,
        "overrides": [],
        "source": "graphify-cli",
        "weakGraph": weak_graph,
        "stats": {
            "reuseCount": len(reuse),
            "createCount": len(create),
            "modifyCount": len(modify),
            "acCount": len(requirements.get("acceptanceCriteria") or []),
            "acMapped": len(ac_mappings),
            "clarificationCount": len(clarifications),
            "evidenceFileCount": len(evidence_files),
            "rawEvidenceFileCount": len(raw_evidence),
        },
    }
    if subtasks and subtasks.get("subtaskCount", 0) > 1:
        req_map["subtasks"] = _group_by_subtask(
            subtasks, results, ac_mappings, evidence_files,
        )

    return sanitize_requirement_map(req_map)


def _group_by_subtask(
    subtasks: dict[str, Any],
    results: list[dict[str, Any]],
    ac_mappings: list[dict[str, Any]],
    all_evidence_files: list[str],
) -> dict[str, dict[str, Any]]:
    ac_by_id = {m["acId"]: m for m in ac_mappings}
    grouped: dict[str, dict[str, Any]] = {}

    for st in subtasks.get("subtasks", []):
        st_id = st["id"]
        linked_ac_ids = set(st.get("linkedAcIds") or [])

        st_results = [
            r for r in results
            if (r.get("params") or {}).get("subtaskId") == st_id
        ]
        st_files = filter_impl_paths(sorted(
            {n.get("source_file") for r in st_results for n in (r.get("nodes") or []) if n.get("source_file")}
        ))

        st_acs = [ac_by_id[ac_id] for ac_id in linked_ac_ids if ac_id in ac_by_id]

        grouped[st_id] = {
            "subtaskId": st_id,
            "title": st.get("title", ""),
            "linkedAcIds": sorted(linked_ac_ids),
            "acceptanceMappings": st_acs,
            "modify": [
                {"path": f, "purpose": "graph evidence", "source": "graphify-query"}
                for f in st_files
            ],
            "create": [],
            "reuse": [{"kind": "file", "path": f} for f in st_files],
            "evidenceFiles": st_files,
        }

    return grouped


def _collect_files(results: list[dict[str, Any]]) -> list[str]:
    files: set[str] = set()
    for r in results:
        for n in r.get("nodes") or []:
            if n.get("source_file"):
                files.add(n["source_file"])
        for br in r.get("byRoot") or []:
            for n in br.get("nodes") or []:
                if n.get("source_file"):
                    files.add(n["source_file"])
    return sorted(files)
