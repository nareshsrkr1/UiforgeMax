"""Tests for mediation-driven query planner — 4-tier priority, stack-agnostic."""

from __future__ import annotations

import json
from pathlib import Path

from uiforgemax.graphify.query_planner import plan_queries


def _run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs" / "qp-test"
    (d / "graph").mkdir(parents=True, exist_ok=True)
    return d


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# --- Tier 1: Mediated query strategy ---

def test_tier1_mediated_strategy(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write(run_dir / "graph" / "query-strategy.json", {
        "queries": [
            {"id": "Q-AUTH", "question": "auth handler middleware", "reason": "Find auth"},
            {"id": "Q-DB", "question": "database connection pool", "reason": "Find DB"},
        ],
        "lexicalKeywords": ["auth", "middleware"],
        "focusFiles": ["src/auth.py"],
        "strategy": "hybrid",
    })
    result = plan_queries(
        {"summary": "ignored", "acceptanceCriteria": []},
        None,
        run_dir=run_dir,
    )
    assert result["source"] == "mediated_query_strategy"
    assert result["queryCount"] == 2
    assert result["queries"][0]["id"] == "Q-AUTH"
    assert result["keywords"] == ["auth", "middleware"]
    assert result["focusFiles"] == ["src/auth.py"]


def test_tier1_subtask_filters_queries(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write(run_dir / "graph" / "query-strategy.json", {
        "queries": [
            {"id": "Q-1", "question": "sidebar nav", "subtaskId": "ST-1"},
            {"id": "Q-2", "question": "header bar", "subtaskId": "ST-2"},
        ],
        "lexicalKeywords": ["sidebar", "header"],
    })
    result = plan_queries(
        {"summary": "test", "acceptanceCriteria": []},
        None,
        subtask={"id": "ST-1", "focusKeywords": []},
        run_dir=run_dir,
    )
    assert result["source"] == "mediated_query_strategy"
    assert all(
        q.get("params", {}).get("subtaskId") == "ST-1"
        for q in result["queries"]
    )


# --- Tier 2: graphSearchStrategy ---

def test_tier2_graph_search_strategy():
    reqs = {
        "summary": "Build auth system",
        "acceptanceCriteria": [{"id": "AC-1", "text": "JWT auth"}],
        "graphSearchStrategy": {
            "intent": "api",
            "keywords": ["auth", "jwt", "middleware"],
            "componentNames": ["AuthMiddleware"],
            "perAcSearch": [{"acId": "AC-1", "searchTerms": ["auth", "jwt"]}],
            "searchQueries": ["auth jwt handler"],
            "focusFiles": ["src/middleware/auth.py"],
        },
    }
    result = plan_queries(reqs, None)
    assert result["source"] == "graph_search_strategy"
    assert result["intent"] == "api"
    assert "auth" in result["keywords"]
    assert len(result["queries"]) >= 1


def test_tier2_subtask_scopes_acs():
    reqs = {
        "summary": "Build dashboard",
        "acceptanceCriteria": [
            {"id": "AC-1", "text": "sidebar"},
            {"id": "AC-2", "text": "header"},
        ],
        "graphSearchStrategy": {
            "keywords": ["dashboard", "sidebar"],
            "perAcSearch": [
                {"acId": "AC-1", "searchTerms": ["sidebar", "nav"]},
                {"acId": "AC-2", "searchTerms": ["header", "toolbar"]},
            ],
        },
    }
    result = plan_queries(
        reqs, None,
        subtask={"id": "ST-1", "linkedAcIds": ["AC-1"], "focusKeywords": ["sidebar"]},
    )
    assert result["source"] == "graph_search_strategy"
    for q in result["queries"]:
        assert q.get("params", {}).get("subtaskId") == "ST-1"


def test_tier2_subtask_search_context():
    reqs = {
        "summary": "Build auth",
        "acceptanceCriteria": [],
        "graphSearchStrategy": {
            "keywords": ["auth"],
            "componentNames": [],
            "searchQueries": ["generic auth"],
        },
    }
    subtask = {
        "id": "ST-1",
        "focusKeywords": ["jwt"],
        "searchContext": {
            "componentNames": ["JWTValidator"],
            "searchQueries": ["jwt token validation"],
        },
    }
    result = plan_queries(reqs, None, subtask=subtask)
    assert result["source"] == "graph_search_strategy"
    assert "jwt" in result["keywords"]


# --- Tier 3: Legacy hints ---

def test_tier3_legacy_hints():
    reqs = {
        "summary": "Add dark theme",
        "acceptanceCriteria": [],
        "graphSearchHints": {
            "intent": "theme_ui",
            "shortQuestions": ["Where are CSS theme variables defined?"],
            "keywords": ["theme", "dark", "css-variables"],
            "focusFiles": ["src/styles/theme.css"],
        },
    }
    result = plan_queries(reqs, None)
    assert result["source"] == "legacy_hints"
    assert result["intent"] == "theme_ui"
    assert result["strategy"] == "lexical_first"
    assert result["queryCount"] >= 1


# --- Tier 4: Deterministic fallback ---

def test_tier4_deterministic_fallback():
    reqs = {
        "summary": "Implement UserProfile component with avatar upload",
        "acceptanceCriteria": [
            {"id": "AC-1", "text": "user can upload avatar image"},
        ],
    }
    result = plan_queries(reqs, None)
    assert result["source"] == "deterministic_fallback"
    assert len(result["keywords"]) > 0
    assert "userprofile" in result["keywords"] or any(
        "profile" in k or "avatar" in k for k in result["keywords"]
    )
    assert result["queryCount"] >= 1


def test_tier4_no_hardcoded_react_patterns():
    reqs = {
        "summary": "Add logging middleware to Go API",
        "acceptanceCriteria": [{"id": "AC-1", "text": "log all requests"}],
    }
    result = plan_queries(reqs, None)
    for q in result["queries"]:
        question = q["question"].lower()
        assert "app.tsx" not in question
        assert "styles.css" not in question
        assert "sidebar" not in question


def test_tier4_subtask_overrides_summary():
    reqs = {
        "summary": "Build entire dashboard",
        "acceptanceCriteria": [
            {"id": "AC-1", "text": "sidebar"},
            {"id": "AC-2", "text": "header"},
        ],
    }
    subtask = {
        "id": "ST-1",
        "summary": "Build sidebar navigation",
        "linkedAcIds": ["AC-1"],
        "focusKeywords": ["sidebar", "navigation"],
    }
    result = plan_queries(reqs, None, subtask=subtask)
    assert "sidebar" in result["keywords"]
    for q in result["queries"]:
        assert q.get("params", {}).get("subtaskId") == "ST-1"


# --- Priority order ---

def test_tier1_wins_over_tier2(tmp_path):
    """Mediated strategy takes priority over graphSearchStrategy."""
    run_dir = _run_dir(tmp_path)
    _write(run_dir / "graph" / "query-strategy.json", {
        "queries": [{"id": "Q-MED", "question": "mediated query"}],
        "lexicalKeywords": ["mediated"],
    })
    reqs = {
        "summary": "test",
        "acceptanceCriteria": [],
        "graphSearchStrategy": {
            "keywords": ["strategy"],
            "searchQueries": ["strategy query"],
        },
    }
    result = plan_queries(reqs, None, run_dir=run_dir)
    assert result["source"] == "mediated_query_strategy"


def test_no_run_dir_falls_to_tier2():
    reqs = {
        "summary": "test",
        "acceptanceCriteria": [],
        "graphSearchStrategy": {"keywords": ["auth"], "searchQueries": ["auth handler"]},
    }
    result = plan_queries(reqs, None, run_dir=None)
    assert result["source"] == "graph_search_strategy"


def test_empty_requirements_still_produces_result():
    result = plan_queries({"summary": "", "acceptanceCriteria": []}, None)
    assert result["source"] == "deterministic_fallback"
    assert result["queryCount"] == 0


# --- Query limits ---

def test_max_6_queries_from_mediation(tmp_path):
    run_dir = _run_dir(tmp_path)
    _write(run_dir / "graph" / "query-strategy.json", {
        "queries": [
            {"id": f"Q-{i}", "question": f"query {i}"} for i in range(10)
        ],
        "lexicalKeywords": [],
    })
    result = plan_queries(
        {"summary": "test", "acceptanceCriteria": []}, None, run_dir=run_dir,
    )
    assert len(result["queries"]) <= 6
