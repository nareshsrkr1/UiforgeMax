"""Hard gate: graphify_update must not silently pass with 0 nodes on a real project."""

from __future__ import annotations

from pathlib import Path

import pytest

from uiforgemax.graphify.cli_runner import GraphifyCliError
from uiforgemax.graphify.engine import graphify_update


def _fake_update_ok(nodes: int, node_count: int):
    def _run_update(root: Path, **kwargs):
        return {
            "graphifyOut": str(root / "graphify-out"),
            "graphJson": str(root / "graphify-out" / "graph.json"),
            "nodeCount": node_count,
            "edgeCount": 0,
            "sourceFiles": [],
            "stdout": "",
        }

    return _run_update


def test_empty_graph_on_non_empty_project_raises(tmp_path, monkeypatch):
    # A real, non-empty project (has a source file) whose update produced 0 nodes.
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")

    monkeypatch.setattr("uiforgemax.graphify.engine.run_update", _fake_update_ok(0, 0))

    with pytest.raises(GraphifyCliError, match="EMPTY graph"):
        graphify_update(tmp_path)


def test_empty_graph_on_greenfield_project_is_allowed(tmp_path, monkeypatch):
    # A genuinely empty/greenfield folder — 0 nodes is correct, must not raise.
    monkeypatch.setattr("uiforgemax.graphify.engine.run_update", _fake_update_ok(0, 0))

    result = graphify_update(tmp_path)
    assert result["nodeCount"] == 0


def test_nonzero_nodes_on_non_empty_project_passes(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")

    monkeypatch.setattr("uiforgemax.graphify.engine.run_update", _fake_update_ok(5, 5))

    result = graphify_update(tmp_path)
    assert result["nodeCount"] == 5
