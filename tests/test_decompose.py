"""Tests for sub-task decomposition — auto-decompose, mediation build, searchContext."""

from __future__ import annotations

import json
from pathlib import Path

from uiforgemax.pipeline.decompose import (
    should_auto_decompose,
    auto_decompose,
    build_subtasks_from_mediation,
    load_subtasks,
    subtask_ac_slice,
    subtask_keywords,
)


_REQS_SIMPLE = {
    "summary": "Add dark theme toggle",
    "acceptanceCriteria": [
        {"id": "AC-1", "text": "toggle button in header"},
        {"id": "AC-2", "text": "persists preference"},
    ],
}

_REQS_COMPLEX = {
    "summary": "Build full dashboard",
    "acceptanceCriteria": [
        {"id": "AC-1", "text": "sidebar navigation"},
        {"id": "AC-2", "text": "header with search"},
        {"id": "AC-3", "text": "main content grid"},
        {"id": "AC-4", "text": "footer with links"},
    ],
}

_CLASSIFICATION = {"surface": "ui_only", "requestType": "feature"}


# --- should_auto_decompose ---

def test_auto_decompose_true_for_2_acs():
    assert should_auto_decompose(_REQS_SIMPLE, _CLASSIFICATION) is True


def test_auto_decompose_true_for_0_acs():
    assert should_auto_decompose({"acceptanceCriteria": []}, _CLASSIFICATION) is True


def test_auto_decompose_false_for_3_plus_acs():
    assert should_auto_decompose(_REQS_COMPLEX, _CLASSIFICATION) is False


# --- auto_decompose ---

def test_auto_decompose_single_subtask():
    result = auto_decompose(_REQS_SIMPLE, _CLASSIFICATION, None)
    assert result["subtaskCount"] == 1
    assert result["source"] == "auto"
    assert result["mediatedByIde"] is False

    st = result["subtasks"][0]
    assert st["id"] == "ST-ALL"
    assert set(st["linkedAcIds"]) == {"AC-1", "AC-2"}
    assert st["surfaceHint"] == "ui_only"
    assert result["dependencyOrder"] == ["ST-ALL"]


def test_auto_decompose_extracts_keywords():
    result = auto_decompose(_REQS_SIMPLE, _CLASSIFICATION, None)
    st = result["subtasks"][0]
    assert len(st["focusKeywords"]) > 0


# --- build_subtasks_from_mediation ---

def test_mediation_builds_subtasks():
    payload = {
        "decompositionStrategy": "visual_regions",
        "subtasks": [
            {
                "id": "ST-1",
                "title": "Sidebar",
                "summary": "Build sidebar navigation",
                "linkedAcIds": ["AC-1"],
                "dependencies": [],
                "focusKeywords": ["sidebar", "nav"],
            },
            {
                "id": "ST-2",
                "title": "Header",
                "summary": "Build header with search",
                "linkedAcIds": ["AC-2"],
                "dependencies": ["ST-1"],
                "focusKeywords": ["header", "search"],
            },
        ],
    }
    result = build_subtasks_from_mediation(_REQS_COMPLEX, _CLASSIFICATION, None, payload)
    assert result["subtaskCount"] == 2
    assert result["mediatedByIde"] is True
    assert result["decompositionStrategy"] == "visual_regions"
    assert result["dependencyOrder"][0] == "ST-1"


def test_mediation_preserves_search_context():
    payload = {
        "subtasks": [
            {
                "id": "ST-1",
                "title": "Auth middleware",
                "linkedAcIds": ["AC-1"],
                "searchContext": {
                    "componentNames": ["AuthMiddleware", "JWTValidator"],
                    "filePatterns": ["src/middleware/*.py"],
                    "searchQueries": ["auth jwt handler"],
                },
            },
        ],
    }
    result = build_subtasks_from_mediation(_REQS_COMPLEX, _CLASSIFICATION, None, payload)
    st = result["subtasks"][0]
    assert st["searchContext"]["componentNames"] == ["AuthMiddleware", "JWTValidator"]
    assert st["searchContext"]["filePatterns"] == ["src/middleware/*.py"]
    assert st["searchContext"]["searchQueries"] == ["auth jwt handler"]


def test_mediation_empty_search_context_defaults():
    payload = {
        "subtasks": [
            {"id": "ST-1", "title": "Test", "linkedAcIds": ["AC-1"]},
        ],
    }
    result = build_subtasks_from_mediation(_REQS_COMPLEX, _CLASSIFICATION, None, payload)
    assert result["subtasks"][0]["searchContext"] == {}


