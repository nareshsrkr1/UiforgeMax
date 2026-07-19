"""Tests for real-Graphify query planner + generic requirement map."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from uiforgemax.graphify.cli_runner import graphify_python, resolve_python_spec
from uiforgemax.graphify.pipeline import stage_query_plan, stage_requirement_map
from uiforgemax.graphify.query_planner import plan_queries
from uiforgemax.graphify.requirement_map import build_requirement_map


def test_resolve_python_spec_accepts_full_path():
    resolved = resolve_python_spec(sys.executable)
    assert resolved is not None
    assert Path(resolved).exists()
    assert Path(resolved).resolve() == Path(sys.executable).resolve()


def test_resolve_python_spec_accepts_command_name(monkeypatch):
    # Simulate PATH lookup for a bare command without depending on the host PATH.
    fake = Path(sys.executable)

    def _which(cmd: str):
        return str(fake) if cmd == "python" else None

    monkeypatch.setattr("uiforgemax.graphify.cli_runner.shutil.which", _which)
    resolved = resolve_python_spec("python")
    assert resolved == str(fake.resolve())


def test_graphify_python_honours_command_override(monkeypatch):
    monkeypatch.setenv("UIFORGEMAX_GRAPHIFY_PYTHON", "python")
    monkeypatch.setattr(
        "uiforgemax.graphify.cli_runner.resolve_python_spec",
        lambda spec: sys.executable if spec == "python" else None,
    )
    monkeypatch.setattr(
        "uiforgemax.graphify.cli_runner._python_has_graphify",
        lambda exe, timeout=3.0: True,
    )
    graphify_python.cache_clear()
    try:
        assert Path(graphify_python()).resolve() == Path(sys.executable).resolve()
    finally:
        graphify_python.cache_clear()


def test_plan_queries_are_requirement_shaped_not_nx():
    reqs = {
        "summary": "Dark mode page background and sidebar",
        "acceptanceCriteria": [
            {"id": "AC-1", "text": "Change page background to dark theme"},
            {"id": "AC-2", "text": "Dark left navigation color with contrast"},
        ],
        "dataNeeds": [],
        "policy": {},
    }
    plan = plan_queries(reqs, None, classification={"surface": "ui_only"}, stack={"nx": False})
    questions = " ".join(q["question"] for q in plan["queries"]).lower()
    assert "datagrid" not in questions
    assert "listcustomers" not in questions
    assert plan["intent"] == "theme_ui"
    assert plan["strategy"] == "lexical_first"
    assert plan["queryCount"] <= 1
    assert all(len(q["question"]) <= 72 for q in plan["queries"])
    assert "dark" in questions or "nav" in questions or "css" in questions
    assert plan["engine"] == "graphify-cli"


def test_plan_queries_honours_mediation_short_questions():
    reqs = {
        "summary": "Dark mode",
        "acceptanceCriteria": [],
        "graphSearchHints": {
            "intent": "theme_ui",
            "keywords": ["dark", "sidebar", "styles.css"],
            "focusFiles": ["src/styles.css", "src/App.tsx"],
            "shortQuestions": ["dark sidebar styles.css App.tsx"],
        },
    }
    plan = plan_queries(reqs, None, classification={"surface": "ui_only"})
    assert plan["queries"][0]["question"] == "dark sidebar styles.css App.tsx"
    assert plan["focusFiles"] == ["src/styles.css", "src/App.tsx"]


def test_plan_queries_adds_nx_flavor_only_when_stack_nx():
    reqs = {
        "summary": "Add export button to dashboard page",
        "acceptanceCriteria": [{"id": "AC-1", "text": "Export button visible"}],
        "dataNeeds": [],
        "policy": {},
    }
    plain = plan_queries(reqs, None, classification={"surface": "ui_only"}, stack={"nx": False})
    nx = plan_queries(reqs, None, classification={"surface": "full_stack"}, stack={"nx": True})
    assert not any(q["id"] == "Q-NX" for q in plain["queries"])
    assert any(q["id"] == "Q-NX" for q in nx["queries"])


def test_requirement_map_drops_package_json_noise():
    from uiforgemax.graphify.requirement_map import build_requirement_map

    reqs = {
        "issueKey": "SCRUM-5",
        "summary": "Dark mode",
        "acceptanceCriteria": [{"id": "AC-1", "text": "Dark background"}],
        "dataNeeds": [{"entity": "Customer", "operations": ["list"]}],
        "policy": {"targetApp": "customer-portal"},
        "assumptions": [],
    }
    results = {
        "results": [
            {
                "id": "Q-1",
                "status": "ok",
                "nodes": [
                    {"source_file": "package.json"},
                    {"source_file": "project.json"},
                    {"source_file": "src/App.tsx"},
                    {"source_file": "src/pages/DashboardPage.tsx"},
                ],
                "params": {"acId": "AC-1"},
                "question": "dark background",
            }
        ]
    }
    req_map = build_requirement_map(reqs, results, None, classification={"surface": "ui_only"})
    paths = {m["path"] for m in req_map["modify"]}
    assert "package.json" not in paths
    assert "project.json" not in paths
    assert "src/App.tsx" in paths
    assert req_map["apiGaps"] == []
    assert req_map.get("sanitizedTargets") is True


def test_requirement_map_ui_only_does_not_invent_apis_or_pages():
    reqs = {
        "issueKey": "SCRUM-5",
        "summary": "Dark mode",
        "acceptanceCriteria": [{"id": "AC-1", "text": "Dark background"}],
        "dataNeeds": [{"entity": "Customer", "operations": ["list", "export"]}],
        "policy": {"targetApp": "customer-portal"},
        "assumptions": [],
    }
    results = {
        "results": [
            {
                "id": "Q-AC-AC-1",
                "status": "empty",
                "nodes": [],
                "params": {"acId": "AC-1"},
                "question": "dark background",
            }
        ]
    }
    req_map = build_requirement_map(reqs, results, None, classification={"surface": "ui_only"})
    assert req_map["apiGaps"] == []
    assert req_map["create"] == []
    assert req_map["weakGraph"] is True
    assert "CustomerListPage" not in json.dumps(req_map)
    assert "export" not in json.dumps(req_map["apiGaps"])


def test_stage_query_plan_writes_queries(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "request-classification.json").write_text(
        json.dumps({"surface": "ui_only"}), encoding="utf-8"
    )
    reqs = {
        "summary": "Restyle sidebar",
        "acceptanceCriteria": [{"id": "AC-1", "text": "Dark sidebar"}],
        "dataNeeds": [],
        "policy": {},
    }
    plan = stage_query_plan(run_dir, reqs)
    assert plan["intent"] == "theme_ui"
    assert plan["strategy"] == "lexical_first"
    assert plan["queryCount"] <= 1
    assert (run_dir / "graph" / "queries.json").exists()


def test_stage_requirement_map_from_empty_results(tmp_path: Path):
    run_dir = tmp_path / "run"
    (run_dir / "graph").mkdir(parents=True)
    (run_dir / "request-classification.json").write_text(
        json.dumps({"surface": "ui_only"}), encoding="utf-8"
    )
    (run_dir / "graph" / "query-results.json").write_text(
        json.dumps({"executed": 1, "results": [{"id": "Q1", "status": "empty", "nodes": []}]}),
        encoding="utf-8",
    )
    reqs = {
        "summary": "Dark mode",
        "acceptanceCriteria": [{"id": "AC-1", "text": "Dark bg"}],
        "dataNeeds": [],
        "policy": {},
        "assumptions": [],
    }
    req_map = stage_requirement_map(run_dir, reqs, {})
    assert req_map["source"] == "graphify-cli"
    assert (run_dir / "graph" / "requirement-map.json").exists()
    assert (run_dir / "graph" / "context-pack.json").exists()
