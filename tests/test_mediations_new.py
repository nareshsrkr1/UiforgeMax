"""Tests for new mediations: INTAKE_RECONCILIATION, QUERY_STRATEGY, REQ_MAP_VALIDATION, POST_IMPLEMENT_REVIEW."""

from __future__ import annotations

import json
from pathlib import Path

from uiforgemax.model_mediation.registry import (
    MediationKind,
    MEDIATION_BY_STAGE,
    build_mediation_request,
    pending_mediations,
    _has_jira_with_comments,
)
from uiforgemax.model_mediation.service import (
    apply_mediation,
    save_mediation_response,
    is_mediation_complete,
)
from uiforgemax.state import RunState, Stage, Status


def _run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs" / "test-run"
    for sub in ("inputs", "graph", "plans", "mediation", "implementation"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def _state() -> RunState:
    return RunState(run_id="test-run", project_root="C:/tmp", status=Status.NORMALIZED)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# --- MediationKind enum ---

def test_new_kinds_exist():
    assert MediationKind.INTAKE_RECONCILIATION == "INTAKE_RECONCILIATION"
    assert MediationKind.QUERY_STRATEGY == "QUERY_STRATEGY"
    assert MediationKind.REQ_MAP_VALIDATION == "REQ_MAP_VALIDATION"
    assert MediationKind.POST_IMPLEMENT_REVIEW == "POST_IMPLEMENT_REVIEW"


def test_query_strategy_in_mediation_by_stage():
    assert Stage.GRAPH_QUERY_PLAN in MEDIATION_BY_STAGE
    assert MEDIATION_BY_STAGE[Stage.GRAPH_QUERY_PLAN] == MediationKind.QUERY_STRATEGY


# --- INTAKE_RECONCILIATION ---

def test_has_jira_with_comments_true(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write(run_dir / "inputs" / "jira.raw.json", {
        "key": "PROJ-1",
        "fields": {"summary": "test"},
        "comments": [{"body": "override: use blue theme", "author": "user1"}],
    })
    assert _has_jira_with_comments(run_dir) is True


def test_has_jira_with_comments_false_no_comments(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write(run_dir / "inputs" / "jira.raw.json", {
        "key": "PROJ-1",
        "fields": {"summary": "test"},
    })
    assert _has_jira_with_comments(run_dir) is False


def test_has_jira_with_comments_false_no_file(tmp_path):
    run_dir = _run_dir(tmp_path)
    assert _has_jira_with_comments(run_dir) is False


def test_intake_reconciliation_pending_when_jira_comments(tmp_path):
    run_dir = _run_dir(tmp_path)
    state = _state()
    _write(run_dir / "inputs" / "jira.raw.json", {
        "comments": [{"body": "change color to red"}],
    })
    _write(run_dir / "requirements.normalized.json", {"summary": "test", "acceptanceCriteria": []})
    _write(run_dir / "request-classification.json", {"surface": "ui_only"})
    _write(run_dir / "run-flow.json", {"flags": {}, "activeStages": []})

    pending = pending_mediations(Stage.NORMALIZE, run_dir, state)
    kinds = [k for _, k in pending]
    assert MediationKind.INTAKE_RECONCILIATION in kinds
    assert MediationKind.REQUIREMENT_ANALYSIS in kinds
    idx_recon = kinds.index(MediationKind.INTAKE_RECONCILIATION)
    idx_req = kinds.index(MediationKind.REQUIREMENT_ANALYSIS)
    assert idx_recon < idx_req


def test_intake_reconciliation_not_pending_without_comments(tmp_path):
    run_dir = _run_dir(tmp_path)
    state = _state()
    _write(run_dir / "requirements.normalized.json", {"summary": "test", "acceptanceCriteria": []})
    _write(run_dir / "request-classification.json", {"surface": "ui_only"})
    _write(run_dir / "run-flow.json", {"flags": {}, "activeStages": []})

    pending = pending_mediations(Stage.NORMALIZE, run_dir, state)
    kinds = [k for _, k in pending]
    assert MediationKind.INTAKE_RECONCILIATION not in kinds
    assert MediationKind.REQUIREMENT_ANALYSIS in kinds


def test_merge_intake_reconciliation(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write(run_dir / "requirements.normalized.json", {
        "summary": "original summary",
        "acceptanceCriteria": [],
    })
    payload = {
        "reconciledSummary": "Updated: use blue theme instead of dark",
        "overrides": [{"source": "comment", "author": "pm", "what": "theme color", "from": "dark", "to": "blue"}],
        "contradictions": [{"field": "color", "sources": ["desc", "comment-3"], "resolved": False}],
        "needsHumanClarification": [{"question": "Blue or navy?", "context": "Two comments disagree"}],
        "confidence": 0.8,
    }
    apply_mediation(run_dir, MediationKind.INTAKE_RECONCILIATION, payload)

    recon = json.loads((run_dir / "intake-reconciliation.json").read_text(encoding="utf-8"))
    assert recon["reconciledSummary"] == "Updated: use blue theme instead of dark"

    req = json.loads((run_dir / "requirements.normalized.json").read_text(encoding="utf-8"))
    assert req["intakeReconciled"] is True
    assert req["reconciledSummary"] == "Updated: use blue theme instead of dark"
    assert len(req["intakeOverrides"]) == 1
    assert any("UNRESOLVED" in c for c in req.get("conflicts", []))
    assert len(req.get("clarifications", [])) == 1


# --- QUERY_STRATEGY ---

def test_query_strategy_pending_at_graph_query_plan(tmp_path):
    run_dir = _run_dir(tmp_path)
    state = _state()
    _write(run_dir / "requirements.normalized.json", {"summary": "test", "acceptanceCriteria": []})
    _write(run_dir / "request-classification.json", {"surface": "ui_only"})
    _write(run_dir / "run-flow.json", {"flags": {}, "activeStages": []})

    pending = pending_mediations(Stage.GRAPH_QUERY_PLAN, run_dir, state)
    kinds = [k for _, k in pending]
    assert MediationKind.QUERY_STRATEGY in kinds


def test_build_query_strategy_request(tmp_path):
    run_dir = _run_dir(tmp_path)
    state = _state()
    req = build_mediation_request(Stage.GRAPH_QUERY_PLAN, MediationKind.QUERY_STRATEGY, run_dir, state)
    assert req["kind"] == "QUERY_STRATEGY"
    assert "graph/index.json" in req["readArtifacts"]
    assert "plans/subtasks.json" in req["readArtifacts"]
    assert "queries" in req["outputSchema"]
    assert "lexicalKeywords" in req["outputSchema"]


def test_merge_query_strategy(tmp_path):
    run_dir = _run_dir(tmp_path)
    payload = {
        "queries": [
            {"id": "Q-AUTH", "question": "auth middleware handler", "reason": "Find auth module"},
        ],
        "lexicalKeywords": ["auth", "middleware", "jwt"],
        "focusFiles": ["src/auth/handler.py"],
        "strategy": "hybrid",
    }
    apply_mediation(run_dir, MediationKind.QUERY_STRATEGY, payload)

    strategy = json.loads((run_dir / "graph" / "query-strategy.json").read_text(encoding="utf-8"))
    assert strategy["queries"][0]["id"] == "Q-AUTH"
    assert "auth" in strategy["lexicalKeywords"]


# --- REQ_MAP_VALIDATION ---

def test_req_map_validation_pending_at_requirement_map(tmp_path):
    run_dir = _run_dir(tmp_path)
    state = _state()
    _write(run_dir / "requirements.normalized.json", {"summary": "test", "acceptanceCriteria": []})
    _write(run_dir / "request-classification.json", {"surface": "ui_only"})
    _write(run_dir / "run-flow.json", {"flags": {"useGraph": True}, "activeStages": []})

    pending = pending_mediations(Stage.REQUIREMENT_MAP, run_dir, state)
    kinds = [k for _, k in pending]
    assert MediationKind.GRAPH_EXPLAIN in kinds
    assert MediationKind.REQ_MAP_VALIDATION in kinds
    idx_explain = kinds.index(MediationKind.GRAPH_EXPLAIN)
    idx_valid = kinds.index(MediationKind.REQ_MAP_VALIDATION)
    assert idx_explain < idx_valid


def test_merge_req_map_validation_adds_files(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write(run_dir / "graph" / "requirement-map.json", {
        "modify": [{"path": "src/App.tsx", "purpose": "main"}],
        "create": [],
    })
    payload = {
        "coverageScore": 0.8,
        "adjustments": {
            "addToModify": [{"path": "src/utils/helpers.ts", "purpose": "helper functions"}],
            "addToCreate": [{"path": "src/components/NewWidget.tsx", "purpose": "new widget"}],
        },
        "approved": True,
    }
    apply_mediation(run_dir, MediationKind.REQ_MAP_VALIDATION, payload)

    req_map = json.loads((run_dir / "graph" / "requirement-map.json").read_text(encoding="utf-8"))
    assert req_map["coverageValidated"] is True
    assert req_map["coverageScore"] == 0.8
    modify_paths = [f["path"] for f in req_map["modify"]]
    assert "src/App.tsx" in modify_paths
    assert "src/utils/helpers.ts" in modify_paths
    create_paths = [f["path"] for f in req_map["create"]]
    assert "src/components/NewWidget.tsx" in create_paths


def test_merge_req_map_validation_drops_paths(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write(run_dir / "graph" / "requirement-map.json", {
        "modify": [
            {"path": "src/App.tsx", "purpose": "main"},
            {"path": "package.json", "purpose": "config"},
        ],
        "create": [],
    })
    payload = {
        "coverageScore": 0.9,
        "adjustments": {"dropPaths": ["package.json"]},
    }
    apply_mediation(run_dir, MediationKind.REQ_MAP_VALIDATION, payload)

    req_map = json.loads((run_dir / "graph" / "requirement-map.json").read_text(encoding="utf-8"))
    modify_paths = [f["path"] for f in req_map["modify"]]
    assert "package.json" not in modify_paths
    assert "src/App.tsx" in modify_paths


def test_merge_req_map_validation_no_duplicate_adds(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write(run_dir / "graph" / "requirement-map.json", {
        "modify": [{"path": "src/App.tsx", "purpose": "main"}],
        "create": [],
    })
    payload = {
        "coverageScore": 0.9,
        "adjustments": {
            "addToModify": [{"path": "src/App.tsx", "purpose": "duplicate"}],
        },
    }
    apply_mediation(run_dir, MediationKind.REQ_MAP_VALIDATION, payload)

    req_map = json.loads((run_dir / "graph" / "requirement-map.json").read_text(encoding="utf-8"))
    assert len(req_map["modify"]) == 1


# --- POST_IMPLEMENT_REVIEW ---

def test_post_implement_review_pending_at_implement(tmp_path):
    run_dir = _run_dir(tmp_path)
    state = _state()
    _write(run_dir / "requirements.normalized.json", {"summary": "test", "acceptanceCriteria": []})
    _write(run_dir / "request-classification.json", {"surface": "ui_only"})
    _write(run_dir / "run-flow.json", {"flags": {}, "activeStages": []})

    pending = pending_mediations(Stage.IMPLEMENT, run_dir, state)
    kinds = [k for _, k in pending]
    assert MediationKind.POST_IMPLEMENT_REVIEW in kinds


def test_build_post_implement_review_request(tmp_path):
    run_dir = _run_dir(tmp_path)
    state = _state()
    req = build_mediation_request(Stage.IMPLEMENT, MediationKind.POST_IMPLEMENT_REVIEW, run_dir, state)
    assert req["kind"] == "POST_IMPLEMENT_REVIEW"
    assert "plans/approved-plan.json" in req["readArtifacts"]
    assert "implementation/diff-summary.json" in req["readArtifacts"]
    assert "inputs/page.html" in req["readArtifacts"]
    assert "visual-spec.json" in req["readArtifacts"]
    assert "overallQuality" in req["outputSchema"]


def test_merge_post_implement_review(tmp_path):
    run_dir = _run_dir(tmp_path)
    payload = {
        "overallQuality": 0.9,
        "planCoverage": {"totalActions": 5, "implemented": 5, "missing": [], "extra": []},
        "issues": [],
        "crossFileIssues": [],
        "passesReview": True,
    }
    apply_mediation(run_dir, MediationKind.POST_IMPLEMENT_REVIEW, payload)

    review = json.loads((run_dir / "implementation" / "post-implement-review.json").read_text(encoding="utf-8"))
    assert review["overallQuality"] == 0.9
    assert review["passesReview"] is True


# --- REQUIREMENT_ANALYSIS graphSearchStrategy ---

def test_requirement_analysis_has_graph_search_strategy_schema(tmp_path):
    run_dir = _run_dir(tmp_path)
    state = _state()
    req = build_mediation_request(Stage.NORMALIZE, MediationKind.REQUIREMENT_ANALYSIS, run_dir, state)
    schema = req["outputSchema"]
    assert "graphSearchStrategy" in schema
    strategy = schema["graphSearchStrategy"]
    assert "filePatterns" in strategy
    assert "componentNames" in strategy
    assert "perAcSearch" in strategy
    assert "architecturalPatterns" in strategy
    assert "searchQueries" in strategy


def test_merge_requirements_with_graph_search_strategy(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write(run_dir / "requirements.normalized.json", {
        "summary": "test",
        "acceptanceCriteria": [{"id": "AC-1", "text": "auth"}],
    })
    _write(run_dir / "request-classification.json", {"surface": "full_stack"})
    payload = {
        "summary": "Build auth middleware",
        "acceptanceCriteria": [{"id": "AC-1", "text": "JWT auth middleware"}],
        "graphSearchStrategy": {
            "intent": "api",
            "filePatterns": ["*.py", "*.go"],
            "componentNames": ["AuthMiddleware", "JWTValidator"],
            "perAcSearch": [{"acId": "AC-1", "searchTerms": ["auth", "jwt", "middleware"]}],
            "architecturalPatterns": ["middleware/", "handlers/"],
            "keywords": ["auth", "jwt", "middleware", "token", "bearer"],
            "focusFiles": ["src/middleware/auth.py"],
            "searchQueries": ["auth jwt middleware handler"],
        },
    }
    apply_mediation(run_dir, MediationKind.REQUIREMENT_ANALYSIS, payload)

    req = json.loads((run_dir / "requirements.normalized.json").read_text(encoding="utf-8"))
    assert "graphSearchStrategy" in req
    assert req["graphSearchStrategy"]["intent"] == "api"
    assert "AuthMiddleware" in req["graphSearchStrategy"]["componentNames"]
    assert len(req["graphSearchStrategy"]["perAcSearch"]) == 1


# --- Payload resolution (file-based submission) ---

def test_resolve_payload_inline_json(tmp_path):
    from uiforgemax.tools.mediation import _resolve_payload

    data, err = _resolve_payload('{"summary": "test"}', None, tmp_path)
    assert err is None
    assert data == {"summary": "test"}


def test_resolve_payload_file_prefix(tmp_path):
    from uiforgemax.tools.mediation import _resolve_payload

    payload_path = tmp_path / "plan.json"
    payload_path.write_text('{"summary": "from file"}', encoding="utf-8")
    data, err = _resolve_payload(f"file:{payload_path}", None, tmp_path)
    assert err is None
    assert data == {"summary": "from file"}


def test_resolve_payload_file_param(tmp_path):
    from uiforgemax.tools.mediation import _resolve_payload

    payload_path = tmp_path / "mediation" / "plan.json"
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_text('{"summary": "via param"}', encoding="utf-8")
    # Relative path resolved against run_dir
    data, err = _resolve_payload("{}", "mediation/plan.json", tmp_path)
    assert err is None
    assert data == {"summary": "via param"}


def test_resolve_payload_file_not_found(tmp_path):
    from uiforgemax.tools.mediation import _resolve_payload

    data, err = _resolve_payload("file:/nonexistent/plan.json", None, tmp_path)
    assert data is None
    assert "not found" in err


def test_resolve_payload_invalid_inline_json(tmp_path):
    from uiforgemax.tools.mediation import _resolve_payload

    data, err = _resolve_payload("not json at all", None, tmp_path)
    assert data is None
    assert "valid JSON" in err


# --- Artifact-content embedding (opt-in via UIFORGEMAX_INLINE_ARTIFACTS=1) ---

def test_attach_artifact_contents_off_by_default(tmp_path, monkeypatch):
    from uiforgemax.model_mediation.registry import attach_artifact_contents

    monkeypatch.delenv("UIFORGEMAX_INLINE_ARTIFACTS", raising=False)
    (tmp_path / "requirements.normalized.json").write_text('{"a": 1}', encoding="utf-8")
    request = {"readArtifacts": ["requirements.normalized.json"]}
    enriched = attach_artifact_contents(request, tmp_path)
    assert "artifactContents" not in enriched


def test_attach_artifact_contents_inlines_small_json(tmp_path, monkeypatch):
    from uiforgemax.model_mediation.registry import attach_artifact_contents

    monkeypatch.setenv("UIFORGEMAX_INLINE_ARTIFACTS", "1")
    (tmp_path / "requirements.normalized.json").write_text('{"a": 1}', encoding="utf-8")
    request = {"readArtifacts": ["requirements.normalized.json"]}
    enriched = attach_artifact_contents(request, tmp_path)
    assert enriched["artifactContents"]["requirements.normalized.json"] == '{"a": 1}'
    assert "artifactContentsNote" in enriched


def test_attach_artifact_contents_skips_large_files(tmp_path, monkeypatch):
    from uiforgemax.model_mediation.registry import attach_artifact_contents, _EMBED_MAX_BYTES

    monkeypatch.setenv("UIFORGEMAX_INLINE_ARTIFACTS", "1")
    big = tmp_path / "big.json"
    big.write_text("x" * (_EMBED_MAX_BYTES + 1), encoding="utf-8")
    request = {"readArtifacts": ["big.json"]}
    enriched = attach_artifact_contents(request, tmp_path)
    assert "artifactContents" not in enriched


def test_attach_artifact_contents_skips_non_text_suffixes(tmp_path, monkeypatch):
    from uiforgemax.model_mediation.registry import attach_artifact_contents

    monkeypatch.setenv("UIFORGEMAX_INLINE_ARTIFACTS", "1")
    (tmp_path / "page.html").write_text("<html></html>", encoding="utf-8")
    request = {"readArtifacts": ["page.html"]}
    enriched = attach_artifact_contents(request, tmp_path)
    assert "artifactContents" not in enriched


def test_attach_artifact_contents_skips_missing_files(tmp_path, monkeypatch):
    from uiforgemax.model_mediation.registry import attach_artifact_contents

    request = {"readArtifacts": ["does-not-exist.json"]}
    enriched = attach_artifact_contents(request, tmp_path)
    assert enriched == request  # unchanged — nothing embeddable


def test_attach_artifact_contents_never_mutates_saved_request(tmp_path):
    """The state.mediation['pending'] / saved-to-disk request must stay lean —
    only the returned copy is enriched."""
    from uiforgemax.model_mediation.registry import attach_artifact_contents

    (tmp_path / "requirements.normalized.json").write_text("{}", encoding="utf-8")
    request = {"readArtifacts": ["requirements.normalized.json"]}
    attach_artifact_contents(request, tmp_path)
    assert "artifactContents" not in request  # original dict untouched