def test_mediation_orphan_acs_assigned_to_last():
    payload = {
        "subtasks": [
            {"id": "ST-1", "title": "Sidebar", "linkedAcIds": ["AC-1"]},
            {"id": "ST-2", "title": "Header", "linkedAcIds": ["AC-2"]},
        ],
    }
    result = build_subtasks_from_mediation(_REQS_COMPLEX, _CLASSIFICATION, None, payload)
    last_st = result["subtasks"][-1]
    assert "AC-3" in last_st["linkedAcIds"]
    assert "AC-4" in last_st["linkedAcIds"]


def test_mediation_focus_keywords_capped_at_10():
    payload = {
        "subtasks": [
            {
                "id": "ST-1",
                "title": "Test",
                "linkedAcIds": ["AC-1"],
                "focusKeywords": [f"kw{i}" for i in range(20)],
            },
        ],
    }
    result = build_subtasks_from_mediation(_REQS_COMPLEX, _CLASSIFICATION, None, payload)
    assert len(result["subtasks"][0]["focusKeywords"]) <= 10


def test_mediation_dedup_ids():
    payload = {
        "subtasks": [
            {"id": "ST-1", "title": "First", "linkedAcIds": ["AC-1"]},
            {"id": "ST-1", "title": "Duplicate", "linkedAcIds": ["AC-2"]},
        ],
    }
    result = build_subtasks_from_mediation(_REQS_COMPLEX, _CLASSIFICATION, None, payload)
    ids = [st["id"] for st in result["subtasks"]]
    assert len(ids) == len(set(ids))


def test_mediation_fallback_on_empty_subtasks():
    result = build_subtasks_from_mediation(_REQS_COMPLEX, _CLASSIFICATION, None, {"subtasks": []})
    assert result["source"] == "auto"
    assert result["subtasks"][0]["id"] == "ST-ALL"


def test_mediation_dependency_order():
    payload = {
        "subtasks": [
            {"id": "ST-A", "title": "A", "linkedAcIds": ["AC-1"], "dependencies": ["ST-B"]},
            {"id": "ST-B", "title": "B", "linkedAcIds": ["AC-2"], "dependencies": []},
            {"id": "ST-C", "title": "C", "linkedAcIds": ["AC-3"], "dependencies": ["ST-A"]},
        ],
    }
    result = build_subtasks_from_mediation(_REQS_COMPLEX, _CLASSIFICATION, None, payload)
    order = result["dependencyOrder"]
    assert order.index("ST-B") < order.index("ST-A")
    assert order.index("ST-A") < order.index("ST-C")


def test_parallel_groups():
    payload = {
        "subtasks": [
            {"id": "ST-1", "title": "A", "linkedAcIds": ["AC-1"], "dependencies": []},
            {"id": "ST-2", "title": "B", "linkedAcIds": ["AC-2"], "dependencies": []},
            {"id": "ST-3", "title": "C", "linkedAcIds": ["AC-3"], "dependencies": ["ST-1", "ST-2"]},
        ],
    }
    result = build_subtasks_from_mediation(_REQS_COMPLEX, _CLASSIFICATION, None, payload)
    groups = result["parallelGroups"]
    assert len(groups) == 2
    assert set(groups[0]) == {"ST-1", "ST-2"}
    assert groups[1] == ["ST-3"]


# --- load_subtasks ---

def test_load_subtasks_exists(tmp_path):
    d = tmp_path / "plans"
    d.mkdir()
    (d / "subtasks.json").write_text(json.dumps({"subtaskCount": 2}), encoding="utf-8")
    result = load_subtasks(tmp_path)
    assert result["subtaskCount"] == 2


def test_load_subtasks_missing(tmp_path):
    assert load_subtasks(tmp_path) is None


# --- subtask_ac_slice ---

def test_ac_slice_filters():
    subtask = {"id": "ST-1", "linkedAcIds": ["AC-2"], "summary": "Header only"}
    sliced = subtask_ac_slice(subtask, _REQS_COMPLEX)
    assert len(sliced["acceptanceCriteria"]) == 1
    assert sliced["acceptanceCriteria"][0]["id"] == "AC-2"
    assert sliced["summary"] == "Header only"


def test_ac_slice_no_linked_returns_all():
    subtask = {"id": "ST-ALL", "linkedAcIds": []}
    sliced = subtask_ac_slice(subtask, _REQS_COMPLEX)
    assert sliced == _REQS_COMPLEX


# --- subtask_keywords ---

def test_subtask_keywords():
    st = {"focusKeywords": ["sidebar", "nav", "menu"]}
    assert subtask_keywords(st) == ["sidebar", "nav", "menu"]


def test_subtask_keywords_empty():
    assert subtask_keywords({}) == []
