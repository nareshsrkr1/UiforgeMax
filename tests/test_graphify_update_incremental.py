"""Warm graphify-out must not skip incremental ``graphify update``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from uiforgemax.graphify import cli_runner


def _seed_warm_graph(root: Path, *, nodes: int = 2) -> Path:
    out = root / "graphify-out"
    out.mkdir(parents=True)
    graph = {
        "nodes": [{"id": f"n{i}", "label": f"Node{i}"} for i in range(nodes)],
        "edges": [],
    }
    path = out / "graph.json"
    path.write_text(json.dumps(graph), encoding="utf-8")
    return path


def test_warm_graphify_out_still_runs_incremental_update(tmp_path: Path, monkeypatch):
    graph_path = _seed_warm_graph(tmp_path)
    calls: list[list[str]] = []

    def _fake_run_logged(cmd, **_kwargs: Any):
        calls.append(list(cmd))
        # Simulate incremental update refreshing the graph in place.
        graph_path.write_text(
            json.dumps(
                {
                    "nodes": [
                        {"id": "n0", "label": "Node0"},
                        {"id": "n1", "label": "Node1"},
                        {"id": "n2", "label": "Node2"},
                    ],
                    "edges": [],
                }
            ),
            encoding="utf-8",
        )
        return 0, "updated", False

    monkeypatch.setattr(cli_runner, "_run_logged", _fake_run_logged)
    monkeypatch.setattr(cli_runner, "graphify_python", lambda: "python")

    result = cli_runner.run_update(tmp_path)

    assert len(calls) == 1
    assert "update" in calls[0]
    assert "--force" not in calls[0]
    assert result["reused"] is False
    assert result["nodeCount"] == 3


def test_timeout_falls_back_to_existing_graph(tmp_path: Path, monkeypatch):
    _seed_warm_graph(tmp_path)
    monkeypatch.setattr(
        cli_runner,
        "_run_logged",
        lambda *_a, **_k: (1, "timed out mid-index", True),
    )
    monkeypatch.setattr(cli_runner, "graphify_python", lambda: "python")

    result = cli_runner.run_update(tmp_path)

    assert result["reused"] is True
    assert "timed out" in result["reuseReason"]
    assert result["nodeCount"] == 2
