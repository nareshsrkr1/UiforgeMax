"""Tests for stack-agnostic lexical fallback — keyword matching, config filtering."""

from __future__ import annotations

import json
from pathlib import Path

from uiforgemax.graphify.lexical_fallback import (
    lexical_evidence_from_graph,
    keywords_from_requirements,
    supplement_style_files,
)


def _graph_path(tmp_path: Path, nodes: list[dict]) -> Path:
    p = tmp_path / "graph.json"
    p.write_text(json.dumps({"nodes": nodes}), encoding="utf-8")
    return p


# --- lexical_evidence_from_graph ---

def test_matches_keyword_in_label(tmp_path):
    path = _graph_path(tmp_path, [
        {"label": "AuthMiddleware", "source_file": "src/auth/middleware.py"},
        {"label": "UserModel", "source_file": "src/models/user.py"},
    ])
    results = lexical_evidence_from_graph(path, keywords=["auth"])
    assert len(results) == 1
    assert results[0]["source_file"] == "src/auth/middleware.py"


def test_matches_keyword_in_path(tmp_path):
    path = _graph_path(tmp_path, [
        {"label": "Handler", "source_file": "src/middleware/auth_handler.go"},
    ])
    results = lexical_evidence_from_graph(path, keywords=["auth"])
    assert len(results) == 1


def test_filters_config_files(tmp_path):
    path = _graph_path(tmp_path, [
        {"label": "PackageJson", "source_file": "package.json"},
        {"label": "TsConfig", "source_file": "tsconfig.json"},
        {"label": "AuthModule", "source_file": "src/auth.ts"},
    ])
    results = lexical_evidence_from_graph(path, keywords=["auth", "package", "tsconfig"])
    files = [r["source_file"] for r in results]
    assert "package.json" not in files
    assert "tsconfig.json" not in files
    assert "src/auth.ts" in files


def test_filters_non_impl_extensions(tmp_path):
    path = _graph_path(tmp_path, [
        {"label": "readme", "source_file": "README.md"},
        {"label": "data", "source_file": "data.json"},
        {"label": "AuthModule", "source_file": "src/auth.py"},
    ])
    results = lexical_evidence_from_graph(path, keywords=["auth", "readme", "data"])
    files = [r["source_file"] for r in results]
    assert "src/auth.py" in files
    assert len(files) == 1


def test_supports_multiple_languages(tmp_path):
    nodes = [
        {"label": "Handler", "source_file": "src/handler.py"},
        {"label": "Handler", "source_file": "src/handler.go"},
        {"label": "Handler", "source_file": "src/handler.rs"},
        {"label": "Handler", "source_file": "src/handler.java"},
        {"label": "Handler", "source_file": "src/handler.cs"},
        {"label": "Handler", "source_file": "src/handler.rb"},
        {"label": "Handler", "source_file": "src/handler.php"},
        {"label": "Handler", "source_file": "src/Handler.vue"},
        {"label": "Handler", "source_file": "src/Handler.svelte"},
    ]
    path = _graph_path(tmp_path, nodes)
    results = lexical_evidence_from_graph(path, keywords=["handler"])
    assert len(results) == len(nodes)


def test_empty_keywords_returns_empty(tmp_path):
    path = _graph_path(tmp_path, [
        {"label": "AuthModule", "source_file": "src/auth.py"},
    ])
    assert lexical_evidence_from_graph(path, keywords=[]) == []
    assert lexical_evidence_from_graph(path, keywords=None) == []


def test_no_duplicates(tmp_path):
    path = _graph_path(tmp_path, [
        {"label": "Auth", "source_file": "src/auth.py", "id": "1"},
        {"label": "AuthHelper", "source_file": "src/auth.py", "id": "2"},
    ])
    results = lexical_evidence_from_graph(path, keywords=["auth"])
    assert len(results) == 1


def test_respects_limit(tmp_path):
    nodes = [
        {"label": f"Module{i}", "source_file": f"src/module{i}.py"}
        for i in range(30)
    ]
    path = _graph_path(tmp_path, nodes)
    results = lexical_evidence_from_graph(path, keywords=["module"], limit=5)
    assert len(results) == 5


def test_score_not_leaked(tmp_path):
    path = _graph_path(tmp_path, [
        {"label": "AuthModule", "source_file": "src/auth.py"},
    ])
    results = lexical_evidence_from_graph(path, keywords=["auth"])
    assert "_score" not in results[0]


# --- keywords_from_requirements ---

def test_keywords_from_graph_search_strategy():
    reqs = {
        "graphSearchStrategy": {
            "keywords": ["auth", "jwt", "middleware"],
            "componentNames": ["AuthMiddleware", "JWTValidator"],
        },
    }
    kws = keywords_from_requirements(reqs)
    assert "auth" in kws
    assert "jwt" in kws
    assert "authmiddleware" in kws


def test_keywords_from_legacy_hints():
    reqs = {
        "graphSearchHints": {
            "keywords": ["theme", "dark", "css"],
        },
    }
    kws = keywords_from_requirements(reqs)
    assert kws == ["theme", "dark", "css"]


def test_keywords_from_text_fallback():
    reqs = {
        "summary": "Build authentication middleware for JWT tokens",
        "acceptanceCriteria": [{"text": "validate bearer tokens"}],
    }
    kws = keywords_from_requirements(reqs)
    assert len(kws) > 0
    assert "the" not in kws
    assert "and" not in kws


def test_keywords_strategy_wins_over_hints():
    reqs = {
        "graphSearchStrategy": {"keywords": ["strategy-kw"], "componentNames": []},
        "graphSearchHints": {"keywords": ["hint-kw"]},
    }
    kws = keywords_from_requirements(reqs)
    assert "strategy-kw" in kws
    assert "hint-kw" not in kws


def test_keywords_none_returns_empty():
    assert keywords_from_requirements(None) == []
    assert keywords_from_requirements({}) == []


def test_keywords_capped_at_12():
    reqs = {
        "graphSearchStrategy": {
            "keywords": [f"kw{i}" for i in range(20)],
            "componentNames": [f"Comp{i}" for i in range(20)],
        },
    }
    kws = keywords_from_requirements(reqs)
    assert len(kws) <= 12


# --- supplement_style_files ---

def test_supplement_noop_without_discovery():
    nodes = [{"label": "A", "source_file": "a.ts"}]
    result = supplement_style_files(Path("/tmp"), nodes, allow_file_discovery=False)
    assert result == nodes


def test_supplement_noop_with_none_root():
    nodes = [{"label": "A", "source_file": "a.ts"}]
    result = supplement_style_files(None, nodes, allow_file_discovery=True)
    assert result == nodes
