"""Mediation persistence, merge, and apply."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from uiforgemax.model_mediation.registry import (
    MediationKind,
    wire_model_mediation,
    build_mediation_request,
    pending_mediations,
    skip_mediation,
)
from uiforgemax.state import RunState, Stage, Status


def mediation_dir(run_dir: Path) -> Path:
    d = run_dir / "mediation"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _response_path(run_dir: Path, mediation_key: str) -> Path:
    safe = mediation_key.replace("::", "_").replace("/", "_")
    return mediation_dir(run_dir) / f"{safe}.response.json"


def _request_path(run_dir: Path, mediation_key: str) -> Path:
    safe = mediation_key.replace("::", "_").replace("/", "_")
    return mediation_dir(run_dir) / f"{safe}.request.json"


def is_mediation_skipped() -> bool:
    return skip_mediation()


def is_mediation_complete(run_dir: Path, mediation_key: str) -> bool:
    if is_mediation_skipped():
        return True
    return _response_path(run_dir, mediation_key).exists()


def next_pending_mediation(
    run_dir: Path, state: RunState, stage: Stage
) -> tuple[Stage, MediationKind] | None:
    if is_mediation_skipped():
        return None
    for st, kind in pending_mediations(stage, run_dir, state):
        key = build_mediation_request(st, kind, run_dir, state)["mediationKey"]
        if not is_mediation_complete(run_dir, key):
            return st, kind
    return None


def save_mediation_request(run_dir: Path, request: dict[str, Any]) -> Path:
    path = _request_path(run_dir, request["mediationKey"])
    path.write_text(json.dumps(request, indent=2), encoding="utf-8")
    return path


def save_mediation_response(run_dir: Path, mediation_key: str, payload: dict[str, Any]) -> Path:
    path = _response_path(run_dir, mediation_key)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_mediation_response(run_dir: Path, mediation_key: str) -> dict[str, Any] | None:
    path = _response_path(run_dir, mediation_key)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def apply_pending_mediations(run_dir: Path, stage: Stage, state: RunState) -> None:
    for st, kind in pending_mediations(stage, run_dir, state):
        key = build_mediation_request(st, kind, run_dir, state)["mediationKey"]
        payload = load_mediation_response(run_dir, key)
        if payload:
            apply_mediation(run_dir, kind, payload)


def clear_mediation_responses(run_dir: Path) -> None:
    """Drop IDE responses (e.g. after delta revise)."""
    d = mediation_dir(run_dir)
    for path in d.glob("*.response.json"):
        path.unlink(missing_ok=True)


def load_mediation_request(run_dir: Path, mediation_key: str) -> dict[str, Any] | None:
    path = _request_path(run_dir, mediation_key)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def mediation_pause_result(
    run_dir: Path, state: RunState, stage: Stage
) -> tuple[bool, dict[str, Any] | None]:
    """If IDE mediation is pending, mutate state and return (True, request)."""
    if is_mediation_skipped():
        return False, None
    nxt = next_pending_mediation(run_dir, state, stage)
    if not nxt:
        return False, None
    st, kind = nxt
    request = build_mediation_request(st, kind, run_dir, state)
    save_mediation_request(run_dir, request)
    state.status = Status.AWAITING_MEDIATION
    state.artifacts["pendingMediation"] = request["mediationKey"]
    state.mediation["pending"] = request
    state.record(stage, "awaiting_mediation", kind.value)
    # Return a wire-sized copy for the agent response; full request is on disk.
    return True, wire_model_mediation(request, run_dir)


def apply_mediation(run_dir: Path, kind: MediationKind, payload: dict[str, Any]) -> None:
    if kind == MediationKind.REQUEST_CLASSIFICATION:
        _merge_classification(run_dir, payload)
    elif kind == MediationKind.INTAKE_RECONCILIATION:
        _merge_intake_reconciliation(run_dir, payload)
    elif kind == MediationKind.REQUIREMENT_ANALYSIS:
        _merge_requirements(run_dir, payload)
    elif kind == MediationKind.VISUAL_INTERPRETATION:
        _merge_visual(run_dir, payload)
    elif kind == MediationKind.GRAPH_EXPLAIN:
        _merge_graph_explain(run_dir, payload)
    elif kind == MediationKind.QUERY_STRATEGY:
        _merge_query_strategy(run_dir, payload)
    elif kind == MediationKind.REQ_MAP_VALIDATION:
        _merge_req_map_validation(run_dir, payload)
    elif kind == MediationKind.PLAN_REFINEMENT:
        _merge_plan(run_dir, payload)
    elif kind == MediationKind.TEST_GENERATION:
        _merge_tests(run_dir, payload)
    elif kind == MediationKind.TEST_ENV_RECOVERY:
        _merge_test_env_recovery(run_dir, payload)
    elif kind == MediationKind.TASK_DECOMPOSITION:
        _merge_task_decomposition(run_dir, payload)
    elif kind == MediationKind.VISUAL_VALIDATION:
        _merge_visual_validation(run_dir, payload)
    elif kind == MediationKind.POST_IMPLEMENT_REVIEW:
        _merge_post_implement_review(run_dir, payload)


def _merge_test_env_recovery(run_dir: Path, payload: dict[str, Any]) -> None:
    from uiforgemax.pipeline.testing import apply_test_env_recovery

    # Resolve project roots from run.json when available
    roots: dict = {}
    run_json = run_dir / "run.json"
    if run_json.exists():
        try:
            state = json.loads(run_json.read_text(encoding="utf-8"))
            if state.get("project_root"):
                roots["default"] = Path(state["project_root"])
            for name, path in (state.get("project_roots") or {}).items():
                roots[name] = Path(path)
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    if not roots:
        roots = {"default": run_dir}  # unlikely; installs may no-op
    apply_test_env_recovery(roots, run_dir, payload)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _merge_classification(run_dir: Path, payload: dict[str, Any]) -> None:
    """Persist the IDE-decided request classification, preserving signals."""
    path = run_dir / "request-classification.json"
    current = _load_json(path) if path.exists() else {}
    signals = current.get("signals")
    merged = {**current, **payload}
    if signals is not None:
        merged["signals"] = signals
    merged["mediatedByIde"] = True
    merged.pop("source", None)
    _write_json(path, merged)


def _merge_requirements(run_dir: Path, payload: dict[str, Any]) -> None:
    path = run_dir / "requirements.normalized.json"
    if not path.exists():
        return
    req = _load_json(path)
    if payload.get("summary"):
        req["summary"] = payload["summary"]
    if payload.get("scope"):
        req["scope"] = payload["scope"]
    if payload.get("acceptanceCriteria"):
        req["acceptanceCriteria"] = payload["acceptanceCriteria"]
    if "dataNeeds" in payload:
        req["dataNeeds"] = payload.get("dataNeeds") or []
    if payload.get("graphSearchStrategy"):
        strategy = payload["graphSearchStrategy"]
        shorts = []
        for sq in strategy.get("searchQueries") or []:
            s = " ".join(str(sq).split())[:72]
            if s:
                shorts.append(s)
        req["graphSearchStrategy"] = {
            "intent": strategy.get("intent"),
            "filePatterns": list(strategy.get("filePatterns") or [])[:10],
            "componentNames": list(strategy.get("componentNames") or [])[:15],
            "perAcSearch": list(strategy.get("perAcSearch") or [])[:20],
            "architecturalPatterns": list(strategy.get("architecturalPatterns") or [])[:10],
            "keywords": list(strategy.get("keywords") or [])[:12],
            "focusFiles": list(strategy.get("focusFiles") or [])[:8],
            "searchQueries": shorts[:3],
        }
    elif payload.get("graphSearchHints"):
        hints = payload["graphSearchHints"]
        shorts = []
        for sq in hints.get("shortQuestions") or []:
            s = " ".join(str(sq).split())[:72]
            if s:
                shorts.append(s)
        req["graphSearchHints"] = {
            "intent": hints.get("intent"),
            "keywords": list(hints.get("keywords") or [])[:12],
            "focusFiles": list(hints.get("focusFiles") or [])[:8],
            "shortQuestions": shorts[:2],
        }
    req.setdefault("assumptions", [])
    req["assumptions"].extend(payload.get("assumptions", []))
    req["conflicts"] = payload.get("conflicts", req.get("conflicts", []))
    req["overridesResolved"] = payload.get("overridesResolved", [])
    # ui_only classification → never keep stale API/entity dataNeeds.
    cls_path = run_dir / "request-classification.json"
    if cls_path.exists():
        cls = _load_json(cls_path)
        if cls.get("surface") == "ui_only":
            req["dataNeeds"] = []
    req["mediatedByIde"] = True
    _write_json(path, req)
    # Folded VISUAL_INTERPRETATION: visual fields on the same UNDERSTAND payload.
    visual_keys = (
        "layout",
        "components",
        "visualTokens",
        "exactTextRequirements",
        "matchExactly",
        "beforeAfterDiff",
        "sourceImage",
        "visualSpecUnconfirmed",
        "confidence",
        "interactions",
    )
    if any(k in payload for k in visual_keys):
        _merge_visual(run_dir, {k: payload[k] for k in visual_keys if k in payload})


def _merge_visual(run_dir: Path, payload: dict[str, Any]) -> None:
    path = run_dir / "visual-spec.json"
    visual = _load_json(path) if path.exists() else {}
    visual.update({k: v for k, v in payload.items() if v is not None})
    visual["mediatedByIde"] = True
    _write_json(path, visual)

    req_path = run_dir / "requirements.normalized.json"
    if req_path.exists():
        req = _load_json(req_path)
        comp = req.setdefault("compliance", {})
        # HTML SoT harvest sets htmlExactCopy — never let mediation turn exactness off
        # or replace the list and drop mechanically harvested labels.
        html_locked = bool(comp.get("htmlExactCopy"))
        if payload.get("matchExactly") is not None and not html_locked:
            comp["matchExactly"] = payload["matchExactly"]
        elif html_locked:
            comp["matchExactly"] = True
        if payload.get("exactTextRequirements"):
            incoming = [str(x) for x in payload["exactTextRequirements"] if x]
            if html_locked:
                existing = [str(x) for x in (comp.get("exactTextRequirements") or []) if x]
                seen = {x.lower() for x in existing}
                for text in incoming:
                    if text.lower() not in seen:
                        seen.add(text.lower())
                        existing.append(text)
                comp["exactTextRequirements"] = existing[:40]
            else:
                comp["exactTextRequirements"] = incoming[:40]
        if payload.get("sourceImage"):
            comp["referenceAttachment"] = payload["sourceImage"]
        _write_json(req_path, req)


def _merge_graph_explain(run_dir: Path, payload: dict[str, Any]) -> None:
    from uiforgemax.pipeline.target_sanitize import (
        is_implementation_path,
        sanitize_requirement_map,
    )

    _write_json(run_dir / "graph" / "mediation-explain.json", payload)
    map_path = run_dir / "graph" / "requirement-map.json"
    if not map_path.exists():
        return
    req_map = _load_json(map_path)
    adj = payload.get("requirementMapAdjustments", {}) or {}
    req_map.setdefault("assumptions", []).extend(adj.get("assumptions", []))
    if payload.get("acceptanceMappingsReview"):
        req_map["acceptanceMappingsReview"] = payload["acceptanceMappingsReview"]
        # Apply approved strategies onto acceptanceMappings.
        by_ac = {m.get("acId"): m for m in (req_map.get("acceptanceMappings") or [])}
        for rev in payload["acceptanceMappingsReview"]:
            ac = by_ac.get(rev.get("acId"))
            if ac and rev.get("strategy"):
                ac["strategy"] = rev["strategy"]
                if rev.get("approved") is not None:
                    ac["approved"] = bool(rev["approved"])

    # Structural adjustments — actually rewrite edit targets (not notes-only).
    if adj.get("modify") is not None:
        req_map["modify"] = [
            f for f in adj["modify"] if is_implementation_path((f or {}).get("path"))
        ]
    if adj.get("create") is not None:
        req_map["create"] = [
            f for f in adj["create"] if is_implementation_path((f or {}).get("path"))
        ]
    if adj.get("dropPaths"):
        drop = {str(p).replace("\\", "/") for p in adj["dropPaths"]}
        req_map["modify"] = [
            f for f in (req_map.get("modify") or []) if f.get("path", "").replace("\\", "/") not in drop
        ]
        req_map["create"] = [
            f for f in (req_map.get("create") or []) if f.get("path", "").replace("\\", "/") not in drop
        ]
        req_map["reuse"] = [
            r for r in (req_map.get("reuse") or []) if r.get("path", "").replace("\\", "/") not in drop
        ]
    if adj.get("executionOrder") is not None:
        req_map["executionOrder"] = [
            p for p in adj["executionOrder"] if is_implementation_path(p)
        ]
    else:
        req_map["executionOrder"] = [
            f["path"] for f in (req_map.get("modify") or []) + (req_map.get("create") or [])
        ]

    req_map = sanitize_requirement_map(req_map)
    req_map["mediatedByIde"] = True
    _write_json(map_path, req_map)

    # Folded REQ_MAP_VALIDATION / optional query strategy on the same LOCATE payload.
    if payload.get("coverageAdjustments") or payload.get("coverageOk") is not None:
        _merge_req_map_validation(
            run_dir,
            {
                "adjustments": payload.get("coverageAdjustments") or {},
                "coverageOk": payload.get("coverageOk"),
            },
        )
    if payload.get("queries") or payload.get("lexicalKeywords"):
        _merge_query_strategy(
            run_dir,
            {
                k: payload[k]
                for k in (
                    "queries",
                    "lexicalKeywords",
                    "focusFiles",
                    "strategy",
                    "refinedFromRequirements",
                    "adjustments",
                )
                if k in payload
            },
        )

    # Refresh understanding so Gate 2 reflects corrected targets automatically.
    try:
        from uiforgemax.pipeline.planning import generate_understanding

        reqs = _load_json(run_dir / "requirements.normalized.json")
        api = (
            _load_json(run_dir / "api-resolution.json")
            if (run_dir / "api-resolution.json").exists()
            else {"resolution": [], "gate1Required": False}
        )
        # Re-load map after coverage adjustments.
        req_map = _load_json(map_path)
        generate_understanding(run_dir, reqs, req_map, api)
    except Exception:  # noqa: BLE001
        pass


def _merge_plan(run_dir: Path, payload: dict[str, Any]) -> None:
    plan_path = run_dir / "plans" / "implementation-plan.json"
    plan = _load_json(plan_path) if plan_path.exists() else {}
    for key in (
        "summary",
        "topology",
        "create",
        "modify",
        "executionOrder",
        "tests",
        "visualCompliance",
        "sourceOfTruth",
        "risks",
        "blockers",
        "validationPlan",
        "acceptanceMappings",
    ):
        if payload.get(key) is not None:
            plan[key] = payload[key]
    # Always keep the on-disk SoT catalog authoritative when present.
    try:
        from uiforgemax.pipeline.source_of_truth import collect_source_of_truth

        sot = collect_source_of_truth(run_dir)
        if sot.get("attachments") or sot.get("primaryHtml") or sot.get("primaryImage"):
            plan["sourceOfTruth"] = sot
            vc = dict(plan.get("visualCompliance") or {})
            vc.setdefault("referenceHtml", sot.get("primaryHtml"))
            vc.setdefault("referenceImage", sot.get("primaryImage"))
            vc.setdefault("designNotes", sot.get("designNotes") or [])
            vc.setdefault(
                "referenceArtifacts",
                [
                    {
                        "filename": a.get("filename"),
                        "role": a.get("role"),
                        "path": a.get("path"),
                    }
                    for a in (sot.get("attachments") or [])
                    if a.get("path")
                ],
            )
            plan["visualCompliance"] = vc
    except Exception:  # noqa: BLE001
        pass
    plan["mediatedByIde"] = True
    plan["source"] = "requirement-map.json+ide_mediation"
    # Refresh human-facing Gate 3 package + plan-review after IDE refinement.
    # Mediation can empty create/modify; never leave a stale verdict=pass behind.
    try:
        from uiforgemax.pipeline.planning import (
            _plan_markdown,
            build_plan_approval_package,
            build_plan_review,
        )
        from uiforgemax.pipeline.target_sanitize import sanitize_plan_targets

        plan = sanitize_plan_targets(plan)
        reqs = (
            _load_json(run_dir / "requirements.normalized.json")
            if (run_dir / "requirements.normalized.json").exists()
            else {}
        )
        req_map = (
            _load_json(run_dir / "graph" / "requirement-map.json")
            if (run_dir / "graph" / "requirement-map.json").exists()
            else {}
        )
        api = (
            _load_json(run_dir / "api-resolution.json")
            if (run_dir / "api-resolution.json").exists()
            else {}
        )
        review = build_plan_review(
            plan, requirements=reqs, req_map=req_map, api_resolution=api
        )
        plan["risks"] = review["risks"]
        plan["blockers"] = review["blockers"]
        _write_json(plan_path, plan)
        _write_json(run_dir / "plans" / "plan-review.json", review)
        (run_dir / "plans" / "implementation-plan.md").write_text(
            _plan_markdown(plan), encoding="utf-8"
        )
        approval = build_plan_approval_package(run_dir, plan)
        _write_json(run_dir / "plans" / "plan-approval.json", approval)
    except Exception:  # noqa: BLE001
        _write_json(plan_path, plan)


def _merge_tests(run_dir: Path, payload: dict[str, Any]) -> None:
    _write_json(run_dir / "tests" / "generated-tests.json", payload)


def _merge_task_decomposition(run_dir: Path, payload: dict[str, Any]) -> None:
    from uiforgemax.pipeline.decompose import build_subtasks_from_mediation

    reqs_path = run_dir / "requirements.normalized.json"
    reqs = _load_json(reqs_path) if reqs_path.exists() else {}
    cls_path = run_dir / "request-classification.json"
    cls = _load_json(cls_path) if cls_path.exists() else {}
    vis_path = run_dir / "visual-spec.json"
    vis = _load_json(vis_path) if vis_path.exists() else None

    subtasks = build_subtasks_from_mediation(reqs, cls, vis, payload)
    _write_json(run_dir / "plans" / "subtasks.json", subtasks)


def _merge_visual_validation(run_dir: Path, payload: dict[str, Any]) -> None:
    _write_json(run_dir / "implementation" / "visual-validation.json", payload)


def _merge_query_strategy(run_dir: Path, payload: dict[str, Any]) -> None:
    """Persist IDE-decided query strategy for the query planner to consume."""
    graph_dir = run_dir / "graph"
    graph_dir.mkdir(parents=True, exist_ok=True)
    _write_json(graph_dir / "query-strategy.json", payload)


def _merge_intake_reconciliation(run_dir: Path, payload: dict[str, Any]) -> None:
    """Persist reconciled intake and fold overrides into normalized requirements."""
    _write_json(run_dir / "intake-reconciliation.json", payload)
    req_path = run_dir / "requirements.normalized.json"
    if not req_path.exists():
        return
    req = _load_json(req_path)
    if payload.get("reconciledSummary"):
        req["reconciledSummary"] = payload["reconciledSummary"]
    if payload.get("overrides"):
        req["intakeOverrides"] = payload["overrides"]
    if payload.get("contradictions"):
        existing = req.get("conflicts") or []
        for c in payload["contradictions"]:
            if not c.get("resolved"):
                existing.append(f"UNRESOLVED: {c.get('field')} — {c.get('sources')}")
        req["conflicts"] = existing
    if payload.get("needsHumanClarification"):
        req.setdefault("clarifications", []).extend(
            {"question": item["question"], "context": item.get("context", "")}
            for item in payload["needsHumanClarification"]
        )
    req["intakeReconciled"] = True
    _write_json(req_path, req)


def _merge_req_map_validation(run_dir: Path, payload: dict[str, Any]) -> None:
    """Apply requirement-map coverage validation adjustments."""
    from uiforgemax.pipeline.target_sanitize import is_implementation_path

    _write_json(run_dir / "graph" / "req-map-validation.json", payload)
    map_path = run_dir / "graph" / "requirement-map.json"
    if not map_path.exists():
        return
    req_map = _load_json(map_path)
    adj = payload.get("adjustments") or {}
    if adj.get("addToModify"):
        existing = req_map.get("modify") or []
        existing_paths = {f.get("path") for f in existing}
        for entry in adj["addToModify"]:
            if entry.get("path") and entry["path"] not in existing_paths and is_implementation_path(entry["path"]):
                existing.append(entry)
        req_map["modify"] = existing
    if adj.get("addToCreate"):
        existing = req_map.get("create") or []
        existing_paths = {f.get("path") for f in existing}
        for entry in adj["addToCreate"]:
            if entry.get("path") and entry["path"] not in existing_paths and is_implementation_path(entry["path"]):
                existing.append(entry)
        req_map["create"] = existing
    if adj.get("dropPaths"):
        drop = {str(p).replace("\\", "/") for p in adj["dropPaths"]}
        req_map["modify"] = [f for f in (req_map.get("modify") or []) if f.get("path", "").replace("\\", "/") not in drop]
        req_map["create"] = [f for f in (req_map.get("create") or []) if f.get("path", "").replace("\\", "/") not in drop]
    if adj.get("reorderExecution"):
        req_map["executionOrder"] = [p for p in adj["reorderExecution"] if is_implementation_path(p)]
    req_map["coverageValidated"] = True
    req_map["coverageScore"] = payload.get("coverageScore")
    _write_json(map_path, req_map)


def _merge_post_implement_review(run_dir: Path, payload: dict[str, Any]) -> None:
    """Persist post-implementation code review results."""
    _write_json(run_dir / "implementation" / "post-implement-review.json", payload)
